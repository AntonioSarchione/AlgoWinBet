"""Publication of analysis results for the web dashboard (web/, deployed on Vercel, reads the same Turso database).

A compact, read-optimised copy of each run: fixtures with the model's fair probabilities, the opportunities worth looking at
(STRONG / CANDIDATE / WATCH) and the proposed slips, or the NO BET reasons. Old runs are pruned: the dashboard only needs
recent ones, and the full audit trail stays in the snapshot tables.
"""
from __future__ import annotations

import json

import numpy as np
from datetime import datetime, timedelta, timezone

from .config import Config
from .domain import OpportunityStatus, SelectionRef
from .engine import AnalysisResult, Engine
from .explain import explain_leg
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
-- manual slips: every playable selection of a match (any status, probability >= min_probability), one row per match; kept
-- for the last two runs only
CREATE TABLE IF NOT EXISTS pub_book(run_id INTEGER, fixture_id TEXT, sels TEXT, PRIMARY KEY(run_id, fixture_id));
CREATE INDEX IF NOT EXISTS ix_pubfx ON pub_fixtures(run_id);
CREATE INDEX IF NOT EXISTS ix_pubop ON pub_opportunities(run_id);
"""

FAIR_REFS = {"p_home": SelectionRef(market_code="MATCH_1X2", selection="HOME"),
             "p_draw": SelectionRef(market_code="MATCH_1X2", selection="DRAW"),
             "p_away": SelectionRef(market_code="MATCH_1X2", selection="AWAY"),
             "p_over25": SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5),
             "p_btts": SelectionRef(market_code="BTTS", selection="YES")}
# Columns added after the first release: added in place on existing databases (see _migrate).
EXTRA_COLUMNS = {"pub_fixtures": {"xg_home": "REAL", "xg_away": "REAL", "markets": "TEXT", "book": "TEXT", "rho": "REAL", "estimated": "INTEGER",
                                  "scorers": "TEXT", "trends": "TEXT"},
                 "pub_opportunities": {"p_struct": "REAL", "p_low": "REAL", "p_high": "REAL", "n_books": "INTEGER", "edge": "REAL",
                                       "factors": "TEXT", "sel_key": "TEXT", "home": "TEXT", "away": "TEXT", "score": "REAL",
                                       "disagreement": "REAL", "dq_lineup": "REAL"},
                 "pub_slips": {"horizon_h": "INTEGER", "max_legs": "INTEGER"},
                 "pub_runs": {"optimizer": "TEXT", "versions": "TEXT"}}
SHOWN = {OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE, OpportunityStatus.FAIR, OpportunityStatus.WATCH}


def live_config(cfg: Config | None = None) -> Config:
    cfg = cfg or Config()
    if cfg.ensemble.quote_window_hours is None:
        cfg.ensemble.quote_window_hours = 24.0  # analysis only on prices observed in the last 24h
    if not cfg.bet_bookmakers:
        cfg.bet_bookmakers = ["sisal"]  # the user bets on Sisal only; Pinnacle is a reference price, never a proposal
    return cfg


def _migrate(store: SnapshotStore) -> None:
    store.db.executescript(SCHEMA)
    for table, cols in EXTRA_COLUMNS.items():
        have = {r[1] for r in store.db.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, kind in cols.items():
            if name not in have:
                store.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    store.db.commit()


def model_markets(m: np.ndarray) -> list[dict]:
    """Fair probabilities of the goal markets the dashboard shows, all read from one score matrix (model only, no price).
    Multigol and same-match combos are exact sums over the joint score distribution."""
    n = m.shape[0]
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    tot = i + j
    home, draw, away, gg = i > j, i == j, i < j, (i > 0) & (j > 0)
    p = lambda mask: round(float(m[mask].sum()), 4)
    out = [{"g": "1X2", "l": "1", "p": p(home)}, {"g": "1X2", "l": "X", "p": p(draw)}, {"g": "1X2", "l": "2", "p": p(away)},
           {"g": "Doppia chance", "l": "1X", "p": p(i >= j)}, {"g": "Doppia chance", "l": "X2", "p": p(i <= j)},
           {"g": "Doppia chance", "l": "12", "p": p(i != j)},
           {"g": "Gol/NoGol", "l": "Gol", "p": p(gg)}, {"g": "Gol/NoGol", "l": "NoGol", "p": p(~gg)}]
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        out += [{"g": "Under/Over", "l": f"Over {line}", "p": p(tot > line)}, {"g": "Under/Over", "l": f"Under {line}", "p": p(tot < line)}]
    for a, b in ((1, 2), (1, 3), (2, 3), (2, 4), (3, 5), (4, 6)):
        out.append({"g": "Multigol", "l": f"Multigol {a}-{b}", "p": p((tot >= a) & (tot <= b))})
    for side, g in (("casa", i), ("ospite", j)):
        for line in (0.5, 1.5, 2.5):
            out.append({"g": "Gol squadra", "l": f"Over {line} {side}", "p": p(g > line)})
    for label, mask in (("1 + Over 2.5", home & (tot > 2.5)), ("2 + Over 2.5", away & (tot > 2.5)), ("1 + Gol", home & gg),
                        ("2 + Gol", away & gg), ("Gol + Over 2.5", gg & (tot > 2.5)), ("1X + Under 3.5", (i >= j) & (tot < 3.5)),
                        ("X2 + Under 3.5", (i <= j) & (tot < 3.5)), ("1X + Over 1.5", (i >= j) & (tot > 1.5)),
                        ("X2 + Over 1.5", (i <= j) & (tot > 1.5)), ("NoGol + Under 2.5", ~gg & (tot < 2.5))):
        out.append({"g": "Combo", "l": label, "p": p(mask)})
    for h in (-1, 1):
        adj = i + h
        out += [{"g": f"Handicap europeo {h:+d}", "l": lab, "p": p(msk)} for lab, msk in (("1", adj > j), ("X", adj == j), ("2", adj < j))]
    out += [{"g": "Pari/Dispari", "l": "Dispari", "p": p(tot % 2 == 1)}, {"g": "Pari/Dispari", "l": "Pari", "p": p(tot % 2 == 0)}]
    flat = sorted(((float(m[a, b]), a, b) for a in range(min(n, 7)) for b in range(min(n, 7))), reverse=True)[:8]
    out += [{"g": "Risultato esatto", "l": f"{a}-{b}", "p": round(v, 4)} for v, a, b in flat]
    return out


def book_prices(ops: list) -> str | None:
    """Per headline selection (FAIR_REFS names): the playable Sisal price, the devigged market probability and the final
    probability the slips use (model shrunk toward the market). The dashboard shows them next to the pure model."""
    keys = {r.key: name for name, r in FAIR_REFS.items()}
    out = {keys[o.ref.key]: {"odds": o.odds, "book": o.bookmaker, "pm": None if o.p_market is None else round(o.p_market, 4),
                             "pf": round(o.p_final, 4)} for o in ops if o.ref.key in keys}
    return json.dumps(out) if out else None


def book_rows(ops: list, min_probability: float) -> str | None:
    """All playable selections of one match for the dashboard's manual slips, compact: the optimizer fields and what the
    page shows. Selections below min_probability are left out (never proposed, see Thresholds.min_probability)."""
    out = [{"k": o.ref.key, "m": o.description, "o": o.odds, "b": o.bookmaker, "p": round(o.p_final, 4), "s": round(o.p_struct, 4),
            "pm": None if o.p_market is None else round(o.p_market, 4), "e": round(o.ev, 4), "u": round(o.uncertainty, 4),
            "sc": round(o.score, 6), "d": round(o.model_disagreement, 6), "l": o.data_quality_parts.get("lineup", 0.0), "st": o.status.value}
           for o in ops if o.status != OpportunityStatus.INVALID and o.p_final >= min_probability]
    return json.dumps(out, ensure_ascii=False, separators=(",", ":")) if out else None


def optimizer_settings(cfg: Config) -> dict:
    """Everything web/lib/optimizer.ts needs to reproduce optimize() + assign_stakes() on the published opportunities."""
    return {"optimizer": cfg.optimizer.model_dump(), "z": cfg.thresholds.z, "risk": cfg.risk.model_dump()}


def analyze_and_publish(store: SnapshotStore, cfg: Config | None = None, horizon_days: float = 7.0,
                        now: datetime | None = None, keep_days: int = 14) -> tuple[int, AnalysisResult]:
    cfg = live_config(cfg)
    t = now or datetime.now(timezone.utc)
    prov = SnapshotProvider(store)
    from .meta import load_meta
    meta = load_meta(store, cfg.model.version) if cfg.ensemble.use_meta else None
    if meta is not None and meta.shrink:
        cfg.optimizer.edge_shrink = float(meta.shrink["lambda"])
    eng = Engine(prov, cfg, use_lineups=True, meta=meta)
    res = eng.analyze(None, t, t + timedelta(days=horizon_days), t)
    _migrate(store)

    by_fx: dict[str, list] = {}
    for o in res.opportunities:
        by_fx.setdefault(o.fixture_id, []).append(o)
    est_fx: dict[str, list] = {}  # matches Sisal does not price on the feed: estimated prices, manual slips only
    for o in res.estimated:
        est_fx.setdefault(o.fixture_id, []).append(o)
    fx_rows = []
    xg_of: dict[str, tuple[float, float]] = {}
    markets_of: dict[str, list[dict]] = {}
    for f in res.fixtures:
        fitted = eng.fit(f.competition, t)
        probs = {k: None for k in FAIR_REFS}
        xg, markets, rho = (None, None), None, None
        if fitted and fitted[0].knows(f.home) and fitted[0].knows(f.away):
            m = fitted[0].score_matrix(f.home, f.away)
            probs = {k: round(probability(m, r), 4) for k, r in FAIR_REFS.items()}
            xg = tuple(round(float(x), 3) for x in fitted[0].expected_goals(f.home, f.away))
            xg_of[f.id] = xg
            markets_of[f.id] = model_markets(m)
            markets = json.dumps(markets_of[f.id], ensure_ascii=False)
            rho = round(float(fitted[0].rho), 5)  # with xg the dashboard rebuilds the score matrix (My Combo, same match)
        ops = by_fx.get(f.id) or est_fx.get(f.id, [])
        fx_rows.append((f.id, f.kickoff.isoformat(), f.competition, f.home, f.away, *probs.values(),
                        ops[0].lineup_state if ops else "none", len(ops), *xg, markets, book_prices(ops), rho,
                        int(f.id not in by_fx and f.id in est_fx)))

    # goalscorer table (Fase 9): never a reason to lose the publication
    try:
        import time as _time
        from .scorerpub import scorers_json, team_scorers
        t0 = _time.monotonic()
        sc = team_scorers(store, prov, res.fixtures, xg_of, t)
        print(f"marcatori: {len(sc)} partite in {_time.monotonic() - t0:.0f}s", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"marcatori: non calcolati ({type(e).__name__}: {e})", flush=True)
        sc = {}
    # statistical streaks of every match ("ritardi"): same rule
    try:
        import time as _time
        from .trends import match_trends
        t0 = _time.monotonic()
        tr = match_trends(store, prov, res.fixtures, markets_of, t)
        print(f"ritardi: {len(tr)} partite in {_time.monotonic() - t0:.0f}s", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"ritardi: non calcolati ({type(e).__name__}: {e})", flush=True)
        tr = {}
    fx_rows = [(*r, scorers_json(sc.get(r[0])), tr.get(r[0])) for r in fx_rows]

    versions = {"model": cfg.model.version, "meta": meta.version if meta else "spento", "data": data_version(store)}
    cur = store.db.execute(
        "INSERT INTO pub_runs(created_at,cutoff,horizon_days,n_fixtures,n_with_quotes,no_bet,reasons,status_counts,notes,optimizer,versions) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (datetime.now(timezone.utc).isoformat(), t.isoformat(), horizon_days, len(res.fixtures), len(by_fx), int(res.optimizer.no_bet),
         json.dumps(res.optimizer.reasons, ensure_ascii=False), json.dumps(res.status_counts()), json.dumps(res.notes, ensure_ascii=False),
         json.dumps(optimizer_settings(cfg)), json.dumps(versions)))
    run_id = int(cur.fetchall()[0][0])  # not lastrowid: the remote libsql driver does not report it reliably
    store._bulk("INSERT INTO pub_fixtures(run_id,fixture_id,kickoff,competition,home,away,p_home,p_draw,p_away,p_over25,p_btts,lineup_state,n_quotes,xg_home,xg_away,markets,book,rho,estimated,scorers,trends)",
                [(run_id, *r) for r in fx_rows])
    # score, disagreement, sel_key, home/away and dq_lineup feed the dashboard's own slip optimizer (web/lib/optimizer.ts)
    store._bulk("INSERT INTO pub_opportunities(run_id,fixture_id,kickoff,competition,match,market,bookmaker,odds,fair_odds,p_final,p_market,ev,"
                "ev_lower,uncertainty,data_quality,status,odds_stale,lineup_state,p_struct,p_low,p_high,n_books,edge,factors,sel_key,home,away,"
                "score,disagreement,dq_lineup)",
                [(run_id, o.fixture_id, o.kickoff.isoformat(), o.competition, f"{o.home} - {o.away}", o.description, o.bookmaker, o.odds,
                  round(o.fair_odds, 3), round(o.p_final, 4), None if o.p_market is None else round(o.p_market, 4), round(o.ev, 4),
                  round(o.ev_lower, 4), round(o.uncertainty, 4), round(o.data_quality, 3), o.status.value, int(o.odds_stale), o.lineup_state,
                  round(o.p_struct, 4), round(o.p_low, 4), round(o.p_high, 4), o.n_books, None if o.edge is None else round(o.edge, 4),
                  json.dumps({k: explain_leg(o)[k] for k in ("positive_factors", "negative_factors")}, ensure_ascii=False),
                  o.ref.key, o.home, o.away, round(o.score, 6), round(o.model_disagreement, 6), o.data_quality_parts.get("lineup", 0.0))
                 for o in res.opportunities if o.status in SHOWN])
    book = [(run_id, fid, js) for fid, ops in {**est_fx, **by_fx}.items() if (js := book_rows(ops, cfg.thresholds.min_probability))]
    store._bulk("INSERT OR REPLACE INTO pub_book(run_id,fixture_id,sels)", book)
    store._bulk("INSERT INTO pub_slips(run_id,rank,total_odds,joint_probability,ev,ev_lower,stake,legs,explanation,horizon_h,max_legs)",
                [(run_id, k + 1, round(s.total_odds, 3), round(s.joint_probability, 4), round(s.ev, 4), round(s.ev_lower, 4), round(s.stake, 2),
                  json.dumps([{"match": f"{o.home} - {o.away}", "competition": o.competition, "kickoff": o.kickoff.isoformat(),
                               "market": o.description, "odds": o.odds, "bookmaker": o.bookmaker, "p": round(o.p_final, 4)} for o in s.legs],
                             ensure_ascii=False),
                  json.dumps(s.explanation, ensure_ascii=False, default=str), None, None) for k, s in enumerate(res.optimizer.slips)])
    store.db.commit()  # a run with no rows would otherwise stay uncommitted on remote libsql
    from .optimizer import optimize
    from .paper import register
    by_profile = {name: (res.optimizer.slips if name == cfg.optimizer.profile else
                         optimize(res.opportunities, res.analyses, cfg.with_slip_profile(name)).slips)
                  for name in cfg.optimizer.profiles}
    register(store, run_id, res, versions, by_profile)  # paper trading: first appearance of every proposal, never rewritten
    prune(store, keep_days)
    return run_id, res


def data_version(store: SnapshotStore) -> dict:
    """What the analysis was computed on, cheap to read on Turso: the last stored request (every payload has an id, so the
    same id means the same inputs), the results known and the latest result."""
    raw = store.db.execute("SELECT MAX(id) FROM raw_requests").fetchone()
    res = store.db.execute("SELECT COUNT(*), MAX(kickoff) FROM results").fetchone()
    return {"last_request": raw[0] if raw else None, "results": res[0] if res else 0, "last_result": res[1] if res else None}


def prune(store: SnapshotStore, keep_days: int = 14) -> None:
    """Keep the last `keep_days` of published runs (and always the latest one); the manual-slip book only for the last two."""
    store.db.execute("DELETE FROM pub_book WHERE run_id NOT IN (SELECT id FROM pub_runs ORDER BY id DESC LIMIT 2)")
    store.db.commit()
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
    _migrate(store)
    row = store.db.execute("SELECT MAX(created_at) FROM pub_runs").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None
