"""Walk-forward comparison of goal-model variants (Fase 2): every week the model is fitted only on results known before
Monday 00:00 UTC and predicts that week's matches. Scores are about probability quality, not betting:
  1X2 log loss and RPS, Over/Under 2.5 and Gol/NoGol log loss (lower = better),
and, where the season CSVs have them, the same 1X2 log loss of the closing price (Pinnacle, else Betfair Exchange, else
the market average, margin removed) on the same matches, as the reference a good model approaches.
"""
from __future__ import annotations

import copy
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import Config
from .domain import SelectionRef
from .engine import Engine
from .markets import probability

REFS = {k: SelectionRef(market_code=c, selection=s, line=l) for k, (c, s, l) in {
    "H": ("MATCH_1X2", "HOME", None), "D": ("MATCH_1X2", "DRAW", None), "A": ("MATCH_1X2", "AWAY", None),
    "O25": ("TOTAL_GOALS", "OVER", 2.5), "BTTS": ("BTTS", "YES", None)}.items()}
REFERENCE_BOOKS = ("pinnacle", "betfair-ex", "market-avg")

# variants compared by default: name -> ModelCfg fields changed from the current configuration
VARIANTS: dict[str, dict] = {
    "base": {},
    "campionati": {"l2_comp_home": 50.0, "l2_comp_mu": 20.0},
    "elo-club": {"club_elo_per_100": 0.10},
    "senza-elo-nazionali": {"nation_elo_per_100": 0.0},  # base has it on since 2026-10-01
    "senza-storico-nazionali": {"national_history_years": 0.0},  # base has 4 years since 2026-10-01
    "storico-nazionali-2": {"national_history_years": 2.0},
    "storico-nazionali-6": {"national_history_years": 6.0},
    "elo-nazionali-0.08": {"nation_elo_per_100": 0.08},
    "elo-nazionali-0.25": {"nation_elo_per_100": 0.25},
    "emivita-180": {"xi_half_life_days": 180.0},
    "emivita-540": {"xi_half_life_days": 540.0},
    "tutto": {"l2_comp_home": 50.0, "l2_comp_mu": 20.0, "club_elo_per_100": 0.10},
}


def group_of(competition: str) -> str:
    c = competition.lower()
    if any(k in c for k in ("champions", "europa league", "conference")):  # club cups, qualifying rounds included
        return "coppe"
    if any(k in c for k in ("nations league", "world cup", "uefa euro", "qualif", "friendl")):
        return "nazionali"
    return "campionati"


@dataclass
class Scores:
    n: int = 0
    ll_1x2: float = 0.0
    rps: float = 0.0
    ll_o25: float = 0.0
    ll_btts: float = 0.0
    n_ref: int = 0
    ll_1x2_on_ref: float = 0.0  # model, on the matches with a closing price
    ll_1x2_ref: float = 0.0     # closing price, same matches

    def add(self, p: dict[str, float], hg: int, ag: int, ref: tuple[float, float, float] | None) -> None:
        eps = 1e-12
        res = "H" if hg > ag else "D" if hg == ag else "A"
        ll = -math.log(max(p[res], eps))
        self.n += 1
        self.ll_1x2 += ll
        cum_p = np.cumsum([p["H"], p["D"], p["A"]])[:2]
        cum_y = np.cumsum([res == "H", res == "D", res == "A"])[:2]
        self.rps += float(np.sum((cum_p - cum_y) ** 2) / 2)
        over = hg + ag > 2.5
        self.ll_o25 += -math.log(max(p["O25"] if over else 1 - p["O25"], eps))
        btts = hg > 0 and ag > 0
        self.ll_btts += -math.log(max(p["BTTS"] if btts else 1 - p["BTTS"], eps))
        if ref:
            self.n_ref += 1
            self.ll_1x2_on_ref += ll
            self.ll_1x2_ref += -math.log(max(ref["HDA".index(res)], eps))

    def row(self) -> dict:
        n, r = max(self.n, 1), max(self.n_ref, 1)
        return {"n": self.n, "ll_1x2": self.ll_1x2 / n, "rps": self.rps / n, "ll_o25": self.ll_o25 / n, "ll_btts": self.ll_btts / n,
                "n_ref": self.n_ref, "ll_1x2_model_on_ref": self.ll_1x2_on_ref / r if self.n_ref else None,
                "ll_1x2_ref": self.ll_1x2_ref / r if self.n_ref else None}


