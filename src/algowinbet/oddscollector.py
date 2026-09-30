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
MAX_TOURNAMENTS = 5  # /odds-by-tournaments rejects more (400 "Please provide a maximum of 5 tournament IDs"), and that 400 is billed

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
            parts = self.client.get("/participants", {"sportId": 10})["data"]  # cached 7 days: fixtures carry only ids
            self._mapper = OddsPapiMapper(self.names, markets if isinstance(markets, list) else [], parts)
        return self._mapper

    def _link(self, ext_id: str, fixture_id: str) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES(?,?,?,?)",
                              (SOURCE, ext_id, fixture_id, self.now().isoformat()))
        self.store.db.commit()

    def last_snapshot_at(self) -> datetime | None:
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint='/odds-by-tournaments' AND status=200",
                                    (SOURCE,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def _chunks(self) -> list[list[str]]:
        ids = list(map(str, self.tournaments))
        return [ids[i:i + MAX_TOURNAMENTS] for i in range(0, len(ids), MAX_TOURNAMENTS)]

    def snapshot_cost(self) -> int:
        return len(self.wanted_books) * len(self._chunks())

    def sync_odds(self) -> CollectStats:
        """One billable request per bookmaker and per block of at most 5 tournaments (the endpoint takes exactly one
        `bookmaker` and at most 5 `tournamentIds`: both verified live, anything else is a billed 400 INVALID_PARAMETER)."""
        st = CollectStats("odds")

        def work():
            books = self.client.resolve_bookmakers(self.wanted_books)[:3]
            m = self.mapper()
            t = self.now()
            calendar = self.provider.list_fixtures(None, t - timedelta(hours=3), t + timedelta(days=10))
            for book in books:
                for chunk in self._chunks():
                    env = self.client.get("/odds-by-tournaments", {"tournamentIds": ",".join(chunk), "bookmaker": book})
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

    def sync_prematch_history(self, days_ahead: int = 10, max_fixtures: int = 60) -> CollectStats:
        """/historical-odds for linked fixtures NOT played yet: the whole price path up to now, one call per fixture.
        The OddsPapi docs say this endpoint is free, but that is unverified, so the first call is bracketed by /account reads
        (the provider's own request counter): if it was billed, the step stops after that single call."""
        st = CollectStats("history")

        def work():
            t = self.now()
            upcoming = {f.id: f for f in self.provider.list_fixtures(None, t, t + timedelta(days=days_ahead))}
            links = self.store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall()
            rows = sorted(((e, fid) for e, fid in links if fid in upcoming), key=lambda r: upcoming[r[1]].kickoff)
            if not rows:
                st.skipped.append("nessuna partita futura collegata a OddsPapi (serve prima una fotografia)")
                return
            books = self.client.resolve_bookmakers(self.wanted_books)[:3]
            m = self.mapper()
            c0 = self.client.account()["request_count"]
            c1 = self.client.account()["request_count"]
            for i, (ext_id, fid) in enumerate(rows[:max_fixtures]):
                fx = upcoming[fid]
                env = self.client.get("/historical-odds", {"fixtureId": ext_id, "bookmakers": ",".join(books)})
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                quotes = m.history(env["data"], fx, closing=False)
                st.add("quotes", self.store.save_quotes(SOURCE, quotes, raw_id))
                if quotes:
                    first = min(q.observed_at for q in quotes)
                    print(f"  storico {fx.home}-{fx.away} (calcio d'inizio {fx.kickoff:%d/%m %H:%M} UTC): {len(quotes)} prezzi, "
                          f"il primo del {first:%d/%m %H:%M} UTC ({(fx.kickoff - first).total_seconds() / 3600:.0f}h prima)", flush=True)
                else:
                    print(f"  storico {fx.home}-{fx.away}: nessun prezzo pre-partita", flush=True)
                if i == 0:
                    c2 = self.client.account()["request_count"]
                    if all(isinstance(c, int) for c in (c0, c1, c2)):
                        cost = (c2 - c1) - (c1 - c0)  # minus what one /account read costs, if anything
                        print(f"  VERIFICA COSTO: contatore OddsPapi {c0} -> {c1} (/account) -> {c2} (/historical-odds): "
                              f"/historical-odds {'GRATUITO' if cost <= 0 else f'COSTA {cost}'}", flush=True)
                        if cost > 0:
                            st.skipped.append(f"/historical-odds non è gratuito ({cost} richiesta): fermato dopo la prima partita")
                            return
                    else:
                        st.skipped.append("contatore /account non leggibile: costo non verificato, fermato dopo la prima partita")
                        return
            if len(rows) > max_fixtures:
                st.skipped.append(f"{len(rows) - max_fixtures} partite oltre il limite per run")
        self._run(st, work)
        return st

    def sync_closing(self, days_back: int = 3, max_fixtures: int = 10) -> CollectStats:
        """Free /historical-odds for linked fixtures that kicked off at least 2h ago and have no closing line yet."""
        st = CollectStats("closing")

        def work():
            t = self.now()
            rows = self.store.db.execute(
                "SELECT l.ext_id, l.fixture_id FROM fixture_links l WHERE l.source=? AND NOT EXISTS "
                "(SELECT 1 FROM quotes q WHERE q.fixture_id=l.fixture_id AND q.source=? AND q.kind='close')", (SOURCE, HIST_SOURCE)).fetchall()
            eligible = {f.id: f for f in self.provider.list_fixtures(None, t - timedelta(days=days_back), t - timedelta(hours=2))}
            if not any(fid in eligible for _, fid in rows):
                return  # nothing to close: no metadata lookups either
            books = self.client.resolve_bookmakers(self.wanted_books)[:3]
            m = self.mapper()
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
