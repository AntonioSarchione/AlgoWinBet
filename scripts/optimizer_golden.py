"""Golden cases for the dashboard's TypeScript slip optimizer (web/lib/optimizer.ts).

Runs the Python optimizer + stake assignment on real engine opportunities (mock data, relaxed thresholds so slips exist) under
several filter settings and writes inputs and expected slips to web/scripts/optimizer.golden.json. The web parity test
(web/scripts/optimizer-parity.ts) must reproduce every case; tests/test_optimizer_golden.py fails when this file is stale.

    python scripts/optimizer_golden.py
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from algowinbet.config import Config
from algowinbet.engine import Engine
from algowinbet.optimizer import optimize
from algowinbet.providers import MockProvider
from algowinbet.publish import optimizer_settings
from algowinbet.risk import assign_stakes
from algowinbet.snapshots import SnapshotProvider, SnapshotStore

OUT = Path(__file__).resolve().parents[1] / "web" / "scripts" / "optimizer.golden.json"
CASES = [
    {"name": "default", "odds_min": None, "odds_max": None, "max_legs": 10},
    {"name": "doubles", "odds_min": None, "odds_max": None, "max_legs": 2},
    {"name": "singles", "odds_min": 1.2, "odds_max": 3.0, "max_legs": 1},
    {"name": "narrow-odds", "odds_min": 3.0, "odds_max": 6.0, "max_legs": 4},
    {"name": "long", "odds_min": 8.0, "odds_max": 40.0, "max_legs": 6},
    # 5+ legs at odds >= 1.25: the Sisal multiple bonus enters ev, ev_lower and the stake
    {"name": "min-legs", "odds_min": None, "odds_max": None, "max_legs": 6, "min_legs": 3, "min_slip_ev": -0.6},
    {"name": "profile-probabilita", "odds_min": None, "odds_max": None, "max_legs": 6, "profile": "probabilita"},
    {"name": "profile-value", "odds_min": None, "odds_max": None, "max_legs": 6, "profile": "value"},
    {"name": "edge-shrink", "odds_min": None, "odds_max": None, "max_legs": 4, "edge_shrink": 0.3},
    {"name": "bonus", "odds_min": 8.0, "odds_max": 60.0, "max_legs": 8, "min_slip_ev": -0.6,
     "multi_bonus_min_odds": 1.0},
    # one competition counted as national teams: never in a slip with the others
    {"name": "nazionali-club", "odds_min": None, "odds_max": None, "max_legs": 6, "national": True},
    # the 45% floor off: low-probability legs, the national value rule alone
    {"name": "leg-floor-off", "odds_min": None, "odds_max": None, "max_legs": 6, "national": True, "min_leg_probability": 0.0},
]


def opp_record(o) -> dict:
    return {"fixture_id": o.fixture_id, "sel_key": o.ref.key, "competition": o.competition, "home": o.home, "away": o.away,
            "odds": o.odds, "p_final": o.p_final, "p_struct": o.p_struct, "p_market": o.p_market, "uncertainty": o.uncertainty,
            "disagreement": o.model_disagreement, "score": o.score, "status": o.status.value,
            "dq_lineup": o.data_quality_parts.get("lineup", 0.0)}


def build() -> dict:
    mock = MockProvider(seed=7)
    s = SnapshotStore(":memory:")
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72
    t = cfg.thresholds
    t.min_ev, t.max_uncertainty, t.min_dq_candidate, t.min_dq_strong, t.min_edge = 0.0, 0.5, 0.0, 0.0, -1.0
    t.fair_ev = -0.05  # a mix of value (CANDIDATE/STRONG) and fair-price (FAIR) selections
    cfg.optimizer.min_slip_ev = -0.2
    res = Engine(SnapshotProvider(s), cfg).analyze(None, mock.as_of, mock.as_of + timedelta(days=3), mock.as_of)
    out = {"settings": optimizer_settings(cfg), "opportunities": [opp_record(o) for o in res.opportunities], "cases": []}
    for case in CASES:
        c = cfg.with_slip_profile(case["profile"]) if case.get("profile") else cfg.model_copy(deep=True)
        c.optimizer.edge_shrink = case.get("edge_shrink", c.optimizer.edge_shrink)
        c.optimizer.max_legs = case["max_legs"]
        c.optimizer.odds_min = case["odds_min"] or c.optimizer.odds_min
        c.optimizer.odds_max = case["odds_max"] or c.optimizer.odds_max
        c.optimizer.min_slip_ev = case.get("min_slip_ev", c.optimizer.min_slip_ev)
        c.optimizer.min_legs = case.get("min_legs", c.optimizer.min_legs)
        c.optimizer.multi_bonus_min_odds = case.get("multi_bonus_min_odds", c.optimizer.multi_bonus_min_odds)
        c.optimizer.min_leg_probability = case.get("min_leg_probability", c.optimizer.min_leg_probability)
        if case.get("national"):
            case["national_competitions"] = [sorted({o.competition for o in res.opportunities})[0]]
            c.optimizer.national_competitions = case["national_competitions"]
        r = optimize(res.opportunities, res.analyses, c)
        assign_stakes(r.slips, c.risk)
        out["cases"].append({**case, "no_bet": r.no_bet, "slips": [
            {"legs": [[l.fixture_id, l.ref.key] for l in sl.legs], "total_odds": sl.total_odds, "joint_probability": sl.joint_probability,
             "ev": sl.ev, "ev_lower": sl.ev_lower, "objective": sl.objective, "stake": sl.stake, "bonus": sl.bonus} for sl in r.slips]})
    return out


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"scritto {OUT}")
