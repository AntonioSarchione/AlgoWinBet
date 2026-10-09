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
