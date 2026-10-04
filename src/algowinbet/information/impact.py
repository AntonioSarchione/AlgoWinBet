"""Player impact model: how much a team's expected goals move when specific players start or not.

log lambda_side = log lambda_team + sum_own a_i (s_i - sbar_i) - sum_opp d_j (s_j - sbar_j)
  a_i: attacking value of player i (own goals),  d_j: defensive value of player j (opponent goals)
  s_i: start indicator/probability, sbar_i: usual start rate (the team-level model already contains the average XI).

Priors by position (and player importance) are always present; when historical lineups exist the effects are learned by ridge
Poisson regression around those priors, with posterior sd for every player. Few observations => stays at the prior, wide sd.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from ..domain import HistoricalLineup, MatchResult, Player, Position
from .availability import TeamAvailability

P = Position
PRIOR_MEAN = {"a": {P.FWD: 0.06, P.MID: 0.03, P.DEF: 0.01, P.GK: 0.0},
              "d": {P.GK: 0.05, P.DEF: 0.03, P.MID: 0.015, P.FWD: 0.005}}
PRIOR_SD = {"a": {P.FWD: 0.10, P.MID: 0.05, P.DEF: 0.03, P.GK: 0.01},
            "d": {P.GK: 0.08, P.DEF: 0.06, P.MID: 0.04, P.FWD: 0.02}}


@dataclass
class Adjustment:
    d_home: float = 0.0  # log-lambda shift, home goals
    d_away: float = 0.0
    sd_home: float = 0.0
    sd_away: float = 0.0
    contributions: list[tuple[str, str, float]] = field(default_factory=list)  # (player name, text, log effect on the affected side)
    learned: bool = False

    @property
    def is_zero(self) -> bool:
        return abs(self.d_home) < 1e-12 and abs(self.d_away) < 1e-12


class PlayerImpactModel:
    def __init__(self, roster: list[Player], base: dict[str, float]):
        self.players = {p.id: p for p in roster}
        self.ids = [p.id for p in roster]
        self.idx = {i: k for k, i in enumerate(self.ids)}
        self.base = base
        n = len(self.ids)
        imp = np.array([self.players[i].importance for i in self.ids])
        self.prior_a = np.array([PRIOR_MEAN["a"][self.players[i].position] for i in self.ids]) * imp
        self.prior_d = np.array([PRIOR_MEAN["d"][self.players[i].position] for i in self.ids]) * imp
        self.sd0_a = np.array([PRIOR_SD["a"][self.players[i].position] for i in self.ids])
        self.sd0_d = np.array([PRIOR_SD["d"][self.players[i].position] for i in self.ids])
        self.delta_a = np.zeros(n)
        self.delta_d = np.zeros(n)
        self.sd_a = self.sd0_a.copy()
        self.sd_d = self.sd0_d.copy()
        self.n_obs = np.zeros(n, dtype=int)
        self.learned = False
        self.sbar = np.array([base[i] for i in self.ids])
        self.sbar_now = self.sbar  # usual start rate today (recent matches): transfers and new regulars

    def set_current(self, base_now: dict[str, float]) -> "PlayerImpactModel":
        """Prediction uses the recent start rates (a player who left starts 0% now); the fit keeps the whole-window ones."""
        self.base = base_now
        self.sbar_now = np.array([base_now[i] for i in self.ids])
        return self

    @property
    def a(self) -> np.ndarray:
        return self.prior_a + self.delta_a

    @property
    def d(self) -> np.ndarray:
        return self.prior_d + self.delta_d

    # ---------------------------------------------------------------- fit
    def fit(self, lineups: list[HistoricalLineup], results: list[MatchResult],
            offsets: dict[tuple[str, str], float]) -> "PlayerImpactModel":
        """offsets[(fixture_id, 'home'|'away')] = log lambda from the team-level model for that match side."""
        lu = {(l.fixture_id, l.team): l.starters for l in lineups}
        rows_y, rows_off, Xo, Xd = [], [], [], []
        n = len(self.ids)
        for r in results:
            sh, sa = lu.get((r.fixture_id, r.home)), lu.get((r.fixture_id, r.away))
            if sh is None or sa is None or (r.fixture_id, "home") not in offsets:
                continue
            xh, xa = self._vec(sh), self._vec(sa)
            rows_y += [r.home_goals, r.away_goals]
            rows_off += [offsets[(r.fixture_id, "home")], offsets[(r.fixture_id, "away")]]
            Xo += [xh - self.sbar, xa - self.sbar]
            Xd += [xa - self.sbar, xh - self.sbar]
            for i in sh + sa:
                if i in self.idx:
                    self.n_obs[self.idx[i]] += 1
        if len(rows_y) < 20:
            return self
        y, off = np.array(rows_y, float), np.array(rows_off)
        Xo, Xd = np.array(Xo), np.array(Xd)
        eta0 = off + Xo @ self.prior_a - Xd @ self.prior_d
        w_a, w_d = 1 / self.sd0_a**2, 1 / self.sd0_d**2

        def nll(th):
            da, dd, c = th[:n], th[n : 2 * n], th[2 * n]
            eta = eta0 + Xo @ da - Xd @ dd + c
            lam = np.exp(np.clip(eta, -5, 3))
            r_ = y - lam
            f = -(y * eta - lam).sum() + 0.5 * (w_a * da**2).sum() + 0.5 * (w_d * dd**2).sum()
            g = np.concatenate([-(Xo.T @ r_) + w_a * da, (Xd.T @ r_) + w_d * dd, [-r_.sum()]])
            return f, g

        res = minimize(nll, np.zeros(2 * n + 1), jac=True, method="L-BFGS-B")
        self.delta_a, self.delta_d = res.x[:n], res.x[n : 2 * n]
        eta = eta0 + Xo @ self.delta_a - Xd @ self.delta_d + res.x[2 * n]
        lam = np.exp(np.clip(eta, -5, 3))
        self.sd_a = 1 / np.sqrt((lam[:, None] * Xo**2).sum(0) + w_a)
        self.sd_d = 1 / np.sqrt((lam[:, None] * Xd**2).sum(0) + w_d)
        self.learned = True
        return self

    def _vec(self, starters: list[str]) -> np.ndarray:
        v = np.zeros(len(self.ids))
        for i in starters:
            if i in self.idx:
                v[self.idx[i]] = 1.0
        return v

    # ------------------------------------------------------------ predict
    def adjustment(self, home: TeamAvailability, away: TeamAvailability) -> Adjustment:
        ph = np.array([home.p_start.get(i, self.base[i]) if i in home.p_start else 0.0 for i in self.ids])
        pa = np.array([away.p_start.get(i, self.base[i]) if i in away.p_start else 0.0 for i in self.ids])
        in_h = np.array([self.players[i].team == home.team for i in self.ids])
        in_a = np.array([self.players[i].team == away.team for i in self.ids])
        dh_s, da_s = (ph - self.sbar_now) * in_h, (pa - self.sbar_now) * in_a
        a, d = self.a, self.d
        eff_h = a * dh_s - d * da_s  # per-player contribution to home log-lambda
        eff_a = a * da_s - d * dh_s
        unc_h = (home.source != "confirmed") * (a**2 * ph * (1 - ph) * in_h)
        unc_a = (away.source != "confirmed") * (a**2 * pa * (1 - pa) * in_a)
        var_h = (self.sd_a**2 * dh_s**2).sum() + (self.sd_d**2 * da_s**2).sum() + unc_h.sum() + (d**2 * pa * (1 - pa) * in_a * (away.source != "confirmed")).sum()
        var_a = (self.sd_a**2 * da_s**2).sum() + (self.sd_d**2 * dh_s**2).sum() + unc_a.sum() + (d**2 * ph * (1 - ph) * in_h * (home.source != "confirmed")).sum()
        dh, da_ = float(np.clip(eff_h.sum(), -0.5, 0.5)), float(np.clip(eff_a.sum(), -0.5, 0.5))
        contribs: list[tuple[str, str, float]] = []
        for k, i in enumerate(self.ids):
            pl = self.players[i]
            if in_h[k] and abs(a[k] * dh_s[k]) > 0.004:
                contribs.append((pl.name, f"{pl.name} ({pl.position.value}, {pl.team}) start {ph[k]:.0%} vs usuale {self.sbar_now[k]:.0%}: attacco", float(a[k] * dh_s[k])))
            if in_a[k] and abs(a[k] * da_s[k]) > 0.004:
                contribs.append((pl.name, f"{pl.name} ({pl.position.value}, {pl.team}) start {pa[k]:.0%} vs usuale {self.sbar_now[k]:.0%}: attacco", float(a[k] * da_s[k])))
            if in_h[k] and abs(d[k] * dh_s[k]) > 0.004:
                contribs.append((pl.name, f"{pl.name} ({pl.position.value}, {pl.team}) start {ph[k]:.0%} vs usuale {self.sbar_now[k]:.0%}: difesa", float(-d[k] * dh_s[k])))
            if in_a[k] and abs(d[k] * da_s[k]) > 0.004:
                contribs.append((pl.name, f"{pl.name} ({pl.position.value}, {pl.team}) start {pa[k]:.0%} vs usuale {self.sbar_now[k]:.0%}: difesa", float(-d[k] * da_s[k])))
        contribs.sort(key=lambda z: -abs(z[2]))
        return Adjustment(dh, da_, math.sqrt(var_h), math.sqrt(var_a), contribs[:6], self.learned)
