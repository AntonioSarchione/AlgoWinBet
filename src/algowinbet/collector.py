"""Forward data collection: fetch -> map -> store with observation timestamps. Designed to be run repeatedly by a scheduler
(Windows Task Scheduler / cron): each mode is idempotent and budget-aware, so calling it too often wastes nothing."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .domain import Fixture, FixtureStatus, Player, Position
from .information.news import norm
from .names import TeamNames
from .providers.goalapi import (BudgetExceeded, GoalApiClient, GoalApiError, GoalMapper, MappingReport, NotFound, PlanUpgradeRequired, SOURCE)
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
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint=? AND status=200 "
                                    "AND params LIKE '%\"from\"%'",  # real syncs only: a manual probe must not count as one
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
                # GOAL's "to" date is exclusive: to=today left out today's matches, so evening matches only arrived with the
                # next morning's request (every evening result run from 2026-10-03 to 10-05 saved 0, the morning one 8)
                rows = list(self.client.pages(f"/leagues/{lid}/results", {"from": f"{t0 - timedelta(days=days):%Y-%m-%d}",
                                                                          "to": f"{t0 + timedelta(days=1):%Y-%m-%d}"}, max_pages=60))
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
            read: set[str] = set()
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
                read.add(f.id)
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                lus = self.mapper.lineups(env.get("data"), f, fetched)
                roster = self.provider.list_players(f.competition)
                for l in lus:
                    l.starters = self._reconcile_players(l.starters, l.team, roster)
                    l.bench = self._reconcile_players(l.bench, l.team, roster)
                st.add("lineups", self.store.save_lineups(SOURCE, lus, raw_id))
                if named := self._save_missing_players(env.get("data"), f.home, f.away, f.kickoff - self.XI_BEFORE_KICKOFF):
                    st.add("players", named)
                done += 1
            self._name_unnamed_xi(st, read)
        self._run(st, work)
        st.report = self.mapper.report
        return st

    # ------------------------------------------------- historical lineups (Fase 6-bis)
    LINEUP_PLAYERS = "goal-lineups"
    XI_BEFORE_KICKOFF = timedelta(minutes=60)  # official XI are published about an hour before kickoff  # players table source of the players seen in GOAL lineups (their ids match the XI)

    def pending_lineup_history(self, competitions: list[str], since: datetime) -> list[tuple]:
        """Finished matches of these competitions since `since` with no confirmed XI stored, newest first."""
        return [r[:5] for r in self.pending_history(competitions, since) if r[5]]

    def pending_history(self, competitions: list[str], since: datetime) -> list[tuple]:
        """Finished matches since `since` missing the XI or the goal events, newest first:
        (fixture_id, competition, home, away, kickoff, needs XI, needs events, total goals)."""
        qs = ",".join("?" * len(competitions))
        return self.store.db.execute(
            f"SELECT * FROM (SELECT r.fixture_id, r.competition, r.home, r.away, r.kickoff, "
            f"NOT EXISTS (SELECT 1 FROM lineups l WHERE l.fixture_id = r.fixture_id AND l.status = 'confirmed') "
            f"AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.name = 'no-lineup:' || r.fixture_id) AS xi, "
            f"NOT EXISTS (SELECT 1 FROM event_reads e WHERE e.fixture_id = r.fixture_id) AS ev, "
            f"COALESCE(r.home_goals, 0) + COALESCE(r.away_goals, 0) AS goals FROM results r WHERE r.fixture_id LIKE 'goal:%' "
            f"AND r.competition IN ({qs}) AND r.kickoff >= ?) WHERE xi OR ev ORDER BY kickoff DESC",
            (*competitions, since.isoformat())).fetchall()

    EVENTS_GRACE = timedelta(days=3)  # events published late: an empty or short list is read again until then

    def backfill_lineups(self, competitions: list[str], since: datetime, max_requests: int, max_seconds: float,
                         clock: Callable[[], float] | None = None, workers: int = 8, batch: int = 50, guard=None) -> CollectStats:
        """XI and goal events of finished matches (1 GOAL request each, both read at the same time for a match that needs both):
        the player impact model learns from the XI who really matters, the goalscorer model (Fase 9) learns from the events who
        scores. Requests run `workers` at a time and the database is written once per `batch` matches (one write to Turso costs
        about 0.7 s: per-match writes made the first backfill ~4 s a match). Raw lineup payloads are not kept; the event list of
        each match is (event_reads.payload, a few hundred bytes), so it can be mapped again without requests.
        Players keep the team of their most recent match seen (a transfer moves them)."""
        import time as _time
        from concurrent.futures import ThreadPoolExecutor
        from .events import event_rows, goals_of, lineup_keys, parse_events, read_row, resolve
        clock = clock or _time.monotonic
        st = CollectStats("history")
        t0 = clock()
        # first rows of the backfill were dated at kickoff: moved to the usual publication time (idempotent)
        self.store.db.execute(
            "UPDATE lineups SET observed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', observed_at, '-60 minutes'), "
            "published_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', observed_at, '-60 minutes') WHERE source = ? AND status = 'confirmed' "
            "AND observed_at = (SELECT kickoff FROM results WHERE results.fixture_id = lineups.fixture_id)", (SOURCE,))
        self.store.db.commit()
        todo, n_req = [], 0
        for row in self.pending_history(competitions, since):
            cost = int(bool(row[5])) + int(bool(row[6]))
            if n_req + cost > max_requests:
                break
            todo.append(row)
            n_req += cost
        now = datetime.now(timezone.utc)
        all_keys = dict(self.store.db.execute("SELECT key, player_id FROM player_keys WHERE source = ?", (SOURCE,)).fetchall())

        def fetch(task):
            row, what = task
            try:
                return task, self.client.get(f"/fixtures/{row[0].split(':', 1)[1]}/{what}").get("data"), None
            except (GoalApiError, BudgetExceeded) as e:
                return task, None, e

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for k in range(0, len(todo), batch):
                if clock() - t0 > max_seconds:
                    st.skipped.append("tempo esaurito")
                    break
                chunk = todo[k:k + batch]
                tasks = [(r, w) for r in chunk for w, need in (("lineups", r[5]), ("events", r[6])) if need]
                if guard is not None:
                    try:
                        guard.check(len(tasks))
                    except BudgetExceeded as e:
                        st.stopped_by_budget = True
                        st.errors.append(str(e))
                        break
                got: dict[tuple[str, str], object] = {}
                sent = 0
                for (row, what), data, err in pool.map(fetch, tasks):
                    if err is not None and not isinstance(err, NotFound):  # 404: nothing published, same as an empty answer
                        st.errors.append(f"{row[2]}-{row[3]} {what}: {err}")
                        continue
                    sent += 1
                    got[(row[0], what)] = data
                st.requests += sent
                if guard is not None and sent:
                    guard.add(sent)
                lus, players, empty, keys = [], [], [], {}
                for fid, comp, home, away, ko, need_xi, _need_ev, _goals in chunk:
                    if not need_xi or (fid, "lineups") not in got:
                        continue
                    data = got[(fid, "lineups")]
                    kickoff = datetime.fromisoformat(ko)
                    seen = kickoff - self.XI_BEFORE_KICKOFF
                    fx = Fixture(id=fid, competition=comp, home=home, away=away, kickoff=kickoff, status=FixtureStatus.FINISHED)
                    # a finished match: the XI listed is the one that played, whatever flag the payload carries
                    xi = [l.model_copy(update={"status": "confirmed", "published_at": seen, "observed_at": seen})
                          for l in self.mapper.lineups(data, fx, seen) if len(l.starters) >= 7]
                    keys.update(lineup_keys(data))
                    if xi:
                        lus += xi
                        players += [(p, kickoff) for p in self._lineup_players(data, home, away)]
                    else:
                        empty.append(fid)
                st.add("lineups", self.store.save_lineups(SOURCE, lus) if lus else 0)
                st.add("players", self._save_newest_players(players))
                if keys:
                    all_keys.update(keys)
                    self.store._bulk("INSERT OR REPLACE INTO player_keys(source, key, player_id)", [(SOURCE, k, v) for k, v in keys.items()])
                if empty:  # nothing published for these matches: remembered, never asked again
                    self.store._bulk("INSERT OR REPLACE INTO jobs(name, done_at, detail)",
                                     [(f"no-lineup:{f}", now.isoformat(), "GOAL senza formazione") for f in empty])
                    st.add("senza formazione", len(empty))
                ev_rows, reads, late = [], [], 0
                squads = self._squads([r for r in chunk if r[6] and (r[0], "events") in got])
                for fid, comp, home, away, ko, _need_xi, need_ev, goals in chunk:
                    if not need_ev or (fid, "events") not in got:
                        continue
                    data = got[(fid, "events")]
                    evs = parse_events(data, home, away)
                    if goals_of(evs) < goals and now - datetime.fromisoformat(ko) < self.EVENTS_GRACE:
                        late += 1  # scorers not all published yet: read again on a later morning
                        continue
                    resolve(evs, all_keys, squads.get(fid, {}))
                    ev_rows += event_rows(fid, evs)
                    reads.append(read_row(fid, data, goals_of(evs), now))
                if ev_rows:
                    self.store._bulk("INSERT OR REPLACE INTO match_events(fixture_id, seq, minute, team, kind, detail, player_id, player_key, "
                                     "player_name, assist_id, assist_key, assist_name, source)", ev_rows)
                if reads:
                    self.store._bulk("INSERT OR REPLACE INTO event_reads(fixture_id, source, n, observed_at, payload)", reads)
                st.add("eventi", len(reads))
                if late:
                    st.add("eventi in ritardo", late)
                if any(isinstance(e, str) and "budget" in e for e in st.errors):
                    break
        return st

    def remap_events(self, batch: int = 200) -> int:
        """Map again every stored event list (event_reads.payload) with the current parser: no request. Returns the events
        written."""
        from .events import event_rows, parse_events, resolve
        keys = dict(self.store.db.execute("SELECT key, player_id FROM player_keys WHERE source = ?", (SOURCE,)).fetchall())
        rows = self.store.db.execute("SELECT e.fixture_id, r.home, r.away, e.payload FROM event_reads e JOIN results r ON r.fixture_id = e.fixture_id "
                                     "WHERE e.payload IS NOT NULL").fetchall()
        n = 0
        for k in range(0, len(rows), batch):
            chunk = rows[k:k + batch]
            squads = self._squads([(f, None, h, a) for f, h, a, _ in chunk])
            out = []
            for fid, home, away, payload in chunk:
                evs = parse_events(json.loads(payload), home, away)
                resolve(evs, keys, squads.get(fid, {}))
                out += event_rows(fid, evs)
            ids = [r[0] for r in chunk]
            self.store.db.execute(f"DELETE FROM match_events WHERE source = ? AND fixture_id IN ({','.join('?' * len(ids))})", (SOURCE, *ids))
            n += self.store._bulk("INSERT OR REPLACE INTO match_events(fixture_id, seq, minute, team, kind, detail, player_id, player_key, "
                                  "player_name, assist_id, assist_key, assist_name, source)", out) if out else 0
        return n

    def _squads(self, chunk: list[tuple]) -> dict[str, dict[str, dict[str, str]]]:
        """{fixture: {team: {player id: name}}} from the stored XI and bench of these matches (names from the players table):
        two reads for the whole batch."""
        fids = [r[0] for r in chunk]
        out: dict[str, dict[str, dict[str, str]]] = {r[0]: {r[2]: {}, r[3]: {}} for r in chunk}
        if not fids:
            return out
        rows = self.store.db.execute(f"SELECT fixture_id, team, starters, bench FROM lineups WHERE status = 'confirmed' "
                                     f"AND fixture_id IN ({','.join('?' * len(fids))})", fids).fetchall()
        ids = [(f, t, set(json.loads(s or "[]")) | set(json.loads(b or "[]"))) for f, t, s, b in rows]
        every = sorted(set().union(*(p for _, _, p in ids))) if ids else []
        names: dict[str, str] = {}
        for i in range(0, len(every), 500):
            part = every[i:i + 500]
            names.update(self.store.db.execute(f"SELECT id, name FROM players WHERE id IN ({','.join('?' * len(part))})", part).fetchall())
        for f, team, pids in ids:
            out.setdefault(f, {}).setdefault(team, {}).update({p: names[p] for p in pids if p in names})
        return out

    NAMES_LOOKBACK = timedelta(days=2)
    NAMES_PER_RUN = 4

    def _save_missing_players(self, data, home: str, away: str, at: datetime) -> int:
        """Players of a live XI the players table does not know yet (a national team's players from leagues we do not read):
        saved with the name in the payload, so the match page shows names, not ids. A known player keeps its row (its club),
        and a later club match moves a new one to its club (the row is dated at this XI)."""
        new = {p.id: p for p in self._lineup_players(data, home, away, default=Position.MID)}
        if not new:
            return 0
        ids = list(new)
        known = {r[0] for r in self.store.db.execute(f"SELECT id FROM players WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()}
        rows = [(p.id, p.name, p.team, p.position.value, 1.0, None, self.LINEUP_PLAYERS, at.isoformat()) for p in new.values() if p.id not in known]
        if rows:
            self.store._bulk("INSERT OR IGNORE INTO players(id,name,team,position,importance,start_rate,source,updated_at)", rows)
        return len(rows)

    def _name_unnamed_xi(self, st: CollectStats, read: set[str]) -> None:
        """Confirmed XI of the last days holding ids the players table does not know (stored before the names were saved):
        their lineups are read once more for the names, at most NAMES_PER_RUN matches a run, each match once (none read in this run)."""
        since = (self.now() - self.NAMES_LOOKBACK).isoformat()
        rows = self.store.db.execute(
            "SELECT l.fixture_id, l.starters, l.bench FROM lineups l WHERE l.source = ? AND l.status = 'confirmed' AND l.observed_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.name = 'xi-names:' || l.fixture_id)", (SOURCE, since)).fetchall()
        ids: dict[str, set[str]] = {}
        for fid, s, b in rows:
            ids.setdefault(fid, set()).update(i for i in json.loads(s or "[]") + json.loads(b or "[]") if i.startswith("goal:"))
        every = sorted(set().union(*ids.values())) if ids else []
        known: set[str] = set()
        for i in range(0, len(every), 500):
            part = every[i:i + 500]
            known.update(r[0] for r in self.store.db.execute(f"SELECT id FROM players WHERE id IN ({','.join('?' * len(part))})", part).fetchall())
        todo = [f for f, p in ids.items() if p - known and f not in read][:self.NAMES_PER_RUN]
        for fid in todo:
            fx = self.store.db.execute("SELECT home, away, kickoff FROM fixtures WHERE fixture_id = ? ORDER BY observed_at DESC LIMIT 1", (fid,)).fetchone()
            if fx is None:
                continue
            data = self.client.get(f"/fixtures/{fid.split(':', 1)[1]}/lineups").get("data")
            if named := self._save_missing_players(data, fx[0], fx[1], datetime.fromisoformat(fx[2]) - self.XI_BEFORE_KICKOFF):
                st.add("players", named)
            self.store.mark_job(f"xi-names:{fid}", self.now(), "nomi dei giocatori letti")

    def _lineup_players(self, data, home: str, away: str, default: Position | None = None) -> list[Player]:
        out: list[Player] = []
        for key, team in (("home", home), ("away", away)):
            side = data.get(key) if isinstance(data, dict) else None
            if not isinstance(side, dict):
                continue
            for p in (side.get("startingLineups") or []) + (side.get("substitutes") or []):
                if not isinstance(p, dict) or not p.get("playerId") or not p.get("lineupPlayer"):
                    continue
                pos = self.mapper._POS.get(str(p.get("playerPosition") or p.get("lineupPosition") or "").strip().lower(), default)
                if pos is not None:
                    out.append(Player(id=f"goal:{p['playerId']}", name=str(p["lineupPlayer"]), team=team, position=pos))
        return out

    def _save_newest_players(self, players: list[tuple[Player, datetime]]) -> int:
        """The most recent match decides the team: an older match read later never moves a player back."""
        newest: dict[str, tuple[Player, datetime]] = {}
        for p, at in players:
            if p.id not in newest or at > newest[p.id][1]:
                newest[p.id] = (p, at)
        if not newest:
            return 0
        seen = {}
        ids = list(newest)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            seen.update(self.store.db.execute(f"SELECT id, updated_at FROM players WHERE id IN ({','.join('?' * len(part))})", part).fetchall())
        fresh = [(p, at) for p, at in newest.values() if not seen.get(p.id) or seen[p.id] < at.isoformat()]
        by_time: dict[datetime, list[Player]] = {}
        for p, at in fresh:
            by_time.setdefault(at, []).append(p)
        old = {r[0]: (r[1], r[2]) for r in self.store.db.execute("SELECT id, importance, start_rate FROM players").fetchall()} if fresh else {}
        rows = [(p.id, p.name, p.team, p.position.value, (old.get(p.id) or (1.0, None))[0] or 1.0, (old.get(p.id) or (None, None))[1],
                 self.LINEUP_PLAYERS, at.isoformat()) for p, at in fresh]
        if rows:
            self.store._bulk("INSERT OR REPLACE INTO players(id,name,team,position,importance,start_rate,source,updated_at)", rows)
        return len(rows)

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
