from algowinbet.duoeval import Binary, pair_substitutes


def test_one_substitute_per_minute_is_paired_by_minutes():
    rows = [("a", 90, 1, False), ("b", 70, 1, False), ("c", 20, 0, False), ("d", 0, 0, False)]
    pairs, skipped = pair_substitutes(rows, {})
    assert pairs == {"b": "c"} and not skipped


def test_double_change_is_paired_by_role_or_skipped():
    rows = [("fw", 60, 1, False), ("mf", 60, 1, False), ("fw2", 30, 0, False), ("mf2", 30, 0, False)]
    roles = {"fw": "FWD", "fw2": "FWD", "mf": "MID", "mf2": "MID"}
    assert pair_substitutes(rows, roles)[0] == {"fw": "fw2", "mf": "mf2"}
    pairs, skipped = pair_substitutes(rows, {})  # same role (MID by default): who replaced whom is unknown
    assert pairs == {} and sum(skipped.values()) == 2


def test_sent_off_starter_has_no_substitute():
    rows = [("a", 55, 1, True), ("b", 35, 0, False)]
    assert pair_substitutes(rows, {})[0] == {}


def test_starter_probability_is_calibrated_before_the_substitute_is_judged():
    b = Binary()
    for _ in range(100):
        b.add(0.2, 0.1, 1, 1)
        b.add(0.2, 0.1, 0, 0)
    s = b.scale()
    assert abs((1 - 2.718281828 ** (-s * 0.2)) - 0.5) < 1e-3
    loss, pred, act, bias, n = b.score(1.0, s, subbed=True)
    assert n == 200 and act == 0.5 and bias > 0  # the substitute never added anything: any f > 0 over-predicts
