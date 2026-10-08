import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

from algowinbet import health
from algowinbet.snapshots import SnapshotStore

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 6, 10, tzinfo=UTC)


def _store():
    st = SnapshotStore(":memory:")
    st.db.executescript("CREATE TABLE IF NOT EXISTS pub_runs(id INTEGER PRIMARY KEY, created_at TEXT);")
    return st


def _levels(checks):
    return {c.key: c.level for c in checks}


def test_fresh_chain_is_ok_and_stale_goal_or_analysis_is_an_error():
    st = _store()
    st.db.execute("INSERT INTO raw_requests(source, fetched_at) VALUES('goal-api', ?)", ((NOW - timedelta(hours=1)).isoformat(),))
    st.db.execute("INSERT INTO pub_runs(created_at) VALUES(?)", ((NOW - timedelta(hours=2)).isoformat(),))
    lv = _levels(health.run_health(st, NOW)[0])
    assert lv["src-goal-api"] == "ok" and lv["analysis"] == "ok" and "src-oddspapi" not in lv  # not configured: no check

    st.db.execute("INSERT INTO raw_requests(source, fetched_at) VALUES('oddspapi', ?)", ((NOW - timedelta(days=2)).isoformat(),))
    st.db.execute("UPDATE raw_requests SET fetched_at=? WHERE source='goal-api'", ((NOW - timedelta(days=2)).isoformat(),))
    st.db.execute("UPDATE pub_runs SET created_at=?", ((NOW - timedelta(days=4)).isoformat(),))
    lv = _levels(health.run_health(st, NOW)[0])
    assert lv["src-goal-api"] == "error" and lv["src-oddspapi"] == "warn" and lv["analysis"] == "error"


def test_save_keeps_the_last_reports():
    st = _store()
    checks, extra = health.run_health(st, NOW)
    for _ in range(health.KEEP + 3):
        health.save_health(st, checks, extra, NOW)
    assert st.db.execute("SELECT COUNT(*) FROM health").fetchone()[0] == health.KEEP


def test_backup_is_a_readable_copy_without_the_skipped_tables(tmp_path):
    src = tmp_path / "db.sqlite"
    con = sqlite3.connect(src)
    con.executescript("CREATE TABLE quotes(id INTEGER PRIMARY KEY, odds REAL); CREATE TABLE blobs(hash TEXT, body BLOB);"
                      "INSERT INTO quotes(odds) VALUES (1.5), (2.1); INSERT INTO blobs VALUES ('h', zeroblob(100000));")
    con.commit()
    con.close()
    out = tmp_path / "b" / "backup.db.gz"
    r = health.backup(src, out, skip=("blobs",))
    assert r["rows"] == {"quotes": 2} and r["gz_bytes"] < r["raw_bytes"]
    plain = tmp_path / "restored.db"
    plain.write_bytes(gzip.decompress(out.read_bytes()))
    con = sqlite3.connect(plain)
    assert con.execute("SELECT SUM(odds) FROM quotes").fetchone()[0] == 3.6
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='blobs'").fetchone()[0] == 0
    sizes = health.table_sizes(src)
    assert sizes == {} or max(sizes, key=sizes.get) == "blobs"


def test_net_growth_reads_the_quote_rows_of_a_report_six_days_old():
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from algowinbet.health import SCHEMA, _rows_then
    from algowinbet.snapshots import SnapshotStore as _S
    store = _S(":memory:")
    store.db.executescript(SCHEMA)
    now = _dt(2026, 10, 20, 6, tzinfo=_tz.utc)
    for days, n in ((8, 1000), (7, 1200), (2, 5000)):
        store.db.execute("INSERT INTO health(at, ok, report) VALUES(?,?,?)", ((now - _td(days=days)).isoformat(), 1, _json.dumps({"quote_rows": n})))
    days, rows = _rows_then(store.db, now)
    assert round(days) == 7 and rows == 1200  # the newest report at least 6 days old
    assert _rows_then(store.db, now - _td(days=5)) is None  # no report old enough yet: fall back to the rows of the last week


def test_suspended_api_football_account_is_a_warning():
    st = _store()
    st.db.execute("INSERT INTO raw_requests(source, fetched_at) VALUES('goal-api', ?)", ((NOW - timedelta(hours=1)).isoformat(),))
    st.db.execute("INSERT INTO pub_runs(created_at) VALUES(?)", ((NOW - timedelta(hours=2)).isoformat(),))
    assert "src-api-football-blocked" not in _levels(health.run_health(st, NOW)[0])
    st.mark_job("api-football-blocked", NOW - timedelta(hours=3), "suspended")
    assert _levels(health.run_health(st, NOW)[0])["src-api-football-blocked"] == "warn"