@dataclass
class EvalReport:
    weeks: int = 0
    seconds: dict[str, float] = field(default_factory=dict)
    scores: dict[str, dict[str, dict]] = field(default_factory=dict)  # variant -> group -> metrics


def _closing_1x2(provider, fixture_id: str) -> tuple[float, float, float] | None:
    qs = [q for q in provider.get_quotes(fixture_id) if q.kind == "close" and q.market_code == "MATCH_1X2"]
    for book in REFERENCE_BOOKS:
        odds = {q.selection: q.odds for q in qs if q.bookmaker == book}
        if all(k in odds for k in ("HOME", "DRAW", "AWAY")):
            inv = np.array([1 / odds["HOME"], 1 / odds["DRAW"], 1 / odds["AWAY"]])
            return tuple(inv / inv.sum())
    return None


def _closing_all(provider, ids: set[str]) -> dict[str, tuple[float, float, float] | None]:
    """Closing 1X2 of every match in one query when the provider is the database (one round trip on Turso)."""
    store = getattr(provider, "store", None)
    if store is None:
        return {fid: _closing_1x2(provider, fid) for fid in ids}
    rows = store.db.execute("SELECT fixture_id, bookmaker, selection, odds FROM quotes WHERE kind='close' AND market_code='MATCH_1X2' "
                            "AND bookmaker IN (?,?,?)", REFERENCE_BOOKS).fetchall()
    by: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for fid, book, sel, odds in rows:
        if fid in ids:
            by[fid][book][sel] = odds
    out = {}
    for fid, books in by.items():
        for book in REFERENCE_BOOKS:
            o = books.get(book, {})
            if all(k in o for k in ("HOME", "DRAW", "AWAY")):
                inv = np.array([1 / o["HOME"], 1 / o["DRAW"], 1 / o["AWAY"]])
                out[fid] = tuple(inv / inv.sum())
                break
    return out


class _Frozen:
    """The provider with its history read once (Turso pays a round trip and thousands of rows per read)."""

    def __init__(self, provider, until: datetime):
        self.base = provider
        self.rows = list(provider.list_history(None, until))

    def list_history(self, competitions, until):
        return [r for r in self.rows if (not competitions or r.competition in competitions) and r.available_at <= until]

    def __getattr__(self, item):
        if item in ("base", "rows"):
            raise AttributeError(item)
        return getattr(self.base, item)


INTL_GROUP = "nazionali (tutte le partite)"


def _international_tests(provider, cfg: Config, finished: list, start: datetime, end: datetime) -> list[tuple]:
    """Extra test matches for national teams: every non-neutral international between two national sides of our
    competitions, played in the window and not already among our results. Predicted with our national competition's
    goal level, so every variant prices them the same way."""
    fn = getattr(provider, "international_results", None)
    body = fn() if fn else None
    if not body:
        return []
    from .elo import international_results, national_history
    from .names import TeamNames
    intl = international_results(body, TeamNames.load(cfg.model.aliases_path))
    ours = [r for r in provider.rows if group_of(r.competition) == "nazionali"]
    if not ours:
        return []
    comp = max({r.competition for r in ours}, key=lambda c: sum(r.competition == c for r in ours))
    nations = {t for r in ours for t in (r.home, r.away)}
    extra = [r for r in national_history(intl, provider.rows, nations, start, end + timedelta(days=7))
             if r.kickoff < end and r.home in nations and r.away in nations and not r.neutral]
    return [(r, comp, (INTL_GROUP,)) for r in extra]


