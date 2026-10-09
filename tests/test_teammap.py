import json
from datetime import datetime, timedelta, timezone

from algowinbet.apifcollector import SCHEMA as APIF_SCHEMA
from algowinbet.domain import Fixture
from algowinbet.names import TeamNames
from algowinbet.snapshots import SnapshotStore
from algowinbet.teammap import TeamMap


def test_team_map_pairs_source_spellings_with_goal_names_from_linked_matches():
    store = SnapshotStore(":memory:")
    store.db.executescript(APIF_SCHEMA)
    now = datetime.now(timezone.utc)
    ko = now + timedelta(days=1)
    store.save_fixtures("goal-api", [Fixture(id="goal:1", competition="Bundesliga", home="Mainz 05", away="Leverkusen", kickoff=ko),
                                     Fixture(id="goal:2", competition="Bundesliga", home="Köln", away="Borussia M'gladbach", kickoff=ko)], now)
    rows = [{"fixtureId": "op1", "participant1Name": "FSV Mainz", "participant2Name": "Bayer Leverkusen", "startTime": ko.isoformat()},
            {"fixtureId": "op2", "participant1Name": "1. FC Cologne", "participant2Name": "Borussia Monchengladbach", "startTime": ko.isoformat()},
            {"fixtureId": "op9", "participant1Name": "Stade Rennais FC", "participant2Name": "AJ Auxerre", "startTime": ko.isoformat()}]
    store.put_raw("oddspapi", "/fixtures", {"tournamentId": 35}, 200, json.dumps({"data": rows}).encode(), now)
    store.db.executemany("INSERT INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES('oddspapi',?,?,?)",
                         [("op1", "goal:1", now.isoformat()), ("op2", "goal:2", now.isoformat())])
    store.db.execute("INSERT INTO apif_teams(team_id, name, apif_name) VALUES(1, 'Köln', '1. FC Köln')")
    names = TeamNames({"Leverkusen": ["Bayer Leverkusen"]})
    tm = TeamMap(store, names)
    tm.load_goal()
    tm.load_oddspapi()
    tm.load_apif()
    add = tm.aliases_to_add()
    assert add["Mainz 05"] == {"FSV Mainz"} and add["Köln"] == {"1. FC Cologne", "1. FC Köln"}
    assert add["Borussia M'gladbach"] == {"Borussia Monchengladbach"} and "Leverkusen" not in add  # alias already there
    assert tm.unlinked["oddspapi"]["Stade Rennais FC"] == 1  # never on a linked match: no guess
    assert not tm.conflicts()


def test_fotmob_rename_gives_fotmob_results_the_goal_names(tmp_path, capsys):
    import argparse
    from algowinbet.cli import cmd_fotmob_rename
    from algowinbet.domain import MatchResult
    db, aliases = str(tmp_path / "t.db"), tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"Arsenal FC": ["Arsenal"]}), encoding="utf-8")
    store = SnapshotStore(db)
    ko = datetime(2026, 9, 17, 19, tzinfo=timezone.utc)
    store.save_results("fotmob", [MatchResult(fixture_id="fotmob:5", competition="UEFA Champions League", home="Arsenal", away="Slavia Praha",
                                              kickoff=ko, home_goals=2, away_goals=0)], ko)
    store.db.execute("CREATE TABLE IF NOT EXISTS fotmob_player_stats(fixture_id TEXT, team TEXT, player_id TEXT)")
    store.db.execute("INSERT INTO fotmob_player_stats VALUES('fotmob:5', 'Arsenal', 'p1')")
    store.db.commit()
    store.close()
    ns = dict(config="configs/collect.json", aliases=str(aliases), db=db)
    cmd_fotmob_rename(argparse.Namespace(apply=False, **ns))
    assert "Arsenal-Slavia Praha -> Arsenal FC-Slavia Praha" in capsys.readouterr().out
    store = SnapshotStore(db)
    assert store.db.execute("SELECT home FROM results").fetchone()[0] == "Arsenal"  # dry run
    store.close()
    cmd_fotmob_rename(argparse.Namespace(apply=True, **ns))
    store = SnapshotStore(db)
    assert store.db.execute("SELECT home FROM results").fetchone()[0] == "Arsenal FC"
    assert store.db.execute("SELECT team FROM fotmob_player_stats").fetchone()[0] == "Arsenal FC"


def test_summary_counts_and_skips_the_spellings_left_out_on_purpose():
    from algowinbet.teammap import summary
    store = SnapshotStore(":memory:")
    store.db.executescript(APIF_SCHEMA)
    now = datetime.now(timezone.utc)
    store.save_fixtures("goal-api", [Fixture(id="goal:1", competition="UEFA Nations League", home="Republic of Ireland", away="Portugal",
                                             kickoff=now + timedelta(days=1))], now)
    store.db.execute("INSERT INTO apif_teams(team_id, name, apif_name) VALUES(1, 'Republic of Ireland', 'Ireland')")
    tm = TeamMap(store, TeamNames())
    tm.load_goal()
    tm.load_apif()
    sm = summary(tm)
    assert sm["aliases"] == 0 and sm["conflicts"] == 0 and sm["examples"] == []
