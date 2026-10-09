"""Market registry. A selection is a boolean mask over the (home_goals, away_goals) score grid,
so any goal-based market and any same-match combination is priced from one joint distribution.

Half-time markets: a code with a period suffix ("MATCH_1X2@H1", "TOTAL_GOALS@H2") applies the full-time rule to the goals of
that half; markets spanning both halves (HT_FT, HIGHEST_HALF, WIN_BOTH_HALVES_*...) use the joint grid of the two halves.
Each half is Poisson with the full-time expected goals split by FIRST_HALF_SHARE (the full-time grid itself stays the
Dixon-Coles one). FIRST_GOAL / LAST_GOAL follow from competing Poisson processes. DRAW_NO_BET refunds the stake on a draw:
probability() returns the win probability given no refund and void_probability() the refund probability.

New goal-based market = add a MarketType + a mask rule. Adding it to the registry is what enables it (spec 45.2).

Corners and cards (Fase 7): the same rules over a count grid of that statistic (home x away), priced from its own matrix
(models/counts.py). Their codes carry the statistic (CORNERS_TOTAL = TOTAL_GOALS over corners); STAT_MARKETS maps each one
to its goal rule.
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
        MarketType("DRAW_NO_BET", "Match", "1X2", ("HOME", "AWAY"), True),
        MarketType("TEAM_ODD_EVEN_HOME", "Team", "TEAM_TOTALS", ("ODD", "EVEN"), True),
        MarketType("TEAM_ODD_EVEN_AWAY", "Team", "TEAM_TOTALS", ("ODD", "EVEN"), True),
        # markets over both halves (joint grid of the two halves)
        MarketType("HT_FT", "Halves", "HALVES", tuple(f"{a}/{b}" for a in "1X2" for b in "1X2"), True),
        MarketType("HIGHEST_HALF", "Halves", "HALVES", ("1ST", "EQUAL", "2ND"), True),
        MarketType("HIGHEST_HALF_HOME", "Halves", "HALVES", ("1ST", "EQUAL", "2ND"), True),
        MarketType("HIGHEST_HALF_AWAY", "Halves", "HALVES", ("1ST", "EQUAL", "2ND"), True),
        MarketType("SCORE_BOTH_HALVES_HOME", "Halves", "HALVES", ("YES", "NO"), True),
        MarketType("SCORE_BOTH_HALVES_AWAY", "Halves", "HALVES", ("YES", "NO"), True),
        MarketType("WIN_BOTH_HALVES_HOME", "Halves", "HALVES", ("YES", "NO"), True),
        MarketType("WIN_BOTH_HALVES_AWAY", "Halves", "HALVES", ("YES", "NO"), True),
        MarketType("WIN_EITHER_HALF_HOME", "Halves", "HALVES", ("YES", "NO"), True),
        MarketType("WIN_EITHER_HALF_AWAY", "Halves", "HALVES", ("YES", "NO"), True),
        # order of the goals (competing Poisson processes)
        MarketType("FIRST_GOAL", "Goals", "SEQUENCE", ("HOME", "NONE", "AWAY"), True),
        MarketType("LAST_GOAL", "Goals", "SEQUENCE", ("HOME", "NONE", "AWAY"), True),
        # corners and cards (count grid of the statistic, see STAT_MARKETS)
        MarketType("CORNERS_1X2", "Corners", "CORNERS", ("HOME", "DRAW", "AWAY"), True),
        MarketType("CORNERS_TOTAL", "Corners", "CORNERS", ("OVER", "UNDER"), True, True),
        MarketType("CORNERS_TEAM_HOME", "Corners", "CORNERS", ("OVER", "UNDER"), True, True),
        MarketType("CORNERS_TEAM_AWAY", "Corners", "CORNERS", ("OVER", "UNDER"), True, True),
        MarketType("CARDS_1X2", "Cards", "CARDS", ("HOME", "DRAW", "AWAY"), True),
        MarketType("CARDS_TOTAL", "Cards", "CARDS", ("OVER", "UNDER"), True, True),
        MarketType("CARDS_TEAM_HOME", "Cards", "CARDS", ("OVER", "UNDER"), True, True),
        MarketType("CARDS_TEAM_AWAY", "Cards", "CARDS", ("OVER", "UNDER"), True, True),
    ]
}

# stat market -> (statistic, goal rule it applies to that statistic's counts)
STAT_MARKETS: dict[str, tuple[str, str]] = {
    f"{p}_{k}": (stat, rule) for p, stat in (("CORNERS", "corners"), ("CARDS", "cards"))
    for k, rule in (("1X2", "MATCH_1X2"), ("TOTAL", "TOTAL_GOALS"), ("TEAM_HOME", "TEAM_TOTAL_HOME"), ("TEAM_AWAY", "TEAM_TOTAL_AWAY"))
}


def stat_of(market_code: str) -> str | None:
    """'corners' / 'cards' for a statistic market (full time only), None for goal markets."""
    spec = STAT_MARKETS.get(market_code)
    return spec[0] if spec else None


def stat_probability(matrix: np.ndarray, ref: SelectionRef) -> float:
    """Probability of a statistic market from that statistic's count grid (any size)."""
    spec = STAT_MARKETS.get(ref.market_code)
    if spec is None:
        raise UnsupportedMarket(f"{ref.market_code}: not a statistic market")
    n = matrix.shape[0]
    i, j = np.meshgrid(np.arange(n), np.arange(matrix.shape[1]), indexing="ij")
    return float(matrix[_rule(spec[1], ref.selection, ref.line, i, j)].sum())


