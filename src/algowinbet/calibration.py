"""Probability calibration + forecast metrics. Calibrators are fitted per market family on out-of-sample
predictions (temporal validation) and versioned."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

EPS = 1e-6


def _logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y) -> float:
    p, y = np.clip(np.asarray(p, float), EPS, 1 - EPS), np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_curve(p, y, bins: int = 10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append((float(p[m].mean()), float(y[m].mean()), int(m.sum())))
    return rows


def ece(p, y, bins: int = 10) -> float:
    rows = calibration_curve(p, y, bins)
    n = sum(r[2] for r in rows)
    return float(sum(abs(a - b) * c for a, b, c in rows) / n) if n else float("nan")


class Calibrator:
    version = "identity-v1"

    def transform(self, p):
        return np.asarray(p, float)

    def to_json(self) -> dict:
        return {"type": "identity"}


class PlattCalibrator(Calibrator):
    def __init__(self, a: float = 1.0, b: float = 0.0):
        self.a, self.b = a, b
        self.version = f"platt-a{a:.3f}-b{b:.3f}"

    @classmethod
    def fit(cls, p, y) -> "PlattCalibrator":
        x, y = _logit(np.asarray(p, float)), np.asarray(y, float)

        def nll(t):
            z = t[0] * x + t[1]
            return float(np.sum(np.logaddexp(0, z) - y * z)) + 0.5 * (t[0] - 1) ** 2  # weak prior toward identity

        r = minimize(nll, [1.0, 0.0], method="BFGS")
        return cls(float(r.x[0]), float(r.x[1]))

    def transform(self, p):
        return 1 / (1 + np.exp(-(self.a * _logit(np.asarray(p, float)) + self.b)))

    def to_json(self) -> dict:
        return {"type": "platt", "a": self.a, "b": self.b}


class IsotonicCalibrator(Calibrator):
    def __init__(self, xs, ys):
        self.xs, self.ys = np.asarray(xs, float), np.asarray(ys, float)
        self.version = f"isotonic-n{len(self.xs)}"

    @classmethod
    def fit(cls, p, y) -> "IsotonicCalibrator":
        order = np.argsort(p)
        xs, ys = np.asarray(p, float)[order], np.asarray(y, float)[order]
        # pool-adjacent-violators
        blocks = [[ys[k], 1.0, xs[k], xs[k]] for k in range(len(xs))]  # mean, weight, x_lo, x_hi
        out: list[list[float]] = []
        for b in blocks:
            out.append(b)
            while len(out) > 1 and out[-2][0] >= out[-1][0]:
                m2, w2, lo2, hi2 = out.pop()
                m1, w1, lo1, hi1 = out.pop()
                out.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, lo1, hi2])
        cx = [(b[2] + b[3]) / 2 for b in out]
        cy = [b[0] for b in out]
        return cls(cx, cy)

    def transform(self, p):
        return np.interp(np.asarray(p, float), self.xs, self.ys)

    def to_json(self) -> dict:
        return {"type": "isotonic", "xs": self.xs.tolist(), "ys": self.ys.tolist()}


def calibrator_from_json(d: dict) -> Calibrator:
    t = d["type"]
    if t == "platt":
        return PlattCalibrator(d["a"], d["b"])
    if t == "isotonic":
        return IsotonicCalibrator(d["xs"], d["ys"])
    return Calibrator()


class CalibrationSet:
    def __init__(self, by_family: dict[str, Calibrator] | None = None):
        self.by_family = by_family or {}

    def transform(self, family: str, p: float) -> float:
        c = self.by_family.get(family)
        return float(c.transform([p])[0]) if c else p

    def version(self, family: str) -> str:
        c = self.by_family.get(family)
        return c.version if c else "identity-v1"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({k: v.to_json() for k, v in self.by_family.items()}, indent=1))

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationSet":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls({k: calibrator_from_json(v) for k, v in json.loads(p.read_text()).items()})


def fit_ensemble_weight(p_struct, p_market, y, grid: int = 21) -> tuple[float, float]:
    """Weight on the structural model (rest on market) minimising log loss. Returns (w_struct, logloss).
    w_struct ~ 0 means the structural model adds nothing beyond the price."""
    ps, pm, y = np.asarray(p_struct, float), np.asarray(p_market, float), np.asarray(y, float)
    best = (0.0, math.inf)
    for w in np.linspace(0, 1, grid):
        ll = log_loss(w * ps + (1 - w) * pm, y)
        if ll < best[1]:
            best = (float(w), ll)
    return best
