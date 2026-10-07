import json
from datetime import datetime, timedelta, timezone

from algowinbet.domain import LineupSnapshot, MatchResult, Player, Position
from algowinbet.fotmobcollector import SCHEMA
from algowinbet.fotmobplayers import link_players, pick
from algowinbet.snapshots import SnapshotStore

KO = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def test_pick_full_name_then_surname_then_words_and_never_a_doubtful_one():
    c = {"goal:1": ["mike", "maignan"], "goal:2": ["theo", "hernandez"], "goal:3": ["lucas", "hernandez"], "goal:4": ["vinicius", "junior"]}
    assert pick("Mike Maignan", c) == "goal:1"
    assert pick("M. Maignan", c) == "goal:1"
    assert pick("T. Hernández", c) == "goal:2"
    assert pick("Hernandez", c) is None  # two Hernandez: left out
    assert pick("Vinícius Júnior", c) == "goal:4"
    assert pick("Nobody Else", c) is None


def _store():
    s = SnapshotStore(":memory:")
    s.db.executescript(SCHEMA)
    s.save_results("goal-api", [MatchResult(fixture_id=f"g{k}", competition="Serie A", home="Milan", away="Inter", kickoff=KO + timedelta(days=7 * k),
                                            home_goals=1, away_goals=0) for k in range(3)], KO)
    s.save_players("goal-api", [Player(id="goal:1", name="Mike Maignan", team="Milan", position=Position.GK),
                                Player(id="goal:2", name="Theo Hernandez", team="Milan", position=Position.DEF),
                                Player(id="goal:9", name="Rafael Leao", team="Milan", position=Position.FWD)], KO)
    for k in range(2):
        s.save_lineups("goal-api", [LineupSnapshot(fixture_id=f"g{k}", team="Milan", status="confirmed", starters=["goal:1", "goal:2"],
                                                   bench=["goal:9"], published_at=KO, observed_at=KO)])
    rows = [(f"g{k}", "Milan", pid, name, 0, 1, 90, None, 0, 0, None, None, None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "x")
            for k in range(2) for pid, name in (("fotmob:11", "Mike Maignan"), ("fotmob:22", "Theo Hernández"))]
    s.db.executemany("INSERT INTO fotmob_player_stats VALUES(" + ",".join("?" * 25) + ")", rows)
    s.db.executemany("INSERT INTO fotmob_absences VALUES(?,?,?,?,?,?,?,?)", [
        ("g2", "Milan", "fotmob:99", "Rafael Leão", "injury", "", None, "x"),   # never played in a match we read: linked from the bench
        ("g2", "Milan", "fotmob:22", "Theo Hernández", "suspension", "", None, "x"),
        ("g2", "Milan", "fotmob:77", "Unknown Kid", "injury", "", None, "x")])
    s.db.commit()
    return s


def test_link_players_by_match_and_from_absences_and_reports_coverage():
    s = _store()
    rep = link_players(s, KO)
    links = dict(s.db.execute("SELECT fotmob_id, goal_id FROM fotmob_player_links").fetchall())
    assert links == {"fotmob:11": "goal:1", "fotmob:22": "goal:2", "fotmob:99": "goal:9"}
    assert rep.by_absence == 1 and rep.starters_checked == 4 and rep.starters_same == 4
    assert rep.absences == {"Serie A": [3, 2, 0, 0]}  # no GOAL XI stored for g2
    assert link_players(s, KO).linked == 3  # a full pass again: same links, no duplicates
