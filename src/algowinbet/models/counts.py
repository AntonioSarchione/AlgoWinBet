"""Count models for corners and cards (Fase 7): the goal model's structure on another statistic.

Each team has a "for" strength (corners won, cards received) and an "against" strength (corners conceded, cards provoked),
with home advantage, a level per competition, time decay and ridge shrinkage: the Poisson fit of DixonColes on the
statistic's counts. The counts are over-dispersed (a match with many corners tends to have many for both sides, cards
depend on referee and stakes), so the matrix uses a negative binomial with the dispersion measured on the fit's own
residuals instead of a Poisson.

Data: football-data season files (every domestic league match, corners, yellow and red cards) and the GOAL match stats.
Cups and national competitions without stats of their own use the teams' strengths from their leagues and the overall level.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
from scipy.stats import nbinom, poisson

from ..domain import MatchResult
from .dixon_coles import DixonColes

GRID = {"corners": 31, "cards": 16}  # counts 0..30 corners, 0..15 cards per team


class CountModel:
    def __init__(self, stat: str, xi: float = math.log(2) / 365.0, l2: float = 1.0):
        self.stat = stat
        self.grid = GRID[stat]
        self.base = DixonColes(xi=xi, l2=l2, comp_mu=True)
        self.size = math.inf  # negative binomial size k: variance = mu + mu^2 / k (inf = Poisson)
        self.fitted = False

    def fit(self, rows: list[MatchResult], as_of: datetime) -> "CountModel":
        """rows: matches with the statistic's counts in home_goals / away_goals."""
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
        excess = float(np.sum(w_a * ((y_a - mu_a) ** 2 - mu_a)))
        self.size = float(np.sum(w_a * mu_a**2) / excess) if excess > 0 else math.inf  # method of moments
        self.fitted = True
        return self

    def knows(self, team: str) -> bool:
        return self.base.knows(team)

    def sample_size(self, home: str, away: str) -> int:
        return self.base.sample_size(home, away)

    def expected(self, home: str, away: str, competition: str | None = None, neutral: bool = False) -> tuple[float, float]:
        """A competition without stats of its own (cups) gets the overall level."""
        comp = competition if competition in self.base.mu_comp else None
        return self.base.expected_goals(home, away, comp, neutral)

    def _pmf(self, lam: float) -> np.ndarray:
        g = np.arange(self.grid)
        if math.isinf(self.size):
            p = poisson.pmf(g, lam)
        else:
            p = nbinom.pmf(g, self.size, self.size / (self.size + lam))
        p[-1] += max(0.0, 1.0 - p.sum())  # the tail beyond the grid counts as the last cell
        return p

    def matrix(self, home: str, away: str, competition: str | None = None, neutral: bool = False) -> np.ndarray:
        lh, la = self.expected(home, away, competition, neutral)
        m = np.outer(self._pmf(lh), self._pmf(la))
        return m / m.sum()
