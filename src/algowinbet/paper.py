"""Paper trading registry (Fase 6): every proposal is written down when it is first made, then settled by the results.

Nothing here can change a proposal after the fact:
  - a selection (Alta, Media or Equa) is recorded once, the first time a published analysis shows it, with the Sisal price,
    the probability and the versions of that moment; later analyses never overwrite it;
  - a slip is recorded once per set of selections, with its price, Sisal bonus and EV;
  - after the match the selection is settled from the final score; the closing prices come from the free price history:
      CLV Sisal ..... taken odds / Sisal closing odds - 1 (positive: we took a better price than the closing one)
      EV a chiusura . taken odds x Pinnacle closing fair probability - 1 (the sharpest available estimate of real value)
      Sisal equa .... Sisal closing probability without margin: the benchmark of the log loss in the pass criterion
Flat stakes of 1 unit, paper only: no bet is ever placed.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from .domain import OpportunityStatus, SelectionRef
from .markets import (UnsupportedMarket, complete_group, needs_goal_order, needs_half_time, outcome, sequence_outcome, stat_of,
                      stat_outcome, void_outcome)
from .pricing import devig

RECORDED = {OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE, OpportunityStatus.FAIR}
SETTLE_AFTER = timedelta(hours=2, minutes=30)  # from kickoff: the match is over
GIVE_UP = timedelta(days=7)  # no result after a week (abandoned, unknown id): closed as not settleable
DETAIL_WAIT = timedelta(days=2)  # half-time score / goal order still missing after this: closed as not settleable
STAT_WAIT = timedelta(days=6)  # corners / cards: the season files arrive a few days after the match
SHARP = ("pinnacle", "betfair-ex")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_legs(id INTEGER PRIMARY KEY, fixture_id TEXT, sel_key TEXT, competition TEXT, match TEXT,
  kickoff TEXT, market TEXT, status TEXT, odds REAL, bookmaker TEXT, p REAL, p_market REAL, ev REAL, run_id INTEGER,
  created_at TEXT, model_version TEXT, meta_version TEXT, result TEXT, settled_at TEXT, score TEXT, close_odds REAL,
  close_fair REAL, close_sisal_fair REAL, UNIQUE(fixture_id, sel_key));
CREATE INDEX IF NOT EXISTS ix_paper_open ON paper_legs(result, kickoff);
CREATE TABLE IF NOT EXISTS paper_slips(id INTEGER PRIMARY KEY, slip_key TEXT UNIQUE, run_id INTEGER, created_at TEXT, legs TEXT,
  total_odds REAL, bonus REAL, joint REAL, ev REAL, ev_lower REAL, first_kickoff TEXT, last_kickoff TEXT, result TEXT,
  payout REAL, settled_at TEXT, clv REAL, profile TEXT);
"""


def _migrate(store) -> None:
    store.db.executescript(SCHEMA)
    have = {r[1] for r in store.db.execute("PRAGMA table_info(paper_slips)").fetchall()}
    if "profile" not in have:
        store.db.execute("ALTER TABLE paper_slips ADD COLUMN profile TEXT")
        store.db.commit()
    have = {r[1] for r in store.db.execute("PRAGMA table_info(paper_legs)").fetchall()}
    if "close_sisal_fair" not in have:
        store.db.execute("ALTER TABLE paper_legs ADD COLUMN close_sisal_fair REAL")
        rows = store.db.execute("SELECT id, fixture_id, sel_key FROM paper_legs WHERE result IN ('won', 'lost', 'void')").fetchall()
        for lid, fid, key in rows:  # selections settled before the column existed
            store.db.execute("UPDATE paper_legs SET close_sisal_fair=? WHERE id=?", (sisal_close_fair(store, fid, _ref(key)), lid))
        store.db.commit()


def _ref(sel_key: str) -> SelectionRef:
    code, sel, line = sel_key.split("|")
    return SelectionRef(market_code=code, selection=sel, line=float(line) if line else None)


def register(store, run_id: int, res, versions: dict, by_profile: dict[str, list] | None = None) -> tuple[int, int]:
    """Records the selections and slips of a published analysis that were never recorded before (slips of every profile when
    given, else the analysis' own). Returns (legs, slips) added."""
    _migrate(store)
    now = datetime.now(timezone.utc).isoformat()
    meta_v = str(versions.get("meta"))
    legs = [(o.fixture_id, o.ref.key, o.competition, f"{o.home} - {o.away}", o.kickoff.isoformat(), o.description, o.status.value,
             o.odds, o.bookmaker, round(o.p_final, 5), None if o.p_market is None else round(o.p_market, 5), round(o.ev, 5), run_id, now,
             versions.get("model"), meta_v) for o in res.opportunities if o.status in RECORDED]
    n_legs = store._bulk("INSERT OR IGNORE INTO paper_legs(fixture_id,sel_key,competition,match,kickoff,market,status,odds,bookmaker,p,"
                         "p_market,ev,run_id,created_at,model_version,meta_version)", legs)
    slips = []
    for profile, s in [(p, s) for p, ss in (by_profile or {"": res.optimizer.slips}).items() for s in ss]:
        key = " + ".join(sorted(f"{l.fixture_id}|{l.ref.key}" for l in s.legs))
        detail = [{"fixture_id": l.fixture_id, "sel_key": l.ref.key, "match": f"{l.home} - {l.away}", "market": l.description,
                   "odds": l.odds, "p": round(l.p_final, 5), "status": l.status.value} for l in s.legs]
        kos = sorted(l.kickoff.isoformat() for l in s.legs)
        slips.append((key, run_id, now, json.dumps(detail, ensure_ascii=False), round(s.total_odds, 4), s.bonus, round(s.joint_probability, 6),
                      round(s.ev, 5), round(s.ev_lower, 5), kos[0], kos[-1], profile or None))
    n_slips = store._bulk("INSERT OR IGNORE INTO paper_slips(slip_key,run_id,created_at,legs,total_odds,bonus,joint,ev,ev_lower,"
                          "first_kickoff,last_kickoff,profile)", slips)
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