def stat_outcome(home: float, away: float, ref: SelectionRef) -> bool:
    """Settles a statistic market from the final counts."""
    spec = STAT_MARKETS.get(ref.market_code)
    if spec is None:
        raise UnsupportedMarket(f"{ref.market_code}: not a statistic market")
    return bool(_rule(spec[1], ref.selection, ref.line, np.array([int(home)]), np.array([int(away)]))[0])

PERIODS = ("H1", "H2")
FIRST_HALF_SHARE = 0.45  # share of the expected goals scored before half-time (to be re-estimated from half-time scores)
HALF_SPAN = {"HT_FT", "HIGHEST_HALF", "HIGHEST_HALF_HOME", "HIGHEST_HALF_AWAY", "SCORE_BOTH_HALVES_HOME", "SCORE_BOTH_HALVES_AWAY",
             "WIN_BOTH_HALVES_HOME", "WIN_BOTH_HALVES_AWAY", "WIN_EITHER_HALF_HOME", "WIN_EITHER_HALF_AWAY"}
SEQUENCE = {"FIRST_GOAL", "LAST_GOAL"}
VOID_ON_DRAW = {"DRAW_NO_BET"}
HG = 8  # goals per team per half on the half grid (0..7)
_H1, _A1, _H2, _A2 = np.meshgrid(*[np.arange(HG)] * 4, indexing="ij")


def split_code(code: str) -> tuple[str, str | None]:
    """'TOTAL_GOALS@H1' -> ('TOTAL_GOALS', 'H1'); full time -> (code, None)."""
    base, _, period = code.partition("@")
    return base, (period or None)


def family_of(market_code: str) -> str:
    base, period = split_code(market_code)
    fam = REGISTRY[base].family
    return f"{fam}_{period}" if period else fam


def is_supported(ref: SelectionRef) -> bool:
    base, period = split_code(ref.market_code)
    try:
        if base in STAT_MARKETS:
            if period:
                raise UnsupportedMarket(ref.market_code)  # first-half corners and cards: not modelled
            stat_probability(np.ones((2, 2)) / 4, ref)
        elif base in SEQUENCE:
            if ref.selection not in REGISTRY[base].selections:
                raise UnsupportedMarket(ref.selection)
        elif _needs_halves(ref.market_code):
            _mask4(ref.market_code, ref.selection, ref.line)
        else:
            mask(ref)
        return True
    except (UnsupportedMarket, KeyError):
        return False


