"""Deterministic synthetic league generator: lets the full pipeline run and be tested with no external source.

Bookmakers price from the TRUE goal rates plus independent noise and a margin, so a correct engine should find
~no persistent edge. `bias_over` plants a known inefficiency (bookmakers underrate goals) to prove the engine
can detect real value.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from scipy.stats import poisson

from ..domain import Fixture, FixtureStatus, InformationEvent, MatchResult, OddsQuote, utc
from ..markets import GRID, REGISTRY, mask
from ..domain import SelectionRef

BOOKS = ("BookA", "BookB", "BookC")


def _grid(lh: float, la: float) -> np.ndarray:
    g = np.arange(GRID)
    m = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))
    return m / m.sum()


def _power_margin(p: np.ndarray, margin: float) -> np.ndarray:
    """Implied probs q_i = p_i^k with k<=1 s.t. sum(q)=1+margin (longshots carry more margin, as in real books)."""
    lo, hi = 0.3, 1.0
    for _ in range(60):
        k = (lo + hi) / 2
        if np.sum(p**k) > 1 + margin:
            lo = k
        else:
            hi = k
    return p ** ((lo + hi) / 2)


class MockProvider:
    name = "mock"

    def __init__(
        self,
        seed: int = 7,
        as_of: datetime | None = None,
        competitions: tuple[str, ...] = ("Mock League A", "Mock League B"),
        n_teams: int = 12,
        past_rounds: int = 20,
        future_rounds: int = 2,
        margin: float = 0.05,
        bias_over: float = 0.0,
        open_noise: float = 0.02,
        close_noise: float = 0.01,
    ):
        self.as_of = as_of or utc(2025, 4, 20, 12, 0)
        self.rng = np.random.default_rng(seed)
        self.margin, self.bias_over = margin, bias_over
        self.open_noise, self.close_noise = open_noise, close_noise
        self.competitions = list(competitions)
        self._fixtures: dict[str, Fixture] = {}
        self._results: dict[str, MatchResult] = {}
        self._quotes: dict[str, list[OddsQuote]] = {}
        self._events: dict[str, list[InformationEvent]] = {}
        self._truth: dict[str, tuple[float, float]] = {}
        for c, comp in enumerate(competitions):
            self._build_competition(comp, c, n_teams, past_rounds, future_rounds)

    # ------------------------------------------------------------- generation
    def _build_competition(self, comp, c, n_teams, past_rounds, future_rounds):
        rng = self.rng
        teams = [f"{comp.split()[-1]}{c}-Team{k + 1:02d}" for k in range(n_teams)]
        atk = rng.normal(0, 0.25, n_teams)
        dfn = rng.normal(0, 0.25, n_teams)
        home_adv, mu = 0.25, 0.25
        # circle-method round robin (repeated to cover all rounds)
        idx = list(range(n_teams))
        rounds = []
        for r in range(past_rounds + future_rounds):
            pairs = [(idx[k], idx[n_teams - 1 - k]) for k in range(n_teams // 2)]
            if (r // (n_teams - 1)) % 2 == 1:
                pairs = [(b, a) for a, b in pairs]
            rounds.append(pairs)
            idx = [idx[0]] + [idx[-1]] + idx[1:-1]
        first_future = self.as_of + timedelta(days=2)
        start = first_future - timedelta(weeks=past_rounds)
        for r, pairs in enumerate(rounds):
            kickoff = start + timedelta(weeks=r, hours=r % 3)
            for k, (h, a) in enumerate(pairs):
                fid = f"mock-{c}-{r:02d}-{k:02d}"
                lh = float(np.exp(mu + home_adv + atk[h] - dfn[a]))
                la = float(np.exp(mu + atk[a] - dfn[h]))
                past = kickoff + timedelta(hours=3) <= self.as_of
                fx = Fixture(
                    id=fid, competition=comp, home=teams[h], away=teams[a], kickoff=kickoff,
                    status=FixtureStatus.FINISHED if past else FixtureStatus.SCHEDULED, provider="mock",
                )
                self._fixtures[fid] = fx
                self._truth[fid] = (lh, la)
                if past:
                    hg, ag = int(rng.poisson(lh)), int(rng.poisson(la))
                    self._results[fid] = MatchResult(
                        fixture_id=fid, competition=comp, home=fx.home, away=fx.away,
                        kickoff=kickoff, home_goals=hg, away_goals=ag,
                    )
                    self._quotes[fid] = self._make_quotes(fid, lh, la, kickoff - timedelta(hours=48), "open", self.open_noise)
                    self._quotes[fid] += self._make_quotes(fid, lh, la, kickoff - timedelta(hours=1), "close", self.close_noise)
                else:
                    self._quotes[fid] = self._make_quotes(fid, lh, la, self.as_of - timedelta(hours=1), "current", self.open_noise)
                    self._events[fid] = []

    def _make_quotes(self, fid, lh, la, at, kind, noise) -> list[OddsQuote]:
        out = []
        common = self.rng.normal(0, noise, 2)  # shared market view: real books are highly correlated
        for book in BOOKS:
            e = common + self.rng.normal(0, 0.015, 2)
            bl_h = lh * np.exp(e[0] - self.bias_over)
            bl_a = la * np.exp(e[1] - self.bias_over)
            m = _grid(bl_h, bl_a)
            groups = [("MATCH_1X2", None, ("HOME", "DRAW", "AWAY")), ("TOTAL_GOALS", 2.5, ("OVER", "UNDER")),
                      ("TOTAL_GOALS", 1.5, ("OVER", "UNDER")), ("TOTAL_GOALS", 3.5, ("OVER", "UNDER")),
                      ("BTTS", None, ("YES", "NO"))]
            for code, line, sels in groups:
                ps = np.array([m[mask(SelectionRef(market_code=code, selection=s, line=line))].sum() for s in sels])
                odds = 1.0 / _power_margin(ps, self.margin)  # margin shape consistent with the power devig
                for s, o in zip(sels, odds):
                    out.append(OddsQuote(
                        fixture_id=fid, market_code=code, selection=s, line=line, bookmaker=book,
                        odds=round(float(max(o, 1.01)), 2), observed_at=at, kind=kind,
                    ))
        return out

    # ---------------------------------------------------------------- adapter
    def list_competitions(self) -> list[str]:
        return list(self.competitions)

    def list_fixtures(self, competitions, start, end) -> list[Fixture]:
        return sorted(
            (f for f in self._fixtures.values()
             if (not competitions or f.competition in competitions) and start <= f.kickoff <= end),
            key=lambda f: f.kickoff,
        )

    def list_history(self, competitions, until) -> list[MatchResult]:
        return sorted(
            (r for r in self._results.values()
             if (not competitions or r.competition in competitions) and r.available_at <= until),
            key=lambda r: r.kickoff,
        )

    def get_quotes(self, fixture_id: str) -> list[OddsQuote]:
        return list(self._quotes.get(fixture_id, []))

    def list_markets(self, fixture_id: str) -> set[str]:
        return {q.market_code for q in self._quotes.get(fixture_id, [])}

    def get_events(self, fixture_id: str) -> list[InformationEvent]:
        return list(self._events.get(fixture_id, []))

    def result_of(self, fixture_id: str) -> MatchResult | None:
        return self._results.get(fixture_id)
