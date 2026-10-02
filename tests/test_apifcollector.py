import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from algowinbet.apifcollector import ApifLeague, ApiFootballCollector, status_of
from algowinbet.domain import Fixture, LineupSnapshot, Player, Position
from algowinbet.information.availability import build_availability
from algowinbet.providers.apifootball import ApiFootballClient
from algowinbet.snapshots import BudgetGuard, SnapshotProvider, SnapshotStore

NOW = datetime(2026, 10, 3, 13, 30, tzinfo=timezone.utc)
KO = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)


def _api_fixture(fid, league, home, away, hid, aid, ko=KO):
    return {"fixture": {"id": fid, "date": ko.isoformat()}, "league": {"id": league},
            "teams": {"home": {"id": hid, "name": home}, "away": {"id": aid, "name": away}}}


def _xi(team_id, base):
    return {"team": {"id": team_id}, "formation": "4-3-3",
            "startXI": [{"player": {"id": base + i, "name": f"P{base + i}", "pos": "GDDDDMMMFFF"[i]}} for i in range(11)],
            "substitutes": [{"player": {"id": base + 50, "name": f"S{base}", "pos": "M"}}]}


class FakeApi:
    def __init__(self, lineups=True):
        self.calls = []
        self.lineups = lineups

    def __call__(self, url, headers):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path, q))
        if u.path == "/fixtures":
            resp = [_api_fixture(1, 135, "AC Milan", "Inter", 489, 505), _api_fixture(2, 2, "Real Madrid", "Bayern Munich", 541, 157,
                                                                                     KO + timedelta(minutes=10)),
                    _api_fixture(3, 999, "Other", "Team", 1, 2)] if q["date"] == "2026-10-03" else []
        elif u.path == "/injuries":
            resp = [{"fixture": {"id": 1}, "team": {"id": 489}, "player": {"id": 7, "name": "Out Guy", "type": "Missing Fixture", "reason": "Knee Injury"}},
                    {"fixture": {"id": 1}, "team": {"id": 505}, "player": {"id": 8, "name": "Ban Guy", "type": "Missing Fixture", "reason": "Red Card"}},
                    {"fixture": {"id": 1}, "team": {"id": 505}, "player": {"id": 9, "name": "Maybe", "type": "Questionable", "reason": "Knock"}}] \
                if q["date"] == "2026-10-03" else []
        elif u.path == "/fixtures/lineups":
            resp = ([_xi(489, 100), _xi(505, 200)] if q["fixture"] == "1" else [_xi(541, 300), _xi(157, 400)]) if self.lineups else []
        elif u.path == "/players/squads":
            resp = [{"team": {"id": int(q["team"])}, "players": [{"id": 7, "name": "Out Guy", "position": "Attacker"},
                                                                 {"id": 100, "name": "Keeper", "position": "Goalkeeper"}]}]
        else:
            resp = []
        body = json.dumps({"errors": [], "response": resp}).encode()
        return 200, {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": str(100 - len(self.calls))}, body


def _setup(fake, daily=100, reserve=0):
    s = SnapshotStore(":memory:")
    s.save_fixtures("goal", [Fixture(id="g1", competition="Serie A", home="Milan", away="Inter", kickoff=KO),
                             Fixture(id="g2", competition="UEFA Champions League", home="Real Madrid", away="Bayern München",
                                     kickoff=KO + timedelta(minutes=10))], NOW - timedelta(days=1))
    budget = BudgetGuard(s, "api-football", daily=daily, reserve=reserve, now=lambda: NOW)
    client = ApiFootballClient(api_key="k", store=s, budget=budget, transport=fake, sleep=lambda x: None, now=lambda: NOW)
    leagues = [ApifLeague(135, "Serie A", True), ApifLeague(2, "UEFA Champions League", False)]
    return s, ApiFootballCollector(client, s, leagues, now=lambda: NOW, squads_per_day=5)


def test_status_of_maps_injury_rows():
    assert status_of("Missing Fixture", "Knee Injury") == "OUT"
    assert status_of("Missing Fixture", "Suspended") == "SUSPENDED"
    assert status_of("Missing Fixture", "Yellow Cards") == "SUSPENDED"
    assert status_of("Questionable", "Knock") == "DOUBTFUL"


def test_tick_links_fixtures_and_saves_injuries_lineups_and_squads():
    fake = FakeApi()
    s, col = _setup(fake)
    st = col.run()
    assert not st.errors, st.errors
    links = dict(s.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source='api-football'").fetchall())
    assert links == {"1": "g1", "2": "g2"}  # similar names linked, unknown league ignored
    ev = {e.player: e.payload["status"] for e in SnapshotProvider(s).get_events("g1")}
    assert ev == {"apif:7": "OUT", "apif:8": "SUSPENDED", "apif:9": "DOUBTFUL"}
    lus = SnapshotProvider(s).get_lineups("g1")
    assert {l.team for l in lus} == {"Milan", "Inter"} and all(len(l.starters) == 11 and l.status == "confirmed" for l in lus)
    roles = dict(s.db.execute("SELECT id, position FROM players").fetchall())
    assert roles["apif:7"] == "FWD" and roles["apif:100"] == "GK"  # squad role wins over the lineup's
    paths = [p for p, _ in fake.calls]
    lineup_calls = [q["fixture"] for p, q in fake.calls if p == "/fixtures/lineups"]
    assert lineup_calls == ["1", "2"]  # domestic league first
    # a second tick the same minute asks nothing again: days and injuries fresh, lineups complete, squads fresh
    n = len(fake.calls)
    col.run()
    assert len(fake.calls) == n, fake.calls[n:]
    assert paths.count("/fixtures") == 2  # today and tomorrow, once each


def test_cups_wait_when_the_budget_is_needed_for_the_leagues():
    fake = FakeApi(lineups=False)  # not published yet: the league match stays due
    s, col = _setup(fake, daily=4, reserve=0)  # 2 for the days, 1 lineup try now, 1 kept for the retry
    col.run()
    lineup_calls = [q["fixture"] for p, q in fake.calls if p == "/fixtures/lineups"]
    assert "1" in lineup_calls and "2" not in lineup_calls


def test_lineup_with_ids_of_another_source_is_ignored():
    roster = [Player(id=f"apif:{i}", name=str(i), team="Milan", position=Position.MID) for i in range(14)]
    base = {p.id: (0.8 if int(p.id[5:]) < 11 else 0.2) for p in roster}
    foreign = LineupSnapshot(fixture_id="g1", team="Milan", status="confirmed", starters=[f"goal:{i}" for i in range(11)],
                             published_at=NOW, observed_at=NOW)
    av = build_availability("Milan", roster, base, [], [foreign], NOW + timedelta(minutes=1), "g1")
    assert av.source == "prior"  # not "confirmed" with every rostered player out
    own = foreign.model_copy(update={"starters": [f"apif:{i}" for i in range(11)]})
    assert build_availability("Milan", roster, base, [], [own], NOW + timedelta(minutes=1), "g1").source == "confirmed"


def test_plan_tick_adds_the_api_football_step_when_a_league_has_an_id():
    from algowinbet.autorun import AutoConfig, League, plan_tick
    s = SnapshotStore(":memory:")
    assert "apif" not in plan_tick(s, AutoConfig(leagues=[League("Serie A")]), NOW, NOW)
    assert "apif" in plan_tick(s, AutoConfig(leagues=[League("Serie A", apif=135)]), NOW, NOW)
