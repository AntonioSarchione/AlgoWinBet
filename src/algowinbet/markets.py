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
        # Result-type markets share the 1X2 family (calibration, information routing); goal counts share TOTALS.
        MarketType("EURO_HANDICAP", "Match", "1X2", ("HOME", "DRAW", "AWAY"), True, True),
        MarketType("ASIAN_HANDICAP", "Match", "1X2", ("HOME", "AWAY"), True, True),
        MarketType("WINNING_MARGIN", "Match", "1X2", (), False),
        MarketType("WIN_TO_NIL_HOME", "Team", "1X2", ("YES", "NO"), True),
        MarketType("WIN_TO_NIL_AWAY", "Team", "1X2", ("YES", "NO"), True),
        MarketType("ODD_EVEN", "Goals", "TOTALS", ("ODD", "EVEN"), True),
        MarketType("TOTAL_EXACT", "Goals", "TOTALS", (), False),
        MarketType("TEAM_EXACT_HOME", "Team", "TEAM_TOTALS", (), False),
        MarketType("TEAM_EXACT_AWAY", "Team", "TEAM_TOTALS", (), False),
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
    elif code in ("EURO_HANDICAP", "ASIAN_HANDICAP"):
        # line = handicap given to the home team. European: whole lines, three outcomes. Asian: only x.5 lines here
        # (whole and quarter lines refund stakes, which a single win probability cannot price).
        if line is None:
            raise UnsupportedMarket(f"{code}: missing line")
        whole = abs(line - round(line)) < 1e-9
        half = abs(line * 2 - round(line * 2)) < 1e-9 and not whole
        if (code == "EURO_HANDICAP" and not whole) or (code == "ASIAN_HANDICAP" and not half):
            raise UnsupportedMarket(f"{code}: line {line} not supported")
        adj = _I + line
        table = {"HOME": adj > _J, "DRAW": np.isclose(adj, _J), "AWAY": adj < _J}
        if code == "ASIAN_HANDICAP":
            table.pop("DRAW")
    elif code == "WINNING_MARGIN":
        # H2 = home by exactly 2, H3+ = home by 3 or more, A.. = away, D = score draw (goals), NG = 0-0
        table = {"D": (_I == _J) & (_I > 0), "NG": (_I == 0) & (_J == 0), "DI": _I == _J}
        if sel[:1] in ("H", "A") and sel[1:].rstrip("+").isdigit():
            k = int(sel[1:].rstrip("+"))
            diff = (_I - _J) if sel[0] == "H" else (_J - _I)
            table[sel] = diff >= k if sel.endswith("+") else diff == k
    elif code in ("WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"):
        yes = (_I > _J) & (_J == 0) if code == "WIN_TO_NIL_HOME" else (_J > _I) & (_I == 0)
        table = {"YES": yes, "NO": ~yes}
    elif code == "ODD_EVEN":
        table = {"ODD": total % 2 == 1, "EVEN": total % 2 == 0}
    elif code in ("TOTAL_EXACT", "TEAM_EXACT_HOME", "TEAM_EXACT_AWAY"):
        base = {"TOTAL_EXACT": total, "TEAM_EXACT_HOME": _I, "TEAM_EXACT_AWAY": _J}[code]
        n = sel.rstrip("+")
        if not n.isdigit() or int(n) >= GRID - 1:
            raise UnsupportedMarket(f"{code}: bad selection {sel}")
        table = {sel: base >= int(n) if sel.endswith("+") else base == int(n)}
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
    if c in ("EURO_HANDICAP", "ASIAN_HANDICAP"):
        kind = "europeo" if c == "EURO_HANDICAP" else "asiatico"
        h = f"{ln:+g}".replace("+0", "0")
        return f"Handicap {kind} ({h} casa): " + {"HOME": "1", "DRAW": "X", "AWAY": "2"}[s]
    if c == "WINNING_MARGIN":
        if s in ("D", "NG", "DI"):
            return {"D": "Margine: pareggio con gol", "NG": "Margine: 0-0", "DI": "Margine: pareggio"}[s]
        return f"Margine: {'casa' if s[0] == 'H' else 'ospite'} vince di {s[1:].replace('+', ' o più')}"
    if c in ("WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"):
        return f"{'Casa' if c.endswith('HOME') else 'Ospite'} vince a zero: {'Sì' if s == 'YES' else 'No'}"
    if c == "ODD_EVEN":
        return f"Totale gol {'dispari' if s == 'ODD' else 'pari'}"
    if c == "TOTAL_EXACT":
        return f"Gol totali: {s.replace('+', ' o più')}"
    if c in ("TEAM_EXACT_HOME", "TEAM_EXACT_AWAY"):
        return f"Gol {'casa' if c.endswith('HOME') else 'ospite'}: {s.replace('+', ' o più')}"
    return f"{c} {s} {ln if ln is not None else ''}".strip()


def group_key(ref: SelectionRef) -> tuple[str, float | None]:
    return (ref.market_code, ref.line)


def complete_group(market_code: str) -> tuple[str, ...] | None:
    spec = REGISTRY.get(market_code)
    return spec.selections if spec and spec.exclusive_complete else None
