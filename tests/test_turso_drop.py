"""A dropped connection to Turso costs one step of one tick, not the run (and no failure e-mail)."""
import pytest

from algowinbet import autorun
from algowinbet.autorun import AutoConfig, run_tick, transient_db_error
from algowinbet.collector import CollectStats
from algowinbet.snapshots import HybridConnection, SnapshotStore

DROP = ValueError("Hrana: `http error: `connection closed before message completed``")


def test_only_network_drops_are_transient():
    assert transient_db_error(DROP)
    assert not transient_db_error(ValueError("SQLite error: no such table: quotes"))
    assert not transient_db_error(TypeError("'builtins.Cursor' object is not iterable"))


class _Goal:
    def __init__(self):
        self.calls = []

    def stale_leagues(self, kind):
        return []

    def sync_fixtures(self, days, leagues=None):
        self.calls.append("fixtures")
        raise DROP

    def sync_results(self, leagues=None):
        self.calls.append("results")
        st = CollectStats("results")
        st.add("results", 3)
        return st


def test_tick_goes_on_after_a_dropped_connection_and_reconnects(monkeypatch):
    store = SnapshotStore(":memory:")
    recovered = []
    monkeypatch.setattr(store, "recover", lambda: recovered.append(1))
    monkeypatch.setattr(autorun, "plan_tick", lambda *a, **k: ["fixtures", "results"])
    monkeypatch.setattr(autorun, "late_result_leagues", lambda *a, **k: [])
    goal = _Goal()
    out = run_tick(store, AutoConfig(), goal, None)
    assert goal.calls == ["fixtures", "results"] and recovered == [1]
    assert "Turso" in out[0].errors[0] and out[1].saved == {"results": 3}


def test_other_errors_still_stop_the_tick(monkeypatch):
    store = SnapshotStore(":memory:")
    monkeypatch.setattr(autorun, "plan_tick", lambda *a, **k: ["fixtures"])
    goal = _Goal()
    goal.sync_fixtures = lambda days, leagues=None: (_ for _ in ()).throw(KeyError("bug"))
    with pytest.raises(KeyError):
        run_tick(store, AutoConfig(), goal, None)


def test_hybrid_reset_opens_a_new_primary_connection():
    opened = []

    class Conn:
        def close(self):
            opened.append("closed")

    h = HybridConnection(replica=object(), open_remote=lambda: opened.append("open") or Conn())
    h.reset()
    assert opened == ["open", "closed", "open"] and h.dirty
    SnapshotStore(":memory:").recover()  # local file: nothing to reconnect


def test_replica_sync_drop_is_transient():
    assert transient_db_error(ValueError("sync error: http dispatch error: connection closed before message completed"))
    assert not transient_db_error(ValueError("sync error: invalid auth token"))


def test_a_drop_outside_the_steps_ends_the_tick_without_failing(monkeypatch, capsys):
    from algowinbet import cli

    def boom(a):
        raise DROP
    monkeypatch.setattr(cli, "_collect_auto", boom)
    cli.cmd_collect_auto(None)  # no exception: exit code 0, no failure e-mail
    assert "Turso" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_collect_auto", lambda a: (_ for _ in ()).throw(KeyError("bug")))
    with pytest.raises(KeyError):
        cli.cmd_collect_auto(None)
