"""Deterministic explanation payloads (spec 21). Numbers come from the records; text is generated from them."""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import Config
from .domain import Opportunity, Slip


def explain_leg(o: Opportunity) -> dict[str, Any]:
    pos, neg = [], []
    if o.edge is not None and o.edge > 0:
        pos.append(f"probabilità stimata {o.p_final:.1%} vs mercato {o.p_market:.1%} (edge {o.edge:+.1%})")
    if o.ev > 0:
        pos.append(f"EV {o.ev:+.1%} (quota {o.odds:.2f} vs fair {o.fair_odds:.2f})")
    if o.ev_lower > 0:
        pos.append(f"EV positivo anche al limite inferiore dell'intervallo ({o.ev_lower:+.1%})")
    if o.model_disagreement < 0.03 and o.p_market is not None:
        pos.append("modello strutturale e mercato concordi")
    if o.data_quality >= 0.8:
        pos.append(f"qualità dati alta ({o.data_quality:.0%})")
    if o.n_books >= 3:
        pos.append(f"miglior quota tra {o.n_books} bookmaker")
    if o.p_market is None:
        neg.append("nessun prezzo di mercato completo per confronto (edge non calcolabile)")
    if o.uncertainty > 0.06:
        neg.append(f"incertezza elevata (±{o.uncertainty:.1%}, intervallo {o.p_low:.1%}–{o.p_high:.1%})")
    if o.model_disagreement > 0.05:
        neg.append(f"modello e mercato divergono di {o.model_disagreement:.1%}")
    if o.data_quality_parts.get("lineup", 0) <= 0.5:
        neg.append("formazioni non disponibili/non confermate: impatto non modellato")
    if o.data_quality_parts.get("completeness", 1) < 0.6:
        neg.append("storico squadre limitato")
    return {
        "positive_factors": pos, "negative_factors": neg,
        "model_consensus": {"struct": o.p_struct, "market": o.p_market, "final": o.p_final},
        "counterfactual": {
            "ev_struct_only": o.p_struct * o.odds - 1,
            "ev_market_only": (o.p_market * o.odds - 1) if o.p_market is not None else None,
            "breakeven_odds": o.fair_odds,
        },
        "data_quality": o.data_quality_parts,
    }


def explain_slip(s: Slip, cfg: Config) -> dict[str, Any]:
    pos, neg = [], []
    strong = sum(1 for l in s.legs if l.status.value == "STRONG")
    if s.ev > 0:
        pos.append(f"EV schedina {s.ev:+.1%} con probabilità congiunta {s.joint_probability:.1%}")
    if s.ev_lower > 0:
        pos.append(f"EV positivo anche stimando la probabilità al limite inferiore ({s.ev_lower:+.1%})")
    if strong:
        pos.append(f"{strong}/{len(s.legs)} leg con status STRONG")
    if s.correlation_penalty < 0.05:
        pos.append("dipendenza tra le leg bassa")
    if len({l.competition for l in s.legs}) > 1:
        pos.append("leg distribuite su più competizioni")
    if s.joint_probability < 0.15:
        neg.append(f"probabilità congiunta bassa ({s.joint_probability:.1%}): la schedina perde molto più spesso di quanto vince")
    if s.ev_lower <= 0:
        neg.append("EV non robusto: con probabilità al limite inferiore l'EV è ≤ 0")
    unconf = sum(1 for l in s.legs if l.data_quality_parts.get("lineup", 0) <= 0.5)
    if unconf:
        neg.append(f"{unconf} leg senza formazione confermata")
    if any(l.p_market is None for l in s.legs):
        neg.append("almeno una leg senza prezzo di mercato di confronto")
    if len(s.legs) >= 5:
        neg.append(f"{len(s.legs)} leg: l'errore di stima si accumula")
    struct_joint = s.joint_probability * float(np.prod([l.p_struct / l.p_final for l in s.legs]))
    return {
        "positive_factors": pos,
        "negative_factors": neg,
        "what_would_change_it": [
            f"quota totale di pareggio (EV=0): {s.fair_odds:.2f} (quota attuale {s.total_odds:.2f})",
            "nuova formazione ufficiale / infortuni su leg con giocatori chiave (impatto non ancora modellato nel M1)",
            "movimento di quota > 3% su una qualsiasi leg",
        ],
        "counterfactual": {
            "ev_struct_model_only": struct_joint * s.total_odds - 1,
            "joint_probability_low": s.joint_probability * math.exp(-cfg.thresholds.z * (s.uncertainty / max(s.joint_probability, 1e-9))),
        },
        "legs": [explain_leg(l) for l in s.legs],
    }
