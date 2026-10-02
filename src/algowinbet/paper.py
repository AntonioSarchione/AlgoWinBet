"""Paper trading registry (Fase 6): every proposal is written down when it is first made, then settled by the results.

Nothing here can change a proposal after the fact:
  - a selection (Alta, Media or Equa) is recorded once, the first time a published analysis shows it, with the Sisal price,
    the probability and the versions of that moment; later analyses never overwrite it;
  - a slip is recorded once per set of selections, with its price, Sisal bonus and EV;
  - after the match the selection is settled from the final score; the closing prices come from the free price history:
      CLV Sisal ..... taken odds / Sisal closing odds - 1 (positive: we took a better price than the closing one)
      EV a chiusura . taken odds x Pinnacle closing fair probability - 1 (the sharpest available estimate of real value)
Flat stakes of 1 unit, paper only: no bet is ever placed.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from .domain import OpportunityStatus, SelectionRef
from .markets import UnsupportedMarket, complete_group, outcome, void_outcome
from .pricing import devig

RECORDED = {OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE, OpportunityStatus.FAIR}
SETTLE_AFTER = timedelta(hours=2, minutes=30)  # from kickoff: the match is over
GIVE_UP = timedelta(days=7)  # no result after a week (abandoned, unknown id): closed as not settleable
SHARP = ("pinnacle", "betfair-ex")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_legs(id INTEGER PRIMARY KEY, fixture_id TEXT, sel_key TEXT, competition TEXT, match TEXT,
  kickoff TEXT, market TEXT, status TEXT, odds REAL, bookmaker TEXT, p REAL, p_market REAL, ev REAL, run_id INTEGER,
  created_at TEXT, model_version TEXT, meta_version TEXT, result TEXT, settled_at TEXT, score TEXT, close_odds REAL,
  close_fair REAL, UNIQUE(fixture_id, sel_key));
CREATE INDEX IF NOT EXISTS ix_paper_open ON paper_legs(result, kickoff);
CREATE TABLE IF NOT EXISTS paper_slips(id INTEGER PRIMARY KEY, slip_key TEXT UNIQUE, run_id INTEGER, created_at TEXT, legs TEXT,
  total_odds REAL, bonus REAL, joint REAL, ev REAL, ev_lower REAL, first_kickoff TEXT, last_kickoff TEXT, result TEXT,
  payout REAL, settled_at TEXT, clv REAL);
"""


def _ref(sel_key: str) -> SelectionRef:
    code, sel, line = sel_key.split("|")
    return SelectionRef(market_code=code, selection=sel, line=float(line) if line else None)


def register(store, run_id: int, res, versions: dict) -> tuple[int, int]:
    """Records the selections and slips of a published analysis that were never recorded before. Returns (legs, slips) added."""
    store.db.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    meta_v = str(versions.get("meta"))
    legs = [(o.fixture_id, o.ref.key, o.competition, f"{o.home} - {o.away}", o.kickoff.isoformat(), o.description, o.status.value,
             o.odds, o.bookmaker, round(o.p_final, 5), None if o.p_market is None else round(o.p_market, 5), round(o.ev, 5), run_id, now,
             versions.get("model"), meta_v) for o in res.opportunities if o.status in RECORDED]
    n_legs = store._bulk("INSERT OR IGNORE INTO paper_legs(fixture_id,sel_key,competition,match,kickoff,market,status,odds,bookmaker,p,"
                         "p_market,ev,run_id,created_at,model_version,meta_version)", legs)
    slips = []
    for s in res.optimizer.slips:
        key = " + ".join(sorted(f"{l.fixture_id}|{l.ref.key}" for l in s.legs))
        detail = [{"fixture_id": l.fixture_id, "sel_key": l.ref.key, "match": f"{l.home} - {l.away}", "market": l.description,
                   "odds": l.odds, "p": round(l.p_final, 5), "status": l.status.value} for l in s.legs]
        kos = sorted(l.kickoff.isoformat() for l in s.legs)
        slips.append((key, run_id, now, json.dumps(detail, ensure_ascii=False), round(s.total_odds, 4), s.bonus, round(s.joint_probability, 6),
                      round(s.ev, 5), round(s.ev_lower, 5), kos[0], kos[-1]))
    n_slips = store._bulk("INSERT OR IGNORE INTO paper_slips(slip_key,run_id,created_at,legs,total_odds,bonus,joint,ev,ev_lower,"
                          "first_kickoff,last_kickoff)", slips)
    return n_legs, n_slips


