"""Market model: implied probabilities, overround removal, multi-bookmaker consensus, fair odds, edge, EV.
EV and edge are different quantities and are always reported separately (spec 12.3)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .domain import OddsQuote, SelectionRef
from .markets import complete_group

SOURCE_LEVEL_SCORE = {"A": 1.0, "B": 0.85, "C": 0.6, "D": 0.4, "E": 0.2}


def implied(odds: float) -> float:
    return 1.0 / odds


def overround(odds: list[float]) -> float:
    return sum(1.0 / o for o in odds) - 1.0


def devig(odds: dict[str, float], method: str = "power") -> dict[str, float]:
    """Remove the bookmaker margin from a complete mutually-exclusive set of odds."""
    q = {k: 1.0 / o for k, o in odds.items()}
    s = sum(q.values())
    if method == "proportional" or s <= 1.0:
        return {k: v / s for k, v in q.items()}
    if method == "power":  # find c>=1 with sum(q^c)=1 (shrinks longshots more than favourites)
        lo, hi = 1.0, 20.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if sum(v**mid for v in q.values()) > 1.0:
                lo = mid
            else:
                hi = mid
        c = (lo + hi) / 2
        p = {k: v**c for k, v in q.items()}
        t = sum(p.values())
        return {k: v / t for k, v in p.items()}
    raise ValueError(method)


def fair_odds(p: float) -> float:
    return 1.0 / p if p > 0 else math.inf


def ev(p: float, odds: float) -> float:
    return p * odds - 1.0


def edge(p_model: float, p_market: float) -> float:
    return p_model - p_market


@dataclass
class MarketView:
    ref: SelectionRef
    best_odds: float
    best_book: str
    best_observed_at: datetime
    n_books: int
    p_market: float | None  # devigged reference price (Pinnacle, else the book average); None when the set is not complete
    dispersion: float  # std of per-book devigged p (0 if single book)
    overround: float | None
    source_level: str
    per_book: dict[str, float] = field(default_factory=dict)


def latest_quotes(quotes: list[OddsQuote]) -> list[OddsQuote]:
    """Latest quote per (bookmaker, selection): the price observable at the cutoff."""
    best: dict[tuple[str, str], OddsQuote] = {}
    for q in quotes:
        k = (q.bookmaker, q.ref.key)
        if k not in best or q.observed_at > best[k].observed_at:
            best[k] = q
    return list(best.values())


REFERENCE_BOOKS = ("pinnacle", "betfair-ex")  # sharp prices, in order of preference: the benchmark Sisal is measured against
SANE = 0.12  # a reference price this far (in probability) from the other books is a feed error, not information


def reference_price(per_book: dict[str, float]) -> float | None:
    """Fair probability of a selection: the first reference book that prices it (and agrees with the other books within
    SANE), else the average over the books."""
    if not per_book:
        return None
    avg = float(np.mean(list(per_book.values())))
    for ref in REFERENCE_BOOKS:
        for book, p in per_book.items():
            if ref in book.lower() and abs(p - avg) <= SANE:
                return p
    return avg


def _bettable(book: str, bettable: list[str] | None) -> bool:
    return not bettable or any(b.lower() in book.lower() for b in bettable)


def build_market_views(quotes: list[OddsQuote], devig_method: str = "power", bettable: list[str] | None = None) -> list[MarketView]:
    """One view per selection. With `bettable` (e.g. ["sisal"]) a view exists only for selections those bookmakers price and its
    best odds come from them; the other books (Pinnacle) still feed the devigged market probability of those selections."""
    quotes = latest_quotes(quotes)
    by_ref: dict[str, list[OddsQuote]] = {}
    for q in quotes:
        by_ref.setdefault(q.ref.key, []).append(q)

    # per-book devig on complete groups
    book_groups: dict[tuple[str, str, float | None], dict[str, OddsQuote]] = {}
    for q in quotes:
        book_groups.setdefault((q.bookmaker, q.market_code, q.line), {})[q.selection] = q
    p_by_ref_book: dict[str, dict[str, float]] = {}
    ovr_by_ref: dict[str, list[float]] = {}
    for (book, code, line), sels in book_groups.items():
        need = complete_group(code)
        if not need or set(sels) != set(need):
            continue
        odds = {s: sels[s].odds for s in need}
        pdv = devig(odds, devig_method)
        o = overround(list(odds.values()))
        for s in need:
            p_by_ref_book.setdefault(sels[s].ref.key, {})[book] = pdv[s]
            ovr_by_ref.setdefault(sels[s].ref.key, []).append(o)

    views = []
    for key, qs in by_ref.items():
        playable = [q for q in qs if _bettable(q.bookmaker, bettable)]
        if not playable:
            continue  # nobody we can bet with prices it: not an option at all
        top = max(playable, key=lambda q: q.odds)
        per_book = p_by_ref_book.get(key, {})
        p_mkt = reference_price(per_book)
        disp = float(np.std(list(per_book.values()))) if len(per_book) > 1 else 0.0
        lvl = float(np.mean([SOURCE_LEVEL_SCORE.get(q.source_level, 0.5) for q in qs]))
        views.append(
            MarketView(
                ref=top.ref,
                best_odds=top.odds,
                best_book=top.bookmaker,
                best_observed_at=top.observed_at,
                n_books=len(qs),
                p_market=p_mkt,
                dispersion=disp,
                overround=float(np.mean(ovr_by_ref[key])) if key in ovr_by_ref else None,
                source_level=top.source_level,
                per_book=per_book,
            )
        )
        views[-1].source_level = "A" if lvl >= 0.97 else "B" if lvl >= 0.8 else "C"
    return views
