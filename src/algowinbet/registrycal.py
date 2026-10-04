"""Calibration of the paper registry against the market: is a gap between predicted and observed hit rate the model's
fault, or the matches' luck?

For every decided selection (won/lost) the model probability is compared with two market probabilities of the same
selection: Pinnacle at the close (fair, the sharpest) and Sisal at the close (fair). If the model and the market are off
by the same amount, the matches went that way (luck); if only the model is off, the model is miscalibrated.
Selections of the same match win and lose together, so every interval is grouped by match.
"""
from __future__ import annotations

import math
from collections import defaultdict

Z95 = 1.96


def _clustered(xs: list[float], groups: list[str]) -> tuple[float | None, float | None]:
    """Mean and cluster-robust 95% half-width."""
    n = len(xs)
    if not n:
        return None, None
    m = sum(xs) / n
    r: dict[str, float] = defaultdict(float)
    for x, g in zip(xs, groups):
        r[g] += x - m
    k = len(r)
    if k < 2:
        return m, None
    return m, Z95 * math.sqrt(k / (k - 1) * sum(v * v for v in r.values())) / n


def group_stats(rows: list[dict]) -> dict:
    """rows: fixture_id, p, y (1/0), pin (Pinnacle close fair or None), sis (Sisal close fair or None)."""
    fx = [r["fixture_id"] for r in rows]
    out = {"n": len(rows), "matches": len(set(fx))}
    out["p"] = sum(r["p"] for r in rows) / len(rows) if rows else None
    out["y"] = sum(r["y"] for r in rows) / len(rows) if rows else None
    out["gap_model"] = _clustered([r["y"] - r["p"] for r in rows], fx)
    for key in ("pin", "sis"):
        sub = [r for r in rows if r[key] is not None]
        out[f"n_{key}"] = len(sub)
        out[f"gap_{key}"] = _clustered([r["y"] - r[key] for r in sub], [r["fixture_id"] for r in sub])
        # model against the market on the very same selections: the part of the gap that is the model's own
        out[f"model_vs_{key}"] = _clustered([r["p"] - r[key] for r in sub], [r["fixture_id"] for r in sub])
    return out


def report(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Overall, by status, by market and by match day."""
    by: dict[str, dict[str, list[dict]]] = {"tutte": {"tutte": rows}, "stato": defaultdict(list), "mercato": defaultdict(list),
                                            "giorno": defaultdict(list)}
    for r in rows:
        by["stato"][r["status"]].append(r)
        by["mercato"][r["market"]].append(r)
        by["giorno"][r["kickoff"][:10]].append(r)
    return {dim: {k: group_stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))} for dim, groups in by.items()}


def load_rows(store) -> list[dict]:
    rows = store.db.execute(
        "SELECT fixture_id, sel_key, status, kickoff, p, result, close_fair, close_sisal_fair FROM paper_legs WHERE result IN ('won','lost')"
    ).fetchall()
    return [{"fixture_id": f, "market": k.split("|")[0], "status": st, "kickoff": ko or "", "p": p, "y": 1 if res == "won" else 0,
             "pin": pin, "sis": sis} for f, k, st, ko, p, res, pin, sis in rows]


def _pt(m: float | None, h: float | None) -> str:
    if m is None:
        return "–"
    return f"{m * 100:+.1f}" + (f" ±{h * 100:.1f}" if h is not None else "")


def print_report(rep: dict) -> None:
    print("Scarti in punti percentuali (esito − probabilità), ±intervallo 95% raggruppato per partita.")
    print("  modello: esito − modello · Pinnacle/Sisal: esito − chiusura senza margine · mod−Pin: modello − Pinnacle sulle stesse selezioni")
    for dim, groups in rep.items():
        print(f"\nper {dim}:")
        print(f"  {'gruppo':<24}{'sel':>5}{'part':>6}{'p mod':>8}{'vinte':>8}   {'modello':<14}{'Pinnacle':<14}{'Sisal':<14}{'mod−Pin':<14}")
        for k, g in groups.items():
            if dim != "tutte" and g["n"] < 5:
                continue
            print(f"  {k[:23]:<24}{g['n']:>5}{g['matches']:>6}{g['p'] * 100:>7.1f}%{g['y'] * 100:>7.1f}%   "
                  f"{_pt(*g['gap_model']):<14}{_pt(*g['gap_pin']):<14}{_pt(*g['gap_sis']):<14}{_pt(*g['model_vs_pin']):<14}")
