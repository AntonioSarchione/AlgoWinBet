"""Model quality report (Fase 3): a weekly walk-forward replay of every finished match in the window, stored on Turso for
the "Qualità del modello" page.

For each match:
  model ...... fitted on the results known at 00:00 UTC of match day (nothing from that day or later)
  market ..... the reference price observable 2 hours before kickoff (Pinnacle, else Betfair Exchange, else the market
               average; margin removed), as the analysis would have seen it
  ensemble ... model and market blended as the live analysis does (adaptive weight)
  closing .... the closing price of the same reference book: the benchmark, and the price CLV is measured against
Scores per family (1X2, Over/Under 2.5, Goal/NoGoal) and group (leagues, cups, national teams): log loss, Brier, calibration.
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
from .meta import CALIB_METHODS, FAMILY_SELECTIONS, MIN_N, OWN_ONLY, Pool, describe, fit_all, fit_calibrator, fit_pool
from .modeleval import _Frozen, group_of
from .pricing import devig

REFERENCE = ("pinnacle", "betfair-ex", "market-avg")
PLAYABLE = ("sisal", "market-avg")  # what a bet would have been placed at: Sisal when we have it, else the average price
DECISION = timedelta(hours=2)
FAMILIES = {
    "1X2": [("MATCH_1X2", s, None) for s in ("HOME", "DRAW", "AWAY")],
    "U/O 2.5": [("TOTAL_GOALS", s, 2.5) for s in ("OVER", "UNDER")],
    "Goal/NoGoal": [("BTTS", s, None) for s in ("YES", "NO")],
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


def _brand(book: str) -> str:
    """OddsPapi slugs carry the country ("sisal.it"): compare brands."""
    b = book.lower()
    for suffix in (".it", "-it"):
        if b.endswith(suffix):
            return b[: -len(suffix)]
    return b


SANE = 0.12  # a reference price this far (in probability) from the market average is a feed error, not information


def _price(qs: list[tuple], family: list[tuple], books: tuple[str, ...], until: datetime | None, closing: bool,
           fair: bool, anchor: list[float] | None = None) -> tuple[list[float], str] | None:
    """Price of a whole family at one book: latest quote of each selection observed by `until` (closing=True: the
    closing quote, else the latest before kickoff). fair=True removes the margin. First book with a complete set wins;
    with `anchor` (the market average, fair), a book whose fair price strays more than SANE from it is skipped (the
    season files carry some exchange prices taken in play)."""
    for book in books:
        sel_odds = []
        for code, sel, line in family:
            cand = [q for q in qs if q[0] == code and q[1] == sel and (q[2] or None) == line and _brand(q[3]) == book
                    and (q[6] == "close" if closing else (q[6] != "close" and (until is None or q[5] <= until)))]
            if not cand:
                break
            sel_odds.append(max(cand, key=lambda q: q[5])[4])
        else:
            if fair:
                p = devig({str(k): o for k, o in enumerate(sel_odds)})
                pf = [p[str(k)] for k in range(len(sel_odds))]
                if anchor and book != "market-avg" and max(abs(a - b) for a, b in zip(pf, anchor)) > SANE:
                    continue
                return pf, book
            return sel_odds, book
    return None


MOVE = timedelta(hours=24)  # price movement: reference price at the decision time against the same book 24 hours earlier


def _monday(t: datetime) -> datetime:
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _nested_meta(samples: list[dict], min_n: int = MIN_N) -> None:
    """Walk-forward meta-model: each week is predicted by pools fitted on the earlier weeks only (row["meta"]; the same
    with the price movement, row["meta_mov"]). Weeks before MIN_N earlier matches get no meta probability."""
    weeks = sorted({_monday(s["kickoff"]) for s in samples})
    for w in weeks:
        past = [s for s in samples if s["kickoff"] < w]
        now = [s for s in samples if _monday(s["kickoff"]) == w]
        if len(past) < min_n:
            continue
        for fam in FAMILY_SELECTIONS:
            fp = [s for s in past if s["fam"] == fam]
            fn = [s for s in now if s["fam"] == fam]
            if not fn:
                continue
            for kind in ("pool", "calib"):
                for g in sorted({s["group"] for s in fn}):
                    rows = [s for s in fp if s["group"] == g and (kind == "calib" or "market" in s)]
                    if len(rows) < min_n and g not in OWN_ONLY:
                        rows = [s for s in fp if kind == "calib" or "market" in s]
                    if len(rows) < min_n:
                        continue
                    pm = np.array([r["model"] for r in rows])
                    y = np.array([r["k"] for r in rows])
                    if kind == "pool":
                        pk = np.array([r["market"] for r in rows])
                        pool = fit_pool(pm, pk, y, kind)
                        mov = fit_pool(pm, pk, y, kind, np.array([r.get("mov", [0.0] * pm.shape[1]) for r in rows]))
                        for s in fn:
                            if s["group"] == g and "market" in s:
                                s["meta"] = pool.apply(s["model"], s["market"])
                                s["meta_mov"] = mov.apply(s["model"], s["market"], s.get("mov"))
                        continue
                    cals = {m: fit_calibrator(pm, y, m) for m in CALIB_METHODS}
                    for s in fn:
                        if s["group"] == g and "market" not in s:
                            s["cal"] = {m: c.apply(s["model"]) for m, c in cals.items()}


SHRINK_PRIOR, SHRINK_WEIGHT = 0.5, 50  # with few picks the share stays near one half (worth 50 picks)
SHRINK_DRAWS = 2000  # resamples for the interval


def _shrink_fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float | None]:
    """(lambda, raw slope) of edge_shrink for edges x and outcomes over the market y."""
    sxx, sxy = float(x @ x), float(x @ y)
    if sxx <= 0:
        return SHRINK_PRIOR, None
    mean_sq = sxx / len(x)
    lam = (sxy + SHRINK_PRIOR * SHRINK_WEIGHT * mean_sq) / (sxx + SHRINK_WEIGHT * mean_sq)
    return float(min(1.0, max(0.0, lam))), sxy / sxx


def edge_shrink(bets: list[dict], draws: int = SHRINK_DRAWS) -> dict:
    """Share of the predicted edge over the market price that the picked selections actually earned: least squares of
    (won - p_market) on (p - p_market) through the origin, pulled toward SHRINK_PRIOR, clipped to [0, 1]. Picking the
    best edges among thousands inflates them, so the share is usually well below 1. "ci" / "raw_ci": 90% interval of the
    share and of the raw slope from the picks resampled (fixed seed, so a rerun on the same picks gives the same interval);
    only shown, the analysis uses "lambda"."""
    d = [(b["p"] - b["pm"], (1.0 if b["won"] else 0.0) - b["pm"]) for b in bets if b.get("pm") is not None]
    if not d:
        return {"lambda": 1.0, "raw": None, "n": 0}
    x, y = np.array(d).T
    lam, raw = _shrink_fit(x, y)
    out = {"lambda": lam, "raw": raw, "n": len(d)}
    if len(d) >= 2 and draws:
        idx = np.random.default_rng(0).integers(0, len(d), size=(draws, len(d)))
        fits = [_shrink_fit(x[i], y[i]) for i in idx]
        lams = [f[0] for f in fits]
        raws = [f[1] for f in fits if f[1] is not None]
        out["ci"] = [float(np.quantile(lams, 0.05)), float(np.quantile(lams, 0.95))]
        if raws:
            out["raw_ci"] = [float(np.quantile(raws, 0.05)), float(np.quantile(raws, 0.95))]
    return out


def _sisal_vs_sharp(samples: list[dict]) -> dict:
    """How Sisal prices compare with the sharp fair price 2 hours before kickoff, per family: Sisal's margin, how often a
    Sisal price is at or above the fair one (EV >= 0 against Pinnacle / Betfair Exchange) and by how much."""
    out = {}
    for fam in FAMILIES:
        rows = [s for s in samples if s["fam"] == fam and s.get("book") == "sisal" and s.get("ref_book") in ("pinnacle", "betfair-ex")]
        evs = [o * p - 1 for s in rows for o, p in zip(s["odds"], s["market"])]
        if not evs:
            continue
        out[fam] = {"n_matches": len(rows), "n": len(evs), "margin": float(np.mean([sum(1 / o for o in s["odds"]) - 1 for s in rows])),
                    "share_fair": float(np.mean([e >= 0 for e in evs])), "share_value": float(np.mean([e >= 0.02 for e in evs])),
                    "mean_ev": float(np.mean(evs)), "best_ev": float(max(evs))}
    return out


def run_quality(provider, cfg: Config, start: datetime, end: datetime, min_n: int = MIN_N) -> dict:
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
    samples: list[dict] = []
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
            avg_now = _price(qs, sels, ("market-avg",), r.kickoff - DECISION, closing=False, fair=True)
            avg_close = _price(qs, sels, ("market-avg",), None, closing=True, fair=True)
            mkt = _price(qs, sels, REFERENCE, r.kickoff - DECISION, closing=False, fair=True, anchor=avg_now[0] if avg_now else None)
            close = _price(qs, sels, REFERENCE, None, closing=True, fair=True, anchor=avg_close[0] if avg_close else None)
            row = {"k": k, "model": p["attuale"], "v1": p["v1"], "kickoff": r.kickoff, "group": group, "fam": fam,
                   "match": f"{r.home}-{r.away} {r.kickoff:%d/%m/%y}"}
            if mkt:
                pm = mkt[0]
                ens = []
                for ps, pq in zip(p["attuale"], pm):
                    s2 = max(ps * (1 - ps), 1e-4) / n_eff
                    w = tau2 / (tau2 + s2)
                    ens.append(w * ps + (1 - w) * pq)
                tot = sum(ens)
                row["market"], row["ens"], row["ref_book"] = pm, [x / tot for x in ens], mkt[1]
                early = _price(qs, sels, (mkt[1],), r.kickoff - DECISION - MOVE, closing=False, fair=True)
                row["mov"] = [math.log(max(a, 1e-6) / max(b, 1e-6)) for a, b in zip(pm, early[0])] if early else [0.0] * len(pm)
                bet_odds = _price(qs, sels, PLAYABLE, r.kickoff - DECISION, closing=False, fair=False)
                if bet_odds:
                    row["odds"], row["book"] = bet_odds
            if close:
                row["close"] = close[0]
            samples.append(row)
    _nested_meta(samples, min_n)
    # model-only calibration: per family, the method with the lowest out-of-sample log loss on the matches without a price
    calib_method: dict[str, str] = {}
    calib_scores: dict[str, dict[str, float]] = {}
    for fam in FAMILIES:
        rows = [s for s in samples if s["fam"] == fam and "cal" in s]
        if rows:
            calib_scores[fam] = {m: float(np.mean([_ll(s["cal"][m], s["k"]) for s in rows])) for m in CALIB_METHODS}
            calib_scores[fam]["n"] = len(rows)
            calib_method[fam] = min(CALIB_METHODS, key=lambda m: calib_scores[fam][m])
    for s in samples:
        if "cal" in s:
            s["meta"] = s["cal"][calib_method[s["fam"]]]

    acc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    bets: dict[str, list[dict]] = defaultdict(list)
    monthly: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in samples:
        fam, group, k = row["fam"], row["group"], row["k"]
        for g in (group, "tutte"):
            acc[(g, fam)].append(row)
        if fam == "1X2" and group == "campionati" and "close" in row:
            mk = f"{row['kickoff']:%Y-%m}"
            monthly[mk]["model"].append(_ll(row["model"], k))
            monthly[mk]["close"].append(_ll(row["close"], k))
            if "meta" in row:
                monthly[mk]["meta"].append(_ll(row["meta"], k))
        # value test on the probability the live analysis bets on: the meta-model when fitted, else the adaptive ensemble
        live = row.get("meta") if "market" in row and "meta" in row else row.get("ens")
        if live is None or "odds" not in row:
            continue
        sels = FAMILIES[fam]
        for i, (pe, o) in enumerate(zip(live, row["odds"])):
            if pe < t.min_probability or pe * o - 1 < t.min_ev:
                continue
            won = i == k
            close = row.get("close")
            bets[fam].append({"ev": pe * o - 1, "pnl": (o - 1) if won else -1.0, "won": won, "book": row["book"], "p": pe, "pm": row["market"][i],
                              "clv": close[i] * o - 1 if close else None, "meta": "meta" in row,
                              "label": f"{row['match']} {sels[i][1]} @{o:.2f} p={pe:.3f} mkt={row['market'][i]:.3f} "
                                       f"mod={row['model'][i]:.3f} prezzi {row['odds']}"})
    report = {"groups": {}, "calibration": {}, "value": {}, "monthly": []}
    for (g, fam), rows in sorted(acc.items()):
        out = {"n": len(rows)}
        for key in ("model", "v1", "ens", "meta", "meta_mov", "market", "close"):
            sub = [x for x in rows if key in x]
            if not sub:
                continue
            out[f"n_{key}"] = len(sub)
            out[f"ll_{key}"] = float(np.mean([_ll(x[key], x["k"]) for x in sub]))
            out[f"brier_{key}"] = float(np.mean([_brier(x[key], x["k"]) for x in sub]))
        both = [x for x in rows if "close" in x and "ens" in x and "meta" in x]  # like for like: the matches with every price
        if both:
            out["n_same"] = len(both)
            for key in ("model", "ens", "meta", "meta_mov", "close"):
                out[f"ll_{key}_same"] = float(np.mean([_ll(x[key], x["k"]) for x in both]))
        alone = [x for x in rows if "market" not in x and "meta" in x]  # model only: calibration without a market price
        if alone:
            out["n_alone"] = len(alone)
            out["ll_model_alone"] = float(np.mean([_ll(x["model"], x["k"]) for x in alone]))
            out["ll_meta_alone"] = float(np.mean([_ll(x["meta"], x["k"]) for x in alone]))
            for m in CALIB_METHODS:
                out[f"ll_{m}_alone"] = float(np.mean([_ll(x["cal"][m], x["k"]) for x in alone]))
        report["groups"].setdefault(g, {})[fam] = out
        if g == "tutte":
            cal = {}
            for key in ("model", "ens", "meta", "close"):
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
            "n_meta": sum(b["meta"] for b in bs),
            "books": {k: sum(b["book"] == k for b in bs) for k in PLAYABLE},
            "examples": [b["label"] for b in sorted(bs, key=lambda b: -b["ev"])[:8]],
        }
    report["monthly"] = [{"month": m, "n": len(v["model"]), "ll_model": float(np.mean(v["model"])), "ll_close": float(np.mean(v["close"])),
                          **({"ll_meta": float(np.mean(v["meta"])), "n_meta": len(v["meta"])} if v.get("meta") else {})}
                         for m, v in sorted(monthly.items())]
    report["sisal_vs_sharp"] = _sisal_vs_sharp(samples)
    report["edge_shrink"] = edge_shrink([b for bs in bets.values() for b in bs])
    params = fit_all(samples, min_n, calib_method)  # what the live analysis will use: fitted on every match of the window
    report["calib_methods"] = {fam: {**sc, "chosen": calib_method[fam]} for fam, sc in calib_scores.items()}
    report["meta"] = {key: {**v, "text": describe(Pool(**v), key.split("|")[1])} for key, v in params.items()}
    report["thresholds"] = {"min_ev": t.min_ev, "min_probability": t.min_probability, "market_prior_sd": cfg.ensemble.market_prior_sd}
    report["n_matches"] = sum(1 for _ in acc.get(("tutte", "1X2"), []))
    report["seconds"] = time.monotonic() - t_start
    if report["edge_shrink"]["n"]:
        params["_shrink"] = report["edge_shrink"]
    report["_params"] = params
    return report


def save_quality(store, report: dict, start: datetime, end: datetime, model_version: str) -> int:
    store.db.executescript(SCHEMA)
    cur = store.db.execute(
        "INSERT INTO quality_runs(created_at,window_start,window_end,model_version,n_matches,seconds,report) VALUES(?,?,?,?,?,?,?) RETURNING id",
        (datetime.now(timezone.utc).isoformat(), start.isoformat(), end.isoformat(), model_version, report["n_matches"],
         round(report["seconds"], 1), json.dumps({k: v for k, v in report.items() if not k.startswith("_")}, separators=(",", ":"))))
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
                    f"meta {m['ll_meta_same']:.4f} meta+movimento {m['ll_meta_mov_same']:.4f} chiusura {m['ll_close_same']:.4f}") if m.get("n_same") else ""
            alone = (f" | senza quote n={m['n_alone']}: modello {m['ll_model_alone']:.4f} calibrato {m['ll_meta_alone']:.4f}"
                     if m.get("n_alone") else "")
            print(f"  {fam:<10} n={m['n']:<5} LL modello {m['ll_model']:.4f} (v1 {m['ll_v1']:.4f})" + same + alone)
    es = report.get("edge_shrink") or {}
    if es.get("n"):
        raw = f"{es['raw']:.2f}" if es.get("raw") is not None else "–"
        ci = (f", intervallo 90% {es['ci'][0]:.2f}–{es['ci'][1]:.2f}" if es.get("ci") else "") + \
             (f", grezza {es['raw_ci'][0]:.2f}–{es['raw_ci'][1]:.2f}" if es.get("raw_ci") else "")
        print(f"\nEV prudente: quota del vantaggio sul mercato confermata dai risultati {es['lambda']:.2f} (grezza {raw}, {es['n']} giocate{ci})")
    if report.get("sisal_vs_sharp"):
        print("\nSisal contro prezzo equo Pinnacle (2 ore prima del calcio d'inizio):")
        for fam, v in report["sisal_vs_sharp"].items():
            print(f"  {fam:<10} partite {v['n_matches']}, margine Sisal {v['margin']:.1%}, quote >= equa {v['share_fair']:.1%}, "
                  f">= +2% {v['share_value']:.1%}, EV medio {v['mean_ev']:+.1%}, migliore {v['best_ev']:+.1%}")
    if report.get("calib_methods"):
        print("\nCalibrazione senza quote (log loss fuori campione, più basso = meglio):")
        for fam, sc in report["calib_methods"].items():
            print(f"  {fam:<10} n={sc['n']:<5} " + " · ".join(f"{m} {sc[m]:.4f}" for m in CALIB_METHODS) + f" -> scelta: {sc['chosen']}")
    print("\nMeta-modello (adattato su tutta la finestra, usato dall'analisi live):")
    for key, v in report.get("meta", {}).items():
        print(f"  {key:<32} {v['text']}")
    print("\nTest del valore (1 unità per giocata, soglie live):")
    for fam, v in report["value"].items():
        clv = f"{v['mean_clv']:+.1%}" if v["mean_clv"] is not None else "—"
        print(f"  {fam:<10} giocate {v['n']}, vinte {v['hits']}, ROI {v['roi']:+.1%}, EV atteso {v['mean_ev']:+.1%}, CLV {clv} (n={v['n_clv']}) {v['books']}")
        for e in v.get("examples", []):
            print(f"      {e}")
