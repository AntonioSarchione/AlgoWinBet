"""Fase 10: a LightGBM correction on top of the Dixon-Coles 1X2 probabilities.

The goal is a model that knows something the market does not, so that the meta-model gives it weight next to Pinnacle
(on the domestic 1X2 it currently gets about none). LightGBM does not replace Dixon-Coles: it starts from its log
probabilities (init_score) and learns a correction from features the goal model does not see. No market price is a
feature: the market is blended later by the meta-model, and a model fed with prices would only learn to copy them.

Everything is walk-forward, exactly like modeleval: the features of a match are computed from results known before the
Monday of its week, the booster is trained only on earlier weeks (the seasons before the test window included), and stops
growing when the most recent weeks it has not trained on stop improving (no correction at all if they never do).
The correction enters the live model only after `boost-eval` shows a clear gain (MIN_GAIN on the 1X2 log loss, 95%
interval below zero); until then model.boost = "off".

v1 (2026-10-05) learned noise (+0.009 log loss on 2449 matches). v2: more history, early stopping, no feature repeating
Dixon-Coles' probabilities, and what Dixon-Coles does not see: recent shots, shots on target and xG for and against.
"""
from __future__ import annotations

import bisect
import copy
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import Config
from .engine import Engine
from .markets import probability
from .meta import group_of
from .modeleval import REFS, _Frozen

MIN_GAIN = 0.005  # the project's bar for any model change (1X2 log loss)
FORM_N = 5  # matches in the recent-form features
STATS_N = 8  # matches with statistics in the shots / xG features
STAT_NAMES = ("shots", "shots_on_target", "expected_goals")
_STAT_SHORT = {"shots": "sh", "shots_on_target": "sot", "expected_goals": "xgs"}
FEATURES = (["xg_sum", "xg_diff"]
            + [f"{f}_{s}" for f in ("ppg", "gd", "rest", "load", "n") for s in ("h", "a")]
            + [f"{_STAT_SHORT[st]}_{d}_{s}" for st in STAT_NAMES for d in ("for", "against") for s in ("h", "a")]
            + ["national", "cup"])
PARAMS = {  # deliberately small corrections: shallow trees, strong regularisation, early stopping
    "objective": "multiclass", "num_class": 3, "learning_rate": 0.02, "num_leaves": 4, "min_data_in_leaf": 150,
    "lambda_l2": 30.0, "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "verbosity": -1, "seed": 7,
}
ROUNDS = 400  # upper bound: early stopping decides
VALID_WEEKS = 8  # the most recent training weeks judge when to stop
PATIENCE = 30
HISTORY_WEEKS = 70  # training-only weeks before the test window (the stored seasons go back about two years)
MIN_TRAIN = 1000  # rows needed before the first test week is predicted


@dataclass
class Row:
    fixture_id: str
    monday: datetime
    group: str
    x: list[float]
    p_dc: tuple[float, float, float]
    y: int  # 0 home, 1 draw, 2 away


class TeamLog:
    """Every team's results in time order: (kickoff, available_at, goals for, goals against, points, stats) where stats
    maps a statistic to (for, against) when the match has it."""

    def __init__(self, rows, stats: dict[str, dict[str, tuple[float, float]]] | None = None):
        stats = stats or {}
        self.by: dict[str, list[tuple]] = defaultdict(list)
        for r in sorted(rows, key=lambda r: r.kickoff):
            pts_h = 3 if r.home_goals > r.away_goals else 1 if r.home_goals == r.away_goals else 0
            pts_a = 3 if r.away_goals > r.home_goals else 1 if r.home_goals == r.away_goals else 0
            st = stats.get(r.fixture_id, {})
            self.by[r.home].append((r.kickoff, r.available_at, r.home_goals, r.away_goals, pts_h, {k: (h, a) for k, (h, a) in st.items()}))
            self.by[r.away].append((r.kickoff, r.available_at, r.away_goals, r.home_goals, pts_a, {k: (a, h) for k, (h, a) in st.items()}))
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

    def stat_features(self, team: str, at: datetime) -> dict[tuple[str, str], float]:
        """Mean for / against of each statistic over the last STATS_N known matches that have it (NaN: none)."""
        ms = self.known(team, at)
        out: dict[tuple[str, str], float] = {}
        for name in STAT_NAMES:
            vals = [m[5][name] for m in reversed(ms) if name in m[5]][:STATS_N]
            out[(name, "for")] = float(np.mean([v[0] for v in vals])) if vals else math.nan
            out[(name, "against")] = float(np.mean([v[1] for v in vals])) if vals else math.nan
        return out


