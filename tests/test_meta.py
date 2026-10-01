from datetime import timedelta

import numpy as np

from algowinbet.config import Config
from algowinbet.domain import OddsQuote, SelectionRef
from algowinbet.engine import Engine
from algowinbet.meta import MetaSet, Pool, family_key, fit_pool, group_of, load_meta, save_meta
from algowinbet.pricing import reference_price
from algowinbet.providers import MockProvider
from algowinbet.snapshots import SnapshotProvider, SnapshotStore


def _synthetic(n, a, b, c, seed=1):
    rng = np.random.default_rng(seed)
    pm = rng.dirichlet([4, 3, 3], n)
    pk = np.clip(pm + rng.normal(0, 0.05, pm.shape), 0.02, None)
    pk /= pk.sum(axis=1, keepdims=True)
    z = a * np.log(pm) + b * np.log(pk) + np.array(c)
    p = np.exp(z) / np.exp(z).sum(axis=1, keepdims=True)
    y = np.array([rng.choice(3, p=row) for row in p])
    return pm, pk, y


def test_pool_fit_recovers_the_true_blend():
    pm, pk, y = _synthetic(20000, 0.3, 0.9, [0.0, 0.0, 0.0])
    p = fit_pool(pm, pk, y, "pool")
    assert abs(p.a - 0.3) < 0.15 and abs(p.b - 0.9) < 0.15 and p.c == [0.0, 0.0, 0.0]  # no lean on top of the market
    out = p.apply(pm[0], pk[0])
    assert abs(sum(out) - 1) < 1e-9 and min(out) > 0


def test_pool_with_few_matches_stays_near_the_prior():
    pm, pk, y = _synthetic(3, 1.0, 0.0, [0, 0, 0])
    p = fit_pool(pm, pk, y, "pool")
    assert abs(p.a - 0.25) < 0.3 and abs(p.b - 0.75) < 0.3


def test_calibration_only_pool_ignores_the_market():
    pm, _, y = _synthetic(5000, 1.4, 0.0, [0, 0, 0])  # the model is too timid: the fit sharpens it
    p = fit_pool(pm, None, y, "calib")
    assert p.kind == "calib" and p.b == 0.0 and p.a > 1.15
    assert p.apply([0.5, 0.3, 0.2])[0] > 0.5
    pm, _, y = _synthetic(5000, 1.0, 0.0, [0.0, 0.3, 0.0])  # model-only fit keeps a lean: draws underrated
    assert fit_pool(pm, None, y, "calib").c[1] > 0.15


def test_national_teams_never_borrow_the_club_fit():
    calib = {"a": 0.5, "b": 0.0, "c": [0.0, 0.0, 0.0], "n": 900, "kind": "calib", "d": 0.0}
    m = MetaSet({"tutte|1X2|calib": calib})
    assert m.pick("coppe", "1X2", "calib") is not None and m.pick("nazionali", "1X2", "calib") is None


def test_family_key_and_group():
    assert family_key(SelectionRef(market_code="MATCH_1X2", selection="DRAW")) == ("1X2", 1)
    assert family_key(SelectionRef(market_code="TOTAL_GOALS", selection="UNDER", line=2.5)) == ("U/O 2.5", 1)
    assert family_key(SelectionRef(market_code="TOTAL_GOALS", selection="UNDER", line=3.5)) is None
    assert group_of("UEFA Champions League") == "coppe" and group_of("Serie A") == "campionati"


def test_reference_price_prefers_pinnacle_unless_it_strays():
    assert reference_price({"sisal": 0.50, "pinnacle": 0.46}) == 0.46
    assert reference_price({"sisal": 0.50, "snai": 0.52}) == 0.51
    assert reference_price({"sisal": 0.20, "snai": 0.22, "pinnacle": 0.60}) != 0.60  # feed error: far from the rest


def test_meta_is_saved_loaded_and_used_by_the_live_analysis():
    mock = MockProvider(seed=7)
    s = SnapshotStore(":memory:")
    assert not load_meta(s)  # no table yet: no meta-model
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72
    # a blend that trusts the market alone: the 1X2 probabilities must become the market's
    params = {"tutte|1X2|pool": {"a": 0.0, "b": 1.0, "c": [0.0, 0.0, 0.0], "n": 500, "kind": "pool", "d": 0.0}}
    save_meta(s, params, mock.as_of, mock.as_of, cfg.model.version)
    meta = load_meta(s, cfg.model.version)
    assert meta and meta.version.startswith("meta-") and not load_meta(s, "other-version")
    res = Engine(SnapshotProvider(s), cfg, meta=meta).analyze(None, mock.as_of, mock.as_of + timedelta(days=3), mock.as_of)
    x12 = [o for o in res.opportunities if o.ref.market_code == "MATCH_1X2" and o.p_market is not None]
    assert x12 and all(o.calibration_version == meta.version for o in x12)
    by_fx = {}
    for o in x12:
        by_fx.setdefault(o.fixture_id, []).append(o)
    for legs in by_fx.values():
        if len(legs) == 3:
            tot = sum(o.p_market for o in legs)
            for o in legs:
                assert abs(o.p_final - o.p_market / tot) < 1e-6
    other = [o for o in res.opportunities if o.ref.market_code not in ("MATCH_1X2",)]
    assert all(o.calibration_version != meta.version for o in other)  # families without a fit keep the adaptive blend