def _rule(code: str, sel: str, line: float | None, I: np.ndarray, J: np.ndarray) -> np.ndarray:
    """Win condition of a full-time-style market over goal arrays I (home) and J (away) of any shape."""
    if code not in REGISTRY:
        raise UnsupportedMarket(f"unknown market {code}")
    total = I + J
    if code == "MATCH_1X2":
        table = {"HOME": I > J, "DRAW": I == J, "AWAY": I < J}
    elif code == "DOUBLE_CHANCE":
        table = {"1X": I >= J, "X2": I <= J, "12": I != J}
    elif code == "BTTS":
        table = {"YES": (I > 0) & (J > 0), "NO": (I == 0) | (J == 0)}
    elif code in ("TOTAL_GOALS", "TEAM_TOTAL_HOME", "TEAM_TOTAL_AWAY"):
        if line is None or abs(line * 2 - round(line * 2)) > 1e-9 or abs(line - round(line)) < 1e-9:
            raise UnsupportedMarket(f"{code}: only half-integer lines (x.5) are supported, got {line}")
        base = {"TOTAL_GOALS": total, "TEAM_TOTAL_HOME": I, "TEAM_TOTAL_AWAY": J}[code]
        table = {"OVER": base > line, "UNDER": base < line}
    elif code == "CORRECT_SCORE":
        try:
            h, a = (int(x) for x in sel.split("-"))
        except ValueError as e:
            raise UnsupportedMarket(f"bad correct score {sel}") from e
        if h >= GRID or a >= GRID:
            raise UnsupportedMarket("score outside grid")
        table = {sel: (I == h) & (J == a)}
    elif code in ("EURO_HANDICAP", "ASIAN_HANDICAP"):
        # line = handicap given to the home team. European: whole lines, three outcomes. Asian: only x.5 lines here
        # (whole and quarter lines refund stakes, which a single win probability cannot price).
        if line is None:
            raise UnsupportedMarket(f"{code}: missing line")
        whole = abs(line - round(line)) < 1e-9
        half = abs(line * 2 - round(line * 2)) < 1e-9 and not whole
        if (code == "EURO_HANDICAP" and not whole) or (code == "ASIAN_HANDICAP" and not half):
            raise UnsupportedMarket(f"{code}: line {line} not supported")
        adj = I + line
        table = {"HOME": adj > J, "DRAW": np.isclose(adj, J), "AWAY": adj < J}
        if code == "ASIAN_HANDICAP":
            table.pop("DRAW")
    elif code == "WINNING_MARGIN":
        # H2 = home by exactly 2, H3+ = home by 3 or more, A.. = away, D = score draw (goals), NG = 0-0
        table = {"D": (I == J) & (I > 0), "NG": (I == 0) & (J == 0), "DI": I == J}
        if sel[:1] in ("H", "A") and sel[1:].rstrip("+").isdigit():
            k = int(sel[1:].rstrip("+"))
            diff = (I - J) if sel[0] == "H" else (J - I)
            table[sel] = diff >= k if sel.endswith("+") else diff == k
    elif code in ("WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"):
        yes = (I > J) & (J == 0) if code == "WIN_TO_NIL_HOME" else (J > I) & (I == 0)
        table = {"YES": yes, "NO": ~yes}
    elif code == "ODD_EVEN":
        table = {"ODD": total % 2 == 1, "EVEN": total % 2 == 0}
    elif code in ("TEAM_ODD_EVEN_HOME", "TEAM_ODD_EVEN_AWAY"):
        side = I if code.endswith("HOME") else J
        table = {"ODD": side % 2 == 1, "EVEN": side % 2 == 0}
    elif code == "DRAW_NO_BET":
        table = {"HOME": I > J, "AWAY": I < J}  # a draw refunds the stake (void_probability)
    elif code in ("TOTAL_EXACT", "TEAM_EXACT_HOME", "TEAM_EXACT_AWAY"):
        base = {"TOTAL_EXACT": total, "TEAM_EXACT_HOME": I, "TEAM_EXACT_AWAY": J}[code]
        n = sel.rstrip("+")
        if not n.isdigit() or int(n) >= GRID - 1:
            raise UnsupportedMarket(f"{code}: bad selection {sel}")
        table = {sel: base >= int(n) if sel.endswith("+") else base == int(n)}
    else:
        raise UnsupportedMarket(f"{code}: needs the half-time grid")
    if sel not in table:
        raise UnsupportedMarket(f"{code}: unknown selection {sel}")
    return np.asarray(table[sel])


