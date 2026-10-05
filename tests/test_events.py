"""Fase 9: goal events read with the XI in the morning backfill, scorers resolved to the lineup player ids."""
from datetime import datetime, timedelta, timezone

from algowinbet.collector import GoalCollector
from algowinbet.events import match_name, parse_events
from algowinbet.snapshots import SnapshotStore

from test_lineup_backfill import FakeClient


def _side(prefix, n=11):
    return {"startingLineups": [{"playerId": f"{prefix}{i}", "playerKey": f"k{prefix}{i}", "lineupPlayer": f"Name{i} {prefix.upper()}urname{i}",
                                 "playerPosition": "Forwards"} for i in range(n)],
            "substitutes": [{"playerId": f"{prefix}s", "playerKey": f"k{prefix}s", "lineupPlayer": f"Sub {prefix.upper()}", "playerPosition": "Goalkeepers"}]}


def _goal(t, side, name, key, info=None, score="1 - 0"):
    other = "away" if side == "home" else "home"
    return {"time": str(t), "timeNum": t, "type": "GOAL", f"{side}Scorer": name, f"{side}ScorerId": key, f"{other}Scorer": None,
            f"{side}Assist": None, "score": score, "info": info}


def test_parse_kinds_and_names():
    evs = parse_events([_goal(12, "home", "A. B", "1"), _goal(75, "away", "C. D", "2", "Penalty"), _goal(80, "home", "E. F", "3", "Own Goal")],
                       "Milan", "Roma")
    assert [(e.team, e.kind, e.minute) for e in evs] == [("Milan", "GOAL", 12.0), ("Roma", "PENALTY", 75.0), ("Milan", "OWN_GOAL", 80.0)]
    c = {"1": "Rasmus Højlund", "2": "Lautaro Martínez", "3": "Lisandro Martínez", "4": "Vinícius Júnior"}
    assert (match_name("R. Hojlund", c), match_name("L. Martinez", c), match_name("Lautaro Martinez", c), match_name("Vinicius Junior", c)) \
        == ("1", None, "2", "4")  # two players fit 'L. Martinez': never guessed


def test_backfill_reads_xi_and_events_together_and_resolves_scorers():
    st = SnapshotStore(":memory:")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    fresh = datetime.now(timezone.utc) - timedelta(hours=10)
    for fid, ko, hg in (("goal:m1", old, 2), ("goal:m2", old - timedelta(days=7), 1), ("goal:m3", fresh, 1)):
        st.db.execute("INSERT INTO results(fixture_id, source, competition, home, away, kickoff, home_goals, away_goals) VALUES(?,?,?,?,?,?,?,0)",
                      (fid, "goal-api", "Serie A", "Milan", "Roma", ko.isoformat(), hg))
    data = {"/fixtures/m1/lineups": {"home": _side("h"), "away": _side("a")},
            "/fixtures/m1/events": [_goal(10, "home", "N. Hurname9", "kh9"), _goal(60, "home", "Name3 Hurname3", "unknown", "Penalty", "2 - 0")],
            "/fixtures/m2/events": [_goal(5, "home", "N. Hurname9", "kh9")],
            "/fixtures/m3/lineups": {"home": _side("h"), "away": _side("a")},
            "/fixtures/m3/events": []}  # scorers not published yet
    # m2's XI was read on an earlier morning (no keys then): its scorer is matched by name
    st.db.execute("INSERT INTO lineups(fixture_id, team, status, starters, bench, source) VALUES('goal:m2','Milan','confirmed',?, '[]','goal-api')",
                  ('["goal:h9"]',))
    c = GoalCollector(FakeClient(data), st, [])
    res = c.backfill_lineups(["Serie A"], old - timedelta(days=30), 100, 60)
    assert sorted(c.client.calls) == sorted(["/fixtures/m1/lineups", "/fixtures/m1/events", "/fixtures/m2/events", "/fixtures/m3/lineups",
                                             "/fixtures/m3/events"])
    rows = st.db.execute("SELECT fixture_id, kind, player_id FROM match_events ORDER BY fixture_id, seq").fetchall()
    assert rows == [("goal:m1", "GOAL", "goal:h9"), ("goal:m1", "PENALTY", "goal:h3"), ("goal:m2", "GOAL", "goal:h9")]
    assert res.saved["eventi"] == 2 and res.saved["eventi in ritardo"] == 1
    left = c.pending_history(["Serie A"], old - timedelta(days=30))
    assert [(r[0], bool(r[5]), bool(r[6])) for r in left] == [("goal:m3", False, True)]  # read again on a later morning, events only


def test_late_backfill_only_when_no_kickoff_is_left_before_the_reset():
    from algowinbet.cli import late_backfill_due
    from algowinbet.domain import Fixture

    class Cal:
        def __init__(self, kickoffs):
            self.f = [Fixture(id=f"goal:{i}", competition="Serie A", home="A", away="B", kickoff=k) for i, k in enumerate(kickoffs)]

        def list_fixtures(self, comps, start, end):
            return [f for f in self.f if start <= f.kickoff <= end]

    day = datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert not late_backfill_due(Cal([]), day.replace(hour=19, minute=40))  # too early: evening lineups may still come
    assert late_backfill_due(Cal([day.replace(hour=19)]), day.replace(hour=21, minute=30))  # results run after the last match
    assert not late_backfill_due(Cal([day.replace(hour=22)]), day.replace(hour=20, minute=10))  # a 22:00 UTC kickoff still needs its XI
    assert late_backfill_due(Cal([day.replace(hour=12) + timedelta(days=1)]), day.replace(hour=22))  # tomorrow's matches: after the reset
