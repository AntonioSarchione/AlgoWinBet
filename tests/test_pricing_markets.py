import itertools

import numpy as np
import pytest

from algowinbet import markets as M
from algowinbet.domain import SelectionRef
from algowinbet.pricing import devig, edge, ev, overround


def test_devig_sums_to_one_and_removes_margin():
    odds = {"HOME": 2.0, "DRAW": 3.4, "AWAY": 3.8}
    assert overround(list(odds.values())) > 0
    for method in ("proportional", "power"):
        p = devig(odds, method)
        assert sum(p.values()) == pytest.approx(1.0, abs=1e-9)
        assert all(0 < v < 1 for v in p.values())


def test_power_devig_shrinks_longshots_more_than_proportional():
    odds = {"A": 1.3, "B": 3.6, "C": 12.0}
    assert devig(odds, "power")["C"] < devig(odds, "proportional")["C"]


def test_ev_and_edge_are_distinct():
    assert ev(0.5, 2.2) == pytest.approx(0.1)
    assert edge(0.5, 0.45) == pytest.approx(0.05)


def _matrix():
    rng = np.random.default_rng(0)
    m = rng.random((M.GRID, M.GRID)) ** 6
    return m / m.sum()


def test_exclusive_groups_partition_the_grid():
    m = _matrix()
    p1x2 = sum(M.probability(m, SelectionRef(market_code="MATCH_1X2", selection=s)) for s in ("HOME", "DRAW", "AWAY"))
    pou = sum(M.probability(m, SelectionRef(market_code="TOTAL_GOALS", selection=s, line=2.5)) for s in ("OVER", "UNDER"))
    pb = sum(M.probability(m, SelectionRef(market_code="BTTS", selection=s)) for s in ("YES", "NO"))
    assert p1x2 == pytest.approx(1) and pou == pytest.approx(1) and pb == pytest.approx(1)


def test_joint_matches_brute_force_and_differs_from_marginal_product():
    m = _matrix()
    a = SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5)
    b = SelectionRef(market_code="BTTS", selection="YES")
    brute = sum(m[i, j] for i, j in itertools.product(range(M.GRID), repeat=2) if i + j > 2.5 and i > 0 and j > 0)
    assert M.joint_probability(m, [a, b]) == pytest.approx(brute)
    assert M.joint_probability(m, [a, b]) != pytest.approx(M.probability(m, a) * M.probability(m, b), rel=1e-3)


def test_integer_lines_unsupported():
    with pytest.raises(M.UnsupportedMarket):
        M.mask(SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.0))


def test_outcome_settlement():
    assert M.outcome(2, 1, SelectionRef(market_code="MATCH_1X2", selection="HOME"))
    assert M.outcome(2, 1, SelectionRef(market_code="BTTS", selection="YES"))
    assert not M.outcome(2, 0, SelectionRef(market_code="BTTS", selection="YES"))
    assert M.outcome(1, 1, SelectionRef(market_code="TOTAL_GOALS", selection="UNDER", line=2.5))


# ------------------------------------------------------------------ half-time, both-halves, order of goals, refunds
def _poisson_grid(lh=1.6, la=1.1):
    from scipy.stats import poisson
    g = np.arange(M.GRID)
    m = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))
    return m / m.sum()


def R(code, sel, line=None):
    return SelectionRef(market_code=code, selection=sel, line=line)


