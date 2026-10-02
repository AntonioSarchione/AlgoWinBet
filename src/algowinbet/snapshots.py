"""Append-only snapshot storage with real observation timestamps (spec 8.3, 27.3).

Every fetch is recorded: the raw response (gzip, deduplicated by hash) and the normalised rows derived from it. Quotes, lineups
and fixtures are stored WITH the time we observed them, so that later backtests can replay exactly what was knowable at any
cutoff. `SnapshotProvider` exposes the store through the ProviderAdapter contract: analysis never spends API requests.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .domain import (Fixture, FixtureStatus, HistoricalLineup, InformationEvent, LineupSnapshot, MatchResult, MatchStat, NewsItem,
                     OddsQuote, Player, Position)
from .names import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, body BLOB);
CREATE TABLE IF NOT EXISTS raw_requests(id INTEGER PRIMARY KEY, source TEXT, endpoint TEXT, params TEXT, fetched_at TEXT,
  status INTEGER, hash TEXT, cost INTEGER);
CREATE TABLE IF NOT EXISTS fixtures(id INTEGER PRIMARY KEY, source TEXT, fixture_id TEXT, competition TEXT, home TEXT, away TEXT,
  kickoff TEXT, status TEXT, observed_at TEXT, raw_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_fix ON fixtures(fixture_id, observed_at);
CREATE TABLE IF NOT EXISTS results(fixture_id TEXT PRIMARY KEY, source TEXT, competition TEXT, home TEXT, away TEXT, kickoff TEXT,
  home_goals INTEGER, away_goals INTEGER, observed_at TEXT);
CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY, fixture_id TEXT, market_code TEXT, selection TEXT, line REAL, line_key TEXT,
  bookmaker TEXT, odds REAL, observed_at TEXT, kind TEXT, source TEXT, raw_id INTEGER,
  UNIQUE(fixture_id, market_code, selection, line_key, bookmaker, observed_at, source));
CREATE INDEX IF NOT EXISTS ix_q ON quotes(fixture_id, observed_at);
CREATE TABLE IF NOT EXISTS lineups(id INTEGER PRIMARY KEY, fixture_id TEXT, team TEXT, status TEXT, formation TEXT, starters TEXT,
  bench TEXT, published_at TEXT, observed_at TEXT, source TEXT, source_level TEXT, raw_id INTEGER,
  UNIQUE(fixture_id, team, status, observed_at, source));
CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY, name TEXT, team TEXT, position TEXT, importance REAL, start_rate REAL,
  source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS news_items(id TEXT PRIMARY KEY, source TEXT, level TEXT, published_at TEXT, observed_at TEXT, text TEXT,
  team TEXT, fixture_id TEXT);
CREATE TABLE IF NOT EXISTS match_stats(fixture_id TEXT, period TEXT, stat TEXT, home REAL, away REAL, observed_at TEXT, source TEXT,
  raw_id INTEGER, PRIMARY KEY(fixture_id, period, stat, source));
CREATE TABLE IF NOT EXISTS player_status(id INTEGER PRIMARY KEY, source TEXT, fixture_id TEXT, team TEXT, player_id TEXT,
  player_name TEXT, status TEXT, reason TEXT, observed_at TEXT, UNIQUE(source, fixture_id, player_id, status, reason));
CREATE INDEX IF NOT EXISTS ix_status_fx ON player_status(fixture_id);
CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY, done_at TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS api_usage(source TEXT, period TEXT, used INTEGER, PRIMARY KEY(source, period));
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


REMOTE_PREFIXES = ("libsql://", "https://", "wss://")

# GOAL lists some matches twice in the same /leagues/{id}/results page: two fixture ids built from two upstream feeds (different
# apiId), same clubs, kickoff and score (37 pairs on early 2025/26 pages). Same canonical clubs with kickoffs this close = one match.
SAME_MATCH = timedelta(hours=3)


def _match_key(home: str, away: str) -> tuple[str, str]:
    return normalize(home), normalize(away)


def canonical_ids(matches: Iterable[tuple[str, str, datetime, str]]) -> dict[str, str]:
    """(home, away, kickoff, fixture_id) of every stored row -> fixture_id: the id each match is known by everywhere (calendar,
    history, results, odds links), the smallest of the ids GOAL gave it. Kickoffs chained within SAME_MATCH are one match."""
    out: dict[str, str] = {}
    last: dict[tuple[str, str], tuple[datetime, list[str]]] = {}
    groups: list[list[str]] = []
    for h, a, ko, fid in sorted(matches, key=lambda m: (m[2], m[3])):
        k = _match_key(h, a)
        if k in last and ko - last[k][0] <= SAME_MATCH:
            last[k][1].append(fid)
            last[k] = (ko, last[k][1])
        else:
            g = [fid]
            last[k] = (ko, g)
            groups.append(g)
    for g in groups:
        c = min(g)
        out.update((fid, c) for fid in g)
    return out


def connect(path: str | Path):
    """Local SQLite file, or Turso when `path` is a libsql:// URL or the word "turso" (URL from TURSO_DATABASE_URL, token from
    TURSO_AUTH_TOKEN). TURSO_MODE=remote (default) talks to the cloud database directly; TURSO_MODE=replica keeps a local
    libSQL embedded replica (local reads, writes to the cloud primary). Either way a job on a fresh machine (GitHub Actions)
    sees everything previous runs stored. Returns (connection, needs_sync)."""
    p = str(path)
    if p == "turso":
        p = os.environ.get("TURSO_DATABASE_URL", "")
        if not p:
            raise RuntimeError("TURSO_DATABASE_URL mancante (e TURSO_AUTH_TOKEN): impostali come segreti, mai nel repo")
    if p.startswith(REMOTE_PREFIXES):
        import libsql  # optional dependency: pip install libsql
        if os.environ.get("TURSO_MODE", "remote") == "remote":
            # Default on ephemeral CI runners: no local copy to download on every run (the DB only grows).
            return libsql.connect(database=p, auth_token=os.environ.get("TURSO_AUTH_TOKEN", "")), False
        replica = Path(os.environ.get("TURSO_REPLICA_PATH", "data/turso-replica.db"))
        replica.parent.mkdir(parents=True, exist_ok=True)
        import time
        t0, had = time.monotonic(), replica.exists()
        conn = libsql.connect(str(replica), sync_url=p, auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""))
        conn.sync()
        size = replica.stat().st_size / 1e6 if replica.exists() else 0.0
        print(f"replica del database: {'aggiornata' if had else 'scaricata da zero'} in {time.monotonic() - t0:.0f}s ({size:.0f} MB)", flush=True)
        return HybridConnection(conn, libsql.connect(database=p, auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""))), True
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ALGOWINBET_DB_DRIVER") == "libsql":  # run the local test-suite on the same engine used in production
        import libsql
        return libsql.connect(p), False
    return sqlite3.connect(p), False


class HybridConnection:
    """Reads from the embedded replica (local, ~0 ms), writes on a direct connection to the primary. Measured on the Actions
    runners (2026-10-02): a write through the replica costs ~2.7 s, the same write direct ~0.7 s. A read that follows writes
    first pulls them into the replica (~0.4 s), so every read still sees what this run wrote."""

    _READS = ("SELECT", "WITH", "PRAGMA", "EXPLAIN")

    def __init__(self, replica, remote):
        self.replica, self.remote, self.dirty, self.replica_ok = replica, remote, False, True

    def execute(self, sql: str, *args):
        if sql.lstrip()[:7].upper().startswith(self._READS):
            if self.dirty:
                self.sync()
            return (self.replica if self.replica_ok else self.remote).execute(sql, *args)
        self.dirty = True
        return self.remote.execute(sql, *args)

    def executemany(self, sql: str, seq):
        self.dirty = True
        return self.remote.executemany(sql, seq)

    def executescript(self, script: str):
        self.dirty = True
        return self.remote.executescript(script)

    def commit(self) -> None:
        self.remote.commit()

    def sync(self) -> None:
        """Retried: a dropped HTTP connection must not end the run. If the replica cannot catch up, reads go to the primary
        for the rest of the run (slower, still correct)."""
        import time
        for wait in (0.5, 2.0, 5.0, None):
            try:
                self.replica.sync()
                self.dirty = False
                return
            except ValueError as e:  # libsql reports sync failures as ValueError("sync error: ...")
                if wait is None:
                    print(f"replica non sincronizzata ({e}): letture dal database principale", flush=True)
                    self.replica_ok, self.dirty = False, False
                    return
                time.sleep(wait)

    def close(self) -> None:
        self.remote.close()
        self.replica.close()


class BudgetExceeded(RuntimeError):
    pass


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.db, self.remote = connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        if self.remote:
            self.db.sync()
        self.db.close()

    # ---------------------------------------------------------------- raw
    def put_raw(self, source: str, endpoint: str, params: dict | None, status: int, body: bytes, fetched_at: datetime,
                cost: int = 1) -> int:
        h = hashlib.sha256(body).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO blobs(hash, body) VALUES(?,?)", (h, gzip.compress(body)))
        cur = self.db.execute(
            "INSERT INTO raw_requests(source,endpoint,params,fetched_at,status,hash,cost) VALUES(?,?,?,?,?,?,?)",
            (source, endpoint, json.dumps(params or {}, sort_keys=True), _iso(fetched_at), status, h, cost))
        self.db.commit()
        return int(cur.lastrowid)

    def raw_body(self, raw_id: int) -> bytes:
        row = self.db.execute("SELECT b.body FROM raw_requests r JOIN blobs b ON b.hash=r.hash WHERE r.id=?", (raw_id,)).fetchone()
        return gzip.decompress(row[0]) if row else b""

    def last_raw(self, source: str, endpoint_like: str) -> tuple[int, dict] | None:
        row = self.db.execute("SELECT id FROM raw_requests WHERE source=? AND endpoint LIKE ? AND status=200 ORDER BY id DESC LIMIT 1",
                              (source, endpoint_like)).fetchone()
        return (row[0], json.loads(self.raw_body(row[0]))) if row else None

    # ---------------------------------------------------------- normalised
    # Remote databases (Turso) pay one network round trip per statement: rows are written in multi-row statements, and
    # lookups are done with one query per batch, never one per row.
    def _bulk(self, head: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        width = len(rows[0])
        per = max(1, 900 // width)  # stay under SQLite's bound-parameter limit on every build
        n = 0
        for i in range(0, len(rows), per):
            chunk = rows[i:i + per]
            values = ",".join(["(" + ",".join(["?"] * width) + ")"] * len(chunk))
            cur = self.db.execute(f"{head} VALUES {values}", [v for r in chunk for v in r])
            n += max(cur.rowcount or 0, 0)
        self.db.commit()
        return n

    def _latest_fixture_state(self) -> dict[str, tuple[str, str]]:
        rows = self.db.execute("SELECT f.fixture_id, f.kickoff, f.status FROM fixtures f "
                               "JOIN (SELECT fixture_id, MAX(id) mid FROM fixtures GROUP BY fixture_id) m ON f.id=m.mid").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def save_fixtures(self, source: str, fixtures: list[Fixture], observed_at: datetime, raw_id: int | None = None) -> int:
        """Keeps the history of CHANGES (postponement, status), not every poll."""
        last = self._latest_fixture_state()
        batch: dict[str, tuple] = {}
        for f in fixtures:
            if last.get(f.id) == (_iso(f.kickoff), f.status.value):
                continue
            batch[f.id] = (source, f.id, f.competition, f.home, f.away, _iso(f.kickoff), f.status.value, _iso(observed_at), raw_id)
        return self._bulk("INSERT INTO fixtures(source,fixture_id,competition,home,away,kickoff,status,observed_at,raw_id)", list(batch.values()))

    def _new_matches(self, results: list[MatchResult]) -> list[MatchResult]:
        """Drop results that repeat a match already stored, or earlier in the batch, under another fixture id (one query)."""
        if not results:
            return []
        lo, hi = min(r.kickoff for r in results) - SAME_MATCH, max(r.kickoff for r in results) + SAME_MATCH
        known: dict[tuple[str, str], list[tuple[str, datetime]]] = {}
        for fid, h, a, ko in self.db.execute("SELECT fixture_id, home, away, kickoff FROM results WHERE kickoff BETWEEN ? AND ?",
                                             (_iso(lo), _iso(hi))).fetchall():
            known.setdefault(_match_key(h, a), []).append((fid, _dt(ko)))
        out = []
        for r in sorted(results, key=lambda r: (r.kickoff, r.fixture_id)):
            same = known.setdefault(_match_key(r.home, r.away), [])
            if any(fid != r.fixture_id and abs(ko - r.kickoff) <= SAME_MATCH for fid, ko in same):
                continue
            same.append((r.fixture_id, r.kickoff))
            out.append(r)
        return out

    def save_results(self, source: str, results: list[MatchResult], observed_at: datetime) -> int:
        results = self._new_matches(results)
        return self._bulk("INSERT OR IGNORE INTO results(fixture_id,source,competition,home,away,kickoff,home_goals,away_goals,observed_at)",
                          [(r.fixture_id, source, r.competition, r.home, r.away, _iso(r.kickoff), r.home_goals, r.away_goals, _iso(observed_at))
                           for r in results])

    def save_quotes(self, source: str, quotes: list[OddsQuote], raw_id: int | None = None) -> int:
        rows = [(q.fixture_id, q.market_code, q.selection, q.line, "" if q.line is None else repr(q.line), q.bookmaker, q.odds,
                 _iso(q.observed_at), q.kind, source, raw_id) for q in quotes]
        # A refetched price path repeats almost every stored point: drop those before writing. Reads are local (replica),
        # each write statement is a round trip to the primary, so sending only the new rows is what keeps a tick short.
        have: set[tuple] = set()
        for fid in {r[0] for r in rows}:
            have.update(self.db.execute("SELECT fixture_id, market_code, selection, line_key, bookmaker, observed_at FROM quotes "
                                        "WHERE fixture_id=? AND source=?", (fid, source)).fetchall())
        new = [r for r in rows if (r[0], r[1], r[2], r[4], r[5], r[7]) not in have]
        return self._bulk("INSERT OR IGNORE INTO quotes(fixture_id,market_code,selection,line,line_key,bookmaker,odds,observed_at,kind,source,raw_id)",
                          new)

    def save_lineups(self, source: str, lineups: list[LineupSnapshot], raw_id: int | None = None) -> int:
        return self._bulk("INSERT OR IGNORE INTO lineups(fixture_id,team,status,formation,starters,bench,published_at,observed_at,source,source_level,raw_id)",
                          [(l.fixture_id, l.team, l.status, l.formation, json.dumps(l.starters), json.dumps(l.bench), _iso(l.published_at),
                            _iso(l.observed_at), source, l.source_level, raw_id) for l in lineups])

    def save_players(self, source: str, players: list[Player], at: datetime) -> int:
        old = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT id, importance, start_rate FROM players").fetchall()}
        rows = {}
        for p in players:
            o = old.get(p.id)
            imp = o[0] if o and p.importance == 1.0 and o[0] is not None else p.importance  # keep hand-tuned values
            sr = p.start_rate if p.start_rate is not None else (o[1] if o else None)
            rows[p.id] = (p.id, p.name, p.team, p.position.value, imp, sr, source, _iso(at))
        self._bulk("INSERT OR REPLACE INTO players(id,name,team,position,importance,start_rate,source,updated_at)", list(rows.values()))
        return len(players)

    def save_news(self, items: list[NewsItem]) -> int:
        rows = {}
        for it in items:
            h = hashlib.sha1(f"{it.source}|{it.published_at.isoformat()}|{it.text}".encode()).hexdigest()[:16]
            rows[h] = (h, it.source, it.source_level, _iso(it.published_at), _iso(it.observed_at), it.text, it.team, it.fixture_id)
        return self._bulk("INSERT OR IGNORE INTO news_items(id,source,level,published_at,observed_at,text,team,fixture_id)", list(rows.values()))

    def save_stats(self, source: str, stats: list[MatchStat], raw_id: int | None = None) -> int:
        """Post-match figures: the latest fetch replaces the previous one (providers correct stats after the final whistle)."""
        rows = {(x.fixture_id, x.period, x.stat): (x.fixture_id, x.period, x.stat, x.home, x.away, _iso(x.observed_at), source, raw_id)
                for x in stats}
        self._bulk("INSERT OR REPLACE INTO match_stats(fixture_id,period,stat,home,away,observed_at,source,raw_id)", list(rows.values()))
        return len(stats)

    def stats_of(self, fixture_id: str, period: str = "FT") -> dict[str, tuple[float | None, float | None]]:
        rows = self.db.execute("SELECT stat, home, away FROM match_stats WHERE fixture_id=? AND period=?", (fixture_id, period)).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    # --------------------------------------------------------------- jobs
    def job_done(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE name=?", (name,)).fetchone() is not None

    def mark_job(self, name: str, at: datetime, detail: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO jobs(name, done_at, detail) VALUES(?,?,?)", (name, _iso(at), detail))
        self.db.commit()

    # ------------------------------------------------------------- budget
    def usage(self, source: str, period: str) -> int:
        row = self.db.execute("SELECT used FROM api_usage WHERE source=? AND period=?", (source, period)).fetchone()
        return int(row[0]) if row else 0

    def add_usage(self, source: str, period: str, n: int) -> None:
        self.db.execute("INSERT INTO api_usage(source,period,used) VALUES(?,?,?) ON CONFLICT(source,period) DO UPDATE SET used=used+?",
                        (source, period, n, n))
        self.db.commit()

    def set_usage_at_least(self, source: str, period: str, used: int) -> None:
        self.db.execute("INSERT INTO api_usage(source,period,used) VALUES(?,?,?) ON CONFLICT(source,period) DO UPDATE SET used=MAX(used,?)",
                        (source, period, used, used))
        self.db.commit()

    def stats(self) -> dict[str, int]:
        out = {}
        for t in ("raw_requests", "fixtures", "results", "quotes", "lineups", "players", "news_items", "match_stats"):
            out[t] = self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out


class BudgetGuard:
    """Persistent request counter. `charge()` raises BEFORE a request would exceed the limit (minus reserve)."""

    def __init__(self, store: SnapshotStore, source: str, daily: int | None = None, monthly: int | None = None, reserve: int = 0,
                 now=lambda: datetime.now(timezone.utc)):
        self.store, self.source, self.daily, self.monthly, self.reserve, self.now = store, source, daily, monthly, reserve, now

    def _periods(self) -> tuple[str, str]:
        t = self.now()
        return f"D{t:%Y-%m-%d}", f"M{t:%Y-%m}"

    def used(self) -> tuple[int, int]:
        d, m = self._periods()
        return self.store.usage(self.source, d), self.store.usage(self.source, m)

    def remaining(self) -> dict[str, int | None]:
        ud, um = self.used()
        return {"daily": None if self.daily is None else self.daily - self.reserve - ud,
                "monthly": None if self.monthly is None else self.monthly - self.reserve - um}

    def check(self, n: int = 1) -> None:
        for k, v in self.remaining().items():
            if v is not None and v < n:
                raise BudgetExceeded(f"{self.source}: budget {k} esaurito (rimaste {max(v, 0)}, servono {n}, riserva {self.reserve})")

    def add(self, n: int = 1) -> None:
        d, m = self._periods()
        self.store.add_usage(self.source, d, n)
        self.store.add_usage(self.source, m, n)

    def charge(self, n: int = 1) -> None:
        """check + add: for providers that bill every attempt (GOAL)."""
        self.check(n)
        self.add(n)

    def sync_from_headers(self, limit: int | None, remaining: int | None, kind: str | None) -> None:
        """Trust the provider's own counter when it says we used more than we counted."""
        if limit is None or remaining is None:
            return
        d, m = self._periods()
        used = limit - remaining
        if kind and kind.upper().startswith("MONTH"):
            self.store.set_usage_at_least(self.source, m, used)
        else:
            self.store.set_usage_at_least(self.source, d, used)


