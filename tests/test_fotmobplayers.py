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
    hist = s.db.execute("SELECT fixture_id, player_id, status, observed_at FROM absence_history ORDER BY player_id").fetchall()
    ko2 = (KO + timedelta(days=14) - timedelta(days=1)).isoformat()
    assert hist == [("g2", "goal:2", "SUSPENDED", ko2), ("g2", "goal:9", "OUT", ko2)]  # the unlinked kid is left out


def test_second_pass_writes_only_what_changed():
    s = _store()
    link_players(s, KO)
    s.db.execute("UPDATE fotmob_player_links SET linked_at = 'old'")
    s.db.commit()
    link_players(s, KO + timedelta(days=1))  # nothing changed: no link rewritten, so the old date stays
    assert {r[0] for r in s.db.execute("SELECT linked_at FROM fotmob_player_links")} == {"old"}
    s.db.execute("DELETE FROM fotmob_absences WHERE player_id = 'fotmob:22'")  # an absence gone: its history row goes too
    s.db.execute("INSERT INTO fotmob_player_links VALUES('fotmob:5', 'goal:5', 'Gone', 1, 1, 'partite', 'old')")  # a stale link
    s.db.commit()
    link_players(s, KO)
    assert s.db.execute("SELECT COUNT(*) FROM fotmob_player_links WHERE fotmob_id = 'fotmob:5'").fetchone()[0] == 0
    assert [r[0] for r in s.db.execute("SELECT player_id FROM absence_history")] == ["goal:9"]


def test_absent_players_found_in_the_official_xi_are_counted_and_the_switch_feeds_the_xi_model():
    from algowinbet.probable import _status, load_xi_data
    from algowinbet.snapshots import SnapshotProvider
    s = _store()
    s.save_lineups("goal-api", [LineupSnapshot(fixture_id="g2", team="Milan", status="confirmed", starters=["goal:2"], bench=["goal:9"],
                                               published_at=KO, observed_at=KO)])
    rep = link_players(s, KO)
    assert rep.played == {"Serie A": [2, 1, 1]}  # Theo listed suspended but started; Leao on the bench
    assert rep.played_kind["suspension"] == 1
    prov = SnapshotProvider(s)
    before = KO + timedelta(days=14) - timedelta(hours=1)
    assert _status(load_xi_data(s, prov), "g2", "goal:2", before) is None  # off by default
    assert _status(load_xi_data(s, prov, fotmob_absences=True), "g2", "goal:2", before) == "SUSPENDED"
