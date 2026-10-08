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
    rep = run_quality(SnapshotProvider(st), Config(), T0 + timedelta(days=40), T0 + timedelta(days=80), min_n=40)
    m = rep["groups"]["campionati"]["1X2"]
    assert m["n"] > 150 and m["n_ens"] == m["n"] and 0.8 < m["ll_model"] < 1.3
    # walk-forward meta-model: later weeks only (fitted on earlier ones), scored on the same matches as the rest
    assert 0 < m["n_meta"] < m["n"] and m["n_same"] == m["n_meta"] and "ll_meta_same" in m
    assert "campionati|1X2|pool" in rep["meta"] and "campionati|1X2|pool" in rep["_params"]
    assert {"model", "ens", "close"} <= set(rep["calibration"]["1X2"])
    v = rep["value"]["1X2"]
    assert v["n"] > 0 and v["books"]["market-avg"] == v["n"]  # bets placed at the average price (no Sisal stored)
    assert rep["monthly"] and rep["monthly"][0]["n"] > 0
    rid = save_quality(st, rep, T0, T0 + timedelta(days=80), "test")
    saved = json.loads(st.db.execute("SELECT report FROM quality_runs WHERE id=?", (rid,)).fetchone()[0])
    assert saved["n_matches"] == rep["n_matches"] and "_params" not in saved


def test_reference_price_far_from_the_market_average_is_skipped():
    fam = [("MATCH_1X2", s, None) for s in ("HOME", "DRAW", "AWAY")]
    qs = [("MATCH_1X2", s, None, "betfair-ex", o, T0, "current") for s, o in zip(("HOME", "DRAW", "AWAY"), (9.6, 5.9, 1.23))]
    qs += [("MATCH_1X2", s, None, "market-avg", o, T0, "current") for s, o in zip(("HOME", "DRAW", "AWAY"), (1.5, 4.2, 6.5))]
    avg = _price(qs, fam, ("market-avg",), T0, closing=False, fair=True)[0]
    got = _price(qs, fam, ("betfair-ex", "market-avg"), T0, closing=False, fair=True, anchor=avg)
    assert got[1] == "market-avg"


def test_price_matches_the_brand_whatever_the_country_suffix():
    fam = [("MATCH_1X2", s, None) for s in ("HOME", "DRAW", "AWAY")]
    qs = [("MATCH_1X2", s, None, "sisal.it", o, T0, "current") for s, o in zip(("HOME", "DRAW", "AWAY"), (2.0, 3.3, 3.8))]
    got = _price(qs, fam, ("sisal", "market-avg"), T0, closing=False, fair=False)
    assert got == ([2.0, 3.3, 3.8], "sisal")


def test_edge_shrink_is_low_when_picked_edges_do_not_win_more_than_the_market():
    from algowinbet.quality import edge_shrink
    assert edge_shrink([])["lambda"] == 1.0
    fake = [{"p": 0.6, "pm": 0.5, "won": i % 2 == 0} for i in range(400)]  # won 50% = market
    lo = edge_shrink(fake)
    assert lo["n"] == 400 and lo["lambda"] < 0.1
    real = [{"p": 0.6, "pm": 0.5, "won": i % 10 < 6} for i in range(400)]  # won 60% = model
    assert edge_shrink(real)["lambda"] > 0.9
    few = edge_shrink(fake[:4])
    assert 0.3 < few["lambda"] < 0.5  # few picks: pulled toward one half
    assert lo["ci"][0] <= lo["lambda"] <= lo["ci"][1] and lo["raw_ci"][0] < lo["raw"] < lo["raw_ci"][1]
    assert edge_shrink(fake)["ci"] == lo["ci"]  # fixed seed: same picks, same interval
    assert few["ci"][1] - few["ci"][0] > lo["ci"][1] - lo["ci"][0]  # fewer picks, wider interval
