"""Publication of analysis results for the web dashboard (web/, deployed on Vercel, reads the same Turso database).

A compact, read-optimised copy of each run: fixtures with the model's fair probabilities, the opportunities worth looking at
(STRONG / CANDIDATE / WATCH) and the proposed slips, or the NO BET reasons. Old runs are pruned: the dashboard only needs
recent ones, and the full audit trail stays in the snapshot tables.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import Config
from .domain import OpportunityStatus, SelectionRef
from .engine import AnalysisResult, Engine
from .markets import probability
from .snapshots import SnapshotProvider, SnapshotStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS pub_runs(id INTEGER PRIMARY KEY, created_at TEXT, cutoff TEXT, horizon_days REAL, n_fixtures INTEGER,
  n_with_quotes INTEGER, no_bet INTEGER, reasons TEXT, status_counts TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS pub_fixtures(run_id INTEGER, fixture_id TEXT, kickoff TEXT, competition TEXT, home TEXT, away TEXT,
  p_home REAL, p_draw REAL, p_away REAL, p_over25 REAL, p_btts REAL, lineup_state TEXT, n_quotes INTEGER,
  PRIMARY KEY(run_id, fixture_id));
CREATE TABLE IF NOT EXISTS pub_opportunities(run_id INTEGER, fixture_id TEXT, kickoff TEXT, competition TEXT, match TEXT,
  market TEXT, bookmaker TEXT, odds REAL, fair_odds REAL, p_final REAL, p_market REAL, ev REAL, ev_lower REAL, uncertainty REAL,
  data_quality REAL, status TEXT, odds_stale INTEGER, lineup_state TEXT);
CREATE TABLE IF NOT EXISTS pub_slips(run_id INTEGER, rank INTEGER, total_odds REAL, joint_probability REAL, ev REAL, ev_lower REAL,
  stake REAL, legs TEXT, explanation TEXT, PRIMARY KEY(run_id, rank));
CREATE INDEX IF NOT EXISTS ix_pubfx ON pub_fixtures(run_id);
CREATE INDEX IF NOT EXISTS ix_pubop ON pub_opportunities(run_id);
"""

FAIR_REFS = {"p_home": SelectionRef(market_code="MATCH_1X2", selection="HOME"),
             "p_draw": SelectionRef(market_code="MATCH_1X2", selection="DRAW"),
             "p_away": SelectionRef(market_code="MATCH_1X2", selection="AWAY"),
             "p_over25": SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5),
             "p_btts": SelectionRef(market_code="BTTS", selection="YES")}
SHOWN = {OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE, OpportunityStatus.WATCH}


def live_config(cfg: Config | None = None) -> Config:
    cfg = cfg or Config()
    if cfg.ensemble.quote_window_hours is None:
        cfg.ensemble.quote_window_hours = 24.0  # analysis only on prices observed in the last 24h
    return cfg


def analyze_and_publish(store: SnapshotStore, cfg: Config | None = None, horizon_days: float = 3.0,
                        now: datetime | None = None, keep_days: int = 14) -> tuple[int, AnalysisResult]:
    cfg = live_config(cfg)
    t = now or datetime.now(timezone.utc)
    prov = SnapshotProvider(store)
    eng = Engine(prov, cfg, use_lineups=True)
    res = eng.analyze(None, t, t + timedelta(days=horizon_days), t)
    store.db.executescript(SCHEMA)

    by_fx: dict[str, list] = {}
    for o in res.opportunities:
        by_fx.setdefault(o.fixture_id, []).append(o)
    fx_rows = []
    for f in res.fixtures:
        fitted = eng.fit(f.competition, t)
        probs = {k: None for k in FAIR_REFS}
        if fitted and fitted[0].knows(f.home) and fitted[0].knows(f.away):
            m = fitted[0].score_matrix(f.home, f.away)
            probs = {k: round(probability(m, r), 4) for k, r in FAIR_REFS.items()}
        ops = by_fx.get(f.id, [])
        fx_rows.append((f.id, f.kickoff.isoformat(), f.competition, f.home, f.away, *probs.values(),
                        ops[0].lineup_state if ops else "none", len(ops)))

    cur = store.db.execute(
        "INSERT INTO pub_runs(created_at,cutoff,horizon_days,n_fixtures,n_with_quotes,no_bet,reasons,status_counts,notes) VALUES(?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), t.isoformat(), horizon_days, len(res.fixtures), len(by_fx), int(res.optimizer.no_bet),
         json.dumps(res.optimizer.reasons, ensure_ascii=False), json.dumps(res.status_counts()), json.dumps(res.notes, ensure_ascii=False)))
    run_id = int(cur.lastrowid)
    store._bulk("INSERT INTO pub_fixtures(run_id,fixture_id,kickoff,competition,home,away,p_home,p_draw,p_away,p_over25,p_btts,lineup_state,n_quotes)",
                [(run_id, *r) for r in fx_rows])
    store._bulk("INSERT INTO pub_opportunities(run_id,fixture_id,kickoff,competition,match,market,bookmaker,odds,fair_odds,p_final,p_market,ev,"
                "ev_lower,uncertainty,data_quality,status,odds_stale,lineup_state)",
                [(run_id, o.fixture_id, o.kickoff.isoformat(), o.competition, f"{o.home} - {o.away}", o.description, o.bookmaker, o.odds,
                  round(o.fair_odds, 3), round(o.p_final, 4), None if o.p_market is None else round(o.p_market, 4), round(o.ev, 4),
                  round(o.ev_lower, 4), round(o.uncertainty, 4), round(o.data_quality, 3), o.status.value, int(o.odds_stale), o.lineup_state)
                 for o in res.opportunities if o.status in SHOWN])
    store._bulk("INSERT INTO pub_slips(run_id,rank,total_odds,joint_probability,ev,ev_lower,stake,legs,explanation)",
                [(run_id, k + 1, round(s.total_odds, 3), round(s.joint_probability, 4), round(s.ev, 4), round(s.ev_lower, 4), round(s.stake, 2),
                  json.dumps([{"match": f"{o.home} - {o.away}", "competition": o.competition, "kickoff": o.kickoff.isoformat(),
                               "market": o.description, "odds": o.odds, "bookmaker": o.bookmaker, "p": round(o.p_final, 4)} for o in s.legs],
                             ensure_ascii=False),
                  json.dumps(s.explanation, ensure_ascii=False, default=str)) for k, s in enumerate(res.optimizer.slips)])
    prune(store, keep_days)
    return run_id, res


def prune(store: SnapshotStore, keep_days: int = 14) -> None:
    """Keep the last `keep_days` of published runs (and always the latest one)."""
    limit = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    old = [r[0] for r in store.db.execute("SELECT id FROM pub_runs WHERE created_at < ? AND id < (SELECT MAX(id) FROM pub_runs)",
                                          (limit,)).fetchall()]
    if not old:
        return
    marks = ",".join("?" * len(old))
    for t in ("pub_fixtures", "pub_opportunities", "pub_slips"):
        store.db.execute(f"DELETE FROM {t} WHERE run_id IN ({marks})", old)
    store.db.execute(f"DELETE FROM pub_runs WHERE id IN ({marks})", old)
    store.db.commit()


def last_publication(store: SnapshotStore) -> datetime | None:
    store.db.executescript(SCHEMA)
    row = store.db.execute("SELECT MAX(created_at) FROM pub_runs").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None
