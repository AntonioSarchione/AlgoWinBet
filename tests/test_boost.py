"""Fase 10 skeleton: LightGBM on top of Dixon-Coles, walk-forward with no look-ahead."""
import math
from datetime import timedelta

import pytest

from algowinbet.boost import FEATURES, MIN_GAIN, BoostReport, TeamLog, build_rows, evaluate_boost, verdict
from algowinbet.config import Config
from algowinbet.domain import MatchResult, utc
from algowinbet.providers import MockProvider


def _r(fid, home, away, ko, hg, ag):
    return MatchResult(fixture_id=fid, competition="L", home=home, away=away, kickoff=ko, home_goals=hg, away_goals=ag)


def test_team_features_use_only_results_known_at_the_cutoff():
    log = TeamLog([_r("1", "A", "B", utc(2026, 9, 1, 18), 2, 0), _r("2", "A", "C", utc(2026, 9, 6, 18), 0, 1),
                   _r("3", "B", "A", utc(2026, 9, 7, 20), 1, 1)])  # Monday evening: not known on Monday 00:00
    monday = utc(2026, 9, 7)
    ppg, gd, rest, load, n = log.features("A", monday, utc(2026, 9, 9, 18))
    assert n == 2 and ppg == 1.5 and gd == 0.5  # 3 + 0 points, +2 - 1 goals over 2 matches
    assert rest == pytest.approx(3.0)  # from the last KNOWN match (6 Sept) to the kickoff (9 Sept)
    assert load == 2.0


def test_shot_and_xg_form_from_the_last_matches_that_have_them():
    rows = [_r("1", "A", "B", utc(2026, 9, 1, 18), 2, 0), _r("2", "C", "A", utc(2026, 9, 5, 18), 1, 1),
            _r("3", "A", "D", utc(2026, 9, 6, 18), 0, 0)]
    stats = {"1": {"shots": (14.0, 6.0), "expected_goals": (1.8, 0.4)}, "2": {"shots": (10.0, 8.0)}}  # A away in match 2
    log = TeamLog(rows, stats)
    f = log.stat_features("A", utc(2026, 9, 7))
    assert f[("shots", "for")] == 11.0 and f[("shots", "against")] == 8.0  # (14 + 8) / 2 for, (6 + 10) / 2 against
    assert f[("expected_goals", "for")] == 1.8 and math.isnan(f[("shots_on_target", "for")])


@pytest.fixture(scope="module")
def rows():
    prov = MockProvider(seed=3, past_rounds=30, future_rounds=0)
    end = prov.as_of
    return build_rows(prov, Config(), end - timedelta(weeks=40), end)


def test_rows_carry_the_dixon_coles_probabilities_and_every_feature(rows):
    assert len(rows) > 100
    for r in rows[:50]:
        assert len(r.x) == len(FEATURES) and abs(sum(r.p_dc) - 1) < 1e-9 and r.y in (0, 1, 2)
        assert r.monday.weekday() == 0 and r.monday.hour == 0


def test_walk_forward_evaluation_runs_and_judges_against_the_bar(rows):
    pytest.importorskip("lightgbm")
    rep = evaluate_boost(None, Config(), None, None, retrain_every=4, rows=rows, min_train=300)
    t = rep.groups["tutte"]
    assert rep.trained >= 1 and t["n"] > 0 and set(rep.importance) <= set(FEATURES) and len(rep.rounds) == rep.trained
    assert rep.stats_share == 0.0  # the mock has no shots / xG
    assert abs(t["ll_boost"] - t["ll_dc"] - t["diff"][0]) < 1e-9
    # on mock data (simulated from a Dixon-Coles-like truth) there is nothing to learn: no clear gain
    assert verdict(rep).startswith("non basta")


def test_verdict_needs_the_gain_and_an_interval_below_zero():
    rep = BoostReport(groups={"tutte": {"n": 900, "ll_dc": 1.0, "ll_boost": 0.99, "diff": (-0.006, 0.004)}})
    assert verdict(rep).startswith("ENTRA")
    rep.groups["tutte"]["diff"] = (-0.006, 0.007)
    assert verdict(rep).startswith("non basta")
    rep.groups["tutte"]["diff"] = (-MIN_GAIN / 2, 0.001)
    assert verdict(rep).startswith("non basta")


def test_frozen_booster_roundtrip_and_shadow_verdict(rows):
    pytest.importorskip("lightgbm")
    from algowinbet.boost import SHADOW_MIN, Booster, load_booster, save_booster, shadow_verdict
    from algowinbet.snapshots import SnapshotStore
    store = SnapshotStore(":memory:")
    assert load_booster(store) is None
    b = Booster(params={"min_data_in_leaf": 20}).fit(rows)
    save_booster(store, b, utc(2026, 10, 5), len(rows), utc(2026, 10, 5, 9))
    b2, until = load_booster(store)
    assert until == utc(2026, 10, 5) and b2.best == b.best
    assert abs(b2.predict(rows[:20]) - b.predict(rows[:20])).max() < 1e-9  # same predictions after the database
    rep = {"since": "2026-10-05", "groups": {"coppe": {"n": SHADOW_MIN - 1, "diff": [-0.02, 0.01]}}}
    assert shadow_verdict(rep).startswith("in osservazione")
    rep["groups"]["coppe"]["n"] = SHADOW_MIN
    assert shadow_verdict(rep).startswith("CONFERMATO")
    rep["groups"]["coppe"]["diff"] = [-0.02, 0.03]
    assert shadow_verdict(rep).startswith("NON confermato")
