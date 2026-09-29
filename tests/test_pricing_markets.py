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
