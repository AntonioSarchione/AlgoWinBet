"""Market registry. A selection is a boolean mask over the (home_goals, away_goals) score grid,
so any goal-based market and any same-match combination is priced from one joint distribution.

New goal-based market = add a MarketType + a mask rule. Adding it to the registry is what enables it (spec 45.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .domain import SelectionRef

MAX_GOALS = 10
GRID = MAX_GOALS + 1
_I, _J = np.meshgrid(np.arange(GRID), np.arange(GRID), indexing="ij")  # i=home goals, j=away goals


class UnsupportedMarket(ValueError):
    pass


@dataclass(frozen=True)
class MarketType:
    code: str
    category: str
    family: str  # calibration / reporting family
    selections: tuple[str, ...]
    exclusive_complete: bool  # selections form a complete mutually-exclusive set (per line) -> devig possible
    has_line: bool = False


REGISTRY: dict[str, MarketType] = {
    m.code: m
    for m in [
        MarketType("MATCH_1X2", "Match", "1X2", ("HOME", "DRAW", "AWAY"), True),
        MarketType("DOUBLE_CHANCE", "Match", "1X2", ("1X", "X2", "12"), False),
        MarketType("TOTAL_GOALS", "Goals", "TOTALS", ("OVER", "UNDER"), True, True),
        MarketType("BTTS", "Goals", "BTTS", ("YES", "NO"), True),
        MarketType("TEAM_TOTAL_HOME", "Team", "TEAM_TOTALS", ("OVER", "UNDER"), True, True),
        MarketType("TEAM_TOTAL_AWAY", "Team", "TEAM_TOTALS", ("OVER", "UNDER"), True, True),
        MarketType("CORRECT_SCORE", "Goals", "CORRECT_SCORE", (), False),
    ]
}


def family_of(market_code: str) -> str:
    return REGISTRY[market_code].family


def is_supported(ref: SelectionRef) -> bool:
    try:
        mask(ref)
        return True
    except UnsupportedMarket:
        return False


@lru_cache(maxsize=None)
def _mask(code: str, sel: str, line: float | None) -> np.ndarray:
    if code not in REGISTRY:
        raise UnsupportedMarket(f"unknown market {code}")
    spec = REGISTRY[code]
    total = _I + _J
    if code == "MATCH_1X2":
        table = {"HOME": _I > _J, "DRAW": _I == _J, "AWAY": _I < _J}
    elif code == "DOUBLE_CHANCE":
        table = {"1X": _I >= _J, "X2": _I <= _J, "12": _I != _J}
    elif code == "BTTS":
        table = {"YES": (_I > 0) & (_J > 0), "NO": (_I == 0) | (_J == 0)}
    elif code in ("TOTAL_GOALS", "TEAM_TOTAL_HOME", "TEAM_TOTAL_AWAY"):
        if line is None or abs(line * 2 - round(line * 2)) > 1e-9 or abs(line - round(line)) < 1e-9:
            raise UnsupportedMarket(f"{code}: only half-integer lines (x.5) are supported, got {line}")
        base = {"TOTAL_GOALS": total, "TEAM_TOTAL_HOME": _I, "TEAM_TOTAL_AWAY": _J}[code]
        table = {"OVER": base > line, "UNDER": base < line}
    elif code == "CORRECT_SCORE":
        try:
            h, a = (int(x) for x in sel.split("-"))
        except ValueError as e:
            raise UnsupportedMarket(f"bad correct score {sel}") from e
        if h >= GRID or a >= GRID:
            raise UnsupportedMarket("score outside grid")
        m = np.zeros((GRID, GRID), dtype=bool)
        m[h, a] = True
        table = {sel: m}
    else:  # pragma: no cover
        raise UnsupportedMarket(code)
    if sel not in table:
        raise UnsupportedMarket(f"{code}: unknown selection {sel}")
    out = table[sel]
    out.setflags(write=False)
    return out


def mask(ref: SelectionRef) -> np.ndarray:
    return _mask(ref.market_code, ref.selection, ref.line)


def probability(matrix: np.ndarray, ref: SelectionRef) -> float:
    return float(matrix[mask(ref)].sum())


def joint_probability(matrix: np.ndarray, refs: list[SelectionRef]) -> float:
    """Exact joint probability of several goal-based legs of the SAME match."""
    m = np.ones((GRID, GRID), dtype=bool)
    for r in refs:
        m = m & mask(r)
    return float(matrix[m].sum())


def outcome(home_goals: int, away_goals: int, ref: SelectionRef) -> bool:
    return bool(mask(ref)[min(home_goals, MAX_GOALS), min(away_goals, MAX_GOALS)])


def describe(ref: SelectionRef) -> str:
    c, s, ln = ref.market_code, ref.selection, ref.line
    it = {
        "MATCH_1X2": {"HOME": "1 (casa)", "DRAW": "X (pareggio)", "AWAY": "2 (ospite)"},
        "BTTS": {"YES": "Gol (entrambe segnano)", "NO": "NoGol"},
    }
    if c in it:
        return f"{'1X2' if c == 'MATCH_1X2' else 'BTTS'}: {it[c][s]}"
    if c == "TOTAL_GOALS":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} (totale gol)"
    if c == "TEAM_TOTAL_HOME":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} gol squadra casa"
    if c == "TEAM_TOTAL_AWAY":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} gol squadra ospite"
    if c == "DOUBLE_CHANCE":
        return f"Doppia chance {s}"
    if c == "CORRECT_SCORE":
        return f"Risultato esatto {s}"
    return f"{c} {s} {ln if ln is not None else ''}".strip()


def group_key(ref: SelectionRef) -> tuple[str, float | None]:
    return (ref.market_code, ref.line)


def complete_group(market_code: str) -> tuple[str, ...] | None:
    spec = REGISTRY.get(market_code)
    return spec.selections if spec and spec.exclusive_complete else None
