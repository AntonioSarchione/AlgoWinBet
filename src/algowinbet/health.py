"""Daily health check, database size and compact backup.

  - run_health ...... the morning run checks that every part of the chain still works (collection, prices, analysis,
                      settlement, closing prices, quality, minutes, database size). An "error" fails the run at its very
                      end (after all the work is done), so GitHub sends one e-mail; "warn" is shown on the Sistema page only.
  - table_sizes ..... bytes per table of the local replica (dbstat), or row counts when dbstat is missing
  - backup .......... compact copy of the database (VACUUM INTO + gzip), without the tables given in `skip`
Every result is saved in table `health` (last KEEP rows) for the dashboard.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY, at TEXT, ok INTEGER, report TEXT);"
KEEP = 60
TURSO_FREE_BYTES = 5_000_000_000  # Turso free plan storage (5 GB)
FULL_SOON_DAYS = 90  # warn when the growth of the last week fills the plan within this many days
DB_WARN, DB_ERROR = 0.7, 0.9
STALE = timedelta(hours=30)  # a daily job missed once
SOURCES = {"goal-api": "GOAL API", "oddspapi": "OddsPapi", "api-football": "API-Football"}


@dataclass
class Check:
    key: str
    label: str
    level: str  # ok | warn | error
    detail: str


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age(now: datetime, t: datetime | None) -> str:
    if t is None:
        return "mai"
    h = (now - t).total_seconds() / 3600
    return f"{h:.0f} ore fa" if h < 48 else f"{h / 24:.0f} giorni fa"


def _one(db, sql: str, args=()):
    try:
        row = db.execute(sql, args).fetchone()
    except Exception as e:  # noqa: BLE001 - a table not created yet reads as empty
        if "no such table" in str(e).lower() or "no such column" in str(e).lower():
            return None
        raise
    return row[0] if row else None


def replica_path() -> Path | None:
    if os.environ.get("TURSO_MODE") == "replica":
        p = Path(os.environ.get("TURSO_REPLICA_PATH", "data/turso-replica.db"))
        return p if p.exists() else None
    return None


def table_sizes(path: Path) -> dict[str, int]:
    """Bytes per table (indexes included in their table) from SQLite's dbstat; {} when it is not available."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        owner = dict(con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')").fetchall())
        out: dict[str, int] = {}
        for name, size in con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
            t = owner.get(name, name)  # an index counts in its table; sqlite_* internals under their own name
            out[t] = out.get(t, 0) + int(size)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def database_size(path: Path | None) -> int | None:
    if path is None:
        return None
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file() and not p.name.endswith("-info"))