def test_half_markets_are_coherent_with_the_full_time_grid():
    m = _poisson_grid()
    for code, sels in (("MATCH_1X2@H1", ("HOME", "DRAW", "AWAY")), ("MATCH_1X2@H2", ("HOME", "DRAW", "AWAY")),
                       ("HT_FT", M.REGISTRY["HT_FT"].selections), ("HIGHEST_HALF", ("1ST", "EQUAL", "2ND")),
                       ("FIRST_GOAL", ("HOME", "NONE", "AWAY")), ("TEAM_ODD_EVEN_AWAY", ("ODD", "EVEN"))):
        assert sum(M.probability(m, R(code, s)) for s in sels) == pytest.approx(1.0, abs=1e-6), code
    # with a plain Poisson full-time grid the two halves add up to the same full-time result
    assert M.joint_probability(m, [R("MATCH_1X2", "HOME"), R("TOTAL_GOALS@H1", "UNDER", 9.5)]) == pytest.approx(
        M.probability(m, R("MATCH_1X2", "HOME")), abs=1e-4)
    # fewer goals before the break: more first-half draws than full-time draws, and the second half scores more
    assert M.probability(m, R("MATCH_1X2@H1", "DRAW")) > M.probability(m, R("MATCH_1X2", "DRAW"))
    assert M.probability(m, R("HIGHEST_HALF", "2ND")) > M.probability(m, R("HIGHEST_HALF", "1ST"))
    # the stronger home side scores first more often; both-halves events are rarer than either-half ones
    assert M.probability(m, R("FIRST_GOAL", "HOME")) > M.probability(m, R("FIRST_GOAL", "AWAY"))
    assert M.probability(m, R("WIN_BOTH_HALVES_HOME", "YES")) < M.probability(m, R("WIN_EITHER_HALF_HOME", "YES"))
    assert M.probability(m, R("FIRST_GOAL", "NONE")) == pytest.approx(m[0, 0])


def test_draw_no_bet_prices_the_win_given_no_refund():
    m = _poisson_grid()
    home, away, draw = (M.probability(m, R("MATCH_1X2", s)) for s in ("HOME", "AWAY", "DRAW"))
    assert M.probability(m, R("DRAW_NO_BET", "HOME")) == pytest.approx(home / (home + away))
    assert M.void_probability(m, R("DRAW_NO_BET", "HOME")) == pytest.approx(draw)
    assert M.void_probability(m, R("MATCH_1X2", "HOME")) == 0.0
    assert M.void_probability(m, R("DRAW_NO_BET@H1", "AWAY")) == pytest.approx(M.probability(m, R("MATCH_1X2@H1", "DRAW")))
    with pytest.raises(M.UnsupportedMarket):
        M.joint_probability(m, [R("DRAW_NO_BET", "HOME"), R("TOTAL_GOALS", "OVER", 2.5)])


def test_settlement_of_half_markets_needs_the_half_time_score():
    assert M.outcome(2, 1, R("HT_FT", "X/1"), half_time=(0, 0))
    assert M.outcome(2, 1, R("MATCH_1X2@H2", "HOME"), half_time=(1, 1))
    assert not M.outcome(2, 1, R("BTTS@H1", "YES"), half_time=(1, 0))
    assert M.outcome(3, 1, R("HIGHEST_HALF", "2ND"), half_time=(1, 0))
    assert M.outcome(2, 2, R("WIN_EITHER_HALF_HOME", "YES"), half_time=(1, 0))
    with pytest.raises(M.UnsupportedMarket):
        M.outcome(2, 1, R("MATCH_1X2@H1", "HOME"))  # no half-time score
    with pytest.raises(M.UnsupportedMarket):
        M.outcome(1, 1, R("DRAW_NO_BET", "HOME"))  # refunded: neither won nor lost
    with pytest.raises(M.UnsupportedMarket):
        M.outcome(1, 0, R("FIRST_GOAL", "HOME"))  # needs the order of the goals
    assert M.outcome(2, 1, R("DRAW_NO_BET", "HOME"))


def test_new_markets_have_italian_labels():
    assert R("TOTAL_GOALS@H1", "OVER", 0.5).label() == "Over 0.5 (totale gol) · 1° tempo"
    assert R("HT_FT", "X/1").label() == "Parziale/Finale X/1"
    assert R("DRAW_NO_BET", "AWAY").label() == "Draw no bet: 2 (ospite)"
    assert R("FIRST_GOAL", "NONE").label() == "Primo gol: nessun gol"
    assert M.family_of("TOTAL_GOALS@H2") == "TOTALS_H2" and M.family_of("HT_FT") == "HALVES"
