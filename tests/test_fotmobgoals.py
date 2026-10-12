import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from algowinbet.collector import CollectStats
from algowinbet.domain import MatchResult, MatchStat
from algowinbet.fotmobcollector import FotMobClient, FotMobCollector, FotMobLeague, GoalDetails, goal_details
from algowinbet.paper import _migrate, settle
from algowinbet.snapshots import SnapshotProvider, SnapshotStore

KO = datetime(2026, 10, 3, 18, 45, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


def _goal(t, new, home, over=None, own=None, shootout=False):
    return {"type": "Goal", "time": t, "overloadTime": over, "isHome": home, "ownGoal": own, "newScore": new,
            "isPenaltyShootoutEvent": shootout}


def _half(short, h, a):
    return {"type": "Half", "time": 45 if short == "HT" else 90, "halfStrShort": short, "homeScore": h, "awayScore": a}


def _page(events, finished=True):
    return {"header": {"status": {"finished": finished}}, "general": {"finished": finished},
            "content": {"matchFacts": {"events": {"events": events}}}}


# Spain-Czechia, 3 October 2026, as the FotMob page has it (probe of 2026-10-12)
SPAIN = [_goal(6, [1, 0], True), _goal(35, [2, 0], True), {"type": "AddedTime", "time": 45}, _half("HT", 2, 0),
         _goal(69, [2, 1], False), _goal(89, [3, 1], True), _half("FT", 3, 1)]


def test_half_time_score_and_goal_order_from_the_events():
    assert goal_details(_page(SPAIN), 3, 1) == ((2, 0), (6.0, 69.0), (89.0, 69.0))
    assert goal_details(_page(SPAIN), 2, 1) is None  # not our result: nothing


def test_own_goal_counts_for_the_team_credited_by_the_score_and_stoppage_time_is_first_half():
    ev = [_goal(45, [0, 1], True, over=2, own=True), _half("HT", 0, 1), _goal(46, [1, 1], True), _half("FT", 1, 1)]
    assert goal_details(_page(ev), 1, 1) == ((0, 1), (46.0, 45.02), (46.0, 45.02))


def test_a_half_time_event_that_disagrees_gives_nothing():
    ev = [_goal(44, [1, 0], True), _half("HT", 0, 0), _half("FT", 1, 0)]
    assert goal_details(_page(ev), 1, 0) is None


def test_extra_time_goals_only_when_our_result_has_them_and_shoot_out_never():
    ev = [_goal(10, [1, 0], True), _half("HT", 1, 0), _goal(80, [1, 1], False), _half("FT", 1, 1), _goal(105, [2, 1], True),
          _goal(121, [3, 1], True, shootout=True)]
    assert goal_details(_page(ev), 1, 1) == ((1, 0), (10.0, 80.0), (10.0, 80.0))
    assert goal_details(_page(ev), 2, 1) == ((1, 0), (10.0, 80.0), (105.0, 80.0))


def test_goalless_match():
    assert goal_details(_page([_half("HT", 0, 0), _half("FT", 0, 0)]), 0, 0) == ((0, 0), (None, None), (None, None))


class FakeFotMob:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers):
        path = urlparse(url).path
        self.calls.append(path)
        if path == "/leagues/9806/fixtures/x":
            pp = {"details": {"selectedSeason": "2026/2027"}, "allAvailableSeasons": ["2026/2027"], "fixtures": {"allMatches": [
                {"id": 5181854, "home": {"name": "Spain"}, "away": {"name": "Czechia"},
                 "status": {"utcTime": "2026-10-03T18:45:00Z", "finished": True, "scoreStr": "3 - 1"}}]}}
        elif path == "/leagues/9807/fixtures/x":
            pp = {"details": {"selectedSeason": "2026/2027"}, "fixtures": {"allMatches": []}}
        elif path == "/match/5181854":
            pp = _page(SPAIN)
        else:
            return 404, {}, b"not found"
        return 200, {}, f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": pp}})}</script>'.encode()


