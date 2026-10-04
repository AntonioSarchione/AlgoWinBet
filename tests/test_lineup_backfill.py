from datetime import datetime, timezone

from algowinbet.collector import GoalCollector
from algowinbet.snapshots import SnapshotStore


class FakeClient:
    requests_sent = 0

    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, path, params=None):
        self.calls.append(path)
        self.requests_sent += 1
        return {"data": self.data.get(path), "_fetched_at": datetime.now(timezone.utc)}


def _side(prefix, n=11):
    return {"startingLineups": [{"playerId": f"{prefix}{i}", "lineupPlayer": f"P {prefix}{i}", "playerPosition": "Midfielders"} for i in range(n)],
            "substitutes": [{"playerId": f"{prefix}s", "lineupPlayer": f"S {prefix}", "playerPosition": "Goalkeepers"}]}


def test_backfill_saves_xi_and_players_newest_team_wins():
    st = SnapshotStore(":memory:")
    for fid, home, ko in (("goal:new", "Milan", "2026-09-20T18:45:00+00:00"), ("goal:old", "Lecce", "2025-11-08T18:45:00+00:00"),
                          ("goal:none", "Como", "2026-09-13T18:45:00+00:00")):
        st.db.execute("INSERT INTO results(fixture_id, source, competition, home, away, kickoff, home_goals, away_goals) VALUES(?,?,?,?,?,?,1,0)",
                      (fid, "goal-api", "Serie A", home, "Roma", ko))
    data = {"/fixtures/new/lineups": {"home": _side("h"), "away": _side("a")},
            "/fixtures/old/lineups": {"home": _side("h"), "away": _side("x")},  # player h0 was at Lecce a year ago
            "/fixtures/none/lineups": {"home": {"startingLineups": []}, "away": {"startingLineups": []}}}
    c = GoalCollector(FakeClient(data), st, [])
    res = c.backfill_lineups(["Serie A"], datetime(2025, 7, 1, tzinfo=timezone.utc), 10, 60)
    assert res.saved["lineups"] == 4 and res.saved["senza formazione"] == 1
    assert st.db.execute("SELECT team FROM players WHERE id='goal:h0'").fetchone()[0] == "Milan"  # most recent match decides
    assert c.pending_lineup_history(["Serie A"], datetime(2025, 7, 1, tzinfo=timezone.utc)) == []  # nothing asked twice


def test_ids_translate_and_start_rates():
    from algowinbet.domain import HistoricalLineup, Player, Position
    from algowinbet.engine import RECENT_XI, start_rates
    from algowinbet.snapshots import SnapshotProvider
    st = SnapshotStore(":memory:")
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    st.save_players("goal-lineups", [Player(id="goal:1", name="Mike Maignan", team="Milan", position=Position.GK),
                                     Player(id="goal:2", name="Rafael Leao", team="Milan", position=Position.FWD)], t)
    st.save_players("api-football", [Player(id="apif:9", name="M. Maignan", team="Milan", position=Position.GK),
                                     Player(id="apif:8", name="Nobody Else", team="Milan", position=Position.MID)], t)
    pr = SnapshotProvider(st)
    assert pr._to_goal(["apif:9", "apif:8"]) == ["goal:1", "apif:8"]
    st.db.execute("INSERT INTO results(fixture_id, competition, home, away, kickoff, home_goals, away_goals) VALUES('f','Serie A','Milan','Roma','2026-10-02T18:00:00+00:00',1,0)")
    assert {p.id for p in pr.list_players("Serie A")} == {"goal:1", "goal:2"}  # API-Football duplicates dropped
    roster = pr.list_players("Serie A")
    lus = [HistoricalLineup(fixture_id=f"f{i}", team="Milan", starters=["goal:1"] + (["goal:2"] if i < 10 else [])) for i in range(20)]
    learned, recent = start_rates(roster, lus, {f"f{i}": datetime(2026, 1, 1 + i, tzinfo=timezone.utc) for i in range(20)})
    assert learned["goal:2"] > 0 and recent["goal:2"] == 0.0 and recent["goal:1"] == 1.0  # left the XI: 0% today (rates scaled to 11, capped at 1)
    assert RECENT_XI == 8