@lru_cache(maxsize=None)
def _mask(code: str, sel: str, line: float | None) -> np.ndarray:
    base, period = split_code(code)
    if period or base in HALF_SPAN or base in SEQUENCE:
        raise UnsupportedMarket(f"{code}: not a full-time grid market")
    out = np.asarray(_rule(base, sel, line, _I, _J), dtype=bool)
    out.setflags(write=False)
    return out


def _sign(d: np.ndarray) -> np.ndarray:
    return np.where(d > 0, "1", np.where(d < 0, "2", "X"))


@lru_cache(maxsize=None)
def _mask4(code: str, sel: str, line: float | None) -> np.ndarray:
    """Win condition over the half grid (h1, a1, h2, a2): half markets, both-halves markets and full-time markets (lifted)."""
    base, period = split_code(code)
    if base in SEQUENCE:
        raise UnsupportedMarket(f"{code}: order of goals is not on the half grid")
    if period == "H1":
        out = _rule(base, sel, line, _H1, _A1)
    elif period == "H2":
        out = _rule(base, sel, line, _H2, _A2)
    elif period:
        raise UnsupportedMarket(f"{code}: unknown period")
    elif base == "HT_FT":
        if sel not in REGISTRY[base].selections:
            raise UnsupportedMarket(f"HT_FT: unknown selection {sel}")
        ht, ft = sel.split("/")
        out = (_sign(_H1 - _A1) == ht) & (_sign(_H1 + _H2 - _A1 - _A2) == ft)
    elif base.startswith("HIGHEST_HALF"):
        g1, g2 = {"HIGHEST_HALF": (_H1 + _A1, _H2 + _A2), "HIGHEST_HALF_HOME": (_H1, _H2), "HIGHEST_HALF_AWAY": (_A1, _A2)}[base]
        table = {"1ST": g1 > g2, "EQUAL": g1 == g2, "2ND": g1 < g2}
        if sel not in table:
            raise UnsupportedMarket(f"{code}: unknown selection {sel}")
        out = table[sel]
    elif base in HALF_SPAN:
        home = base.endswith("HOME")
        f1, f2 = (_H1, _H2) if home else (_A1, _A2)
        o1, o2 = (_A1, _A2) if home else (_H1, _H2)
        yes = {"SCORE_BOTH_HALVES": (f1 > 0) & (f2 > 0), "WIN_BOTH_HALVES": (f1 > o1) & (f2 > o2),
               "WIN_EITHER_HALF": (f1 > o1) | (f2 > o2)}[base.rsplit("_", 1)[0]]
        if sel not in ("YES", "NO"):
            raise UnsupportedMarket(f"{code}: unknown selection {sel}")
        out = yes if sel == "YES" else ~yes
    else:
        out = _rule(base, sel, line, np.minimum(_H1 + _H2, MAX_GOALS), np.minimum(_A1 + _A2, MAX_GOALS))
    out = np.asarray(out, dtype=bool)
    out.setflags(write=False)
    return out


