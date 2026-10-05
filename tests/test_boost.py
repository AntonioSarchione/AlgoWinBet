"""Fase 10 skeleton: LightGBM on top of Dixon-Coles, walk-forward with no look-ahead."""
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
    rep = evaluate_boost(None, Config(), None, None, warmup_weeks=6, retrain_every=4, rows=rows)
    t = rep.groups["tutte"]
    assert rep.trained >= 1 and t["n"] > 0 and set(rep.importance) <= set(FEATURES)
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
