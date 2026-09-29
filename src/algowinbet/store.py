"""Append-only SQLite persistence for predictions, slips, paper bets and audit events (spec 27, 37.4)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, created_at TEXT, cutoff TEXT, input_hash TEXT, config_json TEXT,
  model_version TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS opportunities(id INTEGER PRIMARY KEY, run_id INTEGER, fixture_id TEXT, selection_key TEXT,
  cutoff TEXT, odds REAL, p_final REAL, ev REAL, status TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS candidate_slips(id INTEGER PRIMARY KEY, run_id INTEGER, created_at TEXT, odds REAL,
  joint_probability REAL, ev REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS paper_bets(id INTEGER PRIMARY KEY, slip_id INTEGER, created_at TEXT, stake REAL, odds REAL,
  settled_at TEXT, result TEXT, payout REAL);
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY, ts TEXT, type TEXT, actor TEXT, payload_hash TEXT, payload TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def audit(self, type_: str, payload: dict, actor: str = "system") -> None:
        self.db.execute("INSERT INTO audit_events(ts,type,actor,payload_hash,payload) VALUES(?,?,?,?,?)",
                        (_now(), type_, actor, _h(payload), json.dumps(payload, default=str)))
        self.db.commit()

    def save_run(self, cutoff, config: dict, opps, slips, summary: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(created_at,cutoff,input_hash,config_json,model_version,summary_json) VALUES(?,?,?,?,?,?)",
            (_now(), cutoff.isoformat(), _h([o.model_dump() for o in opps]), json.dumps(config, default=str),
             config.get("model", {}).get("version", ""), json.dumps(summary, default=str)))
        run_id = cur.lastrowid
        self.db.executemany(
            "INSERT INTO opportunities(run_id,fixture_id,selection_key,cutoff,odds,p_final,ev,status,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            [(run_id, o.fixture_id, o.ref.key, cutoff.isoformat(), o.odds, o.p_final, o.ev, o.status.value,
              o.model_dump_json()) for o in opps])
        self.db.executemany(
            "INSERT INTO candidate_slips(run_id,created_at,odds,joint_probability,ev,payload) VALUES(?,?,?,?,?,?)",
            [(run_id, _now(), s.total_odds, s.joint_probability, s.ev, s.model_dump_json()) for s in slips])
        self.db.commit()
        self.audit("RUN_SAVED", {"run_id": run_id, "n_opps": len(opps), "n_slips": len(slips)})
        return run_id

    def paper_bet(self, slip_id: int, stake: float, odds: float) -> int:
        cur = self.db.execute("INSERT INTO paper_bets(slip_id,created_at,stake,odds) VALUES(?,?,?,?)",
                              (slip_id, _now(), stake, odds))
        self.db.commit()
        self.audit("PAPER_BET", {"slip_id": slip_id, "stake": stake, "odds": odds})
        return cur.lastrowid