def _void4(code: str) -> np.ndarray:
    _, period = split_code(code)
    i, j = {"H1": (_H1, _A1), "H2": (_H2, _A2)}.get(period or "", (_H1 + _H2, _A1 + _A2))
    return i == j


def _lambdas(matrix: np.ndarray) -> tuple[float, float]:
    g = np.arange(matrix.shape[0])
    return float(matrix.sum(axis=1) @ g), float(matrix.sum(axis=0) @ g)


@lru_cache(maxsize=4096)
def _halves(lh: float, la: float, share: float) -> np.ndarray:
    from scipy.stats import poisson
    g = np.arange(HG)
    p = [poisson.pmf(g, lam) for lam in (lh * share, la * share, lh * (1 - share), la * (1 - share))]
    d = np.einsum("a,b,c,d->abcd", *p)
    d = d / d.sum()
    d.setflags(write=False)
    return d


def half_grid(matrix: np.ndarray, share: float = FIRST_HALF_SHARE) -> np.ndarray:
    """Joint distribution of (home 1st half, away 1st half, home 2nd half, away 2nd half) with the matrix's expected goals."""
    lh, la = _lambdas(matrix)
    return _halves(round(lh, 6), round(la, 6), share)


def _needs_halves(code: str) -> bool:
    base, period = split_code(code)
    return bool(period) or base in HALF_SPAN


def needs_half_time(code: str) -> bool:
    """Settling it needs the half-time score."""
    return _needs_halves(code)


def needs_goal_order(code: str) -> bool:
    """Settling it needs the order of the goals (first / last goal)."""
    return split_code(code)[0] in SEQUENCE


def sequence_outcome(home_goals: int, away_goals: int, ref: SelectionRef, first: tuple[float | None, float | None] | None,
                     last: tuple[float | None, float | None] | None) -> bool:
    """FIRST_GOAL / LAST_GOAL from the minute of each team's first and last goal (None: the team did not score).
    UnsupportedMarket when the minutes are missing or tie."""
    base = split_code(ref.market_code)[0]
    if home_goals == away_goals == 0:
        return ref.selection == "NONE"
    mins = first if base == "FIRST_GOAL" else last
    if not mins:
        raise UnsupportedMarket(f"{ref.market_code}: needs the order of the goals")
    h, a = mins
    if (h is None) != (home_goals == 0) or (a is None) != (away_goals == 0) or (h is not None and h == a):
        raise UnsupportedMarket(f"{ref.market_code}: goal minutes do not match the score")
    if h is None or a is None:
        side = "HOME" if a is None else "AWAY"
    elif base == "FIRST_GOAL":
        side = "HOME" if h < a else "AWAY"
    else:
        side = "HOME" if h > a else "AWAY"
    return ref.selection == side


def _sequence(matrix: np.ndarray, ref: SelectionRef) -> float:
    """First/last goal: with constant scoring rates each goal is the home team's with probability lh/(lh+la)."""
    if ref.selection not in ("HOME", "NONE", "AWAY"):
        raise UnsupportedMarket(f"{ref.market_code}: unknown selection {ref.selection}")
    p0 = float(matrix[0, 0])
    if ref.selection == "NONE":
        return p0
    lh, la = _lambdas(matrix)
    share = lh / (lh + la) if lh + la > 0 else 0.5
    return (1 - p0) * (share if ref.selection == "HOME" else 1 - share)





def mask(ref: SelectionRef) -> np.ndarray:
    return _mask(ref.market_code, ref.selection, ref.line)


def void_probability(matrix: np.ndarray, ref: SelectionRef) -> float:
    """Probability that the stake is refunded (draw-no-bet markets on a draw); 0 for every other market."""
    base, _ = split_code(ref.market_code)
    if base not in VOID_ON_DRAW:
        return 0.0
    if _needs_halves(ref.market_code):
        return float(half_grid(matrix)[_void4(ref.market_code)].sum())
    return float(np.trace(matrix))


