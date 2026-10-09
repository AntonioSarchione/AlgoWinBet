from datetime import datetime, timedelta, timezone

from algowinbet.playerquotes import player_odds, save_player_quotes
from algowinbet.providers.oddspapi import OddsPapiMapper
from algowinbet.snapshots import SnapshotStore

UTC = timezone.utc
CATALOGUE = [
    {"marketId": 10730, "marketName": "Anytime Goal Scorer", "marketType": "players-anytimegoalscorer", "playerProp": True,
     "period": "fulltime", "sportId": 10, "outcomes": [{"outcomeId": 10730, "outcomeName": "Yes"}]},
    {"marketId": 10733, "marketName": "Player Goals", "marketType": "players-goals", "playerProp": True, "period": "result",
     "sportId": 10, "outcomes": [{"outcomeId": 10734, "outcomeName": "1+"}, {"outcomeId": 10735, "outcomeName": "2+"}]},
    {"marketId": 102608, "marketName": "Over Under Player Shots", "marketType": "playertotals-shots", "playerProp": True,
     "handicap": 1.5, "period": "result", "sportId": 10,
     "outcomes": [{"outcomeId": 102608, "outcomeName": "Over"}, {"outcomeId": 102609, "outcomeName": "Under"}]},
    {"marketId": 101, "marketName": "1X2", "marketType": "1x2", "period": "fulltime", "sportId": 10,
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}]},
]


def pl(name, price, active=True):
    return {"playerName": name, "price": price, "active": active}


ROW = {"fixtureId": "x1", "bookmakerOdds": {"sisal.it": {"markets": {
    "10730": {"outcomes": {"10730": {"players": {"11": pl("Kane, Harry", 1.55), "12": pl("Musiala, Jamal", 2.6, active=False)}}}},
    "10733": {"outcomes": {"10734": {"players": {"11": pl("Kane, Harry", 1.55)}}, "10735": {"players": {"11": pl("Kane, Harry", 4.5)}}}},
    "102608": {"outcomes": {"102608": {"players": {"11": pl("Kane, Harry", 1.4)}}, "102609": {"players": {"11": pl("Kane, Harry", 2.8)}}}},
    "101": {"outcomes": {"101": {"players": {"0": {"price": 1.8}}}}},
}}}}


def test_player_odds_keep_scorer_two_plus_and_shots_over():
    m = OddsPapiMapper(markets=CATALOGUE)
    qs = player_odds(ROW, m.markets, "sisal.it")
    got = sorted((q.market, q.line_key, q.player_name, q.odds) for q in qs)
    assert got == [("SCORER", "", "Kane, Harry", 1.55), ("SHOTS", "1.5", "Kane, Harry", 1.4), ("TWO_PLUS", "", "Kane, Harry", 4.5)]
    assert player_odds(ROW, m.markets, "pinnacle") == []


def test_save_replaces_the_fixture_and_drops_old_rows():
    st = SnapshotStore(":memory:")
    m = OddsPapiMapper(markets=CATALOGUE)
    t = datetime(2026, 10, 10, 9, tzinfo=UTC)
    assert save_player_quotes(st, "f1", "sisal.it", player_odds(ROW, m.markets, "sisal.it"), t) == 3
    assert save_player_quotes(st, "f1", "sisal.it", player_odds(ROW, m.markets, "sisal.it"), t + timedelta(hours=1)) == 0  # unchanged
    save_player_quotes(st, "old", "sisal.it", player_odds(ROW, m.markets, "sisal.it"), t - timedelta(days=5))
    save_player_quotes(st, "f1", "sisal.it", [], t)  # an empty snapshot leaves the rows
    assert st.db.execute("SELECT COUNT(*) FROM player_quotes WHERE fixture_id='f1'").fetchone()[0] == 3
    later = player_odds(ROW, m.markets, "sisal.it")[:1]
    save_player_quotes(st, "f1", "sisal.it", later, t + timedelta(hours=2))
    assert st.db.execute("SELECT COUNT(*) FROM player_quotes WHERE fixture_id='f1'").fetchone()[0] == 1
    save_player_quotes(st, "f2", "sisal.it", later, t + timedelta(days=4))  # f1 now older than KEEP_DAYS
    assert {r[0] for r in st.db.execute("SELECT fixture_id FROM player_quotes").fetchall()} == {"f2"}
