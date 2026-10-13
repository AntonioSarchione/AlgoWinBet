import math

from algowinbet.comboeval import Pair


def test_independent_events_score_one_and_no_correlation():
    p = Pair()
    # four equally likely joint outcomes of two fair coins, predicted at 0.5 each
    for ya, yb in [(1, 1), (1, 0), (0, 1), (0, 0)] * 25:
        p.add(0.5, ya, 0.5, yb)
    assert p.joint == 1.0 and p.net == 1.0 and abs(p.corr) < 1e-12


def test_events_that_happen_together_show_up_after_the_marginals():
    p = Pair()
    # always together, each predicted at 0.5: twice the joint independence expects, perfect residual correlation
    for y in [1, 0] * 50:
        p.add(0.5, y, 0.5, y)
    assert p.joint == 2.0 and p.net == 2.0 and math.isclose(p.corr, 1.0)
    q = Pair()
    # independent but one event under-predicted: joint is off, netto is not
    for ya, yb in [(1, 1), (1, 0), (0, 1), (0, 0)] * 25:
        q.add(0.25, ya, 0.5, yb)
    assert q.joint == 2.0 and q.net == 1.0
