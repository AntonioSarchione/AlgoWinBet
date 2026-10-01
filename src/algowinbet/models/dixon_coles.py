"""Structural goal model: Poisson attack/defence with home advantage, exponential time decay,
ridge shrinkage and a Dixon-Coles low-score correction (rho fitted in a second stage).

Output is a full score distribution, from which every goal-based market is derived (spec 3.1, 10.1).
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import poisson

from ..domain import MatchResult
from ..markets import GRID


def _tau(i: np.ndarray, j: np.ndarray, lh: np.ndarray, la: np.ndarray, rho: float) -> np.ndarray:
    t = np.ones_like(lh, dtype=float)
    t = np.where((i == 0) & (j == 0), 1 - lh * la * rho, t)
    t = np.where((i == 0) & (j == 1), 1 + lh * rho, t)
    t = np.where((i == 1) & (j == 0), 1 + la * rho, t)
    t = np.where((i == 1) & (j == 1), 1 - rho, t)
    return t


class DixonColes:
    def __init__(self, xi: float = math.log(2) / 365.0, l2: float = 1.0, comp_mu: bool = False,
                 l2_comp_home: float | None = None, l2_comp_mu: float = 0.0):
        self.xi = xi  # decay per day (default half-life 1 year)
        self.l2 = l2
        # league effects (pooled model only): each competition's home advantage as a deviation from the shared one, shrunk
        # with l2_comp_home (None = one home advantage for all); its goal level shrunk towards the overall level with
        # l2_comp_mu (0 = free, the original behaviour). Leagues with few matches borrow from the others.
        self.l2_comp_home, self.l2_comp_mu = l2_comp_home, l2_comp_mu
        self.home_comp: dict[str, float] = {}
        # comp_mu: one pooled model over every competition, with its own unpenalised goal level per competition. Team
        # strengths are shared, so Champions/Europa League matches put clubs from different leagues on one scale.
        self.comp_mu = comp_mu
        self.mu_comp: dict[str, float] = {}
        self.teams: dict[str, int] = {}
        self.attack = np.zeros(0)
        self.defence = np.zeros(0)
        self.home_adv = 0.25
        self.mu = 0.0
        self.rho = 0.0
        self.n_matches: dict[str, int] = {}
        self.n_total = 0
        self.fitted = False

    # ------------------------------------------------------------------ fit
    def fit(self, results: list[MatchResult], as_of: datetime, prior: dict[str, tuple[float, float]] | None = None) -> "DixonColes":
        """prior: team -> (attack, defence) centre of the ridge penalty (default 0 = league average). Used for newcomers
        such as promoted clubs, which would otherwise be shrunk towards an average top-flight side."""
        if len(results) < 10:
            raise ValueError("need at least 10 results to fit")
        names = sorted({r.home for r in results} | {r.away for r in results})
        self.teams = {t: k for k, t in enumerate(names)}
        T = len(names)
        hi = np.array([self.teams[r.home] for r in results])
        ai = np.array([self.teams[r.away] for r in results])
        x = np.array([r.home_goals for r in results], dtype=float)
        y = np.array([r.away_goals for r in results], dtype=float)
        age = np.array([(as_of - r.kickoff).total_seconds() / 86400.0 for r in results])
        w = np.exp(-self.xi * np.clip(age, 0, None))
        comps = sorted({r.competition for r in results}) if self.comp_mu else ["*"]
        cidx = {c: k for k, c in enumerate(comps)}
        C = len(comps)
        ci = np.array([cidx[r.competition] if self.comp_mu else 0 for r in results])

        a0, d0 = np.zeros(T), np.zeros(T)
        for team, (pa, pd) in (prior or {}).items():
            if team in self.teams:
                a0[self.teams[team]], d0[self.teams[team]] = pa, pd

        lh_c = self.l2_comp_home if self.comp_mu and self.l2_comp_home is not None else None
        lmu = self.l2_comp_mu if self.comp_mu else 0.0

        # parameters: attack (T), defence (T), shared home advantage, goal level per competition (C), then only when the
        # league effects are on: overall goal level (shrinkage centre) and home deviation per competition (C). Without them
        # the vector is exactly the original one, so the default fit is unchanged.
        n_mu0 = 1 if lmu else 0
        n_dh = C if lh_c is not None else 0
        o_mu, o_mu0, o_dh = 2 * T + 1, 2 * T + 1 + C, 2 * T + 1 + C + n_mu0

        def unpack(th):
            mu0 = th[o_mu0] if n_mu0 else 0.0
            dh = th[o_dh : o_dh + n_dh] if n_dh else np.zeros(C)
            return th[:T], th[T : 2 * T], th[2 * T], mu0, th[o_mu : o_mu + C], dh

        def nll(th):
            a, d, h, mu0, mu, dh = unpack(th)
            m = mu[ci]
            eh = m + h + dh[ci] + a[hi] - d[ai]
            ea = m + a[ai] - d[hi]
            lh, la = np.exp(eh), np.exp(ea)
            ll = np.sum(w * (x * eh - lh + y * ea - la))
            rh, ra = w * (x - lh), w * (y - la)
            ga = np.bincount(hi, rh, T) + np.bincount(ai, ra, T)
            gd = -np.bincount(ai, rh, T) - np.bincount(hi, ra, T)
            gmu = -np.bincount(ci, rh + ra, C) + 2 * lmu * (mu - mu0)
            gdh = -np.bincount(ci, rh, C) + (2 * lh_c * dh if lh_c is not None else 0.0)
            pen = self.l2 * ((a - a0) @ (a - a0) + (d - d0) @ (d - d0)) + lmu * ((mu - mu0) @ (mu - mu0))
            if lh_c is not None:
                pen += lh_c * (dh @ dh)
            parts = [-ga + 2 * self.l2 * (a - a0), -gd + 2 * self.l2 * (d - d0), [-rh.sum()], gmu]
            if n_mu0:
                parts.append([-2 * lmu * (mu - mu0).sum()])
            if n_dh:
                parts.append(gdh)
            return -ll + pen, np.concatenate(parts)

        base = math.log(max((x.mean() + y.mean()) / 2, 0.5))
        th0 = np.concatenate([a0, d0, [0.25], np.full(C, base), np.full(n_mu0, base), np.zeros(n_dh)])
        res = minimize(nll, th0, jac=True, method="L-BFGS-B")
        a, d, h, _, mu, dh = unpack(res.x)
        self.attack, self.defence, self.home_adv = a, d, float(h)
        counts_c = np.bincount(ci, None, C)
        self.mu = float(np.average(mu, weights=counts_c))
        self.mu_comp = {c: float(mu[k]) for c, k in cidx.items()} if self.comp_mu else {}
        self.home_comp = {c: float(dh[k]) for c, k in cidx.items()} if lh_c is not None else {}

        # stage 2: rho on low-score cells
        lh = np.exp(mu[ci] + h + dh[ci] + a[hi] - d[ai])
        la = np.exp(mu[ci] + a[ai] - d[hi])

        def neg_rho(r):
            t = _tau(x, y, lh, la, r)
            if np.any(t <= 0):
                return 1e9
            return -np.sum(w * np.log(t))

        self.rho = float(minimize_scalar(neg_rho, bounds=(-0.2, 0.2), method="bounded").x)
        counts: dict[str, int] = {}
        for r in results:
            counts[r.home] = counts.get(r.home, 0) + 1
            counts[r.away] = counts.get(r.away, 0) + 1
        self.n_matches = counts
        self.n_total = len(results)
        self.fitted = True
        return self

    # -------------------------------------------------------------- predict
    def knows(self, team: str) -> bool:
        return team in self.teams

    def expected_goals(self, home: str, away: str, competition: str | None = None) -> tuple[float, float]:
        ah = self.attack[self.teams[home]] if home in self.teams else 0.0
        dh = self.defence[self.teams[home]] if home in self.teams else 0.0
        aa = self.attack[self.teams[away]] if away in self.teams else 0.0
        da = self.defence[self.teams[away]] if away in self.teams else 0.0
        mu = self.mu_comp.get(competition, self.mu) if competition else self.mu
        home = self.home_adv + (self.home_comp.get(competition, 0.0) if competition else 0.0)
        return float(math.exp(mu + home + ah - da)), float(math.exp(mu + aa - dh))

    def for_competition(self, competition: str) -> "CompetitionView":
        return CompetitionView(self, competition)

    def score_matrix(self, home: str, away: str, log_adj: tuple[float, float] = (0.0, 0.0), competition: str | None = None) -> np.ndarray:
        """log_adj shifts log-lambda of (home, away), e.g. from lineup/availability effects."""
        lh, la = self.expected_goals(home, away, competition)
        lh, la = lh * math.exp(log_adj[0]), la * math.exp(log_adj[1])
        g = np.arange(GRID)
        m = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))
        i, j = np.meshgrid(g, g, indexing="ij")
        m = m * _tau(i, j, np.full_like(m, lh), np.full_like(m, la), self.rho)
        m = np.clip(m, 0, None)
        return m / m.sum()

    def sample_size(self, home: str, away: str) -> int:
        return min(self.n_matches.get(home, 0), self.n_matches.get(away, 0))

    # ------------------------------------------------------------ bootstrap
    @staticmethod
    def bootstrap(
        results: list[MatchResult], as_of: datetime, n: int, seed: int = 0, **kw
    ) -> list["DixonColes"]:
        rng = np.random.default_rng(seed)
        out = []
        for _ in range(n):
            idx = rng.integers(0, len(results), len(results))
            try:
                out.append(DixonColes(**kw).fit([results[k] for k in idx], as_of))
            except ValueError:
                continue
        return out


class CompetitionView:
    """A pooled model seen from one competition: same team strengths, that competition's goal level. Drop-in for DixonColes."""

    def __init__(self, model: DixonColes, competition: str):
        self.model, self.competition = model, competition

    def expected_goals(self, home: str, away: str, competition: str | None = None) -> tuple[float, float]:
        return self.model.expected_goals(home, away, competition or self.competition)

    def score_matrix(self, home: str, away: str, log_adj: tuple[float, float] = (0.0, 0.0), competition: str | None = None) -> np.ndarray:
        return self.model.score_matrix(home, away, log_adj, competition or self.competition)

    def __getattr__(self, item):
        return getattr(self.model, item)
