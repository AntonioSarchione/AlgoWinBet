import json
from datetime import timedelta

from algowinbet.config import Config
from algowinbet.domain import MatchResult, OddsQuote
from algowinbet.paper import closing_prices, register, settle, _ref
from algowinbet.publish import analyze_and_publish
from algowinbet.providers import MockProvider
from algowinbet.snapshots import SnapshotProvider, SnapshotStore


def _published():
    mock = MockProvider(seed=7)
    s = SnapshotStore(":memory:")
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72
    cfg.bet_bookmakers = ["BookA", "BookB", "BookC"]
    t = cfg.thresholds
    t.min_ev, t.fair_ev, t.max_uncertainty, t.min_dq_candidate = 0.0, -0.05, 0.5, 0.0  # plenty of recorded selections
    cfg.optimizer.min_slip_ev = -0.5
    rid, res = analyze_and_publish(s, cfg, now=mock.as_of)
    return s, mock, rid, res


def test_proposals_are_recorded_once_with_their_first_price():
    s, mock, rid, res = _published()
    n = s.db.execute("SELECT COUNT(*) FROM paper_legs").fetchone()[0]
    assert n > 0 and n == sum(1 for o in res.opportunities if o.status.value in ("STRONG", "CANDIDATE", "FAIR"))
    n_slips = s.db.execute("SELECT COUNT(*) FROM paper_slips").fetchone()[0]
    assert n_slips >= len(res.optimizer.slips)  # the slips of every profile, each recorded once
    profiles = {r[0] for r in s.db.execute("SELECT DISTINCT profile FROM paper_slips")}
    assert profiles <= {"probabilita", "equilibrata", "value"} and (not n_slips or "equilibrata" in profiles)
    first = s.db.execute("SELECT odds, run_id FROM paper_legs ORDER BY id LIMIT 1").fetchone()
    assert register(s, rid + 1, res, {"model": "x", "meta": "y"}) == (0, 0)  # the same proposals again: nothing new
    assert s.db.execute("SELECT odds, run_id FROM paper_legs ORDER BY id LIMIT 1").fetchone() == first


def test_settlement_uses_the_final_score_and_the_closing_prices():
    s, mock, rid, res = _published()
    legs = s.db.execute("SELECT fixture_id, sel_key, kickoff, odds FROM paper_legs").fetchall()
    fid, key, ko, odds = next(l for l in legs if l[1].startswith("MATCH_1X2|HOME"))
    ref = _ref(key)
    f = next(x for x in res.fixtures if x.id == fid)
    s.save_results("mock", [MatchResult(fixture_id=fid, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff,
                                        home_goals=2, away_goals=1)], f.kickoff + timedelta(hours=3))
    close_at = f.kickoff - timedelta(minutes=2)
    s.save_quotes("hist", [OddsQuote(fixture_id=fid, market_code="MATCH_1X2", selection=sel, bookmaker=b, odds=o, observed_at=close_at, kind="close")
                           for b, prices in (("sisal", (1.80, 3.6, 4.5)), ("pinnacle", (1.85, 3.8, 4.9)))
                           for sel, o in zip(("HOME", "DRAW", "AWAY"), prices)])
    sis, fair = closing_prices(s, fid, ref)
    assert sis == 1.80 and 0.5 < fair < 0.54
    done = settle(s, SnapshotProvider(s), f.kickoff + timedelta(hours=3))
    assert done["won"] >= 1
    row = s.db.execute("SELECT result, score, close_odds, close_fair, close_sisal_fair FROM paper_legs WHERE fixture_id=? AND sel_key=?", (fid, key)).fetchone()
    assert row[0] == "won" and row[1] == "2-1" and row[2] == 1.80 and abs(row[3] - fair) < 1e-9
    assert 0.5 < row[4] < 0.55 and row[4] != row[3]  # Sisal closing without margin, its own estimate
    away = s.db.execute("SELECT result FROM paper_legs WHERE fixture_id=? AND sel_key LIKE 'MATCH_1X2|AWAY%'", (fid,)).fetchone()
    assert away is None or away[0] == "lost"
    # other matches have no result yet: still open
    assert s.db.execute("SELECT COUNT(*) FROM paper_legs WHERE result IS NULL").fetchone()[0] > 0


