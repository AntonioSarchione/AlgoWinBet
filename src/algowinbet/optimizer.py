"""Slip optimizer: hard filters -> beam search -> diversified Pareto-ish selection; NO BET is a valid output (spec 17)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .correlation import joint_of, pair_dependence
from .domain import Opportunity, OpportunityStatus, Slip
from .opportunity import FixtureAnalysis


@dataclass
class OptimizerResult:
    slips: list[Slip]
    no_bet: bool
    reasons: list[str]
    nearest_miss: Slip | None = None
    nearest_miss_violations: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def make_slip(legs: list[Opportunity], matrices, cfg: Config, C=None, idx=None) -> Slip:
    o = cfg.optimizer
    odds = float(np.prod([l.odds for l in legs]))
    joint, pen = joint_of(legs, matrices, C, idx, o)
    rel = math.sqrt(sum((l.uncertainty / max(l.p_final, 1e-9)) ** 2 for l in legs))
    unc_abs = joint * rel
    p_lo = joint * math.exp(-cfg.thresholds.z * rel)
    ev = joint * odds - 1
    ev_lo = p_lo * odds - 1
    dis = float(np.mean([l.model_disagreement for l in legs]))
    div = len({l.competition for l in legs}) / len(legs)
    obj = (o.w_ev * ev + o.w_prob * joint + o.w_div * div - o.w_unc * min(1.0, rel) - o.w_corr * pen - o.w_disagree * dis)
    return Slip(legs=list(legs), total_odds=odds, joint_probability=joint, fair_odds=1 / joint if joint > 0 else math.inf,
                ev=ev, ev_lower=ev_lo, uncertainty=unc_abs, correlation_penalty=pen, model_disagreement=dis, objective=obj)


def violations(s: Slip, cfg: Config) -> list[str]:
    o, v = cfg.optimizer, []
    if s.total_odds < o.odds_min:
        v.append(f"quota {s.total_odds:.2f} < minima {o.odds_min}")
    if s.total_odds > o.odds_max:
        v.append(f"quota {s.total_odds:.2f} > massima {o.odds_max}")
    if s.joint_probability < o.min_probability:
        v.append(f"probabilità {s.joint_probability:.1%} < minima {o.min_probability:.1%}")
    if s.correlation_penalty > o.correlation_limit:
        v.append(f"correlazione {s.correlation_penalty:.2f} > limite {o.correlation_limit}")
    if s.ev < o.min_slip_ev:
        v.append(f"EV schedina {s.ev:+.1%} < minimo {o.min_slip_ev:+.1%}")
    return v


def _overlap(a: Slip, b: Slip) -> float:
    ka, kb = a.key, b.key
    return len(ka & kb) / len(ka | kb)


def optimize(opps: list[Opportunity], analyses: dict[str, FixtureAnalysis], cfg: Config) -> OptimizerResult:
    o, t = cfg.optimizer, cfg.thresholds
    matrices = {fid: a.matrix for fid, a in analyses.items()}
    ok_status = {OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE}
    if o.include_watch:
        ok_status.add(OpportunityStatus.WATCH)
    stats = {"opportunities": len(opps), "eligible_status": 0, "eligible_leg_probability": 0, "slips_evaluated": 0}
    elig = [x for x in opps if x.status in ok_status]
    stats["eligible_status"] = len(elig)
    reasons: list[str] = []
    if not elig:
        reasons.append(
            f"Nessuna opportunità con EV ≥ {t.min_ev:.0%}, incertezza ≤ {t.max_uncertainty:.2f} e qualità dati ≥ {t.min_dq_candidate:.2f} "
            f"su {len(opps)} valutate: quote già coerenti col modello o dati/stime troppo incerti."
        )
        return OptimizerResult([], True, reasons, stats=stats)
    elig = [x for x in elig if x.p_final >= o.min_leg_probability]
    stats["eligible_leg_probability"] = len(elig)
    if not elig:
        reasons.append(f"Tutte le opportunità hanno probabilità < soglia per leg {o.min_leg_probability:.0%}.")
        return OptimizerResult([], True, reasons, stats=stats)
    # keep the best few candidates per fixture to bound the search space
    per_fix: dict[str, list[Opportunity]] = {}
    for x in sorted(elig, key=lambda z: -z.score):
        per_fix.setdefault(x.fixture_id, []).append(x)
    cands = [x for lst in per_fix.values() for x in lst[: max(o.max_legs_per_fixture, 3)]]
    cands.sort(key=lambda z: -z.score)
    cands = cands[:80]
    n = len(cands)
    C = {(i, j): pair_dependence(cands[i], cands[j], matrices, o) for i in range(n) for j in range(i + 1, n)}

    def allowed(idx: tuple[int, ...], new: int) -> bool:
        legs = [cands[i] for i in idx]
        nl = cands[new]
        if sum(1 for l in legs if l.fixture_id == nl.fixture_id) >= o.max_legs_per_fixture:
            return False
        if any(l.fixture_id == nl.fixture_id and l.ref == nl.ref for l in legs):
            return False
        if sum(1 for l in legs if l.competition == nl.competition) >= o.max_legs_per_competition:
            return False
        return True

    pool: dict[frozenset, Slip] = {}
    infeasible: list[tuple[Slip, list[str]]] = []
    beam: list[tuple[int, ...]] = [(i,) for i in range(n)]
    seen: set[frozenset] = set()
    for depth in range(1, o.max_legs + 1):
        scored: list[tuple[float, tuple[int, ...]]] = []
        for idx in beam:
            key = frozenset(idx)
            if key in seen:
                continue
            seen.add(key)
            s = make_slip([cands[i] for i in idx], matrices, cfg, C, list(idx))
            stats["slips_evaluated"] += 1
            if s.total_odds > o.odds_max:
                continue  # odds only grow with more legs: prune branch
            v = violations(s, cfg)
            if not v:
                pool[frozenset((l.fixture_id, l.ref.key) for l in s.legs)] = s
            elif len(infeasible) < 500:
                infeasible.append((s, v))
            scored.append((s.objective, idx))
        scored.sort(key=lambda z: -z[0])
        beam_next: list[tuple[int, ...]] = []
        for _, idx in scored[: o.beam_width]:
            for new in range(n):
                if new not in idx and allowed(idx, new):
                    beam_next.append(tuple(sorted(idx + (new,))))
        if not beam_next:
            break
        beam = beam_next

    if not pool:
        reasons.append(
            f"{len(elig)} opportunità idonee ma nessuna combinazione rispetta insieme quota {o.odds_min}-{o.odds_max}, "
            f"probabilità ≥ {o.min_probability:.0%}, correlazione ≤ {o.correlation_limit}, EV schedina ≥ {o.min_slip_ev:+.0%}, "
            f"max {o.max_legs} leg."
        )
        miss, viol = (None, [])
        if infeasible:
            infeasible.sort(key=lambda z: (len(z[1]), -z[0].objective))
            miss, viol = infeasible[0]
        return OptimizerResult([], True, reasons, miss, viol, stats)

    ranked = sorted(pool.values(), key=lambda s: -s.objective)
    chosen: list[Slip] = []
    for s in ranked:
        if all(_overlap(s, c) <= o.max_overlap for c in chosen):
            chosen.append(s)
        if len(chosen) >= o.output_count:
            break
    return OptimizerResult(chosen, False, [], stats=stats)
