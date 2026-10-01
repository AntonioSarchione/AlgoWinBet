"""Model quality report (Fase 3): a weekly walk-forward replay of every finished match in the window, stored on Turso for
the "Qualità del modello" page.

For each match:
  model ...... fitted on the results known at 00:00 UTC of match day (nothing from that day or later)
  market ..... the reference price observable 2 hours before kickoff (Pinnacle, else Betfair Exchange, else the market
               average; margin removed), as the analysis would have seen it
  ensemble ... model and market blended as the live analysis does (adaptive weight)
  closing .... the closing price of the same reference book: the benchmark, and the price CLV is measured against
Scores per family (1X2, Over/Under 2.5, Gol/NoGol) and group (leagues, cups, national teams): log loss, Brier, calibration.
Value test: a flat 1-unit paper bet on every selection whose ensemble probability clears the live thresholds (EV and
minimum probability) at the bookmaker price of that moment (Sisal when stored, else the market average): hit rate, ROI and
CLV against the closing price.
"""
from __future__ import annotations

import copy
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .calibration import calibration_curve
from .config import Config
from .domain import SelectionRef
from .engine import Engine
from .markets import probability
from .modeleval import _Frozen, group_of
from .pricing import devig

REFERENCE = ("pinnacle", "betfair-ex", "market-avg")
PLAYABLE = ("sisal", "market-avg")  # what a bet would have been placed at: Sisal when we have it, else the average price
DECISION = timedelta(hours=2)
FAMILIES = {
    "1X2": [("MATCH_1X2", s, None) for s in ("HOME", "DRAW", "AWAY")],
    "U/O 2.5": [("TOTAL_GOALS", s, 2.5) for s in ("OVER", "UNDER")],
    "Gol/NoGol": [("BTTS", s, None) for s in ("YES", "NO")],
}
# the previous model (no national-team Elo or international history) shows what the Fase 2 changes bought
VERSIONS = {"attuale": {}, "v1": {"nation_elo_per_100": 0.0, "national_history_years": 0.0}}

SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_runs(id INTEGER PRIMARY KEY, created_at TEXT, window_start TEXT, window_end TEXT,
  model_version TEXT, n_matches INTEGER, seconds REAL, report TEXT);
