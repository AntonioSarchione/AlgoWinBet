"""Forward data collection: fetch -> map -> store with observation timestamps. Designed to be run repeatedly by a scheduler
(Windows Task Scheduler / cron): each mode is idempotent and budget-aware, so calling it too often wastes nothing."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .domain import Fixture, FixtureStatus, Player
from .information.news import norm
from .names import TeamNames
from .providers.goalapi import (BudgetExceeded, GoalApiClient, GoalApiError, GoalMapper, MappingReport, PlanUpgradeRequired, SOURCE)
from .snapshots import SnapshotProvider, SnapshotStore


@dataclass
class CollectStats:
    mode: str
    requests: int = 0
    saved: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: MappingReport = field(default_factory=MappingReport)
    stopped_by_budget: bool = False

    def add(self, kind: str, n: int) -> None:
        self.saved[kind] = self.saved.get(kind, 0) + n


class GoalCollector:
    def __init__(self, client: GoalApiClient, store: SnapshotStore, league_ids: list[str], names: TeamNames | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client, self.store, self.league_ids, self.now = client, store, league_ids, now
        self.mapper = GoalMapper(names)
        self.provider = SnapshotProvider(store)

    # ------------------------------------------------------------- helpers
    def _run(self, stats: CollectStats, fn) -> None:
        before = self.client.requests_sent
        try:
            fn()
        except BudgetExceeded as e:
            stats.stopped_by_budget = True
            stats.errors.append(str(e))
        except PlanUpgradeRequired as e:
            stats.errors.append(f"endpoint non incluso nel piano ({e})")
        except GoalApiError as e:
            stats.errors.append(f"{type(e).__name__}: {e}")
        finally:
            stats.requests += self.client.requests_sent - before

    def plan(self, mode: str, n_fixtures: int = 10) -> int:
        """Rough request estimate for --dry-run."""
        lg = len(self.league_ids)
        return {"fixtures": lg, "results": lg, "lineups": n_fixtures, "odds": n_fixtures, "players": lg * 21}.get(mode, lg)

    # --------------------------------------------------------------- modes
    def sync_fixtures(self, days_ahead: int = 7) -> CollectStats:
        st = CollectStats("fixtures")
        t0 = self.now()

        def work():
            for lid in self.league_ids:
                rows = list(self.client.pages(f"/leagues/{lid}/fixtures", {"from": f"{t0:%Y-%m-%d}", "to": f"{t0 + timedelta(days=days_ahead):%Y-%m-%d}",
                                                                        "status": "SCHEDULED"}))
                fx = [f for f in (self.mapper.fixture(r) for r in rows) if f]
                st.add("fixtures", self.store.save_fixtures(SOURCE, fx, t0))
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def sync_results(self, days_back: int = 3) -> CollectStats:
        st = CollectStats("results")
        t0 = self.now()

        def work():
            for lid in self.league_ids:
                rows = list(self.client.pages(f"/leagues/{lid}/results", {"from": f"{t0 - timedelta(days=days_back):%Y-%m-%d}", "to": f"{t0:%Y-%m-%d}"}))
                res = [r for r in (self.mapper.result(x) for x in rows) if r]
                st.add("results", self.store.save_results(SOURCE, res, t0))
                self.store.save_fixtures(SOURCE, [f for f in (self.mapper.fixture(x) for x in rows) if f], t0)  # final status
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def _upcoming(self, within: timedelta) -> list[Fixture]:
        t = self.now()
        return [f for f in self.provider.list_fixtures(None, t, t + within) if f.status == FixtureStatus.SCHEDULED]

    def _reconcile_players(self, ids: list[str], team: str, roster: list[Player]) -> list[str]:
        """Lineup rows may carry only names: resolve them to roster ids (same normalised name)."""
        by_name = {norm(p.name): p.id for p in roster if p.team == team}
        by_surname: dict[str, list[str]] = {}
        for p in roster:
            if p.team == team:
                by_surname.setdefault(norm(p.name).split()[-1], []).append(p.id)
        out = []
        for i in ids:
            if i.startswith(f"{team}::"):
                name = norm(i.split("::", 1)[1])
                if name in by_name:
                    i = by_name[name]
                elif len(by_surname.get(name.split()[-1], [])) == 1:
                    i = by_surname[name.split()[-1]][0]
            out.append(i)
        return out

    def sync_lineups(self, window_minutes: int = 95, max_fixtures: int | None = None) -> CollectStats:
        """Poll fixtures kicking off within the window. A fixture whose two teams already have a confirmed XI stored is skipped."""
        st = CollectStats("lineups")
        t = self.now()

        def work():
            done = 0
            for f in self._upcoming(timedelta(minutes=window_minutes)):
                have = {l.team for l in self.provider.get_lineups(f.id) if l.status == "confirmed"}
                if {f.home, f.away} <= have:
                    st.skipped.append(f"{f.home}-{f.away}: XI già confermate")
                    continue
                if max_fixtures is not None and done >= max_fixtures:
                    st.skipped.append(f"{f.home}-{f.away}: limite partite per run")
                    continue
                env = self.client.get(f"/fixtures/{f.provider_event_id or f.id.split(':', 1)[-1]}/lineups")
                fetched = env["_fetched_at"]
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                lus = self.mapper.lineups(env.get("data"), f, fetched)
                roster = self.provider.list_players(f.competition)
                for l in lus:
                    l.starters = self._reconcile_players(l.starters, l.team, roster)
                    l.bench = self._reconcile_players(l.bench, l.team, roster)
                st.add("lineups", self.store.save_lineups(SOURCE, lus, raw_id))
                done += 1
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def sync_odds(self, hours_ahead: int = 48, max_fixtures: int = 20) -> CollectStats:
        st = CollectStats("odds")

        def work():
            for f in self._upcoming(timedelta(hours=hours_ahead))[:max_fixtures]:
                env = self.client.get(f"/fixtures/{f.provider_event_id or f.id.split(':', 1)[-1]}/odds")
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                qs = self.mapper.odds(env.get("data"), f, env["_fetched_at"])
                st.add("quotes", self.store.save_quotes(SOURCE, qs, raw_id))
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def sync_players(self) -> CollectStats:
        """Rosters for every team of the tracked leagues (about 1 request per team): run rarely (monthly)."""
        st = CollectStats("players")
        t0 = self.now()

        def work():
            for lid in self.league_ids:
                teams = list(self.client.pages(f"/leagues/{lid}/teams"))
                for row in teams:
                    tid = row.get("id") or row.get("teamId")
                    name = self.mapper.names.canon(str(row.get("name") or ""))
                    if tid is None or not name:
                        self.mapper.report.gap("team: id/nome mancanti")
                        continue
                    ps = [p for p in (self.mapper.player(r, name) for r in self.client.pages(f"/teams/{tid}/players")) if p]
                    st.add("players", self.store.save_players(SOURCE, ps, t0))
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def name_check(self, history_provider) -> set[str]:
        """Team names in stored fixtures that do not exist in the history source: add aliases in team_aliases.json."""
        known = {t for c in history_provider.list_competitions()
                 for r in history_provider.list_history([c], datetime(2100, 1, 1, tzinfo=timezone.utc)) for t in (r.home, r.away)}
        mine = {t for f in self.provider.list_fixtures(None, datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc))
                for t in (f.home, f.away)}
        return self.mapper.names.unmatched(mine, known)
