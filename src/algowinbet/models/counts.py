"""Count models for corners and cards (Fase 7): the goal model's structure on another statistic.

Each team has a "for" strength (corners won, cards received) and an "against" strength (corners conceded, cards provoked),
with home advantage, a level per competition, time decay and ridge shrinkage: the Poisson fit of DixonColes on the
statistic's counts. The counts are over-dispersed (a match with many corners tends to have many for both sides, cards
depend on referee and stakes), so the matrix uses a negative binomial with the dispersion measured on the fit's own
residuals instead of a Poisson.

Optional shared factor (shared=True): one match-level multiplier G ~ Gamma(k_s, 1/k_s) on both sides (referee, stakes,
derby): the two counts become positively correlated, which the product of two independent rows cannot express. k_s comes
from the weighted covariance of the home and away residuals; the totals' spread then follows the data.

Optional referee factor (fit(..., referees=...)): one multiplier per referee on both sides, the ratio of the cards he
showed to the cards expected in his matches, shrunk towards 1 with a prior worth `ref_prior` expected cards (empirical
Bayes, gamma-Poisson), with the same time decay.

Data: football-data season files (every domestic league match, corners, yellow and red cards) and the GOAL match stats.
Cups and national competitions without stats of their own use the teams' strengths from their leagues and the overall level.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
from scipy.special import gammaln, roots_genlaguerre
from scipy.stats import nbinom, poisson

from ..domain import MatchResult
from .dixon_coles import DixonColes

GRID = {"corners": 31, "cards": 16}  # counts 0..30 corners, 0..15 cards per team


class CountModel:
    def __init__(self, stat: str, xi: float = math.log(2) / 365.0, l2: float = 1.0, shared: bool = False,
                 level_xi: float | None = None, ref_prior: float = 40.0):
        self.stat = stat
        self.grid = GRID[stat]
        self.base = DixonColes(xi=xi, l2=l2, comp_mu=True)
        self.size = math.inf  # negative binomial size k: variance = mu + mu^2 / k (inf = Poisson)
        self.shared = shared
        self.shared_size = math.inf  # k_s of the match-level factor (inf = none)
        # level_xi: the competition level re-measured with this (faster) decay after the fit, as a multiplier on both sides;
        # a league whose counts drift (new refereeing guidelines) is followed sooner than the team strengths
        self.level_xi = level_xi
        self.level_adj: dict[str | None, float] = {}
        self.ref_prior = ref_prior
        self.ref_adj: dict[str, float] = {}
        self.fitted = False

    def fit(self, rows: list[MatchResult], as_of: datetime, referees: dict[str, str] | None = None) -> "CountModel":
        """rows: matches with the statistic's counts in home_goals / away_goals; referees: match id -> referee key."""
        self.base.fit(rows, as_of)
        mu, y, w = [], [], []
        for r in rows:
            age = max((as_of - r.kickoff).total_seconds() / 86400.0, 0.0)
            wt = math.exp(-self.base.xi * age)
            lh, la = self.base.expected_goals(r.home, r.away, r.competition, getattr(r, "neutral", False))
            mu += [lh, la]
            y += [r.home_goals, r.away_goals]
            w += [wt, wt]
        mu_a, y_a, w_a = np.array(mu), np.array(y, dtype=float), np.array(w)
        if self.level_xi is not None:
            ratio: dict[str | None, list[float]] = {}
            for i, r in enumerate(rows):
                age = max((as_of - r.kickoff).total_seconds() / 86400.0, 0.0)
                wl = math.exp(-self.level_xi * age)
                acc = ratio.setdefault(r.competition, [0.0, 0.0])
                acc[0] += wl * (y_a[2 * i] + y_a[2 * i + 1])
                acc[1] += wl * (mu_a[2 * i] + mu_a[2 * i + 1])
            self.level_adj = {c: (a / b if b > 0 else 1.0) for c, (a, b) in ratio.items()}
            adj = np.repeat([self.level_adj.get(r.competition, 1.0) for r in rows], 2)
            mu_a = mu_a * adj
        if referees:
            acc_r: dict[str, list[float]] = {}
            for i, r in enumerate(rows):
                key = referees.get(r.fixture_id)
                if key:
                    a = acc_r.setdefault(key, [0.0, 0.0])
                    a[0] += w_a[2 * i] * (y_a[2 * i] + y_a[2 * i + 1])
                    a[1] += w_a[2 * i] * (mu_a[2 * i] + mu_a[2 * i + 1])
            self.ref_adj = {k: (a + self.ref_prior) / (b + self.ref_prior) for k, (a, b) in acc_r.items()}
            adj = np.repeat([self.ref_adj.get(referees.get(r.fixture_id) or "", 1.0) for r in rows], 2)
            mu_a = mu_a * adj
        inv_shared = 0.0
        if self.shared:
            mh, ma, yh, ya, wm = mu_a[0::2], mu_a[1::2], y_a[0::2], y_a[1::2], w_a[0::2]
            cov = float(np.sum(wm * (yh - mh) * (ya - ma)))
            inv_shared = max(cov / float(np.sum(wm * mh * ma)), 0.0)
            self.shared_size = 1.0 / inv_shared if inv_shared > 0 else math.inf
        excess = float(np.sum(w_a * ((y_a - mu_a) ** 2 - mu_a)))
        # method of moments; with the shared factor, only what it leaves unexplained goes to each side's own dispersion
        inv = excess / float(np.sum(w_a * mu_a**2)) - inv_shared
        self.size = 1.0 / inv if inv > 1e-9 else math.inf
        self.fitted = True
        return self

    def knows(self, team: str) -> bool:
        return self.base.knows(team)

    def sample_size(self, home: str, away: str) -> int:
        return self.base.sample_size(home, away)

    def expected(self, home: str, away: str, competition: str | None = None, neutral: bool = False,
                 referee: str | None = None) -> tuple[float, float]:
        """A competition without stats of its own (cups) gets the overall level; an unknown referee counts 1."""
        comp = competition if competition in self.base.mu_comp else None
        lh, la = self.base.expected_goals(home, away, comp, neutral)
        f = self.level_adj.get(comp, 1.0) if comp is not None else 1.0
        f *= self.ref_adj.get(referee or "", 1.0)
        return lh * f, la * f

    def _pmf(self, lam: float) -> np.ndarray:
        g = np.arange(self.grid)
        if math.isinf(self.size):
            p = poisson.pmf(g, lam)
        else:
            p = nbinom.pmf(g, self.size, self.size / (self.size + lam))
        p[-1] += max(0.0, 1.0 - p.sum())  # the tail beyond the grid counts as the last cell
        return p

    def matrix(self, home: str, away: str, competition: str | None = None, neutral: bool = False,
               referee: str | None = None) -> np.ndarray:
        lh, la = self.expected(home, away, competition, neutral, referee)
        if math.isinf(self.shared_size):
            m = np.outer(self._pmf(lh), self._pmf(la))
        else:  # E over G ~ Gamma(k, 1/k) of the product at (lh G, la G): Gauss-Laguerre nodes
            k = self.shared_size
            x, wq = roots_genlaguerre(24, k - 1.0)
            wq = wq * np.exp(-gammaln(k))
            m = sum(wi * np.outer(self._pmf(lh * xi / k), self._pmf(la * xi / k)) for xi, wi in zip(x, wq))
        return m / m.sum()