def _registry():
    s = SnapshotStore(":memory:")
    s.save_results("goal-api", [MatchResult(fixture_id="g1", competition="UEFA Nations League", home="Spain", away="Czechia", kickoff=KO,
                                            home_goals=3, away_goals=1)], KO)
    _migrate(s)
    legs = [("TOTAL_GOALS@H1|OVER|1.5", 1.85), ("FIRST_GOAL|HOME|", 1.5), ("MATCH_1X2|HOME|", 1.3)]
    for k, odds in legs:
        s.db.execute("INSERT INTO paper_legs(fixture_id, sel_key, competition, match, kickoff, market, status, odds, bookmaker, p, ev, run_id, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("g1", k, "UEFA Nations League", "Spain - Czechia", KO.isoformat(), k, "FAIR", odds, "sisal", 0.6, 0.0, 1, "x"))
    s.db.execute("INSERT INTO paper_slips(slip_key, run_id, created_at, legs, total_odds, first_kickoff, last_kickoff) VALUES(?,?,?,?,?,?,?)",
                 ("s1", 1, "x", json.dumps([{"fixture_id": "g1", "sel_key": k, "odds": o} for k, o in legs[:2]]), 2.775,
                  KO.isoformat(), KO.isoformat()))
    s.db.commit()
    return s


def _details(s, api, now=NOW):
    client = FotMobClient(store=s, transport=api, sleep=lambda _: None, now=lambda: now)
    col = FotMobCollector(client, s, [], now=lambda: now)
    return GoalDetails(col, [FotMobLeague(9806, "UEFA Nations League"), FotMobLeague(9807, "UEFA Nations League")])


def test_selections_closed_as_not_settleable_are_settled_once_fotmob_gives_the_goals():
    s, api = _registry(), FakeFotMob()
    assert settle(s, SnapshotProvider(s), now=KO + timedelta(days=3))["non valutabile"] == 2  # no half-time score, no goal order
    assert s.db.execute("SELECT result FROM paper_slips").fetchone()[0] == "non valutabile"
    st = CollectStats("t")
    gd = _details(s, api)
    gd.run(st)  # a week back by default: 3 October is 9 days back on the 12th, nothing read
    assert api.calls == []
    gd.run(st, since=datetime(2026, 10, 1, tzinfo=timezone.utc), max_n=None)
    assert api.calls == ["/leagues/9806/fixtures/x", "/match/5181854"]  # linked from league A, the other league never read
    assert s.stats_of("g1", "1H")["goals"] == (2.0, 0.0)
    assert s.stats_of("g1", "FT")["first_goal_minute"] == (6.0, 69.0)
    settle(s, SnapshotProvider(s), now=NOW)  # past the week of second looks: still not settleable
    assert s.db.execute("SELECT COUNT(*) FROM paper_legs WHERE result = 'non valutabile'").fetchone()[0] == 2
    settle(s, SnapshotProvider(s), now=NOW, recheck_since=datetime(2026, 10, 1, tzinfo=timezone.utc))
    got = dict(s.db.execute("SELECT sel_key, result FROM paper_legs").fetchall())
    assert got == {"TOTAL_GOALS@H1|OVER|1.5": "won", "FIRST_GOAL|HOME|": "won", "MATCH_1X2|HOME|": "won"}
    assert s.db.execute("SELECT result, ROUND(payout, 3) FROM paper_slips").fetchone() == ("won", 2.775)
    assert gd.pending(datetime(2026, 10, 1, tzinfo=timezone.utc)) == []  # nothing left to read
    n = len(api.calls)
    gd.run(st, since=datetime(2026, 10, 1, tzinfo=timezone.utc), max_n=None)
    assert len(api.calls) == n


def test_football_data_half_time_score_is_never_replaced():
    s, api = _registry(), FakeFotMob()
    s.save_stats("football-data", [MatchStat(fixture_id="g1", period="1H", stat="goals", home=2, away=0, observed_at=KO)])
    _details(s, api).run(CollectStats("t"), since=datetime(2026, 10, 1, tzinfo=timezone.utc), max_n=None)
    assert s.db.execute("SELECT source FROM match_stats WHERE fixture_id='g1' AND period='1H'").fetchone()[0] == "football-data"
    assert s.db.execute("SELECT source FROM match_stats WHERE fixture_id='g1' AND stat='first_goal_minute'").fetchone()[0] == "fotmob"
