"""Opportunity engine: model probability x market price -> edge / EV / uncertainty / data quality / status."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .calibration import CalibrationSet
from .config import Config
from .domain import Opportunity, OpportunityStatus, SelectionRef
from .markets import UnsupportedMarket, family_of, probability
from .models import DixonColes
from .pricing import MarketView, SOURCE_LEVEL_SCORE, build_market_views
from .state import MatchState

S = OpportunityStatus


@dataclass
class FixtureAnalysis:
    state: MatchState
    matrix: np.ndarray
    opportunities: list[Opportunity]
    skipped: dict[str, int] = field(default_factory=dict)
    expected_goals: tuple[float, float] = (0.0, 0.0)
    model_known: bool = True


def data_quality(state: MatchState, model: DixonColes, view: MarketView) -> dict[str, float]:
    f = state.fixture
    sample = min(1.0, model.sample_size(f.home, f.away) / 15.0)
    market_present = 1.0 if view.p_market is not None else 0.4
    completeness = 0.6 * sample + 0.4 * market_present
    age_h = max(0.0, (state.cutoff - view.best_observed_at).total_seconds() / 3600.0)
    freshness = float(np.clip(1.0 - age_h / 168.0, 0.3, 1.0))
    lvl = SOURCE_LEVEL_SCORE.get(view.source_level, 0.5)
    reliability = 0.4 * lvl + 0.6 * min(1.0, view.n_books / 3.0)
    consistency = 0.7 if len(view.per_book) < 2 else float(1.0 - min(1.0, view.dispersion / 0.05))
    lineup = {"confirmed": 1.0, "probable": 0.7, "none": 0.5}[state.lineup_state]
    parts = dict(completeness=completeness, freshness=freshness, reliability=reliability,
                 consistency=consistency, lineup=lineup)
    parts["total"] = float(0.25 * completeness + 0.2 * freshness + 0.2 * reliability + 0.15 * consistency + 0.2 * lineup)
    return parts


def classify(ev: float, ev_lower: float, edge: float | None, unc: float, dq: float, has_market: bool, cfg: Config) -> OpportunityStatus:
    t = cfg.thresholds
    if dq < t.min_dq_valid:
        return S.INVALID
    if ev <= t.avoid_ev:
        return S.AVOID
    if ev < t.min_ev:
        return S.NEUTRAL
    # ev >= min_ev from here
    if not has_market or dq < t.min_dq_candidate or unc > t.max_uncertainty:
        return S.WATCH
    if ev_lower > 0 and dq >= t.min_dq_strong and edge is not None and edge >= t.min_edge:
        return S.STRONG
    return S.CANDIDATE


def opportunity_score(ev: float, edge: float | None, dq: float, disagreement: float, unc: float, calibrated: bool) -> float:
    n_ev = float(np.clip(ev / 0.2, -1, 1))
    n_edge = float(np.clip((edge or 0.0) / 0.1, -1, 1))
    agree = 1.0 - min(1.0, disagreement / 0.1)
    return 0.35 * n_ev + 0.15 * n_edge + 0.05 * (1.0 if calibrated else 0.5) + 0.15 * dq + 0.10 * agree - 0.20 * min(1.0, unc / 0.1)


def analyze_fixture(
    state: MatchState,
    model: DixonColes,
    boots: list[DixonColes],
    cfg: Config,
    calib: CalibrationSet | None = None,
) -> FixtureAnalysis:
    calib = calib or CalibrationSet()
    f = state.fixture
    matrix = model.score_matrix(f.home, f.away)
    boot_matrices = [b.score_matrix(f.home, f.away) for b in boots]
    known = model.knows(f.home) and model.knows(f.away)
    views = build_market_views(state.quotes, cfg.ensemble.devig_method)
    n_eff = max(model.sample_size(f.home, f.away), 3)
    ens = cfg.ensemble
    out: list[Opportunity] = []
    skipped: dict[str, int] = {}
    for v in views:
        if v.best_odds < cfg.thresholds.min_odds:
            continue
        try:
            p_s = probability(matrix, v.ref)
        except UnsupportedMarket:
            skipped[v.ref.market_code] = skipped.get(v.ref.market_code, 0) + 1
            continue
        if boot_matrices:
            std_s = float(np.std([probability(m, v.ref) for m in boot_matrices]))
        else:
            std_s = math.sqrt(max(p_s * (1 - p_s), 1e-4) / n_eff)  # empirical proxy: rmse ~ sqrt(pq/n_matches)
        has_mkt = v.p_market is not None
        if has_mkt:
            if ens.mode == "adaptive":
                tau2 = ens.market_prior_sd**2
                w_eff = tau2 / (tau2 + std_s**2)
                post_sd = math.sqrt(w_eff) * std_s  # posterior sd of the true probability
            else:
                w_eff = ens.w_struct
                post_sd = w_eff * std_s
            p_raw = w_eff * p_s + (1 - w_eff) * v.p_market
            disagreement = abs(p_s - v.p_market)
        else:
            p_raw, w_eff, post_sd, disagreement = p_s, 1.0, std_s, 0.05
        fam = family_of(v.ref.market_code)
        p_fin = float(np.clip(calib.transform(fam, p_raw), 1e-4, 1 - 1e-4))
        unc = post_sd
        if not known:
            unc = max(unc, 0.15)  # unseen team: model is guessing
        z = cfg.thresholds.z
        p_lo, p_hi = max(1e-4, p_fin - z * unc), min(1 - 1e-4, p_fin + z * unc)
        dq = data_quality(state, model, v)
        edge_v = (p_fin - v.p_market) if has_mkt else None
        ev_v = p_fin * v.best_odds - 1
        ev_lo = p_lo * v.best_odds - 1
        status = classify(ev_v, ev_lo, edge_v, unc, dq["total"], has_mkt, cfg)
        out.append(Opportunity(
            fixture_id=f.id, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff,
            ref=v.ref, description=v.ref.label(), odds=v.best_odds, bookmaker=v.best_book,
            odds_observed_at=v.best_observed_at, n_books=v.n_books, p_struct=p_s, p_market=v.p_market,
            p_ensemble_raw=p_raw, p_final=p_fin, p_low=p_lo, p_high=p_hi, fair_odds=1 / p_fin, edge=edge_v,
            ev=ev_v, ev_lower=ev_lo, uncertainty=unc, model_disagreement=disagreement,
            data_quality=dq["total"], data_quality_parts=dq, status=status,
            score=opportunity_score(ev_v, edge_v, dq["total"], disagreement, unc, calib.version(fam) != "identity-v1"),
            model_version=cfg.model.version, calibration_version=calib.version(fam), cutoff=state.cutoff,
        ))
    lh, la = model.expected_goals(f.home, f.away)
    return FixtureAnalysis(state=state, matrix=matrix, opportunities=out, skipped=skipped, expected_goals=(lh, la), model_known=known)
