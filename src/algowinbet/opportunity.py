"""Opportunity engine: model probability x market price -> edge / EV / uncertainty / data quality / status."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from .calibration import CalibrationSet
from .config import Config
from .domain import Opportunity, OpportunityStatus, Player, SelectionRef
from .markets import UnsupportedMarket, family_of, probability, void_probability
from .meta import FAMILY_SELECTIONS, MetaSet, family_key, group_of
from .information import Adjustment, PlayerImpactModel, TeamAvailability, build_availability
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
    adjustment: Adjustment = field(default_factory=Adjustment)
    availability: dict[str, TeamAvailability] = field(default_factory=dict)
    matrix_blind: np.ndarray | None = None


def data_quality(state: MatchState, model: DixonColes, view: MarketView, stale: bool = False) -> dict[str, float]:
    f = state.fixture
    sample = min(1.0, model.sample_size(f.home, f.away) / 15.0)
    market_present = 1.0 if view.p_market is not None else 0.4
    completeness = 0.6 * sample + 0.4 * market_present
    age_h = max(0.0, (state.cutoff - view.best_observed_at).total_seconds() / 3600.0)
    freshness = float(np.clip(1.0 - age_h / 168.0, 0.3, 1.0)) * (0.5 if stale else 1.0)
    lvl = SOURCE_LEVEL_SCORE.get(view.source_level, 0.5)
    reliability = 0.4 * lvl + 0.6 * min(1.0, view.n_books / 3.0)
    consistency = 0.7 if len(view.per_book) < 2 else float(1.0 - min(1.0, view.dispersion / 0.05))
    lineup = {"confirmed": 1.0, "probable": 0.7, "none": 0.5}[state.lineup_state]
    parts = dict(completeness=completeness, freshness=freshness, reliability=reliability,
                 consistency=consistency, lineup=lineup)
    parts["total"] = float(0.25 * completeness + 0.2 * freshness + 0.2 * reliability + 0.15 * consistency + 0.2 * lineup)
    return parts


def classify(ev: float, ev_lower: float, edge: float | None, unc: float, dq: float, has_market: bool, cfg: Config,
             p: float | None = None) -> OpportunityStatus:
    t = cfg.thresholds
    if dq < t.min_dq_valid:
        return S.INVALID
    if ev <= t.avoid_ev:
        return S.AVOID
    if p is not None and p < t.min_probability:
        return S.NEUTRAL
    if ev < t.min_ev:
        # no edge, but a likely outcome Sisal prices at (about) the fair odds of a sharp reference is still useful in a slip
        if has_market and ev >= t.fair_ev and dq >= t.min_dq_candidate and unc <= t.max_uncertainty:
            return S.FAIR
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


def _lineup_notes(adj: Adjustment, avail: dict[str, TeamAvailability]) -> list[str]:
    notes = [n for a in avail.values() for n in a.notes]
    for a in avail.values():
        if a.key_absences:
            notes.append(f"{a.team}: titolari abituali fuori dall'XI: " + ", ".join(p.name for p in a.key_absences[:4]))
    for _, text, eff in adj.contributions[:4]:
        notes.append(f"{text} -> {math.exp(eff) - 1:+.1%} gol attesi")
    return notes


def analyze_fixture(
    state: MatchState,
    model: DixonColes,
    boots: list[DixonColes],
    cfg: Config,
    calib: CalibrationSet | None = None,
    impact: PlayerImpactModel | None = None,
    roster: list[Player] | None = None,
    meta: MetaSet | None = None,
) -> FixtureAnalysis:
    calib = calib or CalibrationSet()
    f = state.fixture
    adj, avail = Adjustment(), {}
    if impact is not None and roster:
        rh, ra = [p for p in roster if p.team == f.home], [p for p in roster if p.team == f.away]
        if rh and ra:
            avail = {
                f.home: build_availability(f.home, rh, impact.base, state.events, state.lineups, state.cutoff, f.id),
                f.away: build_availability(f.away, ra, impact.base, state.events, state.lineups, state.cutoff, f.id),
            }
            adj = impact.adjustment(avail[f.home], avail[f.away])
    la_ = (adj.d_home, adj.d_away)
    matrix_blind = model.score_matrix(f.home, f.away)
    matrix = model.score_matrix(f.home, f.away, la_) if not adj.is_zero else matrix_blind
    m_hi = model.score_matrix(f.home, f.away, (adj.d_home + adj.sd_home, adj.d_away - adj.sd_away))
    m_lo = model.score_matrix(f.home, f.away, (adj.d_home - adj.sd_home, adj.d_away + adj.sd_away))
    boot_matrices = [b.score_matrix(f.home, f.away, la_) for b in boots]
    known = model.knows(f.home) and model.knows(f.away)
    views = build_market_views(state.quotes, cfg.ensemble.devig_method, cfg.bet_bookmakers)
    n_eff = max(model.sample_size(f.home, f.away), 3)
    ens = cfg.ensemble
    info_t = state.latest_info_time()
    notes = _lineup_notes(adj, avail)
    out: list[Opportunity] = []
    skipped: dict[str, int] = {}
    by_sel = {(v.ref.market_code, v.ref.line, v.ref.selection): v for v in views}
    group = group_of(f.competition)
    pooled: dict[tuple[str, bool], list[float] | None] = {}

    def meta_probs(fam: str, with_market: bool) -> list[float] | None:
        """Meta-model probabilities of a whole family (None when not fitted or a market price of the family is missing)."""
        if (fam, with_market) not in pooled:
            sels = FAMILY_SELECTIONS[fam]
            pool = meta.pick(group, fam, "pool" if with_market else "calib") if meta else None
            out_p = None
            if pool is not None:
                refs = [SelectionRef(market_code=c, selection=s, line=l) for c, s, l in sels]
                pm = [float(probability(matrix, r)) for r in refs]
                if with_market:
                    vs = [by_sel.get((c, l, s)) for c, s, l in sels]
                    if all(x is not None and x.p_market is not None for x in vs):
                        out_p = pool.apply(pm, [x.p_market for x in vs])
                else:
                    out_p = pool.apply(pm)
            pooled[(fam, with_market)] = out_p
        return pooled[(fam, with_market)]

    for v in views:
        if v.best_odds < cfg.thresholds.min_odds:
            continue
        try:
            p_s = probability(matrix, v.ref)
        except UnsupportedMarket:
            skipped[v.ref.market_code] = skipped.get(v.ref.market_code, 0) + 1
            continue
        p_blind = probability(matrix_blind, v.ref)
        effect_sd = abs(probability(m_hi, v.ref) - probability(m_lo, v.ref)) / 2
        if boot_matrices:
            std_base = float(np.std([probability(m, v.ref) for m in boot_matrices]))
        else:
            std_base = math.sqrt(max(p_s * (1 - p_s), 1e-4) / n_eff)  # empirical proxy: rmse ~ sqrt(pq/n_matches)
        std_s = math.sqrt(std_base**2 + effect_sd**2)  # + uncertainty of the lineup/availability adjustment
        stale = bool(info_t is not None and v.best_observed_at < info_t - timedelta(minutes=10) and abs(p_s - p_blind) >= 0.01)
        has_mkt = v.p_market is not None
        if has_mkt:
            p_prior, tau2 = v.p_market, ens.market_prior_sd**2
            sigma = std_s
            if stale:
                # the quote predates information the model can price: move the market prior by the model's information
                # increment (p_struct - p_struct_blind) and widen it by the increment's own uncertainty
                p_prior = float(np.clip(v.p_market + (p_s - p_blind), 1e-4, 1 - 1e-4))
                tau2 = ens.market_prior_sd**2 + effect_sd**2
                sigma = std_base
            if ens.mode == "adaptive":
                w_eff = tau2 / (tau2 + sigma**2)
                post_sd = math.sqrt(w_eff) * sigma  # posterior sd of the true probability
            else:
                w_eff = ens.w_struct
                post_sd = w_eff * sigma
            p_raw = w_eff * p_s + (1 - w_eff) * p_prior
            disagreement = abs(p_s - v.p_market)
        else:
            p_raw, w_eff, post_sd, disagreement = p_s, 1.0, std_s, 0.05
        fam = family_of(v.ref.market_code)
        calib_version = calib.version(fam)
        fk = family_key(v.ref)
        mp = meta_probs(fk[0], has_mkt) if fk and not stale else None
        if mp is not None:
            # learned blend of model and market, calibration included (meta.py): replaces the fixed adaptive weight
            p_raw = mp[fk[1]]
            p_fin = float(np.clip(p_raw, 1e-4, 1 - 1e-4))
            calib_version = meta.version
        else:
            p_fin = float(np.clip(calib.transform(fam, p_raw), 1e-4, 1 - 1e-4))
        unc = post_sd
        if not known:
            unc = max(unc, 0.15)  # unseen team: model is guessing
        z = cfg.thresholds.z
        p_lo, p_hi = max(1e-4, p_fin - z * unc), min(1 - 1e-4, p_fin + z * unc)
        fair = 1 / p_fin
        p_mkt = v.p_market
        void = void_probability(matrix, v.ref)
        if void > 0:
            # Draw no bet: everything above is "win given no refund". Keep that for the fair price, then switch to the
            # probability that gives the same expected return at this price (win pays the odds, refund pays 1), so EV, the
            # slip optimizer and the stakes treat the leg exactly in expectation.
            eq = lambda x: (1 - void) * x + void / v.best_odds  # noqa: E731
            p_s, p_blind, p_raw, p_fin, p_lo, p_hi = (eq(x) for x in (p_s, p_blind, p_raw, p_fin, p_lo, p_hi))
            p_mkt = eq(p_mkt) if has_mkt else None
            unc *= 1 - void
        dq = data_quality(state, model, v, stale)
        edge_v = (p_fin - p_mkt) if has_mkt else None
        ev_v = p_fin * v.best_odds - 1
        ev_lo = p_lo * v.best_odds - 1
        if has_mkt:  # prudent EV also trusts only part of the edge over the market (optimizer.edge_shrink, from the replay)
            ev_lo = min(ev_lo, min(p_fin, p_mkt + cfg.optimizer.edge_shrink * (p_fin - p_mkt)) * v.best_odds - 1)
        status = classify(ev_v, ev_lo, edge_v, unc, dq["total"], has_mkt, cfg, p_fin)
        if stale and status in (S.STRONG, S.CANDIDATE):
            status = S.WATCH  # the edge may only be a not-yet-updated price: verify the current quote first
        out.append(Opportunity(
            fixture_id=f.id, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff,
            ref=v.ref, description=v.ref.label(), odds=v.best_odds, bookmaker=v.best_book,
            odds_observed_at=v.best_observed_at, n_books=v.n_books, p_struct=p_s, p_market=p_mkt,
            p_ensemble_raw=p_raw, p_final=p_fin, p_low=p_lo, p_high=p_hi, fair_odds=fair, edge=edge_v,
            ev=ev_v, ev_lower=ev_lo, uncertainty=unc, model_disagreement=disagreement,
            data_quality=dq["total"], data_quality_parts=dq, status=status,
            score=opportunity_score(ev_v, edge_v, dq["total"], disagreement, unc, calib_version != "identity-v1"),
            model_version=cfg.model.version, calibration_version=calib_version, cutoff=state.cutoff,
            p_struct_blind=p_blind, lineup_delta_p=p_s - p_blind, lineup_state=state.lineup_state, odds_stale=stale,
            lineup_notes=notes,
        ))
    lh, la = model.expected_goals(f.home, f.away)
    return FixtureAnalysis(state=state, matrix=matrix, opportunities=out, skipped=skipped, expected_goals=(lh, la),
                           model_known=known, adjustment=adj, availability=avail, matrix_blind=matrix_blind)
