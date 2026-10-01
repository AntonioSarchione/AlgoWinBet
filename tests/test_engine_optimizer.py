from datetime import timedelta

import pytest

from algowinbet.config import Config
from algowinbet.correlation import price_combo
from algowinbet.domain import SelectionRef
from algowinbet.engine import Engine
from algowinbet.optimizer import optimize, violations
from algowinbet.providers import MockProvider
from algowinbet.risk import assign_stakes


def test_efficient_market_yields_no_bet_by_default(analysis):
    # mock books price from the true rates: no single edge to find, so on value bets alone the engine abstains. With
    # "Equa" selections (about the fair price) and the Sisal multiple bonus, slips may appear, but never below EV 0.
    cfg = Config()
    cfg.optimizer.multi_bonus, cfg.optimizer.include_fair = [], False
    res = optimize(analysis.opportunities, analysis.analyses, cfg)
    assert res.no_bet and res.reasons
    for s in analysis.optimizer.slips:
        assert s.ev >= cfg.optimizer.min_slip_ev and all(l.status.value in ("FAIR", "STRONG", "CANDIDATE") for l in s.legs)


def test_opportunity_fields_are_consistent(analysis):
    for o in analysis.opportunities[:200]:
        assert o.ev == pytest.approx(o.p_final * o.odds - 1)
        assert o.fair_odds == pytest.approx(1 / o.p_final)
        assert o.p_low <= o.p_final <= o.p_high
        assert 0 <= o.data_quality <= 1
        if o.p_market is not None:
            assert o.edge == pytest.approx(o.p_final - o.p_market)


def _value_analysis():
    prov = MockProvider(seed=5, open_noise=0.0, close_noise=0.0, bias_over=0.2)
    cfg = Config()
    cfg.ensemble.market_prior_sd = 0.08  # trust the model more so planted value shows up
    eng = Engine(prov, cfg)
    return cfg, eng.analyze(None, prov.as_of, prov.as_of + timedelta(days=8), prov.as_of)


def test_planted_inefficiency_produces_slips_that_respect_hard_constraints():
    cfg, res = _value_analysis()
    o = cfg.optimizer
    assert not res.optimizer.no_bet, res.optimizer.reasons
    keys = []
    for s in res.optimizer.slips:
        assert violations(s, cfg) == []
        assert o.odds_min <= s.total_odds <= o.odds_max
        assert len(s.legs) <= o.max_legs
        fixtures = [l.fixture_id for l in s.legs]
        assert len(fixtures) == len(set(fixtures))  # one leg per fixture (no fake SGP pricing)
        assert s.joint_probability <= min(l.p_final for l in s.legs) + 1e-9
        assert s.stake >= 0
        assert s.explanation["positive_factors"] is not None
        keys.append(s.key)
    assert len(keys) == len(set(keys))
    for a in res.optimizer.slips:
        for b in res.optimizer.slips:
            if a is not b:
                assert len(a.key & b.key) / len(a.key | b.key) <= o.max_overlap + 1e-9


def test_impossible_constraints_give_no_bet_with_reason():
    cfg, res = _value_analysis()
    cfg.optimizer.odds_min, cfg.optimizer.odds_max = 1e9, 2e9
    out = optimize(res.opportunities, res.analyses, cfg)
    assert out.no_bet and out.slips == []
    assert any("nessuna combinazione" in r.lower() for r in out.reasons)


def test_stakes_never_exceed_caps():
    cfg, res = _value_analysis()
    slips = assign_stakes(res.optimizer.slips, cfg.risk)
    r = cfg.risk
    assert sum(s.stake for s in slips) <= r.bankroll * r.max_exposure_total_pct + 1e-6
    assert all(s.stake <= r.bankroll * r.max_stake_pct_per_slip + 1e-6 for s in slips)


def test_same_match_combo_uses_joint_not_product(analysis):
    a = next(iter(analysis.analyses.values()))
    refs = [SelectionRef(market_code="MATCH_1X2", selection="HOME"),
            SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5)]
    out = price_combo(a.matrix, refs, 4.0)
    assert out["joint_probability"] != pytest.approx(out["product_of_marginals"], rel=1e-3)
    assert out["fair_odds_joint"] == pytest.approx(1 / out["joint_probability"])


def test_draw_no_bet_ev_counts_the_refund():
    """A draw-no-bet leg pays the odds on a win and the stake back on a draw: the engine's probability must give exactly that
    expected return (p_final * odds = P(win) * odds + P(draw)), while the fair price stays the no-refund one."""
    from algowinbet.domain import OddsQuote
    from algowinbet.markets import probability, void_probability
    from algowinbet.snapshots import SnapshotProvider, SnapshotStore
    mock = MockProvider(seed=7)
    s = SnapshotStore(":memory:")
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    f = fxs[0]
    s.save_quotes("mock", mock.get_quotes(f.id) + [
        OddsQuote(fixture_id=f.id, market_code="DRAW_NO_BET", selection=sel, bookmaker="BookA", odds=o,
                  observed_at=mock.as_of - timedelta(hours=1), kind="current") for sel, o in (("HOME", 1.45), ("AWAY", 2.6))])
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72
    cfg.bet_bookmakers = ["BookA", "BookB", "BookC"]
    eng = Engine(SnapshotProvider(s), cfg)
    res = eng.analyze(None, mock.as_of, mock.as_of + timedelta(days=3), mock.as_of)
    dnb = [o for o in res.opportunities if o.ref.market_code == "DRAW_NO_BET"]
    assert {o.ref.selection for o in dnb} == {"HOME", "AWAY"}
    matrix = res.analyses[f.id].matrix
    for o in dnb:
        void = void_probability(matrix, o.ref)
        assert 0.1 < void < 0.5
        assert o.ev == pytest.approx(o.p_final * o.odds - 1)
        assert o.p_final == pytest.approx((1 - void) / o.fair_odds + void / o.odds)  # win given no refund, then refund
        assert o.p_struct == pytest.approx((1 - void) * probability(matrix, o.ref) + void / o.odds)


def test_pairs_without_an_exact_joint_count_as_fully_dependent(analysis):
    from algowinbet.correlation import pair_dependence
    a = next(iter(analysis.analyses.values()))
    o = analysis.opportunities[0]
    dnb = o.model_copy(update={"ref": SelectionRef(market_code="DRAW_NO_BET", selection="HOME"), "fixture_id": a.state.fixture.id})
    other = o.model_copy(update={"ref": SelectionRef(market_code="MATCH_1X2", selection="AWAY"), "fixture_id": a.state.fixture.id})
    assert pair_dependence(dnb, other, {a.state.fixture.id: a.matrix}, Config().optimizer) == 1.0
