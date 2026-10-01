"""Meta-model and calibration (Fase 4): how much to trust the model against the sharp market price, learned from results.

For a market family (1X2, Over/Under 2.5, Gol/NoGol) the final probabilities are a logarithmic opinion pool

    p_i  proportional to  exp(a * log p_model_i + b * log p_market_i + c_i)        (c_0 = 0)

  a, b ... the weight of the model and of the market price (sharp reference, margin removed). a + b above 1 sharpens the
           forecast, below 1 flattens it: the pool is also the calibration step.
  c_i .... a fixed lean per outcome (e.g. draws the Poisson model underrates).

Without a market price the same form with b = 0 calibrates the model alone. Parameters are fitted by maximum likelihood
on the walk-forward replay of finished matches (quality.py), with a weak pull toward a prior (mostly market), per group
(leagues, cups, national teams) when that group has enough matches, else on all of them. The fit is stored on Turso and
read by the live analysis; quality.py also refits it week by week on past matches only, to score it honestly.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
from scipy.optimize import minimize

from .domain import SelectionRef

FAMILY_SELECTIONS = {
    "1X2": [("MATCH_1X2", "HOME", None), ("MATCH_1X2", "DRAW", None), ("MATCH_1X2", "AWAY", None)],
    "U/O 2.5": [("TOTAL_GOALS", "OVER", 2.5), ("TOTAL_GOALS", "UNDER", 2.5)],
    "Gol/NoGol": [("BTTS", "YES", None), ("BTTS", "NO", None)],
}
MIN_N = 300  # matches needed to fit a group of its own (else the fit on every group is used)
PRIOR = {"pool": (0.25, 0.75), "calib": (1.0, 0.0)}  # (a, b) the fit is pulled toward: mostly market / the model as is
STRENGTH = 4.0  # prior weight, in matches: negligible with hundreds of matches, decisive with a handful
EPS = 1e-6

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_models(id INTEGER PRIMARY KEY, created_at TEXT, window_start TEXT, window_end TEXT,
  model_version TEXT, params TEXT);
"""


def group_of(competition: str) -> str:
    c = competition.lower()
    if any(k in c for k in ("champions", "europa league", "conference")):  # club cups, qualifying rounds included
        return "coppe"
    if any(k in c for k in ("nations league", "world cup", "uefa euro", "qualif", "friendl")):
        return "nazionali"
    return "campionati"


def family_key(ref: SelectionRef) -> tuple[str, int] | None:
    """(family, index of the selection) for the families the meta-model covers, else None."""
    for fam, sels in FAMILY_SELECTIONS.items():
        for i, (code, sel, line) in enumerate(sels):
            if ref.market_code == code and ref.selection == sel and (line is None or (ref.line is not None and abs(ref.line - line) < 1e-9)):
                return fam, i
    return None


@dataclass
class Pool:
    a: float
    b: float
    c: list[float]
    n: int = 0
    kind: str = "pool"  # "pool" (model + market) or "calib" (model only)
    d: float = 0.0  # weight of the extra feature (price movement), 0 when not fitted

    def apply(self, p_model: list[float], p_market: list[float] | None = None, x: list[float] | None = None) -> list[float]:
        lm = np.log(np.clip(np.asarray(p_model, float), EPS, 1.0))
        z = self.a * lm + np.asarray(self.c, float)
        if self.kind == "pool":
            if p_market is None:
                raise ValueError("pool needs the market price")
            z = z + self.b * np.log(np.clip(np.asarray(p_market, float), EPS, 1.0))
        if self.d and x is not None:
            z = z + self.d * np.asarray(x, float)
        z = z - z.max()
        e = np.exp(z)
        return (e / e.sum()).tolist()


