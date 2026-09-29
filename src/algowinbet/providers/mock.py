"""Deterministic synthetic league generator: lets the full pipeline run and be tested with no external source.

World model
- Team strengths (attack/defence) + home advantage; every player has a true attacking value a_i and defensive value d_i.
- log lambda_side = team base + sum_own a_i (x_i - sbar_i) - sum_opp d_j (x_j - sbar_j), x = actual start indicator.
- Injuries occur at random; injury news is published 30h before kickoff, the official XI 75 minutes before.
- Bookmakers: OPEN quote (kickoff-50h) is blind to injuries and XI; MID quote (kickoff-29h) knows injuries but not who plays;
  CLOSE quote (kickoff-1h) knows the actual XI. Books price from TRUE rates + noise + margin, so a correct engine finds ~no
  persistent edge. `bias_over` plants a known inefficiency; stage=post_lineup with fresh_quotes=False leaves stale odds
  after the lineup is out (edge that only exists because the price has not moved yet).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from scipy.stats import poisson

from ..domain import (Fixture, FixtureStatus, HistoricalLineup, InformationEvent, LineupSnapshot, MatchResult, NewsItem,
                      OddsQuote, Player, Position, SelectionRef, utc)
from ..information.availability import _redistribute
from ..information.impact import PRIOR_MEAN
from ..markets import GRID, mask

BOOKS = ("BookA", "BookB", "BookC")
SURNAMES = ["Rossi", "Bianchi", "Conti", "Greco", "Bruno", "Gallo", "Costa", "Fontana", "Moretti", "Barbieri", "Lombardi",
            "Marino", "Ricci", "Ferrari", "Esposito", "Romano", "Colombo", "Vitale", "Leone", "Serra", "Coppola", "Villa",
            "Mancini", "Testa", "Pellegrini", "Valentini", "Guerra", "Palumbo", "Sala", "Fabbri", "Monti", "Cattaneo"]
POS_RATES = {
    Position.GK: [0.92, 0.08],
    Position.DEF: [0.92, 0.88, 0.85, 0.80, 0.30, 0.25],
    Position.MID: [0.90, 0.85, 0.70, 0.35, 0.20],
    Position.FWD: [0.90, 0.80, 0.70, 0.60],
}
SLOTS = {Position.GK: 1, Position.DEF: 4, Position.MID: 3, Position.FWD: 3}


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
        stage: str = "early",  # early | pre_lineup | post_lineup: where "now" is relative to the next round
        fresh_quotes: bool = False,  # post_lineup: have books already repriced on the official XIs?
        player_effect_scale: float = 1.0,
        injury_rate: float = 0.05,
        stats_informed: bool = True,  # roster importance derived from true effects, mimicking goals/xG-per-90 stats
    ):
        sched_ref = as_of or utc(2025, 4, 20, 12, 0)
        self.rng = np.random.default_rng(seed)
        self.margin, self.bias_over = margin, bias_over
        self.open_noise, self.close_noise = open_noise, close_noise
        self.fresh_quotes, self.scale, self.injury_rate = fresh_quotes, player_effect_scale, injury_rate
        self.stats_informed = stats_informed
        self.competitions = list(competitions)
        self._fixtures: dict[str, Fixture] = {}
        self._results: dict[str, MatchResult] = {}
        self._quotes: dict[str, list[OddsQuote]] = {}
        self._news: dict[str, list[NewsItem]] = {}
        self._lineups: dict[str, list[LineupSnapshot]] = {}
        self._players: dict[str, list[Player]] = {}
        self._effects: dict[str, tuple[float, float]] = {}
        self._truth: dict[str, tuple[float, float]] = {}
        self._sbar: dict[str, float] = {}
        first_future = sched_ref + timedelta(days=2)
        start = first_future - timedelta(weeks=past_rounds)
        self._sched_ref = sched_ref
        kick_next = start + timedelta(weeks=past_rounds, hours=past_rounds % 3)
        self.as_of = {"early": sched_ref, "pre_lineup": kick_next - timedelta(hours=20),
                      "post_lineup": kick_next - timedelta(minutes=60)}[stage]
        for c, comp in enumerate(competitions):
            self._build_competition(comp, c, n_teams, past_rounds, future_rounds, start)

    # ------------------------------------------------------------- generation
    def _make_squad(self, team: str) -> list[Player]:
        names = list(self.rng.choice(SURNAMES, size=17, replace=False))
        squad, k = [], 0
        for pos, rates in POS_RATES.items():
            for j, r in enumerate(rates):
                pid = f"{team}-{pos.value}{j}"
                squad.append(Player(id=pid, name=f"{names[k]}", team=team, position=pos))
                self._sbar[pid] = r
                if pos == Position.FWD:
                    a = 0.18 if j == 0 else self.rng.normal(0.06, 0.03)
                elif pos == Position.MID:
                    a = self.rng.normal(0.03, 0.02)
                elif pos == Position.DEF:
                    a = self.rng.normal(0.01, 0.01)
                else:
                    a = 0.0
                if pos == Position.GK:
                    d = 0.10 if j == 0 else self.rng.normal(0.05, 0.02)
                elif pos == Position.DEF:
                    d = self.rng.normal(0.03, 0.015)
                elif pos == Position.MID:
                    d = self.rng.normal(0.015, 0.01)
                else:
                    d = self.rng.normal(0.005, 0.005)
                self._effects[pid] = (a * self.scale, d * self.scale)
                if self.stats_informed:
                    pm = PRIOR_MEAN["a"][pos] + PRIOR_MEAN["d"][pos]
                    squad[-1].importance = float(np.clip((a + d) * self.scale / pm, 0.2, 4.0)) if pm > 0 else 1.0
                k += 1
        return squad

    def _pick_xi(self, squad: list[Player], injured: set[str]) -> list[str]:
        xi = []
        for pos, n in SLOTS.items():
            avail = [p for p in squad if p.position == pos and p.id not in injured]
            if len(avail) < n:  # squad decimated: play through knocks (rare)
                avail += [p for p in squad if p.position == pos and p.id in injured][: n - len(avail)]
            w = np.array([self._sbar[p.id] + 0.05 for p in avail])
            idx = self.rng.choice(len(avail), size=min(n, len(avail)), replace=False, p=w / w.sum())
            xi += [avail[i].id for i in idx]
        return xi

    def _effect(self, own_p: dict[str, float], opp_p: dict[str, float]) -> float:
        e = 0.0
        for pid, x in own_p.items():
            e += self._effects[pid][0] * (x - self._sbar[pid])
        for pid, x in opp_p.items():
            e -= self._effects[pid][1] * (x - self._sbar[pid])
        return e

    def _expected_starts(self, squad: list[Player], injured: set[str]) -> dict[str, float]:
        base = {p.id: self._sbar[p.id] for p in squad}
        p = {i: (0.0 if i in injured else b) for i, b in base.items()}
        return _redistribute(squad, base, p, set(injured))

    def _build_competition(self, comp, c, n_teams, past_rounds, future_rounds, start):
        rng = self.rng
        teams = [f"{comp.split()[-1]}{c}-Team{k + 1:02d}" for k in range(n_teams)]
        squads = {t: self._make_squad(t) for t in teams}
        self._players[comp] = [p for t in teams for p in squads[t]]
        atk = rng.normal(0, 0.25, n_teams)
        dfn = rng.normal(0, 0.25, n_teams)
        home_adv, mu = 0.25, 0.25
        idx = list(range(n_teams))
        rounds = []
        for r in range(past_rounds + future_rounds):
            pairs = [(idx[k], idx[n_teams - 1 - k]) for k in range(n_teams // 2)]
            if (r // (n_teams - 1)) % 2 == 1:
                pairs = [(b, a) for a, b in pairs]
            rounds.append(pairs)
            idx = [idx[0]] + [idx[-1]] + idx[1:-1]
        inj_left = {p.id: 0 for t in teams for p in squads[t]}
        for r, pairs in enumerate(rounds):
            kickoff = start + timedelta(weeks=r, hours=r % 3)
            for pid in inj_left:  # advance injuries one round, maybe start new ones
                if inj_left[pid] > 0:
                    inj_left[pid] -= 1
                elif rng.random() < self.injury_rate:
                    inj_left[pid] = int(rng.integers(1, 4))
            for k, (h, a) in enumerate(pairs):
                fid = f"mock-{c}-{r:02d}-{k:02d}"
                th, ta = teams[h], teams[a]
                inj_h = {p.id for p in squads[th] if inj_left[p.id] > 0}
                inj_a = {p.id for p in squads[ta] if inj_left[p.id] > 0}
                xi_h, xi_a = self._pick_xi(squads[th], inj_h), self._pick_xi(squads[ta], inj_a)
                base_h = mu + home_adv + atk[h] - dfn[a]
                base_a = mu + atk[a] - dfn[h]
                x_h = {p.id: (1.0 if p.id in xi_h else 0.0) for p in squads[th]}
                x_a = {p.id: (1.0 if p.id in xi_a else 0.0) for p in squads[ta]}
                lh = float(np.exp(base_h + self._effect(x_h, x_a)))
                la = float(np.exp(base_a + self._effect(x_a, x_h)))
                e_h, e_a = self._expected_starts(squads[th], inj_h), self._expected_starts(squads[ta], inj_a)
                lam_mid = (float(np.exp(base_h + self._effect(e_h, e_a))), float(np.exp(base_a + self._effect(e_a, e_h))))
                lam_open = (float(np.exp(base_h)), float(np.exp(base_a)))
                past = kickoff + timedelta(hours=3) <= self._sched_ref
                fx = Fixture(id=fid, competition=comp, home=th, away=ta, kickoff=kickoff,
                             status=FixtureStatus.FINISHED if past else FixtureStatus.SCHEDULED, provider="mock")
                self._fixtures[fid] = fx
                self._truth[fid] = (lh, la)
                if past:
                    self._results[fid] = MatchResult(fixture_id=fid, competition=comp, home=th, away=ta, kickoff=kickoff,
                                                     home_goals=int(rng.poisson(lh)), away_goals=int(rng.poisson(la)))
                qs = self._make_quotes(fid, lam_open, kickoff - timedelta(hours=50), "open", self.open_noise)
                qs += self._make_quotes(fid, lam_mid, kickoff - timedelta(hours=29), "current", self.open_noise)
                if past or self.fresh_quotes:
                    qs += self._make_quotes(fid, (lh, la), kickoff - timedelta(hours=1), "close", self.close_noise)
                self._quotes[fid] = qs
                # information: injury news (30h before), official XI (75 min before)
                items = []
                for team, inj, sq in ((th, inj_h, squads[th]), (ta, inj_a, squads[ta])):
                    for p in sq:
                        if p.id in inj and self._sbar[p.id] >= 0.3:
                            t = kickoff - timedelta(hours=30)
                            items.append(NewsItem(source="club-site", source_level="A", published_at=t, observed_at=t,
                                                  text=f"{team}: {p.name} indisponibile per infortunio.", team=team, fixture_id=fid))
                    if rng.random() < 0.04:  # false-alarm rumour about a healthy regular
                        pool = [q for q in sq if q.id not in inj and self._sbar[q.id] >= 0.7]
                        p = pool[int(rng.integers(len(pool)))]
                        t = kickoff - timedelta(hours=28)
                        items.append(NewsItem(source="forum", source_level="E", published_at=t, observed_at=t,
                                              text=f"Forse {p.name} potrebbe essere in dubbio per la partita.", team=team, fixture_id=fid))
                self._news[fid] = items
                pub = kickoff - timedelta(minutes=75)
                self._lineups[fid] = [
                    LineupSnapshot(fixture_id=fid, team=th, status="confirmed", starters=xi_h, formation="4-3-3", published_at=pub, observed_at=pub),
                    LineupSnapshot(fixture_id=fid, team=ta, status="confirmed", starters=xi_a, formation="4-3-3", published_at=pub, observed_at=pub),
                ]

    def _make_quotes(self, fid, lam, at, kind, noise) -> list[OddsQuote]:
        out = []
        common = self.rng.normal(0, noise, 2)  # shared market view: real books are highly correlated
        for book in BOOKS:
            e = common + self.rng.normal(0, 0.015, 2)
            m = _grid(lam[0] * np.exp(e[0] - self.bias_over), lam[1] * np.exp(e[1] - self.bias_over))
            groups = [("MATCH_1X2", None, ("HOME", "DRAW", "AWAY")), ("TOTAL_GOALS", 2.5, ("OVER", "UNDER")),
                      ("TOTAL_GOALS", 1.5, ("OVER", "UNDER")), ("TOTAL_GOALS", 3.5, ("OVER", "UNDER")),
                      ("BTTS", None, ("YES", "NO"))]
            for code, line, sels in groups:
                ps = np.array([m[mask(SelectionRef(market_code=code, selection=s, line=line))].sum() for s in sels])
                odds = 1.0 / _power_margin(ps, self.margin)
                for s, o in zip(sels, odds):
                    out.append(OddsQuote(fixture_id=fid, market_code=code, selection=s, line=line, bookmaker=book,
                                         odds=round(float(max(o, 1.01)), 2), observed_at=at, kind=kind))
        return out

    # ---------------------------------------------------------------- adapter
    def list_competitions(self) -> list[str]:
        return list(self.competitions)

    def list_fixtures(self, competitions, start, end) -> list[Fixture]:
        return sorted((f for f in self._fixtures.values()
                       if (not competitions or f.competition in competitions) and start <= f.kickoff <= end),
                      key=lambda f: f.kickoff)

    def list_history(self, competitions, until) -> list[MatchResult]:
        return sorted((r for r in self._results.values()
                       if (not competitions or r.competition in competitions) and r.available_at <= until),
                      key=lambda r: r.kickoff)

    def get_quotes(self, fixture_id: str) -> list[OddsQuote]:
        return list(self._quotes.get(fixture_id, []))

    def list_markets(self, fixture_id: str) -> set[str]:
        return {q.market_code for q in self._quotes.get(fixture_id, [])}

    def get_events(self, fixture_id: str) -> list[InformationEvent]:
        return []

    def get_news_items(self, fixture_id: str) -> list[NewsItem]:
        return list(self._news.get(fixture_id, []))

    def get_lineups(self, fixture_id: str) -> list[LineupSnapshot]:
        return list(self._lineups.get(fixture_id, []))

    def list_players(self, competition: str) -> list[Player]:
        return list(self._players.get(competition, []))

    def list_lineup_history(self, competition: str, until: datetime) -> list[HistoricalLineup]:
        out = []
        for fid, r in self._results.items():
            if r.competition == competition and r.available_at <= until:
                out += [HistoricalLineup(fixture_id=fid, team=l.team, starters=l.starters) for l in self._lineups[fid]]
        return out

    def result_of(self, fixture_id: str) -> MatchResult | None:
        return self._results.get(fixture_id)
