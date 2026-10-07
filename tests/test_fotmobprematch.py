import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from algowinbet.autorun import AutoConfig, League, fotmob_dispatch_due
from algowinbet.collector import CollectStats
from algowinbet.domain import Fixture, LineupSnapshot, Player, Position
from algowinbet.fotmobcollector import FotMobClient, FotMobCollector, FotMobLeague
from algowinbet.fotmobprematch import FotMobPrematch, due_reads, status_of
from algowinbet.snapshots import SnapshotProvider, SnapshotStore

NOW = datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)
KO = datetime(2026, 10, 9, 18, 45, tzinfo=timezone.utc)
COMPS = {"Serie A"}


def _html(pp: dict) -> bytes:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": pp}})}</script></html>'.encode()


def _u(fid, name, kind="injury", back="Late October"):
    return {"id": fid, "name": name, "unavailability": {"type": kind, "expectedReturn": back}}


class Site:
    """FotMob with one coming match (id 700); its page changes as the tests set `unavailable` and `starters`."""

    def __init__(self):
        self.calls = []
        self.unavailable = {"homeTeam": [_u(11, "Mike Maignan", back="Doubtful"), _u(99, "Unknown Kid")], "awayTeam": [_u(22, "Lautaro Martinez", "suspension", None)]}
        self.starters = {}

    def __call__(self, url, headers):
        path = urlparse(url).path
        self.calls.append(path)
        if path == "/leagues/55/fixtures/x":
            return 200, {}, _html({"details": {"selectedSeason": "2026/2027"}, "fixtures": {"allMatches": [
                {"id": "700", "home": {"name": "Milan"}, "away": {"name": "Inter"},
                 "status": {"utcTime": KO.strftime("%Y-%m-%dT%H:%M:%SZ"), "finished": False}}]}})
        if path == "/match/700":
            lineup = {"lineupType": "standard" if self.starters else "unavailable"}
            for k in ("homeTeam", "awayTeam"):
                lineup[k] = {"unavailable": self.unavailable.get(k, []), "starters": self.starters.get(k, []), "subs": []}
            return 200, {}, _html({"content": {"lineup": lineup}})
        return 404, {}, b"no"


def _store():
    s = SnapshotStore(":memory:")
    s.save_fixtures("goal-api", [Fixture(id="goal:1", competition="Serie A", home="Milan", away="Inter", kickoff=KO, provider="goal-api",
                                         provider_event_id="1")], NOW - timedelta(days=1))
    players = [Player(id=f"goal:m{k}", name=f"Milan Player{k}", team="Milan", position=Position.MID) for k in range(11)]
    players += [Player(id=f"goal:i{k}", name=f"Inter Player{k}", team="Inter", position=Position.MID) for k in range(11)]
    players += [Player(id="goal:mm", name="Mike Maignan", team="Milan", position=Position.GK),
                Player(id="goal:lm", name="Lautaro Martinez", team="Inter", position=Position.FWD)]
    s.save_players("goal-lineups", players, NOW - timedelta(days=30))  # the players seen in GOAL lineups
    # last official XI: Maignan is a regular, Lautaro is not
    s.save_lineups("goal-api", [LineupSnapshot(fixture_id="goal:0", team="Milan", status="confirmed", starters=["goal:mm"] + [f"goal:m{k}" for k in range(10)],
                                               published_at=NOW - timedelta(days=5), observed_at=NOW - timedelta(days=5))])
    return s


def _run(s, site, t):
    client = FotMobClient(store=s, transport=site, sleep=lambda _: None, now=lambda: t)
    col = FotMobCollector(client, s, [FotMobLeague(55, "Serie A")], now=lambda: t)
    st = CollectStats("pre")
    return FotMobPrematch(col).run(st), st


def test_status_of_reads_doubtful_and_suspension():
    assert status_of("injury", "Doubtful") == "DOUBTFUL"
    assert status_of("injury", "Mid October 2026") == "OUT"
    assert status_of("suspension", None) == "SUSPENDED"
    assert status_of("internationalDuty", None) == "OUT"