def probability(matrix: np.ndarray, ref: SelectionRef) -> float:
    """Win probability; for refund markets the win probability given no refund (what their devigged prices express)."""
    base, _ = split_code(ref.market_code)
    if base in SEQUENCE:
        return _sequence(matrix, ref)
    if _needs_halves(ref.market_code):
        p = float(half_grid(matrix)[_mask4(ref.market_code, ref.selection, ref.line)].sum())
    else:
        p = float(matrix[mask(ref)].sum())
    if base in VOID_ON_DRAW:
        v = void_probability(matrix, ref)
        p = p / (1 - v) if v < 1 else 0.0
    return p


def joint_probability(matrix: np.ndarray, refs: list[SelectionRef]) -> float:
    """Exact joint probability of several goal-based legs of the SAME match (half markets on the half grid)."""
    if any(split_code(r.market_code)[0] in SEQUENCE | VOID_ON_DRAW for r in refs):
        raise UnsupportedMarket("combinazione con primo/ultimo goal o draw no bet non supportata")
    if any(_needs_halves(r.market_code) for r in refs):
        m4 = np.ones(_H1.shape, dtype=bool)
        for r in refs:
            m4 = m4 & _mask4(r.market_code, r.selection, r.line)
        return float(half_grid(matrix)[m4].sum())
    m = np.ones((GRID, GRID), dtype=bool)
    for r in refs:
        m = m & mask(r)
    return float(matrix[m].sum())


def void_outcome(home_goals: int, away_goals: int, ref: SelectionRef, half_time: tuple[int, int] | None = None) -> bool:
    """True when a draw-no-bet selection is refunded."""
    base, period = split_code(ref.market_code)
    if base not in VOID_ON_DRAW:
        return False
    if period and half_time is None:
        raise UnsupportedMarket(f"{ref.market_code}: needs the half-time score")
    if period == "H1":
        return half_time[0] == half_time[1]
    if period == "H2":
        return home_goals - half_time[0] == away_goals - half_time[1]
    return home_goals == away_goals


def outcome(home_goals: int, away_goals: int, ref: SelectionRef, half_time: tuple[int, int] | None = None) -> bool:
    """Settles a selection. Half markets need the half-time score; first/last goal and refunds cannot be settled from scores
    (UnsupportedMarket: callers skip them)."""
    base, _ = split_code(ref.market_code)
    if base in SEQUENCE:
        raise UnsupportedMarket(f"{ref.market_code}: needs the order of the goals")
    if void_outcome(home_goals, away_goals, ref, half_time):
        raise UnsupportedMarket(f"{ref.market_code}: stake refunded")
    if _needs_halves(ref.market_code):
        if half_time is None:
            raise UnsupportedMarket(f"{ref.market_code}: needs the half-time score")
        h1, a1 = half_time
        h2, a2 = home_goals - h1, away_goals - a1
        if min(h1, a1, h2, a2) < 0:
            raise ValueError("half-time score above the final score")
        idx = tuple(min(x, HG - 1) for x in (h1, a1, h2, a2))
        return bool(_mask4(ref.market_code, ref.selection, ref.line)[idx])
    return bool(mask(ref)[min(home_goals, MAX_GOALS), min(away_goals, MAX_GOALS)])


_PERIOD_LABEL = {"H1": "1° tempo", "H2": "2° tempo"}
_TEAM = {"HOME": "casa", "AWAY": "ospite"}


def describe(ref: SelectionRef) -> str:
    base, period = split_code(ref.market_code)
    text = _describe(SelectionRef(market_code=base, selection=ref.selection, line=ref.line))
    return f"{text} · {_PERIOD_LABEL[period]}" if period else text