class SnapshotProvider:
    """ProviderAdapter over the store. Reads only: no API requests are ever made from here."""

    name = "snapshots"

    def __init__(self, store: SnapshotStore):
        self.store = store

    def _fx(self, row) -> Fixture:
        return Fixture(id=row[0], competition=row[1], home=row[2], away=row[3], kickoff=_dt(row[4]),
                       status=FixtureStatus(row[5]), provider=row[6])

    def _latest_fixtures(self) -> list[Fixture]:
        rows = self.store.db.execute(
            "SELECT f.fixture_id,f.competition,f.home,f.away,f.kickoff,f.status,f.source FROM fixtures f "
            "JOIN (SELECT fixture_id, MAX(id) mid FROM fixtures GROUP BY fixture_id) m ON f.id=m.mid").fetchall()
        return [self._fx(r) for r in rows]

    def list_competitions(self) -> list[str]:
        rows = self.store.db.execute("SELECT competition FROM fixtures UNION SELECT competition FROM results").fetchall()
        return sorted(r[0] for r in rows)

    def _canonical(self, fixtures: list[Fixture] | None = None, results: list[MatchResult] | None = None) -> dict[str, str]:
        fixtures = self._latest_fixtures() if fixtures is None else fixtures
        results = self._results() if results is None else results
        return canonical_ids([(f.home, f.away, f.kickoff, f.id) for f in fixtures] +
                             [(r.home, r.away, r.kickoff, r.fixture_id) for r in results])

    def _unique_fixtures(self) -> list[Fixture]:
        """One fixture per match: a match GOAL lists under two ids appears once, under its canonical id."""
        fixtures = self._latest_fixtures()
        canon = self._canonical(fixtures)
        out: dict[str, Fixture] = {}
        for f in sorted(fixtures, key=lambda f: (f.id != canon.get(f.id, f.id), f.id)):  # the canonical row wins when stored
            c = canon.get(f.id, f.id)
            if c not in out:
                out[c] = f if f.id == c else f.model_copy(update={"id": c})
        return list(out.values())

    def list_fixtures(self, competitions, start, end) -> list[Fixture]:
        return sorted((f for f in self._unique_fixtures()
                       if (not competitions or f.competition in competitions) and start <= f.kickoff <= end), key=lambda f: f.kickoff)

    def _results(self) -> list[MatchResult]:
        rows = self.store.db.execute("SELECT fixture_id,competition,home,away,kickoff,home_goals,away_goals FROM results").fetchall()
        return [MatchResult(fixture_id=r[0], competition=r[1], home=r[2], away=r[3], kickoff=_dt(r[4]), home_goals=r[5], away_goals=r[6])
                for r in rows]

    def _unique_results(self) -> tuple[list[MatchResult], dict[str, str]]:
        """One result per match under its canonical id (pairs stored before save_results deduplicated are read once)."""
        results = self._results()
        canon = self._canonical(results=results)
        out: dict[str, MatchResult] = {}
        for r in sorted(results, key=lambda r: r.fixture_id):
            c = canon.get(r.fixture_id, r.fixture_id)
            if c not in out:
                out[c] = r if r.fixture_id == c else r.model_copy(update={"fixture_id": c})
        return list(out.values()), canon

    def list_history(self, competitions, until) -> list[MatchResult]:
        return sorted((r for r in self._unique_results()[0] if (not competitions or r.competition in competitions) and r.available_at <= until),
                      key=lambda r: (r.kickoff, r.fixture_id))

    def international_results(self) -> bytes | None:
        """Latest international results CSV (national-team Elo), downloaded weekly by the scheduler."""
        if not hasattr(self, "_intl"):
            from .elo import latest_international
            self._intl = latest_international(self.store)
        return self._intl

    def result_of(self, fixture_id: str) -> MatchResult | None:
        """Result of a match by any of its ids (odds or lineups may sit on the id GOAL did not keep for the result)."""
        results, canon = self._unique_results()
        c = canon.get(fixture_id, fixture_id)
        r = next((r for r in results if r.fixture_id == c), None)
        return r.model_copy(update={"fixture_id": fixture_id}) if r else None

    def get_quotes(self, fixture_id: str) -> list[OddsQuote]:
        rows = self.store.db.execute(
            "SELECT market_code,selection,line,bookmaker,odds,observed_at,kind FROM quotes WHERE fixture_id=? ORDER BY observed_at",
            (fixture_id,)).fetchall()
        return [OddsQuote(fixture_id=fixture_id, market_code=r[0], selection=r[1], line=r[2], bookmaker=r[3], odds=r[4],
                          observed_at=_dt(r[5]), kind=r[6]) for r in rows]

    def list_markets(self, fixture_id: str) -> set[str]:
        return {q.market_code for q in self.get_quotes(fixture_id)}

    def get_events(self, fixture_id: str) -> list[InformationEvent]:
        """Structured player availability (API-Football injuries and suspensions) for this fixture."""
        rows = self.store.db.execute("SELECT source, team, player_id, player_name, status, reason, observed_at FROM player_status "
                                     "WHERE fixture_id=?", (fixture_id,)).fetchall()
        return [InformationEvent(id=f"{src}:{fixture_id}:{p}:{st}", fixture_id=fixture_id, team=team, player=p, event_type="PLAYER_STATUS",
                                 source_level="A", published_at=_dt(at), observed_at=_dt(at), confidence=0.95 if st != "DOUBTFUL" else 0.9,
                                 payload={"status": st, "reason": reason, "name": name, "source": src})
                for src, team, p, name, st, reason, at in rows]

    def _lineup(self, r) -> LineupSnapshot:
        return LineupSnapshot(fixture_id=r[0], team=r[1], status=r[2], formation=r[3], starters=json.loads(r[4]), bench=json.loads(r[5]),
                              published_at=_dt(r[6]), observed_at=_dt(r[7]), source_level=r[8])

    def get_lineups(self, fixture_id: str) -> list[LineupSnapshot]:
        rows = self.store.db.execute(
            "SELECT fixture_id,team,status,formation,starters,bench,published_at,observed_at,source_level FROM lineups "
            "WHERE fixture_id=? ORDER BY observed_at", (fixture_id,)).fetchall()
        return [self._lineup(r) for r in rows]

    def list_lineup_history(self, competition: str, until: datetime) -> list[HistoricalLineup]:
        fids = {r.fixture_id for r in self.list_history([competition], until)}
        out = []
        for fid in fids:
            best: dict[str, LineupSnapshot] = {}
            for l in self.get_lineups(fid):
                if l.status == "confirmed":
                    best[l.team] = l  # latest confirmed per team
            out += [HistoricalLineup(fixture_id=fid, team=t, starters=l.starters) for t, l in best.items()]
        return out

    def list_players(self, competition: str) -> list[Player]:
        teams = {t for f in self._latest_fixtures() if f.competition == competition for t in (f.home, f.away)}
        teams |= {t for r in self._results() if r.competition == competition for t in (r.home, r.away)}
        rows = self.store.db.execute("SELECT id,name,team,position,importance,start_rate FROM players").fetchall()
        return [Player(id=r[0], name=r[1], team=r[2], position=Position(r[3]), importance=r[4] or 1.0, start_rate=r[5])
                for r in rows if r[2] in teams]

    def get_news_items(self, fixture_id: str) -> list[NewsItem]:
        fx = next((f for f in self._latest_fixtures() if f.id == fixture_id), None)
        if not fx:
            return []
        rows = self.store.db.execute(
            "SELECT source,level,published_at,observed_at,text,team,fixture_id FROM news_items WHERE fixture_id=? OR team IN (?,?)",
            (fixture_id, fx.home, fx.away)).fetchall()
        return [NewsItem(source=r[0], source_level=r[1], published_at=_dt(r[2]), observed_at=_dt(r[3]), text=r[4], team=r[5],
                         fixture_id=r[6]) for r in rows]


class MergedProvider:
    """Live/collected data from `primary` + extra history (e.g. multi-season CSV) for the goal model."""

    def __init__(self, primary, history_extra):
        self.primary, self.extra = primary, history_extra
        self.name = f"{primary.name}+{history_extra.name}"

    def __getattr__(self, item):
        if item in ("primary", "extra", "name"):
            raise AttributeError(item)
        return getattr(self.primary, item)

    def list_competitions(self) -> list[str]:
        return sorted(set(self.primary.list_competitions()) | set(self.extra.list_competitions()))

    def list_history(self, competitions, until):
        seen, out = set(), []
        for r in list(self.primary.list_history(competitions, until)) + list(self.extra.list_history(competitions, until)):
            k = (r.home, r.away, r.kickoff.date())
            if k not in seen:
                seen.add(k)
                out.append(r)
        return sorted(out, key=lambda r: r.kickoff)
