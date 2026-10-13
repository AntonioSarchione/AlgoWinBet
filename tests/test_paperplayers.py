import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from algowinbet import fotmobcollector, fotmobplayers, playerquotes
from algowinbet.paperplayers import match_sisal, model_probability, register_players, settle_players
from algowinbet.snapshots import SnapshotStore

KO = datetime(2026, 10, 18, 18, 45, tzinfo=timezone.utc)
FID = "goal:m1"


@dataclass
class Fx:
    id: str
    kickoff: datetime
    competition: str = "Serie A"
    home: str = "Atalanta"
    away: str = "Venezia"


def test_duo_numbers_match_the_site():
    card = {"min": 90, "sot": 1.2, "sh": 2.5, "fc": 1.1, "as": 0.2}
    scorer = {"s": 1, "a": 0.3, "d": 0.06, "f": 0.12}
    # a whole match of his own: nothing to add for the substitute
    assert abs(model_probability("SCORER", 1, card, scorer) - 0.3) < 1e-9
    assert abs(model_probability("TWO_PLUS", 2, card, scorer) - 0.06) < 1e-9
    # 60 minutes a start: the substitute's 30 at his rate (the example of web/app/_components/Scorers.tsx)
    card60 = {**card, "min": 60}
    g = -math.log(0.7)
    assert abs(model_probability("SCORER", 1, card60, scorer) - (1 - 0.7 * math.exp(-g * 0.5))) < 1e-9
    assert model_probability("SHOTS_ON", 1, card60, scorer) > model_probability("SHOTS_ON", 1, card, scorer)
    assert model_probability("SHOTS", 2, card60, scorer) == model_probability("SHOTS", 2, card, scorer)  # not DUO
    assert model_probability("SCORER", 1, card, None) is None and model_probability("ASSIST", 1, {"gk": 1}, scorer) is None


def test_sisal_names_match_ours_only_when_one_fits():
    m = match_sisal(["Mario Pasalic", "Ademola Lookman", "Marco Rossi", "Luca Rossi"], ["Pasalic, Mario", "Lookman", "Rossi"])
    assert m == {"Pasalic, Mario": "Mario Pasalic", "Lookman": "Ademola Lookman"}  # two Rossi: no price


def _setup():
    s = SnapshotStore(":memory:")
    for sql in (fotmobcollector.SCHEMA, fotmobplayers.SCHEMA, playerquotes.SCHEMA):
        s.db.executescript(sql)
    ids = {"Mario Pasalic": ("goal:p1", "MID"), "Ademola Lookman": ("goal:p2", "FWD"), "Nicola Zalewski": ("goal:p3", "DEF")}
    for n, (pid, role) in ids.items():
        s.db.execute("INSERT INTO players(id, name, team, position) VALUES (?, ?, 'Atalanta', ?)", (pid, n, role))
    s.db.execute("INSERT INTO lineups(fixture_id, team, status, starters, observed_at) VALUES (?, 'Atalanta', 'confirmed', ?, ?)",
                 (FID, json.dumps(["goal:p1", "goal:p2", "goal:p3"]), (KO - timedelta(hours=1)).isoformat()))
    at = (KO - timedelta(minutes=50)).isoformat()
    for key, name, market, line, odds in [("1", "Pasalic, Mario", "SCORER", "", 3.5), ("2", "Lookman, Ademola", "SCORER", "", 2.4),
                                          ("2", "Lookman, Ademola", "SHOTS", "1.5", 1.6), ("3", "Zalewski, Nicola", "SCORER", "", 9.0),
                                          ("1", "Pasalic, Mario", "FIRST_SCORER", "", 8.0)]:
        s.db.execute("INSERT INTO player_quotes(fixture_id, bookmaker, market, line_key, player_key, player_name, odds, observed_at) "
                     "VALUES (?, 'sisal.it', ?, ?, ?, ?, ?, ?)", (FID, market, line, key, name, odds, at))
    s.db.commit()
    cards = {FID: json.dumps({"goal:p1": {"min": 70, "sot": 0.9, "sh": 2.0, "fc": 1.0, "as": 0.15},
                              "goal:p2": {"min": 75, "sot": 1.3, "sh": 3.0, "fc": 0.8, "as": 0.2},
                              "goal:p3": {"min": 85, "sot": 0.3, "sh": 0.8, "fc": 1.2, "as": 0.1}})}
    scorers = {FID: json.dumps({"Atalanta": {"state": "ufficiale", "sheets": 30, "players": [
        {"n": "Mario Pasalic", "r": "MID", "s": 1, "a": 0.25, "f": 0.1, "d": 0.04},
        {"n": "Ademola Lookman", "r": "FWD", "s": 1, "a": 0.4, "f": 0.16, "d": 0.09},
        {"n": "Nicola Zalewski", "r": "DEF", "s": 1, "a": 0.08, "f": 0.03, "d": 0.01}]}})}
    return s, cards, scorers


def test_recorded_once_with_the_xi_and_settled_with_the_substitute():
    s, cards, scorers = _setup()
    now = KO - timedelta(minutes=40)
    assert register_players(s, 7, now, [Fx(FID, KO)], scorers, cards) == 4  # first goalscorer is not recorded
    assert register_players(s, 8, now + timedelta(minutes=10), [Fx(FID, KO)], scorers, cards) == 0  # never rewritten
    # Pasalic off at 60' for a substitute who scores; Lookman 90' without a goal, one shot; Zalewski on the bench all match
    links = [("fm:1", "goal:p1"), ("fm:2", "goal:p2"), ("fm:3", "goal:p3")]
    s.db.executemany("INSERT INTO fotmob_player_links(fotmob_id, goal_id) VALUES (?, ?)", links)
    rows = [("fm:1", 1, 60, 0, 0), ("fm:9", 0, 30, 1, 0), ("fm:2", 1, 90, 0, 1), ("fm:3", 1, 0, 0, 0)]
    s.db.executemany("INSERT INTO fotmob_player_stats(fixture_id, team, player_id, starter, minutes, goals, shots) "
                     "VALUES (?, 'Atalanta', ?, ?, ?, ?, ?)", [(FID, *r) for r in rows])
    s.db.commit()
    assert settle_players(s, KO + timedelta(hours=1)) == {"won": 0, "lost": 0, "void": 0, "non valutabile": 0}  # not over yet
    done = settle_players(s, KO + timedelta(hours=3))
    got = {(p, m): (r, own, sub) for p, m, r, own, sub in
           s.db.execute("SELECT player, market, result, own, sub FROM paper_player_legs").fetchall()}
    assert got[("Mario Pasalic", "SCORER")] == ("won", 0, 1)  # DUO: his substitute scored
    assert got[("Ademola Lookman", "SCORER")][0] == "lost"
    assert got[("Ademola Lookman", "SHOTS")][0] == "lost"  # one shot, Over 1.5 needs two
    assert got[("Nicola Zalewski", "SCORER")][0] == "void"  # no minutes: refunded
    assert done == {"won": 1, "lost": 2, "void": 1, "non valutabile": 0}


def test_without_fotmob_rows_the_selection_waits_then_is_not_settleable():
    s, cards, scorers = _setup()
    register_players(s, 7, KO - timedelta(minutes=40), [Fx(FID, KO)], scorers, cards)
    assert sum(settle_players(s, KO + timedelta(days=1)).values()) == 0
    assert settle_players(s, KO + timedelta(days=7))["non valutabile"] == 4
