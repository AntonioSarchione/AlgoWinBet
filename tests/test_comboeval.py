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


def test_attacking_factor_keeps_mean_variance_and_side_weights():
    from algowinbet.comboeval import _frailty, _mixed_size, _pair_weight
    nodes, s2 = _frailty(0.3)
    assert math.isclose(sum(w for w, _ in nodes), 1.0) and math.isclose(sum(w * z for w, z in nodes), 1.0)
    # mixed over z, a negative binomial with the raised size keeps mean^2 / size + mean as its variance
    var = lambda mean, k: sum(w * (mean * z + (mean * z) ** 2 / k) for w, z in nodes) + mean ** 2 * s2  # noqa: E731
    assert math.isclose(var(10.0, _mixed_size(8.0, s2)), 10.0 + 100.0 / 8.0)
    # the factor alone more dispersed than the count model: near Poisson, a little more variance than the model's
    assert _mixed_size(15.0, s2) == (1 + s2) / 1e-3 and var(10.0, _mixed_size(15.0, s2)) > 10.0 + 100.0 / 15.0
    # the correlated pair weights keep each side's own weights and are never negative at |rho| = 1/3
    ws = [w for w, _ in nodes]
    pw = [[_pair_weight(ws[i], ws[j], i, j, -1 / 3) for j in range(3)] for i in range(3)]
    assert all(v >= 0 for row in pw for v in row)
    assert all(math.isclose(sum(pw[i]), ws[i]) for i in range(3))
