"""Fase 10 skeleton: a LightGBM correction on top of the Dixon-Coles 1X2 probabilities.

The goal is a model that knows something the market does not, so that the meta-model gives it weight next to Pinnacle
(on the domestic 1X2 it currently gets about none). LightGBM does not replace Dixon-Coles: it starts from its log
probabilities (init_score) and learns a correction from features the goal model does not see (recent form, rest, fixture
congestion, sample size). No market price is a feature: the market is blended later by the meta-model, and a model fed with
prices would only learn to copy them.

Everything is walk-forward, exactly like modeleval: the features of a match are computed from results known before the
Monday of its week, the booster is trained only on earlier weeks. The correction enters the live model only after
`boost-eval` shows a clear gain (MIN_GAIN on the 1X2 log loss, 95% interval below zero); until then model.boost = "off".
"""
from __future__ import annotations

import bisect
import copy
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .config import Config
from .engine import Engine
from .markets import probability
from .meta import group_of
from .modeleval import REFS, _Frozen

MIN_GAIN = 0.005  # the project's bar for any model change (1X2 log loss)
FORM_N = 5  # matches in the recent-form features
FEATURES = ["dc_h", "dc_d", "dc_a", "xg_h", "xg_a", "xg_sum", "xg_diff", "ppg_h", "ppg_a", "gd_h", "gd_a", "rest_h", "rest_a",
            "load_h", "load_a", "n_h", "n_a", "national", "cup"]
PARAMS = {  # deliberately small corrections: shallow trees, strong regularisation, few rounds
    "objective": "multiclass", "num_class": 3, "learning_rate": 0.03, "num_leaves": 7, "min_data_in_leaf": 60,
    "lambda_l2": 10.0, "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "verbosity": -1, "seed": 7,
}
ROUNDS = 150


@dataclass
class Row:
    fixture_id: str
    monday: datetime
    group: str
    x: list[float]
    p_dc: tuple[float, float, float]
    y: int  # 0 home, 1 draw, 2 away


class TeamLog:
    """Every team's results in time order (kickoff, available_at, goals for, goals against, points)."""

    def __init__(self, rows):
        self.by: dict[str, list[tuple]] = defaultdict(list)
        for r in sorted(rows, key=lambda r: r.kickoff):
            pts_h = 3 if r.home_goals > r.away_goals else 1 if r.home_goals == r.away_goals else 0
            pts_a = 3 if r.away_goals > r.home_goals else 1 if r.home_goals == r.away_goals else 0
            self.by[r.home].append((r.kickoff, r.available_at, r.home_goals, r.away_goals, pts_h))
            self.by[r.away].append((r.kickoff, r.available_at, r.away_goals, r.home_goals, pts_a))
        self.avail = {t: [m[1] for m in ms] for t, ms in self.by.items()}

    def known(self, team: str, at: datetime) -> list[tuple]:
        """Results of the team known at `at` (result available, not just kicked off)."""
        ms = self.by.get(team, [])
        return ms[:bisect.bisect_right(self.avail.get(team, []), at)]

    def features(self, team: str, at: datetime, kickoff: datetime) -> tuple[float, float, float, float, float]:
        ms = self.known(team, at)
        last = ms[-FORM_N:]
        ppg = sum(m[4] for m in last) / len(last) if last else 1.35
        gd = sum(m[2] - m[3] for m in last) / len(last) if last else 0.0
        rest = min((kickoff - ms[-1][0]).total_seconds() / 86400, 21.0) if ms else 21.0
        load = sum(1 for m in ms if at - m[0] <= timedelta(days=21))
        return ppg, gd, rest, float(load), float(min(len(ms), 60))


