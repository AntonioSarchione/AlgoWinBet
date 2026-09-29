"""OddsPapi collection: tournament-wide price snapshots (1 billable request covers every fixture of a league) and free
closing-line history after kickoff. Quotes are stored under the GOAL fixture id so the engine sees one calendar."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from .collector import CollectStats
from .names import TeamNames
from .providers.oddspapi import SOURCE, OddsPapiClient, OddsPapiError, OddsPapiMapper
from .snapshots import BudgetExceeded, SnapshotProvider, SnapshotStore

HIST_SOURCE = "oddspapi-hist"

LINKS_SCHEMA = """CREATE TABLE IF NOT EXISTS fixture_links(source TEXT, ext_id TEXT, fixture_id TEXT, linked_at TEXT,
  PRIMARY KEY(source, ext_id));"""


class OddsCollector:
    def __init__(self, client: OddsPapiClient, store: SnapshotStore, tournament_ids: list[str], bookmakers: list[str],
                 names: TeamNames | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client, self.store, self.tournaments, self.wanted_books, self.now = client, store, tournament_ids, bookmakers, now
        self.names = names or TeamNames()
        self.provider = SnapshotProvider(store)
        store.db.executescript(LINKS_SCHEMA)
        self._mapper: OddsPapiMapper | None = None

    def _run(self, st: CollectStats, fn) -> None:
        before = self.client.requests_sent
        try:
            fn()
        except BudgetExceeded as e:
            st.stopped_by_budget = True
            st.errors.append(str(e))
        except OddsPapiError as e:
            st.errors.append(str(e))
        finally:
            st.requests += self.client.requests_sent - before
            if self._mapper:
                st.report = self._mapper.report

    def mapper(self) -> OddsPapiMapper:
        if self._mapper is None:
            markets = self.client.get("/markets")["data"]  # cached 7 days in the store
            self._mapper = OddsPapiMapper(self.names, markets if isinstance(markets, list) else [])
        return self._mapper

    def _link(self, ext_id: str, fixture_id: str) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES(?,?,?,?)",
                              (SOURCE, ext_id, fixture_id, self.now().isoformat()))
        self.store.db.commit()

    def last_snapshot_at(self) -> datetime | None:
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint='/odds-by-tournaments' AND status=200",
                                    (SOURCE,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def sync_odds(self) -> CollectStats:
        """1 billable request per call (all tournaments at once, max 3 bookmakers per the API rules)."""
        st = CollectStats("odds")

        def work():
            books = self.client.resolve_bookmakers(self.wanted_books)[:3]
            m = self.mapper()
            t = self.now()
            calendar = self.provider.list_fixtures(None, t - timedelta(hours=3), t + timedelta(days=10))
            env = self.client.get("/odds-by-tournaments", {"tournamentIds": ",".join(self.tournaments), "bookmakers": ",".join(books)})
            raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
            rows = env["data"] if isinstance(env["data"], list) else [env["data"]]
            for row in rows:
                fx = m.match_fixture(row, calendar)
                if fx is None:
                    continue
                self._link(str(row.get("fixtureId")), fx.id)
                st.add("quotes", self.store.save_quotes(SOURCE, m.odds(row, fx, env["_fetched_at"]), raw_id))
        self._run(st, work)
        return st

    def sync_closing(self, days_back: int = 3, max_fixtures: int = 10) -> CollectStats:
        """Free /historical-odds for linked fixtures that kicked off at least 2h ago and have no closing line yet."""
        st = CollectStats("closing")

        def work():
            books = self.client.resolve_bookmakers(self.wanted_books)[:3]
            m = self.mapper()
            t = self.now()
            rows = self.store.db.execute(
                "SELECT l.ext_id, l.fixture_id FROM fixture_links l WHERE l.source=? AND NOT EXISTS "
                "(SELECT 1 FROM quotes q WHERE q.fixture_id=l.fixture_id AND q.source=? AND q.kind='close')", (SOURCE, HIST_SOURCE)).fetchall()
            eligible = {f.id: f for f in self.provider.list_fixtures(None, t - timedelta(days=days_back), t - timedelta(hours=2))}
            done = 0
            for ext_id, fid in rows:
                fx = eligible.get(fid)
                if fx is None:
                    continue
                if done >= max_fixtures:
                    st.skipped.append(f"{fx.home}-{fx.away}: limite partite per run")
                    continue
                env = self.client.get("/historical-odds", {"fixtureId": ext_id, "bookmakers": ",".join(books)})
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                st.add("quotes", self.store.save_quotes(HIST_SOURCE, m.history(env["data"], fx), raw_id))
                done += 1
        self._run(st, work)
        return st