def sisal_close_fair(store, fixture_id: str, ref: SelectionRef) -> float | None:
    """Sisal closing probability of the selection without the margin (all outcomes of the market quoted at the close)."""
    group = complete_group(ref.market_code)
    prices = {sel: odds for sel, odds, _ in _closing(store, fixture_id, ref, "sisal")}
    if not group or not set(group) <= set(prices):
        return None
    return devig({s: prices[s] for s in group}).get(ref.selection)


def settle(store, provider, now: datetime | None = None) -> dict[str, int]:
    """Settles the recorded selections whose match is over, then the slips whose selections are all settled."""
    _migrate(store)
    t = now or datetime.now(timezone.utc)
    # selections closed as not settleable stay open to a second look for a week: the half-time score or the goal order
    # can arrive later (API-Football)
    rows = store.db.execute("SELECT id, fixture_id, sel_key, kickoff, result FROM paper_legs WHERE kickoff <= ? AND (result IS NULL OR "
                            "(result = 'non valutabile' AND kickoff >= ?))",
                            ((t - SETTLE_AFTER).isoformat(), (t - GIVE_UP).isoformat())).fetchall()
    done = {"won": 0, "lost": 0, "void": 0, "non valutabile": 0}
    updates = []
    for lid, fid, key, ko, before in rows:
        r = provider.result_of(fid)
        if r is None:
            if t - datetime.fromisoformat(ko) > GIVE_UP:
                updates.append(("non valutabile", t.isoformat(), None, None, None, None, lid))
                done["non valutabile"] += 1
            continue
        ref = _ref(key)
        ht = _half_time(store, fid, r)
        stat = stat_of(ref.market_code)
        try:
            if stat:
                counts = _stat_count(store, fid, stat)
                if counts is None:
                    raise UnsupportedMarket(f"{stat}: counts not known yet")
                res = "won" if stat_outcome(counts[0], counts[1], ref) else "lost"
            elif needs_goal_order(ref.market_code):
                ft = store.stats_of(fid, "FT")
                won = sequence_outcome(r.home_goals, r.away_goals, ref, ft.get("first_goal_minute"), ft.get("last_goal_minute"))
                res = "won" if won else "lost"
            elif void_outcome(r.home_goals, r.away_goals, ref, ht):
                res = "void"
            else:
                res = "won" if outcome(r.home_goals, r.away_goals, ref, ht) else "lost"
        except UnsupportedMarket:
            age = t - datetime.fromisoformat(ko)
            waiting = (stat and age < STAT_WAIT) or (
                (needs_half_time(ref.market_code) or needs_goal_order(ref.market_code)) and age < DETAIL_WAIT)
            if before is not None or waiting:
                continue  # half-time score / goal order may still arrive; already closed as not settleable: leave it
            res = "non valutabile"  # what the scores cannot settle
        close_odds, close_fair = closing_prices(store, fid, ref)
        updates.append((res, t.isoformat(), f"{r.home_goals}-{r.away_goals}", close_odds, close_fair, sisal_close_fair(store, fid, ref), lid))
        done[res] += 1
    for u in updates:
        store.db.execute("UPDATE paper_legs SET result=?, settled_at=?, score=?, close_odds=?, close_fair=?, close_sisal_fair=? WHERE id=?", u)
    store.db.commit()
    done["slips"] = _settle_slips(store, t)
    return done


def _stat_count(store, fixture_id: str, stat: str) -> tuple[float, float] | None:
    """Full-time corners, or cards (yellow + red: a missing red count is 0 once the yellows are known)."""
    ft = store.stats_of(fixture_id, "FT")
    first = ft.get("corners" if stat == "corners" else "yellow_cards")
    if not first or first[0] is None or first[1] is None:
        return None
    if stat == "corners":
        return first
    red = ft.get("red_cards") or (0, 0)
    return first[0] + (red[0] or 0), first[1] + (red[1] or 0)


def _half_time(store, fixture_id: str, r) -> tuple[int, int] | None:
    """Half-time score (football-data for the leagues, API-Football for every competition), when consistent with the result."""
    g = store.stats_of(fixture_id, "1H").get("goals")
    if not g or g[0] is None or g[1] is None:
        return None
    h, a = int(g[0]), int(g[1])
    return (h, a) if 0 <= h <= r.home_goals and 0 <= a <= r.away_goals else None


def _settle_slips(store, t: datetime, bonus_table: list[float] | None = None, bonus_min_odds: float = 1.25) -> int:
    from .config import OptimizerCfg
    o = OptimizerCfg()
    table = bonus_table if bonus_table is not None else o.multi_bonus
    open_slips = store.db.execute("SELECT id, legs FROM paper_slips WHERE last_kickoff <= ? AND (result IS NULL OR "
                                  "(result = 'non valutabile' AND last_kickoff >= ?))",
                                  ((t - SETTLE_AFTER).isoformat(), (t - GIVE_UP).isoformat())).fetchall()
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