def run_health(store, now: datetime | None = None, model_version: str | None = None) -> tuple[list[Check], dict]:
    now = now or datetime.now(timezone.utc)
    db = store.db
    checks: list[Check] = []

    # 1. collection per source
    for src, name in SOURCES.items():
        t = _ts(_one(db, "SELECT MAX(fetched_at) FROM raw_requests WHERE source = ?", (src,)))
        if t is None and src != "goal-api":
            continue  # source not configured
        stale = t is None or now - t > STALE
        checks.append(Check(f"src-{src}", f"Raccolta {name}", ("error" if src == "goal-api" else "warn") if stale else "ok",
                            f"ultima richiesta {_age(now, t)}"))

    blocked = _ts(_one(db, "SELECT done_at FROM jobs WHERE name = 'api-football-blocked'"))
    if blocked and now - blocked < timedelta(hours=48):
        checks.append(Check("src-api-football-blocked", "Account API-Football", "warn",
                            f"sospeso (ultima risposta {_age(now, blocked)}): richieste in pausa, una prova al giorno"))

    # team names (team-map, saved by the morning run): a spelling of another source with no alias, or a club under two names
    row = db.execute("SELECT done_at, detail FROM jobs WHERE name = 'team-map'").fetchone()
    if row:
        try:
            sm = json.loads(row[1] or "{}")
        except ValueError:
            sm = {}
        bad = [f"{n} {lbl}" for k, lbl in (("aliases", "alias mancanti"), ("conflicts", "conflitti"), ("split", "storici divisi"),
                                          ("national", "nazionali senza Elo")) if (n := sm.get(k))]
        old = now - (_ts(row[0]) or now) > timedelta(days=3)
        checks.append(Check("names", "Nomi delle squadre", "warn" if bad or old else "ok",
                            (", ".join(bad) + " (team-map): " + "; ".join(sm.get("examples", [])[:3]) if bad else
                             "ogni nome delle altre fonti ha il suo alias") + (f" · controllo di {_age(now, _ts(row[0]))}" if old else "")))

    # 2. published analysis
    t = _ts(_one(db, "SELECT MAX(created_at) FROM pub_runs"))
    lvl = "error" if t is None or now - t > timedelta(hours=72) else "warn" if now - t > STALE else "ok"
    checks.append(Check("analysis", "Analisi pubblicata", lvl, f"ultima {_age(now, t)}"))

    # 3. Sisal prices for the linked matches of the next 3 days
    soon = _one(db, "SELECT COUNT(DISTINCT l.fixture_id) FROM fixture_links l JOIN fixtures f ON f.fixture_id = l.fixture_id "
                    "WHERE l.source = 'oddspapi' AND f.kickoff BETWEEN ? AND ?", (now.isoformat(), (now + timedelta(days=3)).isoformat())) or 0
    t = _ts(_one(db, "SELECT MAX(observed_at) FROM quotes WHERE lower(bookmaker) LIKE '%sisal%'"))
    late = soon and (t is None or now - t > STALE)
    checks.append(Check("sisal", "Prezzi Sisal", "warn" if late else "ok",
                        f"ultimo prezzo {_age(now, t)} · {soon} partite collegate nei prossimi 3 giorni"))

    # 4. settlement: nothing open long after the give-up time (7 days)
    stuck = _one(db, "SELECT COUNT(*) FROM paper_legs WHERE result IS NULL AND kickoff < ?", ((now - timedelta(days=8)).isoformat(),)) or 0
    late = _one(db, "SELECT COUNT(*) FROM paper_legs WHERE result IS NULL AND kickoff < ?", ((now - timedelta(days=2)).isoformat(),)) or 0
    checks.append(Check("settle", "Chiusura del registro", "error" if stuck else "warn" if late > 50 else "ok",
                        f"{stuck} selezioni aperte da più di 8 giorni · {late} da più di 2 (corner e cartellini attendono fino a 6)"))

    # 5. closing prices of the last week's settlements
    row = db.execute("SELECT COUNT(*), SUM(close_fair IS NOT NULL), SUM(close_odds IS NOT NULL) FROM paper_legs "
                     "WHERE result IN ('won','lost','void') AND settled_at >= ?", ((now - timedelta(days=7)).isoformat(),)).fetchone() \
        if _one(db, "SELECT COUNT(*) FROM paper_legs") is not None else (0, 0, 0)
    n, pin, sis = (row[0] or 0), (row[1] or 0), (row[2] or 0)
    share = pin / n if n else 1.0
    checks.append(Check("close", "Prezzi di chiusura", "warn" if n >= 20 and share < 0.85 else "ok",
                        f"ultimi 7 giorni: Pinnacle {pin}/{n}, Sisal {sis}/{n} (il criterio chiede ≥ 85% Pinnacle)"))

    # 6. weekly quality and meta-model of the current version
    t = _ts(_one(db, "SELECT MAX(created_at) FROM quality_runs"))
    meta = _one(db, "SELECT COUNT(*) FROM meta_models WHERE model_version = ?", (model_version,)) if model_version else None
    lvl = "warn" if t is None or now - t > timedelta(days=8) or (model_version and not meta) else "ok"
    checks.append(Check("quality", "Qualità settimanale", lvl,
                        f"ultima {_age(now, t)} · meta-modello per {model_version or '–'}: {'sì' if meta else 'no'}"))

    # 7. GitHub Actions minutes: counted for information (public repository, no limit)
    from . import actionsminutes as am
    checks.append(Check("minutes", "Minuti GitHub", "ok", f"{am.month_used(store, now)} nel mese (repository pubblico: nessun limite)"))

    # 8. database size (Turso free plan)
    path = replica_path()
    size = database_size(path)
    sizes = table_sizes(path) if path else {}
    growth = None
    if size is not None:
        frac = size / TURSO_FREE_BYTES
        top = ", ".join(f"{k} {v / 1e6:.0f} MB" for k, v in list(sizes.items())[:3])
        # weekly growth from the price rows (most of the database) x bytes per row of the table: the net change since a health
        # report of 6+ days ago (finished matches get pruned, see retention.py), else the rows observed in the last 7 days
        rows = _one(db, "SELECT COUNT(*) FROM quotes") or 0
        then = _rows_then(db, now)
        if then:
            week = (rows - then[1]) * 7 / max(then[0], 1e-9)
        else:
            week = _one(db, "SELECT COUNT(*) FROM quotes WHERE observed_at >= ?", ((now - timedelta(days=7)).isoformat(),)) or 0
        if rows and sizes.get("quotes"):
            growth = max(0.0, week * sizes["quotes"] / rows)
        days_left = (TURSO_FREE_BYTES - size) / (growth / 7) if growth else None
        lvl = "error" if frac >= DB_ERROR else "warn" if frac >= DB_WARN or (days_left is not None and days_left < FULL_SOON_DAYS) else "ok"
        checks.append(Check("db", "Spazio del database", lvl,
                            f"{size / 1e6:.0f} MB su {TURSO_FREE_BYTES / 1e9:.0f} GB ({frac:.0%})"
                            + (f" · prezzi +{growth / 1e6:.0f} MB a settimana, piano pieno tra circa {days_left / 30:.0f} mesi" if days_left else "")
                            + (f" · più grandi: {top}" if top else "")))
    extra = {"db_bytes": size, "tables": sizes, "week_growth_bytes": growth,
             "quote_rows": rows if size is not None else None}  # next week's net growth
    return checks, extra


