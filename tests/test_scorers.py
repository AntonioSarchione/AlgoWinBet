"""Fase 9 goalscorer model: sheets from lineups + events, shares with shrinkage, team totals kept."""
import json
import math
from datetime import datetime, timedelta, timezone

from algowinbet.scorers import OWN_GOAL_SHARE, Params, Tally, load_scorer_data, predict_team
from algowinbet.snapshots import SnapshotStore

T0 = datetime(2025, 9, 1, 18, tzinfo=timezone.utc)
XI = [f"goal:m{i}" for i in range(10)] + ["goal:st"]
BENCH = ["goal:sub"]


def _store(n=20):
    st = SnapshotStore(":memory:")
    st.db.execute("INSERT INTO players(id, name, team, position) VALUES('goal:st','Striker One','Milan','FWD'), ('goal:sub','Bench Guy','Milan','FWD')")
    for i in range(10):
        st.db.execute("INSERT INTO players(id, name, team, position) VALUES(?,?,?,?)", (f"goal:m{i}", f"Mid {i}", "Milan", "MID" if i > 3 else "DEF"))
    for k in range(n):
        fid, ko = f"goal:f{k}", T0 + timedelta(days=7 * k)
        st.db.execute("INSERT INTO results(fixture_id, competition, home, away, kickoff, home_goals, away_goals) VALUES(?,?,?,?,?,2,0)",
                      (fid, "Serie A", "Milan", "Roma", ko.isoformat()))
        st.db.execute("INSERT INTO event_reads(fixture_id, source, n) VALUES(?, 'goal-api', 2)", (fid,))
        st.db.execute("INSERT INTO lineups(fixture_id, team, status, starters, bench, observed_at, source) VALUES(?,?,?,?,?,?,?)",
                      (fid, "Milan", "confirmed", json.dumps(XI), json.dumps(BENCH), ko.isoformat(), "goal-api"))
        st.db.execute("INSERT INTO lineups(fixture_id, team, status, starters, bench, observed_at, source) VALUES(?,?,?,?,?,?,?)",
                      (fid, "Roma", "confirmed", json.dumps([f"goal:r{i}" for i in range(11)]), "[]", ko.isoformat(), "goal-api"))
        # every match: the striker scores, plus a penalty (his) on even weeks or an own goal of Roma on odd weeks
        ev = [(0, "Milan", "GOAL", "goal:st"), (1, "Milan", "PENALTY" if k % 2 == 0 else "OWN_GOAL", "goal:st" if k % 2 == 0 else "goal:r3")]
        for seq, team, kind, pid in ev:
            st.db.execute("INSERT INTO match_events(fixture_id, seq, team, kind, player_id, source) VALUES(?,?,?,?,?,'goal-api')", (fid, seq, team, kind, pid))
    return st


def test_sheets_split_penalties_and_own_goals():
    data = load_scorer_data(_store(2))
    milan = [s for s in data.sheets if s.team == "Milan"]
    assert (milan[0].np_goals, milan[0].pen_goals, milan[0].team_np, milan[0].team_pen) == ({"goal:st": 1}, {"goal:st": 1}, 1, 1)
    assert (milan[1].np_goals, milan[1].pen_goals, milan[1].team_np, milan[1].team_pen) == ({"goal:st": 1}, {}, 1, 0)  # own goal: nobody's


def test_shares_shrink_and_add_up_to_the_team():
    data = load_scorer_data(_store(20))
    t = Tally(365.0)
    for s in data.sheets:
        t.add(s, data.roles)
    lam = 1.8
    out = {p.player_id: p for p in predict_team(t, "Milan", lam, 1.0, Params(), data.roles, data.names, XI, BENCH)}
    assert abs(sum(p.rate for p in out.values()) - lam * (1 - OWN_GOAL_SHARE)) < 1e-9  # every expected goal has a scorer
    assert out["goal:st"].anytime > 0.6 and out["goal:m5"].anytime < 0.15
    raw = {p.player_id: p for p in predict_team(t, "Milan", lam, 1.0, Params(shrink=False), data.roles, data.names, XI, BENCH)}
    assert raw["goal:m5"].rate == 0.0 < out["goal:m5"].rate  # never scored in 20 starts: shrinkage still leaves his role's chance
    st = out["goal:st"]
    assert abs(st.two_plus - (1 - math.exp(-st.rate) * (1 + st.rate))) < 1e-12 and st.first < st.anytime
    # before the XI: the squad of the last sheets, weighed by starts
    pre = {p.player_id: p for p in predict_team(t, "Milan", lam, 1.0, Params(), data.roles, data.names)}
    assert pre["goal:st"].starter == 1.0 and pre["goal:sub"].starter == 0.0


def test_replay_scores_every_player_and_beats_the_role_reference():
    from algowinbet.config import Config
    from algowinbet.scorers import evaluate_scorers
    from algowinbet.snapshots import SnapshotProvider
    st = _store(30)
    rep = evaluate_scorers(st, SnapshotProvider(st), Config(), T0 + timedelta(days=70), T0 + timedelta(days=400), ["Serie A"],
                           expected=lambda r, monday: (2.0, 0.5))
    assert rep.matches == 20
    xi = rep.scores["modello"]["xi"]
    assert xi.n == 20 * (12 + 11) and xi.hits == 20  # the striker scored in every match; Roma never
    m, hw = rep.diffs["modello - ruolo (xi)"]
    assert m + hw < 0  # one player scores every week: the player history beats the role average