def _closing(store, fixture_id: str, ref: SelectionRef, book_like: str) -> list[tuple]:
    line_key = "" if ref.line is None else repr(ref.line)
    return store.db.execute(
        "SELECT selection, odds, observed_at FROM quotes WHERE fixture_id = ? AND market_code = ? AND line_key = ? AND kind = 'close' "
        "AND lower(bookmaker) LIKE ? ORDER BY observed_at", (fixture_id, ref.market_code, line_key, f"%{book_like}%")).fetchall()


def closing_prices(store, fixture_id: str, ref: SelectionRef) -> tuple[float | None, float | None]:
    """(Sisal closing odds of the selection, sharp closing fair probability of the selection) when stored."""
    sisal = {sel: odds for sel, odds, _ in _closing(store, fixture_id, ref, "sisal")}
    fair = None
    group = complete_group(ref.market_code)
    for book in SHARP:
        prices = {sel: odds for sel, odds, _ in _closing(store, fixture_id, ref, book)}
        if group and set(group) <= set(prices):
            fair = devig({s: prices[s] for s in group}).get(ref.selection)
            break
    return sisal.get(ref.selection), fair


def settle(store, provider, now: datetime | None = None) -> dict[str, int]:
    """Settles the recorded selections whose match is over, then the slips whose selections are all settled."""
    store.db.executescript(SCHEMA)
    t = now or datetime.now(timezone.utc)
    rows = store.db.execute("SELECT id, fixture_id, sel_key, kickoff FROM paper_legs WHERE result IS NULL AND kickoff <= ?",
                            ((t - SETTLE_AFTER).isoformat(),)).fetchall()
    done = {"won": 0, "lost": 0, "void": 0, "non valutabile": 0}
    updates = []
    for lid, fid, key, ko in rows:
        r = provider.result_of(fid)
        if r is None:
            if t - datetime.fromisoformat(ko) > GIVE_UP:
                updates.append(("non valutabile", t.isoformat(), None, None, None, lid))
                done["non valutabile"] += 1
            continue
        ref = _ref(key)
        try:
            if void_outcome(r.home_goals, r.away_goals, ref):
                res = "void"
            else:
                res = "won" if outcome(r.home_goals, r.away_goals, ref) else "lost"
        except UnsupportedMarket:
            res = "non valutabile"  # half-time markets, first/last goal: the final score does not settle them
        close_odds, close_fair = closing_prices(store, fid, ref)
        updates.append((res, t.isoformat(), f"{r.home_goals}-{r.away_goals}", close_odds, close_fair, lid))
        done[res] += 1
    for u in updates:
        store.db.execute("UPDATE paper_legs SET result=?, settled_at=?, score=?, close_odds=?, close_fair=? WHERE id=?", u)
    store.db.commit()
    done["slips"] = _settle_slips(store, t)
    return done


def _settle_slips(store, t: datetime, bonus_table: list[float] | None = None, bonus_min_odds: float = 1.25) -> int:
    from .config import OptimizerCfg
    o = OptimizerCfg()
    table = bonus_table if bonus_table is not None else o.multi_bonus
    open_slips = store.db.execute("SELECT id, legs FROM paper_slips WHERE result IS NULL AND last_kickoff <= ?",
                                  ((t - SETTLE_AFTER).isoformat(),)).fetchall()
    n = 0
    for sid, legs_json in open_slips:
        legs = json.loads(legs_json)
        got = {}
        for l in legs:
            row = store.db.execute("SELECT result, close_odds FROM paper_legs WHERE fixture_id = ? AND sel_key = ?",
                                   (l["fixture_id"], l["sel_key"])).fetchone()
            got[(l["fixture_id"], l["sel_key"])] = row
        if any(v is None or v[0] is None for v in got.values()):
            continue  # a selection not settled yet (or recorded later than the slip): wait
        results = [got[(l["fixture_id"], l["sel_key"])][0] for l in legs]
        if "lost" in results:
            result, payout = "lost", 0.0  # one losing selection settles the slip, whatever the others
        elif "non valutabile" in results:
            result, payout = "non valutabile", None
        else:
            live = [l for l, r in zip(legs, results) if r == "won"]  # void selections drop out at odds 1
            odds = math.prod(l["odds"] for l in live) if live else 1.0
            nb = len(live)
            bonus = table[min(nb, 4 + len(table)) - 5] if nb >= 5 and table and all(l["odds"] >= bonus_min_odds for l in live) else 0.0
            result, payout = ("won" if live else "void"), 1 + (odds - 1) * (1 + bonus)
        closes = [got[(l["fixture_id"], l["sel_key"])][1] for l in legs]
        clv = (math.prod(l["odds"] for l in legs) / math.prod(closes) - 1) if all(closes) else None
        store.db.execute("UPDATE paper_slips SET result=?, payout=?, settled_at=?, clv=? WHERE id=?", (result, payout, t.isoformat(), clv, sid))
        n += 1
    store.db.commit()
    return n