def test_morning_read_links_the_match_saves_statuses_with_goal_ids_and_flags_a_regular():
    s, site = _store(), Site()
    assert [w for _, w in due_reads(s, COMPS, NOW)] == ["mattina"]
    rep, st = _run(s, site, NOW)
    assert site.calls == ["/leagues/55/fixtures/x", "/match/700"] and rep.pages == 1
    rows = s.db.execute("SELECT player_id, status, reason FROM player_status WHERE source = 'fotmob' ORDER BY player_id").fetchall()
    assert rows == [("fotmob:99", "OUT", "injury: Late October"), ("goal:lm", "SUSPENDED", "suspension: -"),
                    ("goal:mm", "DOUBTFUL", "injury: Doubtful")]
    assert rep.relevant == ["Mike Maignan (Milan) DOUBTFUL"]  # Lautaro did not start the last XI: no new analysis for him
    assert due_reads(s, COMPS, NOW + timedelta(hours=1)) == []  # read: nothing due until 3 hours before kickoff
    ev = [e for e in SnapshotProvider(s).get_events("goal:1") if e.player == "goal:mm"]
    assert ev and ev[0].payload["status"] == "DOUBTFUL"  # the models read it like API-Football's rows


def test_changes_are_kept_as_history_and_a_player_no_longer_listed_is_available():
    s, site = _store(), Site()
    _run(s, site, NOW)
    t = KO - timedelta(hours=2)
    assert [w for _, w in due_reads(s, COMPS, t)] == ["3 ore prima"]
    site.unavailable["homeTeam"] = [_u(11, "Mike Maignan", back="Early November")]  # Maignan out; the kid is back
    rep, _ = _run(s, site, t)
    hist = s.db.execute("SELECT player_id, status, observed_at FROM player_status WHERE source = 'fotmob' ORDER BY player_id, observed_at").fetchall()
    assert ("goal:mm", "DOUBTFUL", NOW.isoformat()) in hist and ("goal:mm", "OUT", t.isoformat()) in hist
    assert ("fotmob:99", "AVAILABLE", t.isoformat()) in hist
    assert ("goal:lm", "SUSPENDED", NOW.isoformat()) in hist and len([h for h in hist if h[0] == "goal:lm"]) == 1  # unchanged: one row
    assert rep.relevant == ["Mike Maignan (Milan) OUT"]


def test_official_xi_is_read_from_75_minutes_before_and_saved_only_with_known_ids():
    s, site = _store(), Site()
    _run(s, site, NOW)
    _run(s, site, KO - timedelta(hours=2))
    t = KO - timedelta(minutes=70)
    assert [w for _, w in due_reads(s, COMPS, t)] == ["formazione"]
    _run(s, site, t)  # no XI yet on the page
    assert due_reads(s, COMPS, t + timedelta(minutes=3)) == [] and [w for _, w in due_reads(s, COMPS, t + timedelta(minutes=9))] == ["formazione"]
    site.starters = {"homeTeam": [{"id": 100 + k, "name": f"Milan Player{k}"} for k in range(11)],
                     "awayTeam": [{"id": 200 + k, "name": f"Stranger {k}"} for k in range(11)]}  # Inter's names unknown to us
    _run(s, site, t + timedelta(minutes=9))
    assert not [l for l in SnapshotProvider(s).get_lineups("goal:1")]  # one team unknown: the XI is left to GOAL
    site.starters["awayTeam"] = [{"id": 200 + k, "name": f"Inter Player{k}"} for k in range(11)]
    rep, st = _run(s, site, t + timedelta(minutes=18))
    xi = {l.team: l.starters for l in SnapshotProvider(s).get_lineups("goal:1")}
    assert xi["Milan"][0] == "goal:m0" and len(xi["Inter"]) == 11 and "formazioni ufficiali Milan-Inter" in rep.relevant
    assert due_reads(s, COMPS, t + timedelta(minutes=30)) == []  # XI read: no more pages for this match


def test_the_tick_starts_fotmob_when_pages_are_due_and_not_twice_in_a_row():
    s = _store()
    cfg = AutoConfig(leagues=[League(name="Serie A", fotmob=55)])
    assert "prima del calcio d'inizio" in fotmob_dispatch_due(s, cfg, NOW)
    s.mark_job("fotmob-dispatch", NOW, "")
    assert fotmob_dispatch_due(s, cfg, NOW + timedelta(minutes=3)) is None  # the run started 3 minutes ago is still on its way
    _run(s, Site(), NOW)
    s.mark_job("fotmob-tick", NOW + timedelta(minutes=5), "")
    assert fotmob_dispatch_due(s, cfg, NOW + timedelta(minutes=30)) is None  # nothing due, a run half an hour ago
    assert fotmob_dispatch_due(s, cfg, NOW + timedelta(hours=3)) == "partite finite e storico"
    assert fotmob_dispatch_due(s, AutoConfig(leagues=[League(name="Serie A")]), NOW) is None
