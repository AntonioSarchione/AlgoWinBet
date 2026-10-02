"""OddsPapi collection: tournament-wide price snapshots (1 billable request covers every fixture of a league) and free
closing-line history after kickoff. Quotes are stored under the GOAL fixture id so the engine sees one calendar."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .collector import CollectStats
from .domain import OddsQuote
from .names import TeamNames
from .providers.oddspapi import SOURCE, OddsPapiClient, OddsPapiError, OddsPapiMapper
from .snapshots import BudgetExceeded, SnapshotProvider, SnapshotStore

HIST_SOURCE = "oddspapi-hist"
MAX_TOURNAMENTS = 5  # /odds-by-tournaments rejects more (400 "Please provide a maximum of 5 tournament IDs"), and that 400 is billed

LINKS_SCHEMA = """CREATE TABLE IF NOT EXISTS fixture_links(source TEXT, ext_id TEXT, fixture_id TEXT, linked_at TEXT,
  PRIMARY KEY(source, ext_id));"""


CHECKPOINTS_H = (72, 48, 24, 12, 6, 3, 1)  # hours before kickoff kept from a price path (plus the opening and the latest price)


def thin_history(quotes: list[OddsQuote], kickoff: datetime, now: datetime) -> list[OddsQuote]:
    """A full price path is thousands of points per fixture. Per selection keep the opening price, the price in force at each
    checkpoint already passed, and the latest price; the kept points are real observations with their own timestamps."""
    series: dict[tuple, list[OddsQuote]] = {}
    for q in quotes:
        series.setdefault((q.market_code, q.selection, q.line, q.bookmaker), []).append(q)
    out: list[OddsQuote] = []
    for pts in series.values():
        pts.sort(key=lambda q: q.observed_at)
        keep = {0, len(pts) - 1}
        for h in CHECKPOINTS_H:
            at = kickoff - timedelta(hours=h)
            if at > now:
                continue
            before = [i for i, q in enumerate(pts) if q.observed_at <= at]
            if before:
                keep.add(before[-1])
        out += [pts[i] for i in sorted(keep)]
    return out


class OddsCollector:
    def __init__(self, client: OddsPapiClient, store: SnapshotStore, tournament_ids: list[str], bookmakers: list[str],
                 names: TeamNames | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 history_books: list[str] | None = None):
        """bookmakers: counted snapshots (one request each per block of tournaments); history_books: free /historical-odds."""
        self.client, self.store, self.tournaments, self.wanted_books, self.now = client, store, tournament_ids, bookmakers, now
        self.history_books = history_books or bookmakers
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

    def _drop_older_paths(self, raw_id: int) -> None:
        """A newer pre-kickoff price path contains every point of the older ones for the same fixture: keep only the newest
        raw payload (they are the biggest blobs in the database). Closing paths, fetched after kickoff, are never touched."""
        db = self.store.db
        params, fetched = db.execute("SELECT params, fetched_at FROM raw_requests WHERE id=?", (raw_id,)).fetchone()
        old = db.execute("SELECT id, hash FROM raw_requests WHERE source=? AND endpoint='/historical-odds' AND params=? AND id < ? "
                         "AND fetched_at <= ?", (SOURCE, params, raw_id, fetched)).fetchall()
        if not old:
            return
        db.executemany("DELETE FROM raw_requests WHERE id=?", [(i,) for i, _ in old])
        db.executemany("DELETE FROM blobs WHERE hash=? AND NOT EXISTS (SELECT 1 FROM raw_requests WHERE hash=?)", [(h, h) for _, h in old])
        db.commit()

    def sync_prematch_history(self, days_ahead: int = 10, max_fixtures: int = 80, max_seconds: float = 600,
                              clock: Callable[[], float] = time.monotonic, only: list[str] | None = None) -> CollectStats:
        """Free /historical-odds (verified live on 2026-09-30: the provider's request counter did not move) for linked fixtures
        NOT played yet, nearest kickoff first (or just the GOAL fixture ids in `only`): the price path up to now, thinned to
        CHECKPOINTS. Afterwards one free /account read syncs the local budget with the provider's counter, so a future change
        in billing cannot drain the reserve."""
        st = CollectStats("history")

        def work():
            t, t0 = self.now(), clock()
            upcoming = {f.id: f for f in self.provider.list_fixtures(None, t, t + timedelta(days=days_ahead))}
            links = self.store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall()
            wanted = set(only) if only is not None else None
            rows = sorted(((e, fid) for e, fid in links if fid in upcoming and (wanted is None or fid in wanted)),
                          key=lambda r: upcoming[r[1]].kickoff)
            if not rows:
                if wanted is None:
                    st.skipped.append("nessuna partita futura collegata a OddsPapi (serve prima una fotografia)")
                return
            books = self.client.resolve_bookmakers(self.history_books)[:3]
            m = self.mapper()
            for i, (ext_id, fid) in enumerate(rows):
                if i >= max_fixtures or clock() - t0 > max_seconds:
                    st.skipped.append(f"{len(rows) - i} partite rimandate al prossimo giro (limite per run)")
                    break
                fx = upcoming[fid]
                c0 = clock()
                env = self.client.get("/historical-odds", {"fixtureId": ext_id, "bookmakers": ",".join(books)})
                c1 = clock()
                raw_id = self.store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()[0]
                quotes = thin_history(m.history(env["data"], fx, closing=False), fx.kickoff, t)
                c2 = clock()
                st.add("quotes", self.store.save_quotes(SOURCE, quotes, raw_id))
                c3 = clock()
                self._drop_older_paths(raw_id)
                c4 = clock()
                if quotes:
                    first, last = min(q.observed_at for q in quotes), max(q.observed_at for q in quotes)
                    print(f"  storico {fx.home}-{fx.away} ({fx.kickoff:%d/%m %H:%M} UTC): {len(quotes)} prezzi, dal {first:%d/%m %H:%M} "
                          f"({(fx.kickoff - first).total_seconds() / 3600:.0f}h prima) all'ultimo cambio {last:%d/%m %H:%M}  "
                          f"[richiesta {c1 - c0 - getattr(self.client, 'last_raw_s', 0):.1f}s, grezzo {getattr(self.client, 'last_raw_s', 0):.1f}s, "
                          f"lettura {c2 - c1:.1f}s, quote {c3 - c2:.1f}s, pulizia {c4 - c3:.1f}s]", flush=True)
            try:
                self.client.account()
            except OddsPapiError as e:
                st.skipped.append(f"/account non letto ({e}): budget non sincronizzato")
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
            books = self.client.resolve_bookmakers(self.history_books)[:3]
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
                # thinned like the pre-match path (opening, checkpoints, closing line): a full path is 30-40 thousand rows per
                # match and saving it took minutes on Turso, enough to push the whole tick past the job time limit
                st.add("quotes", self.store.save_quotes(HIST_SOURCE, thin_history(m.history(env["data"], fx), fx.kickoff, t), raw_id))
                done += 1
        self._run(st, work)
        return st


def remap_stored_odds(store: SnapshotStore, names: TeamNames | None = None) -> CollectStats:
    """Rebuild every OddsPapi quote from the raw payloads already stored (no API request). Used after a mapping fix: the
    old rows are deleted and each snapshot / history payload is mapped again with the current mapper."""
    import json
    st = CollectStats("remap")
    markets = store.last_raw(SOURCE, "/markets")
    parts = store.last_raw(SOURCE, "/participants")
    if not markets:
        st.errors.append("catalogo /markets non presente nel database")
        return st
    m = OddsPapiMapper(names or TeamNames(), markets[1] if isinstance(markets[1], list) else [], parts[1] if parts else [])
    prov = SnapshotProvider(store)
    far = datetime.now(timezone.utc)
    fixtures = {f.id: f for f in prov.list_fixtures(None, far - timedelta(days=365), far + timedelta(days=60))}
    store.db.executescript(LINKS_SCHEMA)
    links = dict(store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall())
    before = store.db.execute("SELECT COUNT(*) FROM quotes WHERE source IN (?, ?)", (SOURCE, HIST_SOURCE)).fetchone()[0]
    store.db.execute("DELETE FROM quotes WHERE source IN (?, ?)", (SOURCE, HIST_SOURCE))
    store.db.commit()
    st.skipped.append(f"cancellate {before} quote mappate con la versione precedente")
    raws = store.db.execute("SELECT id, endpoint, params, fetched_at FROM raw_requests WHERE source=? AND status=200 AND endpoint IN "
                            "('/odds-by-tournaments', '/historical-odds') ORDER BY id", (SOURCE,)).fetchall()
    calendar = list(fixtures.values())
    for rid, endpoint, params, fetched in raws:
        at = datetime.fromisoformat(fetched)
        body = json.loads(store.raw_body(rid))
        if endpoint == "/odds-by-tournaments":
            for row in body if isinstance(body, list) else [body]:
                fx = fixtures.get(links.get(str(row.get("fixtureId")), "")) or m.match_fixture(row, calendar)
                if fx is not None:
                    st.add("quotes", store.save_quotes(SOURCE, m.odds(row, fx, at), rid))
        else:
            fx = fixtures.get(links.get(str(json.loads(params or "{}").get("fixtureId")), ""))
            if fx is None:
                continue
            if at > fx.kickoff:  # fetched after kickoff: closing line
                st.add("quotes", store.save_quotes(HIST_SOURCE, m.history(body, fx), rid))
            else:
                st.add("quotes", store.save_quotes(SOURCE, thin_history(m.history(body, fx, closing=False), fx.kickoff, at), rid))
    st.report = m.report
    return st