def test_a_slip_settles_when_every_selection_has():
    s, mock, rid, res = _published()
    slip = s.db.execute("SELECT id, legs, last_kickoff FROM paper_slips ORDER BY id LIMIT 1").fetchone()
    if slip is None:
        return
    legs = json.loads(slip[1])
    end = None
    for l in legs:
        f = next(x for x in res.fixtures if x.id == l["fixture_id"])
        ref = _ref(l["sel_key"])
        # a score that wins every 1X2 / goals selection is not guaranteed: settle by forcing each leg's result row
        s.db.execute("UPDATE paper_legs SET result='won', settled_at='x', close_odds=? WHERE fixture_id=? AND sel_key=?",
                     (l["odds"] * 0.95, l["fixture_id"], l["sel_key"]))
        end = max(end or f.kickoff, f.kickoff)
    s.db.commit()
    settle(s, SnapshotProvider(s), end + timedelta(hours=3))
    result, payout, clv = s.db.execute("SELECT result, payout, clv FROM paper_slips WHERE id=?", (slip[0],)).fetchone()
    odds = 1.0
    for l in legs:
        odds *= l["odds"]
    assert result == "won" and payout >= odds - 1e-9 and clv > 0  # bonus never lowers the payout; better than closing


def test_half_time_and_goal_order_markets_settle_when_the_detail_arrives():
    from datetime import datetime, timezone
    from algowinbet.domain import MatchStat
    s, mock, rid, res = _published()
    f = res.fixtures[0]
    keys = ["HT_FT|X/X|", "MATCH_1X2@H1|DRAW|", "FIRST_GOAL|AWAY|", "LAST_GOAL|HOME|"]
    now = datetime.now(timezone.utc).isoformat()
    for k in keys:
        s.db.execute("INSERT INTO paper_legs(fixture_id, sel_key, competition, match, kickoff, market, status, odds, bookmaker, p, ev, run_id, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f.id, k, f.competition, f"{f.home} - {f.away}", f.kickoff.isoformat(), k, "FAIR", 2.0, "sisal", 0.5, 0.0, rid, now))
    s.db.commit()
    s.save_results("mock", [MatchResult(fixture_id=f.id, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff,
                                        home_goals=1, away_goals=1)], f.kickoff + timedelta(hours=3))
    get = lambda k: s.db.execute("SELECT result FROM paper_legs WHERE fixture_id=? AND sel_key=?", (f.id, k)).fetchone()[0]
    settle(s, SnapshotProvider(s), f.kickoff + timedelta(hours=3))
    assert all(get(k) is None for k in keys)  # detail missing: they wait instead of closing as not settleable
    at = f.kickoff + timedelta(hours=3)
    s.save_stats("api-football", [MatchStat(fixture_id=f.id, period="1H", stat="goals", home=0, away=0, observed_at=at),
                                  MatchStat(fixture_id=f.id, period="FT", stat="first_goal_minute", home=60, away=45.02, observed_at=at),
                                  MatchStat(fixture_id=f.id, period="FT", stat="last_goal_minute", home=60, away=45.02, observed_at=at)])
    settle(s, SnapshotProvider(s), f.kickoff + timedelta(hours=4))
    assert [get(k) for k in keys] == ["won", "won", "won", "won"]  # 0-0 then 1-1, away scored first (45+2), home last


def test_detail_that_never_arrives_closes_as_not_settleable_and_reopens_later():
    from algowinbet.domain import MatchStat
    s, mock, rid, res = _published()
    f = res.fixtures[1]
    s.db.execute("INSERT INTO paper_legs(fixture_id, sel_key, competition, match, kickoff, market, status, odds, bookmaker, p, ev, run_id, "
                 "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f.id, "HT_FT|1/1|", f.competition, "x", f.kickoff.isoformat(), "x", "FAIR", 3.0, "sisal", 0.3, 0.0, rid, "x"))
    s.db.commit()
    s.save_results("mock", [MatchResult(fixture_id=f.id, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff,
                                        home_goals=2, away_goals=0)], f.kickoff + timedelta(hours=3))
    get = lambda: s.db.execute("SELECT result FROM paper_legs WHERE fixture_id=? AND sel_key='HT_FT|1/1|'", (f.id,)).fetchone()[0]
    settle(s, SnapshotProvider(s), f.kickoff + timedelta(days=3))
    assert get() == "non valutabile"
    s.save_stats("api-football", [MatchStat(fixture_id=f.id, period="1H", stat="goals", home=1, away=0, observed_at=f.kickoff)])
    settle(s, SnapshotProvider(s), f.kickoff + timedelta(days=4))
    assert get() == "won"  # reopened within the week once the half-time score is known
