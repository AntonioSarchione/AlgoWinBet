from datetime import datetime, timedelta, timezone

from algowinbet import actionsminutes as am
from algowinbet.snapshots import SnapshotStore

UTC = timezone.utc


def test_free_seconds_fill_the_minute_already_billed(monkeypatch):
    monkeypatch.setenv("JOB_T0", "1000")
    # 3 s setup + 77 s of steps: with the 10 s tail the job is in its 2nd minute; 120 - 10 - 6 - 80 = 24 s free
    assert am.billed_minutes(80) == 2
    assert am.free_seconds(clock=lambda: 1077) == 24
    assert am.free_seconds(clock=lambda: 1101) == 0  # 104 s: no room left before the 2-minute boundary
    monkeypatch.delenv("JOB_T0")
    assert am.free_seconds() == 0  # outside Actions


def test_record_run_and_levels(monkeypatch):
    st = SnapshotStore(":memory:")
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)  # half of October gone
    monkeypatch.setenv("JOB_T0", "1000")
    assert am.record_run(st, now, clock=lambda: 1090) == 2
    assert st.usage(am.SOURCE, "M2026-10") == 2
    assert am.level(700, now) == 0  # on pace for ~1,400
    assert am.level(900, now) == 1  # on pace for ~1,800: economy
    assert am.level(1900, now) == 2
    assert am.level(300, datetime(2026, 10, 1, 1, tzinfo=UTC)) == 1  # a heavy first day already projects high


def test_freshness_due_orders_the_most_overdue_first():
    from algowinbet.autorun import freshness_due
    from algowinbet.domain import Fixture
    st = SnapshotStore(":memory:")
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    fx = [Fixture(id="soon", competition="L", home="A", away="B", kickoff=now + timedelta(hours=4)),
          Fixture(id="tomorrow", competition="L", home="C", away="D", kickoff=now + timedelta(hours=30)),
          Fixture(id="fresh", competition="L", home="E", away="F", kickoff=now + timedelta(hours=30)),
          Fixture(id="far", competition="L", home="G", away="H", kickoff=now + timedelta(days=5))]
    st.save_fixtures("goal", fx, now - timedelta(days=1))
    from algowinbet.autorun import LINKS_SCHEMA
    st.db.executescript(LINKS_SCHEMA)
    for f in fx:
        st.db.execute("INSERT INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES('oddspapi', ?, ?, ?)", (f"x-{f.id}", f.id, now.isoformat()))
    for f, hours_ago in (("soon", 2), ("tomorrow", 10), ("fresh", 2), ("far", 12)):
        st.put_raw("oddspapi", "/historical-odds", {"fixtureId": f"x-{f}"}, 200, b"{}", now - timedelta(hours=hours_ago))
    # soon: 2h old vs 1h target (2x); tomorrow: 10h vs 8h (1.25x); fresh: 2h vs 8h (not due); far: 12h vs 24h (not due)
    assert freshness_due(st, now) == ["soon", "tomorrow"]