def build_rows(provider, cfg: Config, start: datetime, end: datetime) -> list[Row]:
    """One row per finished match in [start, end) that Dixon-Coles can price at the Monday of its week."""
    frozen = provider if isinstance(provider, _Frozen) else _Frozen(provider, end)
    eng = Engine(frozen, copy.deepcopy(cfg), use_lineups=False)
    stats_fn = getattr(frozen.base, "match_stat_values", None)
    log = TeamLog(frozen.rows, stats_fn(STAT_NAMES) if stats_fn else None)
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
        f = {"h": log.features(r.home, monday, r.kickoff), "a": log.features(r.away, monday, r.kickoff)}
        st = {"h": log.stat_features(r.home, monday), "a": log.stat_features(r.away, monday)}
        grp = group_of(r.competition)
        x = ([lh + la, lh - la]
             + [f[side][k] for k in range(5) for side in ("h", "a")]
             + [st[side][(name, d)] for name in STAT_NAMES for d in ("for", "against") for side in ("h", "a")]
             + [float(grp == "nazionali"), float(grp == "coppe")])
        y = 0 if r.home_goals > r.away_goals else 1 if r.home_goals == r.away_goals else 2
        out.append(Row(r.fixture_id, monday, grp, x, p, y))
    return out


def _base_ll(rows: list[Row]) -> float:
    return float(np.mean([-math.log(r.p_dc[r.y]) for r in rows]))