def build_rows(provider, cfg: Config, start: datetime, end: datetime) -> list[Row]:
    """One row per finished match in [start, end) that Dixon-Coles can price at the Monday of its week."""
    frozen = provider if isinstance(provider, _Frozen) else _Frozen(provider, end)
    eng = Engine(frozen, copy.deepcopy(cfg), use_lineups=False)
    log = TeamLog(frozen.rows)
    out: list[Row] = []
    for r in sorted((r for r in frozen.rows if start <= r.kickoff < end), key=lambda r: r.kickoff):
        monday = (r.kickoff - timedelta(days=r.kickoff.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        fitted = eng.fit(r.competition, monday)
        if fitted is None or not (fitted[0].knows(r.home) and fitted[0].knows(r.away)):
            continue
        m = fitted[0].score_matrix(r.home, r.away)
        p = tuple(max(float(probability(m, REFS[k])), 1e-6) for k in ("H", "D", "A"))
        s = sum(p)
        p = (p[0] / s, p[1] / s, p[2] / s)
        lh, la = fitted[0].expected_goals(r.home, r.away)
        fh, fa = log.features(r.home, monday, r.kickoff), log.features(r.away, monday, r.kickoff)
        grp = group_of(r.competition)
        x = [*p, lh, la, lh + la, lh - la, fh[0], fa[0], fh[1], fa[1], fh[2], fa[2], fh[3], fa[3], fh[4], fa[4],
             float(grp == "nazionali"), float(grp == "coppe")]
        y = 0 if r.home_goals > r.away_goals else 1 if r.home_goals == r.away_goals else 2
        out.append(Row(r.fixture_id, monday, grp, x, p, y))
    return out


class Booster:
    """LightGBM correction of the Dixon-Coles 1X2: trained from log(p_dc), predicts softmax(log(p_dc) + correction)."""

    def __init__(self, params: dict | None = None, rounds: int = ROUNDS):
        self.params, self.rounds, self.model = {**PARAMS, **(params or {})}, rounds, None

    def fit(self, rows: list[Row]) -> "Booster":
        import lightgbm as lgb  # optional dependency: pip install ".[gbm]"
        x = np.array([r.x for r in rows], dtype=float)
        init = np.log(np.array([r.p_dc for r in rows], dtype=float))
        data = lgb.Dataset(x, label=[r.y for r in rows], init_score=init, feature_name=FEATURES, free_raw_data=False)
        self.model = lgb.train(self.params, data, num_boost_round=self.rounds)
        return self

    def predict(self, rows: list[Row]) -> np.ndarray:
        x = np.array([r.x for r in rows], dtype=float)
        raw = self.model.predict(x, raw_score=True) + np.log(np.array([r.p_dc for r in rows], dtype=float))
        e = np.exp(raw - raw.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def importance(self) -> dict[str, float]:
        g = self.model.feature_importance(importance_type="gain")
        tot = float(g.sum()) or 1.0
        return dict(sorted(((f, float(v) / tot) for f, v in zip(FEATURES, g)), key=lambda kv: -kv[1]))


@dataclass
class BoostReport:
    weeks: int = 0
    trained: int = 0
    groups: dict[str, dict] = field(default_factory=dict)  # group -> n, ll_dc, ll_boost, diff (mean, ci)
    importance: dict[str, float] = field(default_factory=dict)


def evaluate_boost(provider, cfg: Config, start: datetime, end: datetime, warmup_weeks: int = 12, retrain_every: int = 4,
                   rows: list[Row] | None = None) -> BoostReport:
    """Walk-forward: from week warmup_weeks on, the booster trained on every earlier week (retrained every few weeks)
    predicts the week; Dixon-Coles alone on the same matches is the baseline."""
    rows = rows if rows is not None else build_rows(provider, cfg, start, end)
    mondays = sorted({r.monday for r in rows})
    rep = BoostReport(weeks=len(mondays))
    diffs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    booster = None
    for i, monday in enumerate(mondays):
        if i < warmup_weeks:
            continue
        if booster is None or (i - warmup_weeks) % retrain_every == 0:
            train = [r for r in rows if r.monday < monday]
            booster = Booster().fit(train)
            rep.trained += 1
        week = [r for r in rows if r.monday == monday]
        pb = booster.predict(week)
        for r, p in zip(week, pb):
            a, b = -math.log(r.p_dc[r.y]), -math.log(max(float(p[r.y]), 1e-12))
            for g in (r.group, "tutte"):
                diffs[g].append((a, b))
    for g, ds in sorted(diffs.items()):
        n = len(ds)
        d = np.array([b - a for a, b in ds])
        ci = 1.96 * float(d.std(ddof=1)) / math.sqrt(n) if n > 1 else float("nan")
        rep.groups[g] = {"n": n, "ll_dc": float(np.mean([a for a, _ in ds])), "ll_boost": float(np.mean([b for _, b in ds])),
                         "diff": (float(d.mean()), ci)}
    if booster is not None:
        rep.importance = booster.importance()
    return rep


def verdict(rep: BoostReport) -> str:
    t = rep.groups.get("tutte")
    if not t:
        return "nessuna partita valutata"
    m, ci = t["diff"]
    if -m >= MIN_GAIN and m + ci < 0:
        return f"ENTRA: guadagno {-m:+.4f} sul 1X2 (soglia {MIN_GAIN})"
    return f"non basta: guadagno {-m:+.4f} ±{ci:.4f} (soglia {MIN_GAIN}, intervallo sotto zero)"


def print_boost(rep: BoostReport) -> None:
    print(f"LightGBM sopra Dixon-Coles: {rep.weeks} settimane, {rep.trained} addestramenti (solo settimane precedenti)")
    print("log loss 1X2 (più basso = meglio); diff = LightGBM − Dixon-Coles, ±intervallo 95%")
    print(f"  {'gruppo':<14}{'partite':>8} {'Dixon-Coles':>12} {'LightGBM':>10} {'diff':>18}")
    for g, s in sorted(rep.groups.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {g:<14}{s['n']:>8} {s['ll_dc']:>12.4f} {s['ll_boost']:>10.4f} {s['diff'][0]:>+10.4f} ±{s['diff'][1]:.4f}")
    if rep.importance:
        print("peso delle variabili: " + ", ".join(f"{k} {v:.0%}" for k, v in list(rep.importance.items())[:8]))
    print("verdetto: " + verdict(rep))
