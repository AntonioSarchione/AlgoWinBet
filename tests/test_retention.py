"""Quotes retention: finished matches keep only the price points anything reads again, and the price in force at each of
those moments is the same before and after pruning."""
from datetime import timedelta

from algowinbet.domain import Fixture, OddsQuote, utc
from algowinbet.retention import CHECKPOINTS, JOB, prune_quotes
from algowinbet.snapshots import SnapshotStore

NOW = utc(2026, 10, 20, 6)
OLD_KO = utc(2026, 10, 10, 18, 45)  # over for more than 7 days
NEW_KO = utc(2026, 10, 17, 18, 45)  # not yet


def _store():
    store = SnapshotStore(":memory:")
    store.save_fixtures("goal-api", [Fixture(id="goal:old", competition="Serie A", home="A", away="B", kickoff=OLD_KO),
                                     Fixture(id="goal:new", competition="Serie A", home="C", away="D", kickoff=NEW_KO)],
                        utc(2026, 10, 1))
    for fid, ko in (("goal:old", OLD_KO), ("goal:new", NEW_KO)):
        qs = []
        # a full snapshot every 30 minutes for 4 days, price drifting every few ticks, both books
        for k in range(4 * 48):
            at = ko - timedelta(hours=96) + timedelta(minutes=30 * k)
            for book, base in (("sisal.it", 2.0), ("pinnacle", 2.1)):
                qs.append(OddsQuote(fixture_id=fid, market_code="MATCH_1X2", selection="HOME", bookmaker=book, odds=round(base + (k // 7) * 0.01, 2),
                                    observed_at=at, kind="current"))
        qs.append(OddsQuote(fixture_id=fid, market_code="MATCH_1X2", selection="HOME", bookmaker="pinnacle", odds=1.95, observed_at=ko, kind="close"))
        store.save_quotes("oddspapi", qs)
    return store


def _in_force(store, fid, book, t):
    row = store.db.execute("SELECT odds FROM quotes WHERE fixture_id=? AND bookmaker=? AND kind != 'close' AND observed_at <= ? "
                           "ORDER BY observed_at DESC LIMIT 1", (fid, book, t.isoformat())).fetchone()
    return row and row[0]


def test_prune_keeps_every_price_that_is_read_again_and_nothing_else():
    store = _store()
    moments = [OLD_KO - cp for cp in CHECKPOINTS] + [OLD_KO - timedelta(seconds=1)]
    before = {(b, t): _in_force(store, "goal:old", b, t) for b in ("sisal.it", "pinnacle") for t in moments}
    n_old = store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id='goal:old'").fetchone()[0]
    n_new = store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id='goal:new'").fetchone()[0]

    dry = prune_quotes(store, NOW, dry_run=True)
    assert dry["matches"] == 1 and dry["deleted"] > 0
    assert store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id='goal:old'").fetchone()[0] == n_old  # dry run: nothing deleted

    out = prune_quotes(store, NOW)
    left = store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id='goal:old'").fetchone()[0]
    assert out["deleted"] == dry["deleted"] and left == n_old - out["deleted"] and left <= 2 * (len(CHECKPOINTS) + 2) + 1
    assert {(b, t): _in_force(store, "goal:old", b, t) for b in ("sisal.it", "pinnacle") for t in moments} == before
    assert store.db.execute("SELECT odds FROM quotes WHERE fixture_id='goal:old' AND kind='close'").fetchall() == [(1.95,)]
    first = store.db.execute("SELECT MIN(observed_at) FROM quotes WHERE fixture_id='goal:old'").fetchone()[0]
    assert first == (OLD_KO - timedelta(hours=96)).isoformat()  # the opening price stays
    # a match over for less than 7 days is untouched; a second run has nothing left to do
    assert store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id='goal:new'").fetchone()[0] == n_new
    assert store.db.execute("SELECT detail FROM jobs WHERE name=?", (JOB,)).fetchone()[0].endswith("|goal:old")
    assert prune_quotes(store, NOW)["matches"] == 0


def test_time_limit_stops_between_matches():
    store = _store()
    ticks = iter([0, 0, 1000])  # start, first match within the limit, then over it
    assert prune_quotes(store, NOW + timedelta(days=30), max_seconds=10, clock=lambda: next(ticks))["matches"] == 1