class Booster:
    """LightGBM correction of the Dixon-Coles 1X2: trained from log(p_dc), predicts softmax(log(p_dc) + correction)."""

    def __init__(self, params: dict | None = None, rounds: int = ROUNDS):
        self.params, self.rounds, self.model, self.best = {**PARAMS, **(params or {})}, rounds, None, 0

    @staticmethod
    def _data(lgb, rows: list[Row], reference=None):
        return lgb.Dataset(np.array([r.x for r in rows], dtype=float), label=[r.y for r in rows],
                           init_score=np.log(np.array([r.p_dc for r in rows], dtype=float)), feature_name=FEATURES,
                           free_raw_data=False, reference=reference)

    def fit(self, rows: list[Row]) -> "Booster":
        """Early stopping on the last VALID_WEEKS weeks of the training rows, then a refit on all of them with that many
        rounds. No round better than Dixon-Coles alone on those weeks = no correction."""
        import lightgbm as lgb  # optional dependency: pip install ".[gbm]"
        mondays = sorted({r.monday for r in rows})
        cut = mondays[-VALID_WEEKS] if len(mondays) > 2 * VALID_WEEKS else None
        self.best, self.model = 0, None
        if cut is None:
            self.best = self.rounds // 4
        else:
            train, valid = [r for r in rows if r.monday < cut], [r for r in rows if r.monday >= cut]
            dt = self._data(lgb, train)
            probe = lgb.train(self.params, dt, num_boost_round=self.rounds, valid_sets=[self._data(lgb, valid, dt)],
                              callbacks=[lgb.early_stopping(PATIENCE, verbose=False)])
            if probe.best_score["valid_0"]["multi_logloss"] < _base_ll(valid):
                self.best = probe.best_iteration
        if self.best > 0:
            self.model = lgb.train(self.params, self._data(lgb, rows), num_boost_round=self.best)
        return self

    def predict(self, rows: list[Row]) -> np.ndarray:
        base = np.log(np.array([r.p_dc for r in rows], dtype=float))
        if self.model is None:
            return np.exp(base)
        raw = self.model.predict(np.array([r.x for r in rows], dtype=float), raw_score=True) + base
        e = np.exp(raw - raw.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def importance(self) -> dict[str, float]:
        if self.model is None:
            return {}
        g = self.model.feature_importance(importance_type="gain")
        tot = float(g.sum()) or 1.0
        return dict(sorted(((f, float(v) / tot) for f, v in zip(FEATURES, g)), key=lambda kv: -kv[1]))


@dataclass
class BoostReport:
    weeks: int = 0
    trained: int = 0
    rounds: list[int] = field(default_factory=list)  # useful rounds of every training (0 = no correction)
    groups: dict[str, dict] = field(default_factory=dict)  # group -> n, ll_dc, ll_boost, diff (mean, ci)
    importance: dict[str, float] = field(default_factory=dict)
    stats_share: float = 0.0  # test rows with shots / xG features


def evaluate_boost(provider, cfg: Config, start: datetime | None, end: datetime | None, history_weeks: int = HISTORY_WEEKS,
                   retrain_every: int = 4, rows: list[Row] | None = None, min_train: int = MIN_TRAIN) -> BoostReport:
    """Walk-forward over the weeks from `start`: the booster trained on every earlier week (the history before `start`
    included, retrained every few weeks) predicts the week; Dixon-Coles alone on the same matches is the baseline."""
    if rows is None:
        rows = build_rows(provider, cfg, start - timedelta(weeks=history_weeks), end)
    mondays = sorted({r.monday for r in rows})
    tests = [m for m in mondays if (start is None or m >= start) and sum(r.monday < m for r in rows) >= min_train]
    rep = BoostReport(weeks=len(tests))
    diffs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    booster, with_stats, n_test = None, 0, 0
    for i, monday in enumerate(tests):
        if booster is None or i % retrain_every == 0:
            booster = Booster().fit([r for r in rows if r.monday < monday])
            rep.trained += 1
            rep.rounds.append(booster.best)
        week = [r for r in rows if r.monday == monday]
        for r, p in zip(week, booster.predict(week)):
            n_test += 1
            with_stats += not math.isnan(r.x[FEATURES.index("sh_for_h")])
            a, b = -math.log(r.p_dc[r.y]), -math.log(max(float(p[r.y]), 1e-12))
            for g in (r.group, "tutte"):
                diffs[g].append((a, b))
    for g, ds in sorted(diffs.items()):
        n = len(ds)
        d = np.array([b - a for a, b in ds])
        ci = 1.96 * float(d.std(ddof=1)) / math.sqrt(n) if n > 1 else float("nan")
        rep.groups[g] = {"n": n, "ll_dc": float(np.mean([a for a, _ in ds])), "ll_boost": float(np.mean([b for _, b in ds])),
                         "diff": (float(d.mean()), ci)}
    rep.stats_share = with_stats / n_test if n_test else 0.0
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
    print(f"LightGBM sopra Dixon-Coles: {rep.weeks} settimane di prova, {rep.trained} addestramenti (solo settimane precedenti), "
          f"giri utili {rep.rounds} (0 = nessuna correzione)")
    print(f"partite di prova con tiri / xG recenti: {rep.stats_share:.0%}")
    print("log loss 1X2 (più basso = meglio); diff = LightGBM − Dixon-Coles, ±intervallo 95%")
    print(f"  {'gruppo':<14}{'partite':>8} {'Dixon-Coles':>12} {'LightGBM':>10} {'diff':>18}")
    for g, s in sorted(rep.groups.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {g:<14}{s['n']:>8} {s['ll_dc']:>12.4f} {s['ll_boost']:>10.4f} {s['diff'][0]:>+10.4f} ±{s['diff'][1]:.4f}")
    if rep.importance:
        print("peso delle variabili: " + ", ".join(f"{k} {v:.0%}" for k, v in list(rep.importance.items())[:8]))
    print("verdetto: " + verdict(rep))


# ---- shadow test (v3, 2026-10-05): v2 helped only the club cups (-0.020 ±0.013 on 443 matches), found by looking at four
# groups, so it is not trusted yet. The booster is trained once on everything known before SHADOW_FROM, frozen in the
# database, and judged every Monday on the cup matches played since: matches nobody had seen when the design was fixed.
SHADOW_FROM = datetime(2026, 10, 5, tzinfo=timezone.utc)
DESIGN = "v2"  # features + parameters frozen for the shadow test: a change of either starts a new test
APPLY_GROUPS = ("coppe",)  # national teams got worse (few matches, almost no statistics); leagues no change
SHADOW_MIN = 150  # cup matches before the shadow test can confirm anything
SCHEMA = """
CREATE TABLE IF NOT EXISTS boost_models(id INTEGER PRIMARY KEY, created_at TEXT, design TEXT, trained_until TEXT, rows INTEGER,
  rounds INTEGER, model TEXT);
CREATE TABLE IF NOT EXISTS boost_runs(id INTEGER PRIMARY KEY, created_at TEXT, design TEXT, report TEXT);
"""


def save_booster(store, b: Booster, until: datetime, n_rows: int, now: datetime) -> int:
    store.db.executescript(SCHEMA)
    cur = store.db.execute("INSERT INTO boost_models(created_at, design, trained_until, rows, rounds, model) VALUES(?,?,?,?,?,?) RETURNING id",
                           (now.isoformat(), DESIGN, until.isoformat(), n_rows, b.best, b.model.model_to_string() if b.model else ""))
    rid = int(cur.fetchall()[0][0])
    store.db.commit()
    return rid


def load_booster(store) -> tuple[Booster, datetime] | None:
    """The frozen booster of the current design (None: not trained yet)."""
    store.db.executescript(SCHEMA)
    row = store.db.execute("SELECT model, rounds, trained_until FROM boost_models WHERE design=? ORDER BY id DESC LIMIT 1", (DESIGN,)).fetchone()
    if not row:
        return None
    b = Booster()
    b.best = int(row[1])
    if row[0]:
        import lightgbm as lgb
        b.model = lgb.Booster(model_str=row[0])
    return b, datetime.fromisoformat(row[2])


def train_frozen(provider, cfg: Config, store, now: datetime, history_weeks: int = HISTORY_WEEKS + 52) -> tuple[int, Booster, int]:
    """Train the shadow booster on every match before SHADOW_FROM and freeze it. Returns (id, booster, rows)."""
    rows = build_rows(provider, cfg, SHADOW_FROM - timedelta(weeks=history_weeks), SHADOW_FROM)
    b = Booster().fit(rows)
    return save_booster(store, b, SHADOW_FROM, len(rows), now), b, len(rows)


def shadow_report(provider, cfg: Config, store, end: datetime) -> dict | None:
    """The frozen booster against Dixon-Coles on the matches since SHADOW_FROM, applied only to APPLY_GROUPS."""
    got = load_booster(store)
    if got is None:
        return None
    b, _ = got
    rows = build_rows(provider, cfg, SHADOW_FROM, end)
    use = [r for r in rows if r.group in APPLY_GROUPS]
    out = {"design": DESIGN, "since": SHADOW_FROM.isoformat(), "until": end.isoformat(), "rounds": b.best, "groups": {}}
    if use:
        pb = b.predict(use)
        by: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for r, p in zip(use, pb):
            by[r.group].append((-math.log(r.p_dc[r.y]), -math.log(max(float(p[r.y]), 1e-12))))
        for g, ds in by.items():
            d = np.array([bb - a for a, bb in ds])
            ci = 1.96 * float(d.std(ddof=1)) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
            out["groups"][g] = {"n": len(ds), "ll_dc": float(np.mean([a for a, _ in ds])), "ll_boost": float(np.mean([x for _, x in ds])),
                                "diff": [float(d.mean()), ci]}
    return out


def shadow_verdict(rep: dict) -> str:
    g = rep["groups"].get("coppe")
    if not g:
        return "in osservazione: nessuna partita di coppa dal " + rep["since"][:10]
    m, ci = g["diff"]
    if g["n"] < SHADOW_MIN:
        return f"in osservazione: {g['n']}/{SHADOW_MIN} partite di coppa, per ora {-m:+.4f} ±{ci:.4f}"
    if -m >= MIN_GAIN and m + ci < 0:
        return f"CONFERMATO sulle coppe: guadagno {-m:+.4f} ±{ci:.4f} su {g['n']} partite nuove: si può attivare"
    return f"NON confermato sulle coppe: {-m:+.4f} ±{ci:.4f} su {g['n']} partite nuove: resta spento"


def save_shadow(store, rep: dict, now: datetime) -> None:
    import json
    store.db.executescript(SCHEMA)
    store.db.execute("INSERT INTO boost_runs(created_at, design, report) VALUES(?,?,?)", (now.isoformat(), DESIGN, json.dumps(rep)))
    store.db.commit()


def print_shadow(rep: dict) -> None:
    print(f"LightGBM in ombra ({rep['design']}, congelato al {rep['since'][:10]}, {rep['rounds']} giri): partite dal {rep['since'][:10]}")
    for g, s in rep["groups"].items():
        print(f"  {g:<10}{s['n']:>6} partite  Dixon-Coles {s['ll_dc']:.4f}  LightGBM {s['ll_boost']:.4f}  diff {s['diff'][0]:+.4f} ±{s['diff'][1]:.4f}")
    print("verdetto: " + shadow_verdict(rep))
