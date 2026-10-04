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