def evaluate(provider, cfg: Config, start: datetime, end: datetime, variants: dict[str, dict] | None = None,
             with_reference: bool = True, intl_tests: bool = True) -> EvalReport:
    """Every variant predicts the same matches: a match is scored only when all variants can price it (a variant that
    knows more teams does not get a different test set)."""
    variants = variants or VARIANTS
    provider = _Frozen(provider, end)
    finished = [r for r in provider.rows if start <= r.kickoff < end]
    tests = [(r, r.competition, (group_of(r.competition), "tutte") + ((INTL_GROUP,) if group_of(r.competition) == "nazionali" else ()))
             for r in finished]
    if intl_tests:
        tests += _international_tests(provider, cfg, finished, start, end)
    weeks: dict[datetime, list] = defaultdict(list)
    for t in tests:
        k = t[0].kickoff
        weeks[(k - timedelta(days=k.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)].append(t)
    refs = _closing_all(provider, {r.fixture_id for r in finished}) if with_reference else {}
    engines = {}
    for name, changes in variants.items():
        c = copy.deepcopy(cfg)
        for k, v in changes.items():
            setattr(c.model, k, v)
        engines[name] = Engine(provider, c, use_lineups=False)
    rep = EvalReport(weeks=len(weeks))
    acc: dict[str, dict[str, Scores]] = {name: defaultdict(Scores) for name in engines}
    secs = {name: 0.0 for name in engines}
    for monday in sorted(weeks):
        for r, comp, groups in weeks[monday]:
            preds = {}
            for name, eng in engines.items():
                t0 = time.monotonic()
                fitted = eng.fit(comp, monday)
                if fitted is None or not (fitted[0].knows(r.home) and fitted[0].knows(r.away)):
                    break
                m = fitted[0].score_matrix(r.home, r.away)
                preds[name] = {k: float(probability(m, ref)) for k, ref in REFS.items()}
                secs[name] += time.monotonic() - t0
            if len(preds) < len(engines):
                continue
            ref = refs.get(r.fixture_id)
            for name, p in preds.items():
                for g in groups:
                    acc[name][g].add(p, r.home_goals, r.away_goals, ref)
    rep.seconds = secs
    rep.scores = {name: {g: s.row() for g, s in sorted(a.items())} for name, a in acc.items()}
    return rep


def print_report(rep: EvalReport) -> None:
    print(f"Confronto varianti del modello: {rep.weeks} settimane, previsione al lunedì 00:00 UTC con i soli risultati già noti")
    groups = sorted({g for v in rep.scores.values() for g in v})
    base = rep.scores.get("base", {})
    for g in groups:
        print(f"\n[{g}]  (più basso = meglio; diff = differenza rispetto a base)")
        print(f"  {'variante':<30}{'n':>6} {'LL 1X2':>8} {'diff':>8} {'RPS':>7} {'LL O2.5':>8} {'diff':>8} {'LL GG':>7} {'diff':>8}  {'LL chiusura (stesse partite)':>30}")
        for name, by in rep.scores.items():
            s = by.get(g)
            if not s:
                continue
            b = base.get(g) or s
            ref = (f"modello {s['ll_1x2_model_on_ref']:.4f} vs quota {s['ll_1x2_ref']:.4f} (n={s['n_ref']})" if s["n_ref"] else "—")
            print(f"  {name:<30}{s['n']:>6} {s['ll_1x2']:>8.4f} {s['ll_1x2'] - b['ll_1x2']:>+8.4f} {s['rps']:>7.4f} "
                  f"{s['ll_o25']:>8.4f} {s['ll_o25'] - b['ll_o25']:>+8.4f} {s['ll_btts']:>7.4f} {s['ll_btts'] - b['ll_btts']:>+8.4f}  {ref}")
    print("\nTempi: " + ", ".join(f"{k} {v:.0f}s" for k, v in rep.seconds.items()))


def default_window(now: datetime | None = None, weeks: int = 52) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    end = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(weeks=weeks), end
