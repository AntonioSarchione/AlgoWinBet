"""Player cards of the lineups tab: what a player does in a match he starts, from his FotMob appearances."""
import json
from datetime import datetime, timedelta, timezone

from algowinbet.domain import Player, Position
from algowinbet.fotmobcollector import SCHEMA as FM_SCHEMA, add_gk_columns
from algowinbet.fotmobprematch import SCHEMA as PRE_SCHEMA
from algowinbet.playercard import player_cards
from algowinbet.snapshots import SnapshotStore

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def _store():
    st = SnapshotStore(":memory:")
    st.db.executescript(FM_SCHEMA)
    st.db.executescript(PRE_SCHEMA)
    add_gk_columns(st)
    st.save_players("goal-lineups", [Player(id="goal:st", name="Striker", team="Milan", position=Position.FWD),
                                     Player(id="goal:gk", name="Keeper", team="Milan", position=Position.GK),
                                     Player(id="goal:new", name="Newcomer", team="Milan", position=Position.DEF)], NOW)
    st.db.executemany("INSERT INTO fotmob_player_links(fotmob_id, goal_id) VALUES(?,?)",
                      [("fotmob:1", "goal:st"), ("fotmob:2", "goal:gk"), ("fotmob:3", "goal:new")])
    for k in range(12):
        fid, ko = f"goal:m{k}", (NOW - timedelta(days=7 * (k + 1))).isoformat()
        st.db.execute("INSERT INTO results(fixture_id, competition, home, away, kickoff, home_goals, away_goals) VALUES(?,?,?,?,?,?,?)",
                      (fid, "Serie A", "Milan", "Roma", ko, 2, k % 2))  # Milan concede 0 in even matches, 1 in odd ones
        st.db.execute("INSERT INTO match_stats(fixture_id, period, stat, home, away, source) VALUES(?, 'FT', 'shots_on_target', 6, 4, 'football-data')", (fid,))
        rows = [(fid, "Milan", "fotmob:1", "Striker", 1, 85, 0.5, 2, 4, 1, 2, int(k < 6)),
                (fid, "Milan", "fotmob:2", "Keeper", 1, 90, None, 0, 0, 0, 0, 0)]
        if k == 0:
            rows.append((fid, "Milan", "fotmob:3", "Newcomer", 0, 30, 0.0, 0, 0, 1, 0, 0))  # 30 minutes in all: too little
        st.db.executemany("INSERT INTO fotmob_player_stats(fixture_id, team, player_id, name, starter, minutes, xg, shots_on, shots, "
                          "fouls_committed, fouls_drawn, yellow) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return st


def test_cards_per_match_started_from_the_last_appearances():
    st = _store()
    stats = {f"goal:m{k}": {"shots_on_target": (6.0, 4.0)} for k in range(12)}
    out = json.loads(player_cards(st, {"goal:next": {"goal:st", "goal:gk", "goal:new", "goal:nobody"}}, NOW, stats)["goal:next"])
    s = out["goal:st"]
    assert s["n"] == 12 and s["min"] == 85  # every appearance (up to LAST)
    assert abs(s["xg"] - 0.5) < 0.01 and abs(s["sh"] - 4) < 0.01 and abs(s["sot"] - 2) < 0.01  # only one role read: the prior is his own rate
    assert 0.3 < s["cg"] < 0.6  # booked in 6 of 12
    assert s["as"] == 0.0  # no assist, and his role (only him) none either
    g = out["goal:gk"]
    assert g["gk"] == 1 and g["gc"] == 0.5 and g["cs"] == 0.5 and g["sv"] == 3.5 and g["cg"] == 0.0
    assert "goal:new" not in out and "goal:nobody" not in out  # too few minutes, no FotMob appearance


def test_no_players_no_cards():
    assert player_cards(_store(), {}, NOW) == {}


def test_keeper_numbers_from_fotmob_win_over_the_team_estimate():
    st = _store()
    st.db.execute("UPDATE fotmob_player_stats SET saves = 5, goals_conceded = 2, shots_on_faced = 7 WHERE player_id = 'fotmob:2'")
    g = json.loads(player_cards(st, {"goal:next": {"goal:gk"}}, NOW, {})["goal:next"])["goal:gk"]
    assert g["ts"] == 7 and g["gc"] == 2 and g["sv"] == 5 and g["cs"] == 0