def _describe(ref: SelectionRef) -> str:
    c, s, ln = ref.market_code, ref.selection, ref.line
    if c in STAT_MARKETS:
        what = "corner" if c.startswith("CORNERS") else "cartellini"
        kind = STAT_MARKETS[c][1]
        if kind == "MATCH_1X2":
            return f"1X2 {what}: " + {"HOME": "1 (casa)", "DRAW": "X (pari)", "AWAY": "2 (ospite)"}[s]
        who = {"TOTAL_GOALS": "totale", "TEAM_TOTAL_HOME": "squadra casa", "TEAM_TOTAL_AWAY": "squadra ospite"}[kind]
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} {what} {who}"
    if c == "DRAW_NO_BET":
        return f"Draw no bet: {'1 (casa)' if s == 'HOME' else '2 (ospite)'}"
    if c in ("TEAM_ODD_EVEN_HOME", "TEAM_ODD_EVEN_AWAY"):
        return f"Goal {_TEAM[c.rsplit('_', 1)[1]]} {'dispari' if s == 'ODD' else 'pari'}"
    if c == "HT_FT":
        return f"Parziale/Finale {s}"
    if c.startswith("HIGHEST_HALF"):
        who = {"HIGHEST_HALF": "", "HIGHEST_HALF_HOME": " (goal casa)", "HIGHEST_HALF_AWAY": " (goal ospite)"}[c]
        return f"Tempo con più goal{who}: " + {"1ST": "1° tempo", "EQUAL": "uguale", "2ND": "2° tempo"}[s]
    if c in HALF_SPAN:
        kind, team = c.rsplit("_", 1)
        what = {"SCORE_BOTH_HALVES": "segna in entrambi i tempi", "WIN_BOTH_HALVES": "vince entrambi i tempi",
                "WIN_EITHER_HALF": "vince almeno un tempo"}[kind]
        return f"{_TEAM[team].capitalize()} {what}: {'Sì' if s == 'YES' else 'No'}"
    if c in SEQUENCE:
        first = "Primo" if c == "FIRST_GOAL" else "Ultimo"
        return f"{first} goal: " + {"HOME": "casa", "AWAY": "ospite", "NONE": "nessun goal"}[s]
    it = {
        "MATCH_1X2": {"HOME": "1 (casa)", "DRAW": "X (pareggio)", "AWAY": "2 (ospite)"},
        "BTTS": {"YES": "Goal (entrambe segnano)", "NO": "NoGoal"},
    }
    if c in it:
        return f"{'1X2' if c == 'MATCH_1X2' else 'BTTS'}: {it[c][s]}"
    if c == "TOTAL_GOALS":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} (totale goal)"
    if c == "TEAM_TOTAL_HOME":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} goal squadra casa"
    if c == "TEAM_TOTAL_AWAY":
        return f"{'Over' if s == 'OVER' else 'Under'} {ln} goal squadra ospite"
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
            return {"D": "Margine: pareggio con goal", "NG": "Margine: 0-0", "DI": "Margine: pareggio"}[s]
        return f"Margine: {'casa' if s[0] == 'H' else 'ospite'} vince di {s[1:].replace('+', ' o più')}"
    if c in ("WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"):
        return f"{'Casa' if c.endswith('HOME') else 'Ospite'} vince a zero: {'Sì' if s == 'YES' else 'No'}"
    if c == "ODD_EVEN":
        return f"Totale goal {'dispari' if s == 'ODD' else 'pari'}"
    if c == "TOTAL_EXACT":
        return f"Goal totali: {s.replace('+', ' o più')}"
    if c in ("TEAM_EXACT_HOME", "TEAM_EXACT_AWAY"):
        return f"Goal {'casa' if c.endswith('HOME') else 'ospite'}: {s.replace('+', ' o più')}"
    return f"{c} {s} {ln if ln is not None else ''}".strip()


def group_key(ref: SelectionRef) -> tuple[str, float | None]:
    return (ref.market_code, ref.line)


def complete_group(market_code: str) -> tuple[str, ...] | None:
    spec = REGISTRY.get(split_code(market_code)[0])
    return spec.selections if spec and spec.exclusive_complete else None
