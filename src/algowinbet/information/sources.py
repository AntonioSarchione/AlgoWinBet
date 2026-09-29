"""Source hierarchy (spec 7.1). Rumours (D/E) are never treated as facts: they only shift probabilities, weighted by reliability."""
from __future__ import annotations

LEVEL_RELIABILITY = {"A": 0.98, "B": 0.85, "C": 0.60, "D": 0.35, "E": 0.15}
LEVEL_DESCRIPTION = {
    "A": "fonte ufficiale club/competizione",
    "B": "provider strutturato",
    "C": "media sportivo affidabile",
    "D": "fonte secondaria / social",
    "E": "rumor / forum",
}


def reliability(level: str) -> float:
    return LEVEL_RELIABILITY.get(level, 0.15)


def effective_confidence(level: str, parser_confidence: float) -> float:
    return reliability(level) * parser_confidence
