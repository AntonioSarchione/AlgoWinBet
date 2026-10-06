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
