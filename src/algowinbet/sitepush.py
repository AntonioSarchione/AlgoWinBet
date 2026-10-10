"""Push of what the dashboard reads from the archive (data/offline.db, the full database kept in the Actions cache) to the small
site database on Turso (docs/SITE_DB.md).

Only differences travel: the archive keeps, per pushed row, a hash of what the site holds (table site_pushed), and a run
writes only new or changed rows and deletes the rows that left the site scope. The state is updated only after the site
committed, so a failed push is redone by the next run. A push epoch, stored on both sides, detects a site database that was
recreated or wiped: then everything is sent again.

Published runs are pushed whole, in one transaction with their pub_runs row, so the site sees either the previous analysis
or the new one, never half of it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .snapshots import REMOTE_PREFIXES

STATE = """
CREATE TABLE IF NOT EXISTS site_pushed(tbl TEXT, k TEXT, h TEXT, PRIMARY KEY(tbl, k));
"""
# what the site needs on top of the archive's own tables
SITE_EXTRA = [
    "CREATE TABLE IF NOT EXISTS site_meta(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS pub_quote_paths(fixture_id TEXT PRIMARY KEY, n INTEGER, last_at TEXT, paths TEXT)",
]
KEEP_RUNS = 2  # analyses kept on the site: a page that read the previous run id still finds its rows
KEEP_RUN_ROWS = 12  # pub_runs rows (Stato del sistema lists the last 12)
RUN_TABLES = ["pub_fixtures", "pub_opportunities", "pub_book", "pub_players", "pub_slips"]
MAX_PARAMS = 900  # SQLite's bound-parameter limit on every build
MAX_BYTES = 800_000  # per statement: a published fixture row carries ~10 KB of JSON, a price path up to ~200 KB


@dataclass
class Table:
    """A site table filled from the archive. `rows` returns the rows in scope (columns in `cols` order); `key` names the
    columns that identify a row on the site (a primary key or unique constraint there)."""
    name: str
    key: list[str]
    rows: Callable[[sqlite3.Connection, list[int]], tuple[list[str], list[tuple]]]
    indexes: list[str] = field(default_factory=list)


def _select(sql: str, params: Callable[[list[int]], list] | None = None):
    def rows(src: sqlite3.Connection, runs: list[int]) -> tuple[list[str], list[tuple]]:
        cur = src.execute(sql, params(runs) if params else [])
        return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]
    return rows


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _in_runs(sql: str):
    """`sql` with {runs} replaced by the placeholders of the kept run ids."""
    def rows(src: sqlite3.Connection, runs: list[int]) -> tuple[list[str], list[tuple]]:
        if not runs:
            cur = src.execute(sql.replace("{runs}", "NULL"))
        else:
            cur = src.execute(sql.replace("{runs}", _marks(len(runs))), runs)
        return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]
    return rows


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def quote_paths(src: sqlite3.Connection, runs: list[int]) -> tuple[list[str], list[tuple]]:
    """One row per published fixture: every Sisal price path of the match (the odds-trend tab), its number of points and the
    newest observation. Same rows the dashboard read from quotes (bookmaker LIKE 'sisal%')."""
    cols = ["fixture_id", "n", "last_at", "paths"]
    if not runs:
        return cols, []
    fids = [r[0] for r in src.execute(f"SELECT DISTINCT fixture_id FROM pub_fixtures WHERE run_id IN ({_marks(len(runs))})", runs)]
    out = []
    for fid in sorted(fids):
        lines: dict[str, list] = {}
        n, last = 0, None
        for market, line_key, sel, book, odds, at in src.execute(
                "SELECT market_code, COALESCE(line_key, ''), selection, bookmaker, odds, observed_at FROM quotes "
                "WHERE fixture_id = ? AND bookmaker LIKE 'sisal%' ORDER BY observed_at, id", (fid,)):
            lines.setdefault(f"{market}|{line_key}", []).append([sel, book, odds, at])
            n += 1
            last = at if last is None or at > last else last
        if n:
            out.append((fid, n, last, json.dumps(lines, separators=(",", ":"), ensure_ascii=False)))
    return cols, out


def site_meta(src: sqlite3.Connection, runs: list[int]) -> tuple[list[str], list[tuple]]:
    row = src.execute("SELECT fetched_at FROM raw_requests ORDER BY id DESC LIMIT 1").fetchone()
    return ["key", "value"], [("last_tick", row[0])] if row and row[0] else []


TABLES: list[Table] = [
    # the tick asks for matches near now: the newest observation of every fixture from two days ago on
    Table("fixtures", ["id"], lambda s, r: _select(
        "SELECT f.* FROM fixtures f JOIN (SELECT fixture_id, MAX(id) AS mid FROM fixtures GROUP BY fixture_id) m ON f.id = m.mid "
        "WHERE f.kickoff >= ?", lambda _: [_since(2)])(s, r), ["CREATE INDEX IF NOT EXISTS ix_site_fix_ko ON fixtures(kickoff)"]),
    Table("lineups", ["id"], _select("SELECT * FROM lineups"),
          ["CREATE INDEX IF NOT EXISTS ix_site_lu_team ON lineups(team, status)"]),
    Table("results", ["fixture_id"], _select("SELECT * FROM results"),
          ["CREATE INDEX IF NOT EXISTS ix_site_res_home ON results(home, kickoff)",
           "CREATE INDEX IF NOT EXISTS ix_site_res_away ON results(away, kickoff)"]),
    Table("players", ["id"], _select("SELECT * FROM players")),
    Table("player_status", ["id"], _in_runs("SELECT * FROM player_status WHERE fixture_id IN (SELECT fixture_id FROM pub_fixtures WHERE run_id IN ({runs}))")),
    Table("player_quotes", ["fixture_id", "bookmaker", "market", "line_key", "player_key"], _in_runs(
        "SELECT * FROM player_quotes WHERE bookmaker LIKE 'sisal%' AND fixture_id IN (SELECT fixture_id FROM pub_fixtures WHERE run_id IN ({runs}))")),
    Table("api_usage", ["source", "period"], _select("SELECT * FROM api_usage")),
    Table("jobs", ["name"], _select("SELECT * FROM jobs")),
    Table("health", ["id"], _select("SELECT * FROM health ORDER BY id DESC LIMIT 1")),
    Table("quality_runs", ["id"], _select("SELECT * FROM quality_runs ORDER BY id DESC LIMIT 2")),
    Table("paper_legs", ["id"], _select("SELECT * FROM paper_legs")),
    Table("paper_slips", ["id"], _select("SELECT * FROM paper_slips")),
    Table("pub_quote_paths", ["fixture_id"], quote_paths),
    Table("site_meta", ["key"], site_meta),
]


def _hash(row: tuple) -> str:
    return hashlib.sha1(repr(row).encode()).hexdigest()


def _key(row: tuple, cols: list[str], key: list[str]) -> str:
    return json.dumps([row[cols.index(k)] for k in key], separators=(",", ":"), default=str)


def connect_site(target: str):
    """The site database: a Turso URL (token from SITE_AUTH_TOKEN) or a local file (tests, local checks)."""
    if target.startswith(REMOTE_PREFIXES):
        import libsql
        return libsql.connect(database=target, auth_token=os.environ.get("SITE_AUTH_TOKEN", ""))
    return sqlite3.connect(target)


def _ddl(src: sqlite3.Connection, table: str) -> str:
    sql = src.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    return re.sub(r"^CREATE TABLE\s+(IF NOT EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", sql, count=1)


def _columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def _chunks(rows: list[tuple]) -> Iterable[list[tuple]]:
    """Statement-sized slices: under the parameter limit and about MAX_BYTES of values."""
    if not rows:
        return
    per = max(1, MAX_PARAMS // len(rows[0]))
    chunk, size = [], 0
    for r in rows:
        b = sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in r)
        if chunk and (len(chunk) >= per or size + b > MAX_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(r)
        size += b
    if chunk:
        yield chunk


class Pusher:
    def __init__(self, src: sqlite3.Connection, site, log: Callable[[str], None] = print):
        self.src, self.site, self.log = src, site, log
        self.written = 0  # rows inserted, updated or deleted on the site (what Turso bills as rows written)
        self.src.executescript(STATE)

    # ---- state kept in the archive ----
    def _state(self, tbl: str) -> dict[str, str]:
        return dict(self.src.execute("SELECT k, h FROM site_pushed WHERE tbl = ?", (tbl,)).fetchall())

    def _save_state(self, tbl: str, put: dict[str, str], drop: Iterable[str]) -> None:
        drop = list(drop)
        for i in range(0, len(drop), 500):
            part = drop[i:i + 500]
            self.src.execute(f"DELETE FROM site_pushed WHERE tbl = ? AND k IN ({_marks(len(part))})", [tbl, *part])
        self.src.executemany("INSERT OR REPLACE INTO site_pushed(tbl, k, h) VALUES(?, ?, ?)", [(tbl, k, h) for k, h in put.items()])
        self.src.commit()

    # ---- site writes (inside the caller's transaction) ----
    def _upsert(self, table: str, cols: list[str], key: list[str], rows: list[tuple]) -> None:
        names = ",".join(f'"{c}"' for c in cols)
        sets = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c not in key)
        tail = f" ON CONFLICT({','.join(key)}) DO UPDATE SET {sets}" if sets else f" ON CONFLICT({','.join(key)}) DO NOTHING"
        for chunk in _chunks(rows):
            values = ",".join(["(" + _marks(len(cols)) + ")"] * len(chunk))
            self.site.execute(f"INSERT INTO {table}({names}) VALUES {values}{tail}", [v for r in chunk for v in r])
        self.written += len(rows)

    def _insert(self, table: str, cols: list[str], rows: list[tuple]) -> None:
        names = ",".join(f'"{c}"' for c in cols)
        for chunk in _chunks(rows):
            values = ",".join(["(" + _marks(len(cols)) + ")"] * len(chunk))
            self.site.execute(f"INSERT INTO {table}({names}) VALUES {values}", [v for r in chunk for v in r])
        self.written += len(rows)

    def _delete(self, table: str, key: list[str], keys: list[str]) -> None:
        for i in range(0, len(keys), max(1, MAX_PARAMS // len(key))):
            part = [json.loads(k) for k in keys[i:i + max(1, MAX_PARAMS // len(key))]]
            if len(key) == 1:
                self.site.execute(f"DELETE FROM {table} WHERE {key[0]} IN ({_marks(len(part))})", [p[0] for p in part])
            else:
                cond = " OR ".join("(" + " AND ".join(f"{k} IS ?" for k in key) + ")" for _ in part)
                self.site.execute(f"DELETE FROM {table} WHERE {cond}", [v for p in part for v in p])
        self.written += len(keys)

    # ---- schema ----
    def schema(self) -> None:
        """Tables and indexes the site reads; a column added to the archive is added to the site table too (and the table
        is sent again, since its rows gained a value)."""
        known = self._state("__schema__")
        ddl = {t: _ddl(self.src, t) for t in ["pub_runs", *RUN_TABLES] + [t.name for t in TABLES if t.name not in ("pub_quote_paths", "site_meta")]}
        put = {}
        for t, sql in ddl.items():
            cols = _columns(self.src, t)
            h = _hash(tuple(cols))
            if known.get(t) == h:
                continue
            self.site.execute(sql)
            have = set(_columns(self.site, t))
            for c in cols:
                if c not in have:
                    kind = next(r[2] for r in self.src.execute(f"PRAGMA table_info({t})") if r[1] == c)
                    self.site.execute(f'ALTER TABLE {t} ADD COLUMN "{c}" {kind}')
            if t in known:  # rows gained a column: send the table again
                self.src.execute("DELETE FROM site_pushed WHERE tbl = ?", (t,))
                if t in RUN_TABLES or t == "pub_runs":
                    self.src.execute("DELETE FROM site_pushed WHERE tbl = 'pub_run'")
            put[t] = h
        if put:
            for sql in SITE_EXTRA + [i for t in TABLES for i in t.indexes] + [
                    "CREATE INDEX IF NOT EXISTS ix_pubfx ON pub_fixtures(run_id)", "CREATE INDEX IF NOT EXISTS ix_pubop ON pub_opportunities(run_id)"]:
                self.site.execute(sql)
            self.site.commit()
            self._save_state("__schema__", put, [])
            self.log(f"vetrina: schema aggiornato ({', '.join(put)})")

    # ---- epoch: a recreated or wiped site database gets everything again ----
    def epoch(self, full: bool = False) -> None:
        mine = self._state("__epoch__").get("epoch")
        try:
            row = self.site.execute("SELECT value FROM site_meta WHERE key = 'push_epoch'").fetchone()
        except Exception:  # noqa: BLE001 - no site_meta yet: an empty site database
            row = None
        theirs = row[0] if row else None
        if full or not mine or mine != theirs:
            why = "richiesto" if full else ("prima volta" if not mine else "database del sito diverso o svuotato")
            self.src.execute("DELETE FROM site_pushed WHERE tbl <> '__epoch__'")
            self.src.commit()
            new = uuid.uuid4().hex
            self.schema()
            self.site.execute("INSERT INTO site_meta(key, value) VALUES('push_epoch', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (new,))
            self.site.commit()
            self.written += 1
            self._save_state("__epoch__", {"epoch": new}, [])
            self.log(f"vetrina: invio completo ({why})")

    # ---- published analyses ----
    def runs(self) -> list[int]:
        return [r[0] for r in self.src.execute("SELECT id FROM pub_runs ORDER BY id DESC LIMIT ?", (KEEP_RUNS,))]

    def push_runs(self) -> None:
        keep = self.runs()
        sent = self._state("pub_run")
        new = [r for r in keep if str(r) not in sent]
        old = [k for k in sent if int(k) not in keep]
        cols_rows, rows_now = _select("SELECT * FROM pub_runs ORDER BY id DESC LIMIT ?", lambda _: [KEEP_RUN_ROWS])(self.src, keep)
        known = self._state("pub_runs")
        now = {_key(r, cols_rows, ["id"]): r for r in rows_now}
        up = [r for k, r in now.items() if known.get(k) != _hash(r)]
        gone = [k for k in known if k not in now]
        if not (new or old or up or gone):
            return
        for run in new:
            for t in RUN_TABLES:
                cur = self.src.execute(f"SELECT * FROM {t} WHERE run_id = ?", (run,))
                cols = [d[0] for d in cur.description]
                self.site.execute(f"DELETE FROM {t} WHERE run_id = ?", (run,))  # a half-sent run from an older state
                self._insert(t, cols, [tuple(r) for r in cur.fetchall()])
        if up:
            self._upsert("pub_runs", cols_rows, ["id"], up)
        if gone:
            self._delete("pub_runs", ["id"], gone)
        for run in old:
            for t in RUN_TABLES:
                self.site.execute(f"DELETE FROM {t} WHERE run_id = ?", (int(run),))
            self.written += 1
        self.site.commit()  # one transaction: the new analysis and its pub_runs row appear together
        self._save_state("pub_run", {str(r): "1" for r in new}, old)
        self._save_state("pub_runs", {k: _hash(r) for k, r in now.items() if k not in known or known[k] != _hash(r)}, gone)
        if new or old:
            self.log(f"vetrina: analisi inviate {new or '-'}, tolte {old or '-'}")

    # ---- every other table ----
    def push_table(self, t: Table, runs: list[int]) -> int:
        cols, rows = t.rows(self.src, runs)
        known = self._state(t.name)
        now = {_key(r, cols, t.key): r for r in rows}
        hashes = {k: _hash(r) for k, r in now.items()}
        up = [now[k] for k, h in hashes.items() if known.get(k) != h]
        gone = [k for k in known if k not in now]
        if not up and not gone:
            return 0
        before = self.written
        if gone:
            self._delete(t.name, t.key, gone)
        if up:
            self._upsert(t.name, cols, t.key, up)
        self.site.commit()
        self._save_state(t.name, {k: hashes[k] for k in hashes if known.get(k) != hashes[k]}, gone)
        return self.written - before

    def push(self, full: bool = False) -> dict[str, int]:
        t0 = time.monotonic()
        self.epoch(full)
        self.schema()
        self.push_runs()
        runs = self.runs()
        out = {}
        for t in TABLES:
            n = self.push_table(t, runs)
            if n:
                out[t.name] = n
        self.log(f"vetrina: {self.written} righe scritte ({', '.join(f'{k} {v}' for k, v in out.items()) or 'nessun cambiamento nelle tabelle'})"
                 f" in {time.monotonic() - t0:.0f}s")
        return out