def _rows_then(db, now: datetime) -> tuple[float, int] | None:
    """(days ago, quote rows) of the newest saved health report at least 6 days old that counted the rows."""
    try:
        rows = db.execute("SELECT at, report FROM health WHERE at <= ? ORDER BY id DESC LIMIT 5",
                          ((now - timedelta(days=6)).isoformat(),)).fetchall()
    except Exception:  # noqa: BLE001 - no health table yet
        return None
    for at, rep in rows:
        n = json.loads(rep).get("quote_rows")
        if n is not None:
            return (now - datetime.fromisoformat(at)).total_seconds() / 86400, int(n)
    return None


def save_health(store, checks: list[Check], extra: dict, now: datetime) -> None:
    store.db.executescript(SCHEMA)
    ok = all(c.level != "error" for c in checks)
    store.db.execute("INSERT INTO health(at, ok, report) VALUES(?,?,?)",
                     (now.isoformat(), int(ok), json.dumps({"checks": [asdict(c) for c in checks], **extra}, ensure_ascii=False)))
    store.db.execute("DELETE FROM health WHERE id NOT IN (SELECT id FROM health ORDER BY id DESC LIMIT ?)", (KEEP,))
    store.db.commit()


def print_health(checks: list[Check]) -> None:
    mark = {"ok": "ok   ", "warn": "ATTN ", "error": "ERR  "}
    print("Controllo di salute:")
    for c in checks:
        print(f"  {mark[c.level]} {c.label}: {c.detail}")


def backup(src: Path, out: Path, skip: tuple[str, ...] = ()) -> dict:
    """Consistent compact copy of `src` (VACUUM INTO), without the tables in `skip`, gzipped to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "backup.db"
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        con.execute("VACUUM INTO ?", (str(tmp),))
        con.close()
        con = sqlite3.connect(tmp)
        have = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in skip:
            if t in have:
                con.execute(f'DROP TABLE "{t}"')
        con.commit()
        con.execute("VACUUM")
        rows = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(have - set(skip)) if not t.startswith("sqlite_")}
        con.close()
        raw = tmp.stat().st_size
        with open(tmp, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
    return {"raw_bytes": raw, "gz_bytes": out.stat().st_size, "rows": rows, "skipped": [t for t in skip]}
