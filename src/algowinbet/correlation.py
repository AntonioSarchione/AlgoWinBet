"""Correlation engine + joint probability (spec 14, 16).

Same-match legs: exact dependence from the model's joint score distribution (never a product of marginals).
Cross-match legs: independence with a conservative, configurable dependence haircut.
"""
from __future__ import annotations

import math

import numpy as np

from .config import OptimizerCfg
from .domain import Opportunity, SelectionRef
from .markets import UnsupportedMarket, joint_probability, probability


def phi(matrix: np.ndarray, a: SelectionRef, b: SelectionRef) -> float:
    pa, pb = probability(matrix, a), probability(matrix, b)
    pab = joint_probability(matrix, [a, b])
    d = math.sqrt(max(pa * (1 - pa) * pb * (1 - pb), 1e-12))
    return (pab - pa * pb) / d


def pair_dependence(a: Opportunity, b: Opportunity, matrices: dict[str, np.ndarray], cfg: OptimizerCfg) -> float:
    """Non-negative dependence strength C[i][j] used for the penalty; C[i][i] = 0."""
    if a is b:
        return 0.0
    if a.fixture_id == b.fixture_id:
        try:
            return abs(phi(matrices[a.fixture_id], a.ref, b.ref))
        except UnsupportedMarket:
            return 1.0  # no exact joint for this pair (draw no bet, first/last goal): treat it as fully dependent
    if a.competition == b.competition:
        return cfg.same_competition_rho
    return cfg.cross_match_rho


def joint_of(legs: list[Opportunity], matrices: dict[str, np.ndarray], C: dict[tuple[int, int], float] | None = None,
             idx: list[int] | None = None, cfg: OptimizerCfg | None = None) -> tuple[float, float]:
    """Returns (joint_probability, correlation_penalty) using calibrated marginals.

    Same-fixture groups use the exact matrix joint, rescaled by calibrated/structural marginals.
    Cross-fixture pairs contribute exp(-C) haircuts."""
    by_fix: dict[str, list[Opportunity]] = {}
    for o in legs:
        by_fix.setdefault(o.fixture_id, []).append(o)
    joint = 1.0
    for fid, grp in by_fix.items():
        if len(grp) == 1:
            joint *= grp[0].p_final
        else:
            pj = joint_probability(matrices[fid], [o.ref for o in grp])
            scale = float(np.prod([o.p_final / max(o.p_struct, 1e-9) for o in grp]))
            joint *= min(1.0, pj * scale)
    n = len(legs)
    tot = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            pairs += 1
            if C is not None and idx is not None:
                c = C[(min(idx[i], idx[j]), max(idx[i], idx[j]))]
            else:
                c = pair_dependence(legs[i], legs[j], matrices, cfg or OptimizerCfg())
            tot += c
            if legs[i].fixture_id != legs[j].fixture_id:
                joint *= math.exp(-c)
    return joint, (tot / pairs if pairs else 0.0)


def price_combo(matrix: np.ndarray, refs: list[SelectionRef], observed_combo_odds: float) -> dict:
    """Compare a bookmaker's SAME-GAME combo price with the model's joint fair price.
    The product of single-leg odds is only a diagnostic, never the pricing basis (spec 14.2)."""
    pj = joint_probability(matrix, refs)
    prod_marg = float(np.prod([probability(matrix, r) for r in refs]))
    return {
        "joint_probability": pj,
        "product_of_marginals": prod_marg,
        "fair_odds_joint": 1 / pj if pj > 0 else math.inf,
        "observed_odds": observed_combo_odds,
        "ev": pj * observed_combo_odds - 1,
        "dependence_ratio": pj / prod_marg if prod_marg > 0 else math.nan,
    }
