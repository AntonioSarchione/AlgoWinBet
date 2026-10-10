import sqlite3
from datetime import timedelta

import pytest

from algowinbet import health, playerquotes, quality
from algowinbet.config import Config
from algowinbet.providers import MockProvider
from algowinbet.publish import analyze_and_publish
from algowinbet.sitepush import KEEP_RUNS, RUN_TABLES, TABLES, Pusher
from algowinbet.snapshots import SnapshotStore


def _archive(path):
    """A small archive with two published analyses, the paper registry and one Sisal price."""
    mock = MockProvider(seed=7)
    s = SnapshotStore(str(path))
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    for schema in (health.SCHEMA, quality.SCHEMA, playerquotes.SCHEMA):
        s.db.executescript(schema)
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72
    cfg.bet_bookmakers = ["BookA", "BookB", "BookC"]
    t = cfg.thresholds
    t.min_ev, t.fair_ev, t.max_uncertainty, t.min_dq_candidate = 0.0, -0.05, 0.5, 0.0
    cfg.optimizer.min_slip_ev = -0.5
    analyze_and_publish(s, cfg, now=mock.as_of)
    analyze_and_publish(s, cfg, now=mock.as_of)
    fid = s.db.execute("SELECT fixture_id FROM pub_fixtures LIMIT 1").fetchone()[0]
    s.db.execute("INSERT INTO quotes(fixture_id, market_code, selection, line_key, bookmaker, odds, observed_at, kind, source) "
                 "VALUES(?, 'MATCH_1X2', 'HOME', '', 'sisal', 2.1, '2026-10-10T10:00:00+00:00', 'snapshot', 'test')", (fid,))
    s.db.execute("INSERT INTO raw_requests(source, endpoint, fetched_at) VALUES('test', 'x', '2026-10-10T10:00:00+00:00')")
    s.db.commit()
    s.close()
    return fid


def _push(arch, site, **kw):
    src, dst = sqlite3.connect(arch), sqlite3.connect(site)
    p = Pusher(src, dst, log=lambda m: None)
    p.push(**kw)
    src.close()
    dst.close()
    return p.written


def _same(arch, site) -> list[str]:
    """Tables whose site rows differ from the archive's rows in scope."""
    a, s = sqlite3.connect(arch), sqlite3.connect(site)
    runs = [r[0] for r in a.execute("SELECT id FROM pub_runs ORDER BY id DESC LIMIT ?", (KEEP_RUNS,))]
    bad = []
    for t in TABLES:
        cols, rows = t.rows(a, runs)
        extra = " WHERE key <> 'push_epoch'" if t.name == "site_meta" else ""
        got = s.execute(f"SELECT {','.join(chr(34) + c + chr(34) for c in cols)} FROM {t.name}{extra}").fetchall()
        if sorted(map(repr, rows)) != sorted(map(repr, got)):
            bad.append(t.name)
    marks = ",".join("?" * len(runs))
    for t in RUN_TABLES:
        if sorted(map(repr, a.execute(f"SELECT * FROM {t} WHERE run_id IN ({marks})", runs))) != sorted(map(repr, s.execute(f"SELECT * FROM {t}"))):
            bad.append(t)
    a.close()
    s.close()
    return bad


@pytest.fixture
def dbs(tmp_path):
    arch, site = tmp_path / "archive.db", tmp_path / "site.db"
    _archive(arch)
    return str(arch), str(site)


def test_first_push_copies_the_site_scope_and_a_second_one_writes_nothing(dbs):
    arch, site = dbs
    assert _push(arch, site) > 0
    assert _same(arch, site) == []
    assert _push(arch, site) == 0
    s = sqlite3.connect(site)
    assert s.execute("SELECT value FROM site_meta WHERE key = 'last_tick'").fetchone()[0] == "2026-10-10T10:00:00+00:00"
    assert s.execute("SELECT n FROM pub_quote_paths").fetchall() == [(1,)]
    assert s.execute("SELECT name FROM sqlite_master WHERE name = 'quotes'").fetchone() is None  # the price history never travels


def test_only_changed_rows_are_written(dbs):
    arch, site = dbs
    _push(arch, site)
    a = sqlite3.connect(arch)
    a.execute("UPDATE results SET home_goals = home_goals + 1 WHERE fixture_id = (SELECT MAX(fixture_id) FROM results)")
    a.execute("DELETE FROM results WHERE fixture_id = (SELECT MIN(fixture_id) FROM results)")
    a.commit()
    a.close()
    assert _push(arch, site) == 2
    assert _same(arch, site) == []


def test_a_new_analysis_replaces_the_oldest_in_one_go(dbs):
    arch, site = dbs
    _push(arch, site)
    a = sqlite3.connect(arch)
    last = a.execute("SELECT MAX(id) FROM pub_runs").fetchone()[0]
    cols = [r[1] for r in a.execute("PRAGMA table_info(pub_runs)")]
    a.execute(f"INSERT INTO pub_runs SELECT {last + 1},{','.join(c for c in cols if c != 'id')} FROM pub_runs WHERE id = ?", (last,))
    for t in RUN_TABLES:
        cs = [r[1] for r in a.execute(f"PRAGMA table_info({t})")]
        a.execute(f"INSERT INTO {t} SELECT {','.join(str(last + 1) if c == 'run_id' else chr(34) + c + chr(34) for c in cs)} FROM {t} WHERE run_id = ?", (last,))
    a.commit()
    a.close()
    _push(arch, site)
    assert _same(arch, site) == []
    s = sqlite3.connect(site)
    assert sorted({r[0] for r in s.execute("SELECT run_id FROM pub_fixtures")}) == [last, last + 1]


def test_a_failed_push_leaves_the_site_as_it_was_and_the_next_one_completes(dbs, monkeypatch):
    arch, site = dbs
    _push(arch, site)
    a = sqlite3.connect(arch)
    a.execute("UPDATE pub_runs SET notes = 'cambiata' WHERE id = (SELECT MAX(id) FROM pub_runs)")
    a.execute("UPDATE paper_legs SET result = 'won' WHERE id = (SELECT MIN(id) FROM paper_legs)")
    a.commit()
    a.close()
    real = Pusher._upsert

    def boom(self, table, *args):
        if table == "paper_legs":
            raise RuntimeError("rete caduta")
        return real(self, table, *args)

    monkeypatch.setattr(Pusher, "_upsert", boom)
    with pytest.raises(RuntimeError):
        _push(arch, site)
    s = sqlite3.connect(site)
    assert s.execute("SELECT COUNT(*) FROM paper_legs WHERE result = 'won'").fetchone()[0] == 0
    s.close()
    monkeypatch.setattr(Pusher, "_upsert", real)
    assert _push(arch, site) == 1  # pub_runs went through before the failure; the registry row is sent now
    assert _same(arch, site) == []


def test_a_wiped_site_database_gets_everything_again(dbs, tmp_path):
    arch, site = dbs
    _push(arch, site)
    other = str(tmp_path / "new-site.db")
    assert _push(arch, other) > 0
    assert _same(arch, other) == []


def test_a_new_archive_column_reaches_the_site(dbs):
    arch, site = dbs
    _push(arch, site)
    a = sqlite3.connect(arch)
    a.execute("ALTER TABLE results ADD COLUMN extra TEXT")
    a.execute("UPDATE results SET extra = 'x'")
    a.commit()
    a.close()
    _push(arch, site)
    assert _same(arch, site) == []
