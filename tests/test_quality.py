import json
from datetime import timedelta

from algowinbet.config import Config
from algowinbet.domain import OddsQuote
from algowinbet.quality import _price, run_quality, save_quality
from algowinbet.snapshots import SnapshotProvider, SnapshotStore
from test_elo_league import T0, _league


def _store():
    st = SnapshotStore(":memory:")
    res = _league("Liga", 10, 1.6, 1.0, 80, 4, start=T0)
    st.save_results("goal", res, T0)
    qs = []
    for r in res:
        for kind, at, odds in (("current", r.kickoff - timedelta(hours=20), (2.0, 3.4, 3.9)), ("close", r.kickoff - timedelta(minutes=5), (1.9, 3.5, 4.2))):
            qs += [OddsQuote(fixture_id=r.fixture_id, market_code="MATCH_1X2", selection=s, bookmaker="pinnacle", odds=o, observed_at=at, kind=kind)
                   for s, o in zip(("HOME", "DRAW", "AWAY"), odds)]
        qs += [OddsQuote(fixture_id=r.fixture_id, market_code="MATCH_1X2", selection=s, bookmaker="market-avg", odds=o,
                         observed_at=r.kickoff - timedelta(hours=20)) for s, o in zip(("HOME", "DRAW", "AWAY"), (2.3, 3.3, 3.6))]
    st.save_quotes("test", qs)
    return st, res


def test_price_reads_the_family_at_one_book_by_time():
    qs = [("MATCH_1X2", s, None, "pinnacle", o, T0, "current") for s, o in zip(("HOME", "DRAW", "AWAY"), (2.0, 3.4, 3.9))]
    fam = [("MATCH_1X2", s, None) for s in ("HOME", "DRAW", "AWAY")]
    p, book = _price(qs, fam, ("pinnacle",), T0, closing=False, fair=True)
    assert book == "pinnacle" and abs(sum(p) - 1) < 1e-9 and p[0] > p[2]
    assert _price(qs, fam, ("pinnacle",), T0 - timedelta(seconds=1), closing=False, fair=True) is None  # not yet observable
    assert _price(qs, fam, ("pinnacle",), None, closing=True, fair=True) is None


def test_quality_replay_scores_model_ensemble_and_closing_and_is_saved():
    st, res = _store()
    rep = run_quality(SnapshotProvider(st), Config(), T0 + timedelta(days=40), T0 + timedelta(days=80))
    m = rep["groups"]["campionati"]["1X2"]
    assert m["n"] > 150 and m["n_same"] == m["n"] and 0.8 < m["ll_model"] < 1.3
    assert {"model", "ens", "close"} <= set(rep["calibration"]["1X2"])
    v = rep["value"]["1X2"]
    assert v["n"] > 0 and v["books"]["market-avg"] == v["n"]  # bets placed at the average price (no Sisal stored)
    assert rep["monthly"] and rep["monthly"][0]["n"] > 0
    rid = save_quality(st, rep, T0, T0 + timedelta(days=80), "test")
    saved = json.loads(st.db.execute("SELECT report FROM quality_runs WHERE id=?", (rid,)).fetchone()[0])
    assert saved["n_matches"] == rep["n_matches"]