def fit_pool(pm: np.ndarray, pk: np.ndarray | None, y: np.ndarray, kind: str = "pool", x: np.ndarray | None = None) -> Pool:
    """Maximum likelihood with a Gaussian pull toward PRIOR[kind] (weight STRENGTH matches). pm, pk, x: (n, K); y: (n,) index.
    x is an optional extra feature per outcome (e.g. the log change of the market price) with its own weight d (prior 0)."""
    n, k = pm.shape
    lm = np.log(np.clip(pm, EPS, 1.0))
    lk = np.log(np.clip(pk, EPS, 1.0)) if pk is not None else np.zeros_like(lm)
    a0, b0 = PRIOR[kind]
    use_b = kind == "pool"
    use_d = x is not None
    xx = x if use_d else np.zeros_like(lm)
    nb = 2 if use_b else 1

    def unpack(t):
        a = t[0]
        b = t[1] if use_b else 0.0
        c = np.concatenate([[0.0], t[nb:nb + k - 1]])
        d = t[-1] if use_d else 0.0
        return a, b, c, d

    def nll(t):
        a, b, c, d = unpack(t)
        z = a * lm + b * lk + c + d * xx
        z = z - z.max(axis=1, keepdims=True)
        logp = z - np.log(np.exp(z).sum(axis=1, keepdims=True))
        pen = (a - a0) ** 2 + ((b - b0) ** 2 if use_b else 0.0) + float(np.sum(c ** 2)) + d ** 2
        return float(-logp[np.arange(n), y].sum() + STRENGTH * pen)

    t0 = np.array([a0] + ([b0] if use_b else []) + [0.0] * (k - 1) + ([0.0] if use_d else []))
    r = minimize(nll, t0, method="BFGS")
    a, b, c, d = unpack(r.x)
    return Pool(a=float(a), b=float(b), c=[float(v) for v in c], n=int(n), kind=kind, d=float(d))


def _arrays(rows: list[dict], kind: str):
    sub = [r for r in rows if kind == "calib" or "market" in r]
    if not sub:
        return None
    pm = np.array([r["model"] for r in sub], float)
    pk = np.array([r["market"] for r in sub], float) if kind == "pool" else None
    y = np.array([r["k"] for r in sub], int)
    return pm, pk, y


def fit_all(samples: list[dict], min_n: int = MIN_N) -> dict[str, dict]:
    """samples: {"group", "fam", "k", "model", "market"?}. Returns {"<group>|<fam>|<kind>": Pool as dict} for every
    (group or "tutte", family, kind) with at least MIN_N matches."""
    out: dict[str, dict] = {}
    groups = sorted({s["group"] for s in samples}) + ["tutte"]
    for fam in FAMILY_SELECTIONS:
        for g in groups:
            rows = [s for s in samples if s["fam"] == fam and (g == "tutte" or s["group"] == g)]
            for kind in ("pool", "calib"):
                arr = _arrays(rows, kind)
                if arr is None or len(arr[2]) < min_n:
                    continue
                out[f"{g}|{fam}|{kind}"] = asdict(fit_pool(*arr, kind=kind))
    return out


class MetaSet:
    """The fitted pools, looked up by group, family and whether a market price is available."""

    def __init__(self, params: dict[str, dict] | None = None, version: str = "none"):
        self.params = {k: Pool(**v) for k, v in (params or {}).items()}
        self.version = version

    def __bool__(self) -> bool:
        return bool(self.params)

    def pick(self, group: str, fam: str, kind: str) -> Pool | None:
        return self.params.get(f"{group}|{fam}|{kind}") or self.params.get(f"tutte|{fam}|{kind}")


def save_meta(store, params: dict[str, dict], start: datetime, end: datetime, model_version: str) -> int:
    store.db.executescript(SCHEMA)
    cur = store.db.execute(
        "INSERT INTO meta_models(created_at,window_start,window_end,model_version,params) VALUES(?,?,?,?,?) RETURNING id",
        (datetime.now(timezone.utc).isoformat(), start.isoformat(), end.isoformat(), model_version, json.dumps(params)))
    rid = int(cur.fetchone()[0])
    store.db.execute("DELETE FROM meta_models WHERE id NOT IN (SELECT id FROM meta_models ORDER BY id DESC LIMIT 12)")
    store.db.commit()
    return rid


def load_meta(store, model_version: str | None = None) -> MetaSet:
    """Latest fit (for this model version when given): the pools were learned on that model's probabilities."""
    try:
        sql, args = "SELECT id, params FROM meta_models", ()
        if model_version:
            sql, args = sql + " WHERE model_version = ?", (model_version,)
        row = store.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    except Exception as e:  # noqa: BLE001 - table created by the first weekly quality run
        if "no such table" in str(e).lower():
            return MetaSet()
        raise
    return MetaSet(json.loads(row[1]), f"meta-{row[0]}") if row else MetaSet()


def describe(p: Pool, fam: str) -> str:
    names = [s for _, s, _ in FAMILY_SELECTIONS[fam]]
    lean = ", ".join(f"{n} {math.exp(c) - 1:+.0%}" for n, c in zip(names[1:], p.c[1:]))
    if p.kind == "pool":
        return f"modello {p.a:.2f} · mercato {p.b:.2f} · nitidezza {p.a + p.b:.2f} · correzione {lean} (n={p.n})"
    return f"modello {p.a:.2f} · correzione {lean} (n={p.n})"
