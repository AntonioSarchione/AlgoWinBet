"""Walk-forward check of the corners / cards models (Fase 7), before they price anything: every Monday the model is fitted
on the matches known before it and predicts that week's matches with the statistic known. Scores (lower = better):
log loss of Over/Under at the usual lines and of the 1X2 of the statistic, against two baselines:
  media ...... the competition's average count for both sides (no teams), same dispersion
  poisson .... the team model without over-dispersion
plus the mean predicted vs actual total, so a level bias (e.g. how cards are counted) shows at once.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from .domain import SelectionRef
from .markets import stat_probability
from .models.counts import CountModel
from .state import history_at

LINES = {"corners": (8.5, 9.5, 10.5), "cards": (3.5, 4.5, 5.5)}


def _ll(p: float, hit: bool) -> float:
    return -math.log(max(p if hit else 1 - p, 1e-12))


def evaluate_stat(provider, stat: str, start: datetime, end: datetime, seasons: int = 2, half_life: float = 365.0,
                  min_rows: int = 300) -> dict:
    counts = provider.stat_counts(stat)
    xi = math.log(2) / half_life
    scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    level: dict[str, list[float]] = defaultdict(list)
    monday = start - timedelta(days=start.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    size = None
    while monday < end:
        nxt = monday + timedelta(days=7)
        hist = [r for r in history_at(provider, None, monday, seasons) if r.fixture_id in counts]
        rows = [r.model_copy(update={"home_goals": int(counts[r.fixture_id][0]), "away_goals": int(counts[r.fixture_id][1])}) for r in hist]
        tests = [r for r in provider.list_history(None, nxt + timedelta(hours=3)) if monday <= r.kickoff < nxt and r.fixture_id in counts]
        if len(rows) < min_rows or not tests:
            monday = nxt
            continue
        nb = CountModel(stat, xi=xi).fit(rows, monday)
        size = nb.size
        po = CountModel(stat, xi=xi).fit(rows, monday)
        po.size = math.inf
        avg: dict[str, tuple[float, float]] = {}
        for comp in {r.competition for r in rows}:
            rc = [r for r in rows if r.competition == comp]
            avg[comp] = (float(np.mean([r.home_goals for r in rc])), float(np.mean([r.away_goals for r in rc])))
        for r in tests:
            if not (nb.knows(r.home) and nb.knows(r.away)) or r.competition not in avg:
                continue
            h, a = counts[r.fixture_id]
            mats = {"modello": nb.matrix(r.home, r.away, r.competition), "poisson": po.matrix(r.home, r.away, r.competition)}
            mean_model = CountModel(stat)
            mean_model.size = nb.size
            mats["media"] = np.outer(mean_model._pmf(avg[r.competition][0]), mean_model._pmf(avg[r.competition][1]))
            lh, la = nb.expected(r.home, r.away, r.competition)
            level["previsto"].append(lh + la)
            level["accaduto"].append(h + a)
            for name, m in mats.items():
                for line in LINES[stat]:
                    p = stat_probability(m, SelectionRef(market_code=f"{stat.upper()}_TOTAL", selection="OVER", line=line))
                    scores[name][f"O{line}"].append(_ll(p, h + a > line))
                p1 = {s: stat_probability(m, SelectionRef(market_code=f"{stat.upper()}_1X2", selection=s)) for s in ("HOME", "DRAW", "AWAY")}
                res = "HOME" if h > a else "DRAW" if h == a else "AWAY"
                scores[name]["1X2"].append(-math.log(max(p1[res], 1e-12)))
        monday = nxt
    return {"scores": {k: {m: (float(np.mean(v)), len(v)) for m, v in d.items()} for k, d in scores.items()},
            "level": {k: float(np.mean(v)) for k, v in level.items()}, "size": size}


def print_stat_report(stat: str, rep: dict) -> None:
    print(f"[{stat}] log loss (più basso = meglio); dispersione NB size = {rep['size']}")
    names = ["modello", "poisson", "media"]
    cols = sorted({c for d in rep["scores"].values() for c in d})
    print("  " + "variante".ljust(10) + "".join(c.rjust(14) for c in cols))
    for n in names:
        d = rep["scores"].get(n, {})
        print("  " + n.ljust(10) + "".join((f"{d[c][0]:.4f} ({d[c][1]})" if c in d else "-").rjust(14) for c in cols))
    lv = rep["level"]
    if lv:
        print(f"  totale medio previsto {lv['previsto']:.2f} / accaduto {lv['accaduto']:.2f}")