"""


def _outcome(code: str, sel: str, hg: int, ag: int) -> bool:
    if code == "MATCH_1X2":
        return {"HOME": hg > ag, "DRAW": hg == ag, "AWAY": hg < ag}[sel]
    if code == "TOTAL_GOALS":
        return (hg + ag > 2.5) == (sel == "OVER")
    return (hg > 0 and ag > 0) == (sel == "YES")


@dataclass
class _Acc:
    """Per group and family: one row per match with the probabilities of every selection and the outcome index."""
    rows: list[dict] = field(default_factory=list)


def _ll(p: list[float], k: int) -> float:
    return -math.log(max(p[k], 1e-12))


def _brier(p: list[float], k: int) -> float:
    return float(sum((pi - (1.0 if i == k else 0.0)) ** 2 for i, pi in enumerate(p)))


def _quotes(provider, ids: set[str], canon: dict[str, str]) -> dict[str, list[tuple]]:
    """(market, selection, line, book, odds, observed_at, kind) per canonical fixture id, one query."""
    store = provider.store
    rows = store.db.execute(
        "SELECT fixture_id, market_code, selection, line, bookmaker, odds, observed_at, kind FROM quotes "
        "WHERE market_code IN ('MATCH_1X2','TOTAL_GOALS','BTTS') AND (line IS NULL OR line = 2.5)").fetchall()
    out: dict[str, list[tuple]] = defaultdict(list)
    for fid, code, sel, line, book, odds, at, kind in rows:
        c = canon.get(fid, fid)
        if c in ids:
            out[c].append((code, sel, line, book, odds, datetime.fromisoformat(at), kind))
    return out


def _price(qs: list[tuple], family: list[tuple], books: tuple[str, ...], until: datetime | None, closing: bool,
           fair: bool) -> tuple[list[float], str] | None:
    """Price of a whole family at one book: latest quote of each selection observed by `until` (closing=True: the
    closing quote, else the latest before kickoff). fair=True removes the margin. First book with a complete set wins."""
    for book in books:
        sel_odds = []
        for code, sel, line in family:
            cand = [q for q in qs if q[0] == code and q[1] == sel and (q[2] or None) == line and q[3] == book
                    and (q[6] == "close" if closing else (q[6] != "close" and (until is None or q[5] <= until)))]
            if not cand:
                break
            sel_odds.append(max(cand, key=lambda q: q[5])[4])
        else:
            if fair:
                p = devig({str(k): o for k, o in enumerate(sel_odds)})
                return [p[str(k)] for k in range(len(sel_odds))], book
            return sel_odds, book
    return None


def run_quality(provider, cfg: Config, start: datetime, end: datetime) -> dict:
    t_start = time.monotonic()
    frozen = _Frozen(provider, end)
    finished = [r for r in frozen.rows if start <= r.kickoff < end]
    canon = provider._canonical() if hasattr(provider, "_canonical") else {}
    quotes = _quotes(provider, {r.fixture_id for r in finished}, canon) if hasattr(provider, "store") else {}
    engines = {}
    for name, changes in VERSIONS.items():
        c = copy.deepcopy(cfg)
        for k, v in changes.items():
            setattr(c.model, k, v)
        engines[name] = Engine(frozen, c, use_lineups=False)
    tau2 = cfg.ensemble.market_prior_sd ** 2
    t = cfg.thresholds
    acc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    bets: dict[str, list[dict]] = defaultdict(list)
    monthly: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in sorted(finished, key=lambda r: r.kickoff):
        day = r.kickoff.replace(hour=0, minute=0, second=0, microsecond=0)
        mats = {}
        for name, eng in engines.items():
            fitted = eng.fit(r.competition, day)
            if fitted is None or not (fitted[0].knows(r.home) and fitted[0].knows(r.away)):
                break
            mats[name] = (fitted[0].score_matrix(r.home, r.away), fitted[0].sample_size(r.home, r.away))
        if len(mats) < len(engines):
            continue
        qs = quotes.get(r.fixture_id, [])
        group = group_of(r.competition)
        for fam, sels in FAMILIES.items():
            refs = [SelectionRef(market_code=c, selection=s, line=l) for c, s, l in sels]
            k = next(i for i, (c, s, _) in enumerate(sels) if _outcome(c, s, r.home_goals, r.away_goals))
            p = {name: [float(probability(m, ref)) for ref in refs] for name, (m, _) in mats.items()}
            n_eff = max(mats["attuale"][1], 3)
            mkt = _price(qs, sels, REFERENCE, r.kickoff - DECISION, closing=False, fair=True)
            close = _price(qs, sels, REFERENCE, None, closing=True, fair=True)
            row = {"k": k, "model": p["attuale"], "v1": p["v1"]}
            if mkt:
                pm = mkt[0]
                ens = []
                for ps, pq in zip(p["attuale"], pm):
                    s2 = max(ps * (1 - ps), 1e-4) / n_eff
                    w = tau2 / (tau2 + s2)
                    ens.append(w * ps + (1 - w) * pq)
                tot = sum(ens)
                row["market"], row["ens"] = pm, [x / tot for x in ens]
            if close:
                row["close"] = close[0]
            for g in (group, "tutte"):
                acc[(g, fam)].append(row)
            if fam == "1X2" and group == "campionati" and close:
                monthly[f"{r.kickoff:%Y-%m}"]["model"].append(_ll(p["attuale"], k))
                monthly[f"{r.kickoff:%Y-%m}"]["close"].append(_ll(close[0], k))
            # value test on the ensemble (the probability the live analysis bets on)
            if "ens" in row:
                bet_odds = _price(qs, sels, PLAYABLE, r.kickoff - DECISION, closing=False, fair=False)
                if bet_odds:
                    for i, (pe, o) in enumerate(zip(row["ens"], bet_odds[0])):
                        if pe < t.min_probability or pe * o - 1 < t.min_ev:
                            continue
                        won = i == k
                        bets[fam].append({"ev": pe * o - 1, "pnl": (o - 1) if won else -1.0, "won": won, "book": bet_odds[1],
                                          "clv": close[0][i] * o - 1 if close else None,
                                          "label": f"{r.home}-{r.away} {r.kickoff:%d/%m/%y} {sels[i][1]} @{o:.2f} p={pe:.3f} "
                                                   f"mkt={row['market'][i]:.3f} mod={p['attuale'][i]:.3f} prezzi {bet_odds[0]} ref={mkt[1]}"})
    report = {"groups": {}, "calibration": {}, "value": {}, "monthly": []}
    for (g, fam), rows in sorted(acc.items()):
        out = {"n": len(rows)}
        for key in ("model", "v1", "ens", "market", "close"):
            sub = [x for x in rows if key in x]
            if not sub:
                continue
            out[f"n_{key}"] = len(sub)
            out[f"ll_{key}"] = float(np.mean([_ll(x[key], x["k"]) for x in sub]))
            out[f"brier_{key}"] = float(np.mean([_brier(x[key], x["k"]) for x in sub]))
        both = [x for x in rows if "close" in x and "ens" in x]  # like for like: the matches with every price
        if both:
            out["n_same"] = len(both)
            for key in ("model", "ens", "close"):
                out[f"ll_{key}_same"] = float(np.mean([_ll(x[key], x["k"]) for x in both]))
        report["groups"].setdefault(g, {})[fam] = out
        if g == "tutte":
            cal = {}
            for key in ("model", "ens", "close"):
                ps, ys = [], []
                for x in rows:
                    if key in x:
                        ps += x[key]
                        ys += [1.0 if i == x["k"] else 0.0 for i in range(len(x[key]))]
                if ps:
                    cal[key] = [list(b) for b in calibration_curve(ps, ys, 10)]
            report["calibration"][fam] = cal
    for fam, bs in bets.items():
        clv = [b["clv"] for b in bs if b["clv"] is not None]
        report["value"][fam] = {
            "n": len(bs), "hits": sum(b["won"] for b in bs), "roi": float(np.mean([b["pnl"] for b in bs])),
            "mean_ev": float(np.mean([b["ev"] for b in bs])), "n_clv": len(clv), "mean_clv": float(np.mean(clv)) if clv else None,
            "books": {k: sum(b["book"] == k for b in bs) for k in PLAYABLE},
            "examples": [b["label"] for b in sorted(bs, key=lambda b: -b["ev"])[:8]],
        }
    report["monthly"] = [{"month": m, "n": len(v["model"]), "ll_model": float(np.mean(v["model"])), "ll_close": float(np.mean(v["close"]))}
                         for m, v in sorted(monthly.items())]
    report["thresholds"] = {"min_ev": t.min_ev, "min_probability": t.min_probability, "market_prior_sd": cfg.ensemble.market_prior_sd}
    report["n_matches"] = sum(1 for _ in acc.get(("tutte", "1X2"), []))
    report["seconds"] = time.monotonic() - t_start
    return report


def save_quality(store, report: dict, start: datetime, end: datetime, model_version: str) -> int:
    store.db.executescript(SCHEMA)
    cur = store.db.execute(
        "INSERT INTO quality_runs(created_at,window_start,window_end,model_version,n_matches,seconds,report) VALUES(?,?,?,?,?,?,?) RETURNING id",
        (datetime.now(timezone.utc).isoformat(), start.isoformat(), end.isoformat(), model_version, report["n_matches"],
         round(report["seconds"], 1), json.dumps(report, separators=(",", ":"))))
    rid = int(cur.fetchone()[0])
    store.db.execute("DELETE FROM quality_runs WHERE id NOT IN (SELECT id FROM quality_runs ORDER BY id DESC LIMIT 12)")
    store.db.commit()
    return rid


def print_quality(report: dict) -> None:
    print(f"Qualità del modello: {report['n_matches']} partite ({report['seconds']:.0f}s)")
    for g, fams in report["groups"].items():
        print(f"\n[{g}]")
        for fam, m in fams.items():
            same = (f" | stesse partite n={m['n_same']}: modello {m['ll_model_same']:.4f} ensemble {m['ll_ens_same']:.4f} "
                    f"chiusura {m['ll_close_same']:.4f}") if m.get("n_same") else ""
            print(f"  {fam:<10} n={m['n']:<5} LL modello {m['ll_model']:.4f} (v1 {m['ll_v1']:.4f})" + same)
    print("\nTest del valore (1 unità per giocata, soglie live):")
    for fam, v in report["value"].items():
        clv = f"{v['mean_clv']:+.1%}" if v["mean_clv"] is not None else "—"
        print(f"  {fam:<10} giocate {v['n']}, vinte {v['hits']}, ROI {v['roi']:+.1%}, EV atteso {v['mean_ev']:+.1%}, CLV {clv} (n={v['n_clv']}) {v['books']}")
        for e in v.get("examples", []):
            print(f"      {e}")
