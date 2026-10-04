from algowinbet.registrycal import group_stats, report


def _row(f, p, y, pin, market="TOTAL_GOALS", status="FAIR", day="2026-10-03"):
    return {"fixture_id": f, "market": market, "status": status, "kickoff": day + "T15:00:00+00:00", "p": p, "y": y, "pin": pin, "sis": pin}


def test_luck_shows_in_model_and_market_alike():
    # every selection won while model and Pinnacle both said 50%: the gap is the matches', not the model's
    rows = [_row(f"f{i // 3}", 0.5, 1, 0.5) for i in range(30)]
    g = group_stats(rows)
    assert g["matches"] == 10
    assert abs(g["gap_model"][0] - 0.5) < 1e-9 and abs(g["gap_pin"][0] - 0.5) < 1e-9
    assert g["model_vs_pin"][0] == 0


def test_model_off_against_the_market_and_grouping():
    rows = [_row(f"f{i}", 0.4, i % 2, 0.5, market="MATCH_1X2" if i < 10 else "BTTS") for i in range(20)]
    rep = report(rows)
    assert abs(rep["tutte"]["tutte"]["model_vs_pin"][0] + 0.1) < 1e-9
    assert set(rep["mercato"]) == {"MATCH_1X2", "BTTS"} and rep["mercato"]["MATCH_1X2"]["n"] == 10
