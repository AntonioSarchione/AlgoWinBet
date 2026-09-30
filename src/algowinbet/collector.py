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
        return {"fixtures": lg, "results": lg, "lineups": n_fixtures, "odds": n_fixtures, "stats": n_fixtures, "players": lg * 21}.get(mode, lg)

    # --------------------------------------------------------------- modes
    def last_sync(self, lid: str, kind: str) -> datetime | None:
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint=? AND status=200",
                                    (SOURCE, f"/leagues/{lid}/{kind}")).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def stale_leagues(self, kind: str, age: timedelta = timedelta(hours=20)) -> list[str]:
        """Per league: a tick interrupted halfway must not leave the other leagues waiting until tomorrow."""
        t = self.now()
        return [lid for lid in self.league_ids if (last := self.last_sync(lid, kind)) is None or t - last >= age]

    def sync_fixtures(self, days_ahead: int = 7, leagues: list[str] | None = None) -> CollectStats:
        st = CollectStats("fixtures")
        t0 = self.now()

        def work():
            for lid in leagues or self.league_ids:
                rows = list(self.client.pages(f"/leagues/{lid}/fixtures", {"from": f"{t0:%Y-%m-%d}", "to": f"{t0 + timedelta(days=days_ahead):%Y-%m-%d}",
                                                                        "status": "SCHEDULED"}))
                fx = [f for f in (self.mapper.fixture(r) for r in rows) if f]
                st.add("fixtures", self.store.save_fixtures(SOURCE, fx, t0))
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def days_since_last_results(self, lid: str, default: int = 3) -> int:
        """Incremental update per league: only the days since that league's last successful results sync (+1 day overlap
        for late corrections), never the whole history again."""
        last = self.last_sync(lid, "results")
        if last is None:
            return default
        return max(1, min(default * 10, (self.now() - last).days + 1))

    def sync_results(self, days_back: int | None = None, leagues: list[str] | None = None) -> CollectStats:
        st = CollectStats("results")
        t0 = self.now()

        def work():
            for lid in leagues or self.league_ids:
                days = days_back if days_back is not None else self.days_since_last_results(lid)
                rows = list(self.client.pages(f"/leagues/{lid}/results", {"from": f"{t0 - timedelta(days=days):%Y-%m-%d}", "to": f"{t0:%Y-%m-%d}"}, max_pages=60))
                res = [r for r in (self.mapper.result(x) for x in rows) if r]
                st.add("results", self.store.save_results(SOURCE, res, t0))
                self.store.save_fixtures(SOURCE, [f for f in (self.mapper.fixture(x) for x in rows) if f], t0)  # final status
        self._run(st, work)
        st.report = self.mapper.report
        return st

    def backfill_next(self, seasons: int = 2) -> CollectStats | None:
        """Multi-season results history straight from GOAL (replaces manual CSV downloads). One league per call, marked
        done in the store so it never repeats; the daily `results` step keeps it current afterwards."""
        todo = [lid for lid in self.league_ids if not self.store.job_done(f"backfill:goal:{lid}")]
        if not todo:
            return None
        from .state import season_start
        t = self.now()
        first = season_start(t).replace(year=season_start(t).year - seasons)
        st = self.sync_results((t - first).days + 1, leagues=[todo[0]])
        st.mode = f"backfill {todo[0]}"
        if not st.errors:
            self.store.mark_job(f"backfill:goal:{todo[0]}", self.now(), f"{st.saved.get('results', 0)} risultati")
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

    def sync_stats(self, days_back: int = 3, max_fixtures: int = 30) -> CollectStats:
        """Team statistics of finished matches not yet stored (1 request per match, run after results)."""
        st = CollectStats("stats")
        t = self.now()

        def work():
            rows = self.store.db.execute(
                "SELECT r.fixture_id FROM results r WHERE r.kickoff >= ? AND r.kickoff <= ? AND r.fixture_id LIKE 'goal:%' "
                "AND NOT EXISTS (SELECT 1 FROM match_stats s WHERE s.fixture_id = r.fixture_id) ORDER BY r.kickoff DESC",
                ((t - timedelta(days=days_back)).isoformat(), t.isoformat())).fetchall()
            for (fid,) in rows[:max_fixtures]:
                res = self.provider.result_of(fid)
                fx = Fixture(id=fid, competition=res.competition, home=res.home, away=res.away, kickoff=res.kickoff, provider=SOURCE,
                             provider_event_id=fid.split(":", 1)[1])
                env = self.client.get(f"/fixtures/{fx.provider_event_id}/statistics")
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                if isinstance(env.get("data"), dict) and env["data"].get("hasStatistics") is False:
                    st.skipped.append(f"{fx.home}-{fx.away}: statistiche non ancora disponibili")
                    continue
                st.add("stats", self.store.save_stats(SOURCE, self.mapper.stats(env.get("data"), fx, env["_fetched_at"]), raw_id))
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
