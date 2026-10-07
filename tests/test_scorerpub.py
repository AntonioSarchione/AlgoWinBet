"""Fase 9: goalscorer table of the upcoming matches, published with the analysis."""
import json
from datetime import timedelta

from algowinbet.domain import Fixture, LineupSnapshot
from algowinbet.scorerpub import scorers_json, team_scorers
from algowinbet.snapshots import SnapshotProvider
from test_scorers import BENCH, T0, XI, _store


def test_probable_then_official_xi():
    st = _store(20)
    prov = SnapshotProvider(st)
    ko = T0 + timedelta(days=7 * 20)
    fx = Fixture(id="goal:next", competition="Serie A", home="Milan", away="Roma", kickoff=ko)
    now = ko - timedelta(hours=3)
    out = team_scorers(st, prov, [fx], {"goal:next": (1.8, 1.0)}, now)
    milan = out["goal:next"]["Milan"]
    assert milan.state == "probabile" and milan.sheets == 20
    top = milan.players[0]
    assert top["n"] == "Striker One" and top["a"] > 0.5 and top["f"] < top["a"] and top["d"] < top["a"]
    # the official XI without the striker: he drops out of the table, the others share the team's goals
    st.save_lineups("goal-api", [LineupSnapshot(fixture_id="goal:next", team="Milan", status="confirmed", starters=XI[:10] + ["goal:sub"],
                                                bench=["goal:st"], published_at=now, observed_at=now)])
    milan = team_scorers(st, prov, [fx], {"goal:next": (1.8, 1.0)}, now)["goal:next"]["Milan"]
    assert milan.state == "ufficiale"
    rows = {p["n"]: p for p in milan.players}
    assert rows["Bench Guy"]["s"] == 1.0 and rows["Bench Guy"]["a"] > 0.2  # the striker's replacement takes the forward's share
    assert "Striker One" not in rows or rows["Striker One"]["a"] < 0.05  # on the bench: a small chance at most
    js = json.loads(scorers_json({"Milan": milan}))
    assert js["Milan"]["state"] == "ufficiale" and js["Milan"]["players"]


def test_teams_with_little_history_are_left_out():
    st = _store(3)
    prov = SnapshotProvider(st)
    fx = Fixture(id="goal:next", competition="Serie A", home="Milan", away="Roma", kickoff=T0 + timedelta(days=30))
    assert team_scorers(st, prov, [fx], {"goal:next": (1.5, 1.0)}, T0 + timedelta(days=29)) == {}


def test_probable_xi_is_published_until_the_official_one_arrives():
    from algowinbet.scorerpub import probable_lineups
    st = _store(20)
    prov = SnapshotProvider(st)
    ko = T0 + timedelta(days=7 * 20)
    fx = Fixture(id="goal:next", competition="Serie A", home="Milan", away="Roma", kickoff=ko)
    now = ko - timedelta(hours=3)
    st.db.execute("INSERT INTO player_status(source, fixture_id, team, player_id, player_name, status, reason, observed_at) "
                  "VALUES('fotmob', 'goal:next', 'Milan', 'goal:st', 'Striker One', 'DOUBTFUL', 'injury: Doubtful', ?)", (now.isoformat(),))
    out, xi = probable_lineups(st, prov, [fx], now)
    milan = json.loads(out["goal:next"])["Milan"]
    assert len(milan["xi"]) == 10 and xi is not None  # the test squad has no goalkeeper: ten outfield players
    assert sum(int(c) for c in milan["formation"].split("-")) == 10
    roles = [r[2] for r in milan["xi"]]
    assert roles == sorted(roles, key=["GK", "DEF", "MID", "FWD"].index)  # goalkeeper first, forwards last
    st_row = next(r for r in milan["xi"] + milan["alt"] if r[0] == "goal:st")
    assert st_row[1] == "Striker One" and st_row[4] == "DOUBTFUL"
    st.save_lineups("goal-api", [LineupSnapshot(fixture_id="goal:next", team="Milan", status="confirmed", starters=XI[:10] + ["goal:sub"],
                                                bench=["goal:st"], published_at=now, observed_at=now)])
    out, _ = probable_lineups(st, prov, [fx], now, xi)
    assert "Milan" not in json.loads(out.get("goal:next", "{}"))  # official XI stored: no probable one
