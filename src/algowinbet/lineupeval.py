"""Does the official XI improve the probabilities? (Fase 6-bis acceptance test)

Walk-forward over finished domestic matches with both confirmed XI stored: every week the team model and the player
impact model are fitted only on results and lineups known before that Monday; each match is then priced twice, without
and with its official XI (as the live run does about an hour before kickoff). Scores are log losses (lower = better) on
1X2, Over 2.5 and both teams to score, with a 95% interval on the per-match difference. The XI enters the model only
when the 1X2 log loss improves by at least MIN_GAIN (the project's bar for any model change).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from .config import Config
from .engine import Engine
from .information import build_availability
from .markets import probability
from .modeleval import REFS, Scores, _Frozen

MIN_GAIN = 0.005


def _ll(p: dict[str, float], hg: int, ag: int) -> tuple[float, float, float]:
    res = "H" if hg > ag else "D" if hg == ag else "A"
    over, btts = hg + ag > 2.5, hg > 0 and ag > 0
    return (-math.log(max(p[res], 1e-12)), -math.log(max(p["O25"] if over else 1 - p["O25"], 1e-12)),
            -math.log(max(p["BTTS"] if btts else 1 - p["BTTS"], 1e-12)))


VARIANTS = {"priori per ruolo": {"lineup_impact": "prior"}, "appreso": {"lineup_impact": "learned"},
            "appreso ×0.5": {"lineup_impact": "learned", "lineup_scale": 0.5}, "priori ×0.5": {"lineup_impact": "prior", "lineup_scale": 0.5}}


def evaluate_lineups(provider, cfg: Config, start: datetime, end: datetime, competitions: list[str],
                     variants: dict[str, dict] | None = None) -> dict:
    """{variant: report}; each variant against the same matches without the XI."""
    import copy
    frozen = _Frozen(provider, end)
    out = {}
    for name, changes in (variants or VARIANTS).items():
        c = copy.deepcopy(cfg)
        for k, v in changes.items():
            setattr(c.model, k, v)
        out[name] = _evaluate(frozen, provider, c, start, end, competitions)
    return out


def _evaluate(frozen, provider, cfg: Config, start: datetime, end: datetime, competitions: list[str]) -> dict:
    eng = Engine(frozen, cfg, use_lineups=True)
    finished = [r for r in frozen.rows if start <= r.kickoff < end and r.competition in competitions]
    weeks: dict[datetime, list] = defaultdict(list)
    for r in finished:
        weeks[(r.kickoff - timedelta(days=r.kickoff.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)].append(r)
    base, xi = defaultdict(Scores), defaultdict(Scores)
    diffs: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    shifts: list[float] = []
    skipped = defaultdict(int)
    for monday in sorted(weeks):
        for r in weeks[monday]:
            lus = [l for l in provider.get_lineups(r.fixture_id) if l.status == "confirmed"]
            if not ({l.team for l in lus} >= {r.home, r.away}):
                skipped["senza formazioni"] += 1
                continue
            fitted = eng.fit(r.competition, monday)
            if fitted is None or not (fitted[0].knows(r.home) and fitted[0].knows(r.away)):
                skipped["squadra sconosciuta"] += 1
                continue
            imp = eng.impact(r.competition, monday)
            roster = eng.roster(r.competition)
            rh, ra = [p for p in roster if p.team == r.home], [p for p in roster if p.team == r.away]
            if imp is None or not rh or not ra:
                skipped["rosa mancante"] += 1
                continue
            ah = build_availability(r.home, rh, imp.base, [], lus, r.kickoff, r.fixture_id)
            aa = build_availability(r.away, ra, imp.base, [], lus, r.kickoff, r.fixture_id)
            if ah.source != "confirmed" or aa.source != "confirmed":
                skipped["formazione non riconosciuta nella rosa"] += 1
                continue
            adj = imp.adjustment(ah, aa)
            m0 = fitted[0].score_matrix(r.home, r.away)
            m1 = fitted[0].score_matrix(r.home, r.away, (adj.d_home, adj.d_away))
            p0 = {k: float(probability(m0, ref)) for k, ref in REFS.items()}
            p1 = {k: float(probability(m1, ref)) for k, ref in REFS.items()}
            for g in (r.competition, "tutte"):
                base[g].add(p0, r.home_goals, r.away_goals, None)
                xi[g].add(p1, r.home_goals, r.away_goals, None)
                a, b = _ll(p0, r.home_goals, r.away_goals), _ll(p1, r.home_goals, r.away_goals)
                diffs[g].append(tuple(y - x for x, y in zip(a, b)))
            shifts.append(abs(adj.d_home) + abs(adj.d_away))
    out = {}
    for g, ds in diffs.items():
        n = len(ds)
        row = {"n": n, "base": base[g].row(), "xi": xi[g].row()}
        for k, name in enumerate(("1x2", "o25", "btts")):
            xs = [d[k] for d in ds]
            m = sum(xs) / n
            sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else float("nan")
            row[f"d_{name}"] = (m, 1.96 * sd / math.sqrt(n) if n > 1 else float("nan"))
        out[g] = row
    return {"groups": out, "skipped": dict(skipped), "mean_shift": sum(shifts) / len(shifts) if shifts else 0.0,
            "weeks": len(weeks)}


def print_lineup_eval(reps: dict) -> None:
    for name, rep in reps.items():
        print(f"\n=== {name} ===")
        _print_one(rep)


def _print_one(rep: dict) -> None:
    print(f"Formazioni ufficiali nel modello: {rep['weeks']} settimane, spostamento medio dei gol attesi {rep['mean_shift']:.3f} (log)")
    print("differenza di log loss con formazione − senza (negativo = meglio), ±intervallo 95%")
    print(f"  {'gruppo':<18}{'partite':>8}  {'LL 1X2 senza':>13} {'diff 1X2':>16} {'diff O2.5':>16} {'diff GG':>16}")
    for g, r in sorted(rep["groups"].items(), key=lambda kv: -kv[1]["n"]):
        f = lambda t: f"{t[0]:+.4f} ±{t[1]:.4f}"  # noqa: E731
        print(f"  {g:<18}{r['n']:>8}  {r['base']['ll_1x2']:>13.4f} {f(r['d_1x2']):>16} {f(r['d_o25']):>16} {f(r['d_btts']):>16}")
    if rep["skipped"]:
        print("partite saltate: " + ", ".join(f"{k} {v}" for k, v in rep["skipped"].items()))
    t = rep["groups"].get("tutte")
    if t:
        gain = -t["d_1x2"][0]
        print(f"verdetto: guadagno 1X2 {gain:+.4f} (soglia {MIN_GAIN}) -> "
              + ("ENTRA nel modello" if gain >= MIN_GAIN and t["d_1x2"][0] + t["d_1x2"][1] < 0 else "non basta (ancora)"))
