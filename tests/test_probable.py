"""Our probable lineups: candidates from the last sheets, injuries applied, 1 keeper + 10 outfield expected."""
import json
from datetime import datetime, timedelta, timezone

from algowinbet.probable import evaluate_xi, load_xi_data, predict, scale_to_xi, XiModel
from algowinbet.snapshots import SnapshotProvider, SnapshotStore

T0 = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)
CORE = [f"goal:c{i}" for i in range(9)]


def _store(n=24):
    """Milan: keeper g1, 9 regulars, and two players (a, b) taking turns in the last place; Roma always the same 11."""
    st = SnapshotStore(":memory:")
    rows = [("goal:g1", "GK"), ("goal:g2", "GK"), ("goal:a", "FWD"), ("goal:b", "FWD")] + [(p, "MID") for p in CORE]
    rows += [(f"goal:r{i}", "GK" if i == 0 else "DEF") for i in range(11)]
    for pid, pos in rows:
        st.db.execute("INSERT INTO players(id, name, team, position, source) VALUES(?,?,?,?, 'goal-lineups')",
                      (pid, pid[5:], "Roma" if pid.startswith("goal:r") else "Milan", pos))
    for k in range(n):
        fid, ko = f"goal:f{k}", T0 + timedelta(days=7 * k)
        st.db.execute("INSERT INTO results(fixture_id, competition, home, away, kickoff, home_goals, away_goals) VALUES(?,?,?,?,?,1,1)",
                      (fid, "Serie A", "Milan", "Roma", ko.isoformat()))
        milan = ["goal:g1"] + CORE + ["goal:a" if k % 2 == 0 else "goal:b"]
        for team, s, b in (("Milan", milan, ["goal:g2", "goal:b" if k % 2 == 0 else "goal:a"]),
                           ("Roma", [f"goal:r{i}" for i in range(11)], [])):
            st.db.execute("INSERT INTO lineups(fixture_id, team, status, starters, bench, observed_at, source) VALUES(?,?,?,?,?,?,?)",
                          (fid, team, "confirmed", json.dumps(s), json.dumps(b), ko.isoformat(), "goal-api"))
    return st


def test_scaling_keeps_one_keeper_and_ten_outfield():
    roles = {"k1": "GK", "k2": "GK", **{f"o{i}": "MID" for i in range(14)}}
    probs = scale_to_xi({"k1": 0.9, "k2": 0.3, **{f"o{i}": 0.95 if i < 9 else 0.4 for i in range(14)}}, roles)
    assert abs(probs["k1"] + probs["k2"] - 1) < 1e-9 and abs(sum(v for p, v in probs.items() if p.startswith("o")) - 10) < 1e-9
    assert max(probs.values()) <= 1.0


def test_injured_regular_is_left_out_and_replay_scores():
    st = _store()
    data = load_xi_data(st, SnapshotProvider(st))
    ko = T0 + timedelta(days=7 * 24)
    data.status[("goal:next", "goal:c0")] = [("OUT", ko - timedelta(days=1))]
    pred = predict(XiModel(), data, "Milan", ko, "Serie A", "goal:next")
    assert pred.probs["goal:c0"] == 0.0 and "goal:c0" not in pred.xi and "goal:g1" in pred.xi and len(pred.xi) == 11
    rep = evaluate_xi(data, T0 + timedelta(days=7 * 10), ko)
    sc = rep.scores["modello"]
    assert sc.teams == 28 and sc.guessed / sc.teams >= 10.0  # Roma 11/11 always; Milan's last place is a coin toss at worst
    assert rep.scores["stessa dell'ultima"].guessed / sc.teams == 10.5  # a/b alternate: the last XI always misses Milan's last place


def test_red_card_and_fifth_yellow_bring_a_ban_feature():
    from algowinbet.probable import FEATURES, candidates
    st = _store(8)
    for k in range(5):  # c1: a yellow in each of the matches 3..7 (5th in the last one); c2: sent off in the last match
        st.db.execute("INSERT INTO match_events(fixture_id, seq, team, kind, player_id, source) VALUES(?, 10, 'Milan', 'CARD_YELLOW', 'goal:c1', 'goal-api')",
                      (f"goal:f{3 + k}",))
    st.db.execute("INSERT INTO match_events(fixture_id, seq, team, kind, player_id, source) VALUES('goal:f7', 11, 'Milan', 'CARD_RED', 'goal:c2', 'goal-api')")
    data = load_xi_data(st, SnapshotProvider(st))
    cands = {c.player_id: dict(zip(FEATURES, c.x)) for c in candidates(data, "Milan", T0 + timedelta(days=56), "Serie A", "goal:f8")}
    assert cands["goal:c1"]["ban_yellow"] == 1.0 and cands["goal:c1"]["ban_red"] == 0.0
    assert cands["goal:c2"]["ban_red"] == 1.0 and cands["goal:c3"]["ban_yellow"] == cands["goal:c3"]["ban_red"] == 0.0
    # a cup match is not covered by a league ban
    cup = {c.player_id: dict(zip(FEATURES, c.x)) for c in candidates(data, "Milan", T0 + timedelta(days=56), "UEFA Champions League", "goal:cx")}
    assert cup["goal:c2"]["ban_red"] == 0.0
