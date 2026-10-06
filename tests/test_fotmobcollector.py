import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from algowinbet.autorun import AutoConfig, League, plan_tick
from algowinbet.domain import MatchResult
from algowinbet.fotmobcollector import FotMobClient, FotMobCollector, FotMobLeague
from algowinbet.snapshots import SnapshotStore

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
KO = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
OLD = datetime(2025, 9, 20, 16, 0, tzinfo=timezone.utc)


def _html(page_props: dict) -> bytes:
    data = json.dumps({"props": {"pageProps": page_props}})
    return f'<html><script id="__NEXT_DATA__" type="application/json">{data}</script></html>'.encode()


def _fx(mid, home, away, ko, finished=True):
    return {"id": mid, "home": {"name": home, "id": "1"}, "away": {"name": away, "id": "2"},
            "status": {"utcTime": ko.strftime("%Y-%m-%dT%H:%M:%SZ"), "finished": finished}}


def _stat(key, value):
    return {"key": key, "stat": {"value": value}}


def _player(pid, team_id, minutes, xg, shots, on, fouls, drawn):
    return {"id": pid, "name": f"P{pid}", "teamId": team_id, "positionId": 115,
            "shotmap": [{"min": 30, "expectedGoals": xg, "isOnTarget": True}] if shots else [],
            "stats": [{"title": "Top stats", "stats": {"Minutes played": _stat("minutes_played", minutes), "Expected goals (xG)": _stat("expected_goals", xg),
                                                       "Total shots": _stat("total_shots", shots), "Shots on target": _stat("ShotsOnTarget", on),
                                                       "Goals": _stat("goals", 1 if on else 0)}},
                      {"title": "Duels", "stats": {"Fouls committed": _stat("fouls", fouls), "Was fouled": _stat("was_fouled", drawn)}}]}


def _match(mid, ko):
    return {"general": {"matchId": mid, "matchTimeUTCDate": ko.isoformat(), "finished": True,
                        "homeTeam": {"name": "PEC Zwolle", "id": 10}, "awayTeam": {"name": "Heerenveen", "id": 20}},
            "content": {"playerStats": {"1": _player(1, 10, 90, 0.47, 2, 1, 0, 3), "2": _player(2, 20, 77, 0.0, 0, 0, 2, 0),
                                        "3": _player(3, 20, 0, 0.0, 0, 0, 0, 0)},
                        "lineup": {"homeTeam": {"id": 10, "starters": [{"id": 1}], "unavailable": [
                                       {"id": 9, "name": "Hurt", "marketValue": 500000, "unavailability": {"type": "injury", "expectedReturn": "Late October"}}]},
                                   "awayTeam": {"id": 20, "starters": [], "unavailable": []}},
                        "matchFacts": {"events": {"events": [{"type": "Card", "card": "Yellow", "playerId": 2},
                                                             {"type": "Card", "card": "YellowRed", "playerId": 2},
                                                             {"type": "Goal", "playerId": 1}]}}}}


class FakeFotMob:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path, q))
        if u.path == "/leagues/57/fixtures/x":
            if q.get("season") == "2025/2026":
                pp = {"fixtures": {"allMatches": [_fx("400", "Go Ahead Eagles", "PEC Zwolle", OLD)]}}
            else:
                pp = {"allAvailableSeasons": ["2024/2025", "2026/2027", "2025/2026"], "details": {"selectedSeason": "2026/2027"},
                      "fixtures": {"allMatches": [_fx("500", "PEC Zwolle", "SC Heerenveen", KO),
                                                  _fx("501", "Ajax", "PSV", NOW + timedelta(days=3), finished=False)]}}
            return 200, {}, _html(pp)
        if u.path in ("/match/500", "/match/400"):
            return 200, {}, _html(_match(u.path.split("/")[-1], KO))
        return 404, {}, b"not found"


def _store():
    s = SnapshotStore(":memory:")
    s.save_results("goal-api", [MatchResult(fixture_id="g1", competition="Eredivisie", home="PEC Zwolle", away="Heerenveen", kickoff=KO,
                                            home_goals=1, away_goals=0),
                                MatchResult(fixture_id="g0", competition="Eredivisie", home="Go Ahead Eagles", away="PEC Zwolle", kickoff=OLD,
                                            home_goals=0, away_goals=0)], NOW)
    return s


def _collector(s, api, now=NOW):
    client = FotMobClient(store=s, transport=api, sleep=lambda _: None, now=lambda: now)
    return FotMobCollector(client, s, [FotMobLeague(57, "Eredivisie")], now=lambda: now, history_seasons=1)


def test_recent_match_is_linked_and_its_player_stats_saved():
    s, api = _store(), FakeFotMob()
    st = _collector(s, api).run(history_seconds=0)
    assert ("/leagues/57/fixtures/x", {}) in api.calls and ("/match/500", {}) in api.calls
    assert ("/match/400", {}) not in api.calls  # history only with time for it
    rows = {r[0]: r for r in s.db.execute("SELECT player_id, team, starter, minutes, xg, shots, shots_on, fouls_committed, fouls_drawn, "
                                          "yellow, red FROM fotmob_player_stats WHERE fixture_id='g1'").fetchall()}
    # teams named as in our result ("Heerenveen", not FotMob's "SC Heerenveen"); the unused substitute is left out
    assert rows["fotmob:1"] == ("fotmob:1", "PEC Zwolle", 1, 90, 0.47, 2, 1, 0, 3, 0, 0)
    assert rows["fotmob:2"] == ("fotmob:2", "Heerenveen", 0, 77, 0.0, 0, 0, 2, 0, 1, 1)  # second yellow: yellow 1 + red 1
    assert "fotmob:3" not in rows
    assert s.db.execute("SELECT team, name, kind, expected_return, market_value FROM fotmob_absences").fetchall() == [
        ("PEC Zwolle", "Hurt", "injury", "Late October", 500000)]
    assert st.saved["statistiche giocatori FotMob"] == 2
    kept = json.loads(s.raw_body(s.db.execute("SELECT id FROM raw_requests WHERE endpoint='/match/500'").fetchone()[0]))
    assert kept["players"]["1"]["shots"][0]["expectedGoals"] == 0.47  # the shot map is kept, not the 1 MB page


def test_a_second_tick_reads_nothing_again():
    s, api = _store(), FakeFotMob()
    _collector(s, api).run(history_seconds=0)
    n = len(api.calls)
    st = _collector(s, api, NOW + timedelta(minutes=20)).run(history_seconds=0)
    assert len(api.calls) == n and st.requests == 0


def test_history_reads_past_seasons_once_a_day():
    s, api = _store(), FakeFotMob()
    _collector(s, api).run(history_seconds=240)
    assert ("/leagues/57/fixtures/x", {"season": "2025/2026"}) in api.calls
    assert ("/leagues/57/fixtures/x", {"season": "2024/2025"}) not in api.calls  # history_seasons=1
    assert ("/match/400", {}) in api.calls
    assert s.db.execute("SELECT COUNT(*) FROM fotmob_player_stats WHERE fixture_id='g0'").fetchone()[0] == 2
    n = len(api.calls)
    _collector(s, api, NOW + timedelta(hours=2)).run(history_seconds=240)
    assert len(api.calls) == n  # same day: the slot is used, and nothing is left anyway


def test_a_short_tick_does_not_spend_the_day_history_slot():
    s, api = _store(), FakeFotMob()
    _collector(s, api).run(history_seconds=20)
    assert ("/match/400", {}) not in api.calls
    _collector(s, api, NOW + timedelta(minutes=20)).run(history_seconds=240)
    assert ("/match/400", {}) in api.calls


def test_plan_tick_adds_fotmob_after_the_price_steps():
    s = SnapshotStore(":memory:")
    cfg = AutoConfig(leagues=[League(name="Eredivisie", fotmob=57)])
    assert plan_tick(s, cfg, NOW, None) == ["fotmob"]
    assert "fotmob" not in plan_tick(s, AutoConfig(leagues=[League(name="Eredivisie")]), NOW, None)


def test_a_name_spelled_differently_links_through_the_other_team():
    s = SnapshotStore(":memory:")
    s.save_results("goal-api", [MatchResult(fixture_id="p1", competition="Eredivisie", home="PSG", away="PEC Zwolle", kickoff=KO,
                                            home_goals=2, away_goals=0),
                                MatchResult(fixture_id="p2", competition="Eredivisie", home="Ajax", away="Feyenoord", kickoff=KO,
                                            home_goals=1, away_goals=1)], NOW)
    col = _collector(s, FakeFotMob())
    lg = col.leagues[0]
    assert col._match(_fx("9", "Paris Saint-Germain", "PEC Zwolle", KO), col._results(lg)) == "p1"
    assert col._match(_fx("9", "Paris Saint-Germain", "Real Madrid", KO), col._results(lg)) is None


def test_finished_matches_without_our_result_are_reported():
    s, api = _store(), FakeFotMob()
    s.db.execute("DELETE FROM results WHERE fixture_id='g1'")
    st = _collector(s, api, NOW).run(history_seconds=0)
    assert st.requests == 0  # nothing recent of ours to link: the season page is not read
    st = _collector(s, api, NOW).run(history_seconds=240)
    assert any("PEC Zwolle-SC Heerenveen" in m for m in st.skipped)


class FlakyFotMob(FakeFotMob):
    """FakeFotMob that fails every request the way it is told: an exception (no answer) or a status code."""

    def __init__(self, fail):
        super().__init__()
        self.fail = fail

    def __call__(self, url, headers):
        if self.fail is None:
            return super().__call__(url, headers)
        self.calls.append((urlparse(url).path, {}))
        if isinstance(self.fail, BaseException):
            raise self.fail
        return self.fail, {}, b"<html>blocked</html>"


def test_no_answer_ends_fotmob_for_the_tick_without_failing_it():
    s, api = _store(), FlakyFotMob(TimeoutError("timed out"))
    st = _collector(s, api).run(history_seconds=240)
    assert len(api.calls) == 1  # no second request to a site that is not answering
    assert st.errors and "nessuna risposta" in st.errors[0] and any("non risponde" in m for m in st.skipped)
    api.fail = None  # not paused: the next tick tries again, and the day's history slot is still there
    _collector(s, api, NOW + timedelta(minutes=20)).run(history_seconds=240)
    assert ("/match/500", {}) in api.calls and ("/match/400", {}) in api.calls


def test_a_refusal_pauses_fotmob():
    s, api = _store(), FlakyFotMob(403)
    st = _collector(s, api).run(history_seconds=0)
    assert len(api.calls) == 1 and any("in pausa" in m for m in st.skipped)
    api.fail = None
    st = _collector(s, api, NOW + timedelta(hours=6)).run(history_seconds=0)
    assert len(api.calls) == 1 and st.requests == 0 and not st.errors  # paused: silent
    _collector(s, api, NOW + timedelta(hours=13)).run(history_seconds=0)
    assert ("/match/500", {}) in api.calls


def test_a_page_without_data_is_a_refusal_too():
    s, api = _store(), FlakyFotMob(None)
    api.fail = 200  # a challenge page: 200 without __NEXT_DATA__
    st = _collector(s, api).run(history_seconds=0)
    assert len(api.calls) == 1 and any("in pausa" in m for m in st.skipped)


def test_an_unexpected_error_is_a_log_line_and_a_dropped_turso_connection_goes_up():
    import pytest
    s, api = _store(), FakeFotMob()
    col = _collector(s, api)
    col.parse_match = lambda *a: (_ for _ in ()).throw(KeyError("content"))
    st = col.run(history_seconds=0)
    assert any("errore imprevisto" in e and "KeyError" in e for e in st.errors)
    col = _collector(s, api)
    col.due = lambda: (_ for _ in ()).throw(ValueError("Hrana: `http error: stream closed`"))
    with pytest.raises(ValueError):
        col.run(history_seconds=0)


def test_a_new_round_links_from_the_stored_page_without_reading_it_again():
    s, api = _store(), FakeFotMob()
    _collector(s, api).run(history_seconds=0)
    later = NOW + timedelta(days=3, hours=3)  # Ajax-PSV, a coming match on the stored page, is now played
    s.save_results("goal-api", [MatchResult(fixture_id="g2", competition="Eredivisie", home="Ajax", away="PSV", kickoff=NOW + timedelta(days=3),
                                            home_goals=2, away_goals=2)], later)
    n = sum(c[0] == "/leagues/57/fixtures/x" for c in api.calls)
    _collector(s, api, later).run(history_seconds=0)
    assert sum(c[0] == "/leagues/57/fixtures/x" for c in api.calls) == n
    assert ("/match/501", {}) in api.calls


def test_a_match_missing_from_the_stored_page_reads_the_season_once():
    s, api = _store(), FakeFotMob()
    _collector(s, api).run(history_seconds=0)
    later = NOW + timedelta(days=2)  # a match the stored page does not know (postponed, new date)
    s.save_results("goal-api", [MatchResult(fixture_id="g3", competition="Eredivisie", home="Twente", away="Utrecht", kickoff=NOW + timedelta(days=1),
                                            home_goals=0, away_goals=1)], later)
    seasons = lambda: sum(c[0] == "/leagues/57/fixtures/x" for c in api.calls)
    n = seasons()
    _collector(s, api, later).run(history_seconds=0)
    assert seasons() == n + 1  # read again: the page was older than the match
    _collector(s, api, later + timedelta(minutes=30)).run(history_seconds=0)
    assert seasons() == n + 1  # read after the match and still without it: not again


class BrokenPage(FakeFotMob):
    """FakeFotMob where some pages answer 500 while the rest of the site works."""

    def __init__(self, broken):
        super().__init__()
        self.broken = set(broken)

    def __call__(self, url, headers):
        path = urlparse(url).path
        if path in self.broken:
            self.calls.append((path, {}))
            return 500, {}, b"error"
        return super().__call__(url, headers)


def test_a_broken_match_page_never_stalls_the_others():
    s, api = _store(), BrokenPage({"/match/500"})
    for k in range(3):  # three ticks in a row: the page fails first each time, and ends the tick
        st = _collector(s, api, NOW + timedelta(minutes=30 * k)).run(history_seconds=240)
        assert ("/match/400", {}) not in api.calls and any("non risponde" in m for m in st.skipped)
    _collector(s, api, NOW + timedelta(hours=2)).run(history_seconds=240)
    assert ("/match/400", {}) in api.calls  # the broken page waits a day: the history goes on
    assert sum(c[0] == "/match/500" for c in api.calls) == 3
    st = _collector(s, api, NOW + timedelta(days=4)).run(history_seconds=0)  # tried again after the day; failing for 3 days: given up
    assert any("abbandonata" in m for m in st.skipped) and s.job_done("fotmob-none:g1")


def test_a_page_that_comes_back_clears_its_count():
    s, api = _store(), BrokenPage({"/match/500"})
    for k in range(2):
        _collector(s, api, NOW + timedelta(minutes=30 * k)).run(history_seconds=0)
    api.broken = set()
    _collector(s, api, NOW + timedelta(hours=1)).run(history_seconds=0)
    assert s.db.execute("SELECT COUNT(*) FROM jobs WHERE name LIKE 'fotmob-fail:%'").fetchone()[0] == 0
    assert s.db.execute("SELECT COUNT(*) FROM fotmob_player_stats WHERE fixture_id='g1'").fetchone()[0] == 2


def test_a_broken_season_page_waits_a_day_and_lets_the_other_leagues_go_on():
    s, api = _store(), BrokenPage({"/leagues/57/fixtures/x"})
    s.save_results("goal-api", [MatchResult(fixture_id="s1", competition="Serie A", home="Roma", away="Lazio", kickoff=KO,
                                            home_goals=1, away_goals=1)], NOW)
    client = FotMobClient(store=s, transport=api, sleep=lambda _: None, now=lambda: NOW)
    def col(t):
        client.now = lambda: t
        return FotMobCollector(client, s, [FotMobLeague(57, "Eredivisie"), FotMobLeague(55, "Serie A")], now=lambda: t)
    for k in range(3):
        col(NOW + timedelta(minutes=30 * k)).run(history_seconds=0)
    assert ("/leagues/55/fixtures/x", {}) not in api.calls  # stalled behind the broken Eredivisie page
    col(NOW + timedelta(hours=2)).run(history_seconds=0)
    assert ("/leagues/55/fixtures/x", {}) in api.calls and sum(c[0] == "/leagues/57/fixtures/x" for c in api.calls) == 3


def test_backfill_reads_the_history_at_its_own_pace_and_takes_the_day_slot():
    s, api = _store(), FakeFotMob()
    waits = []
    client = FotMobClient(store=s, transport=api, sleep=waits.append, clock=lambda: 0.0, now=lambda: NOW, min_interval=1.0)
    col = FotMobCollector(client, s, [FotMobLeague(57, "Eredivisie")], now=lambda: NOW, history_seasons=1, clock=lambda: 0.0)
    lines = []
    st, left = col.backfill(60, progress=lines.append)
    assert ("/match/400", {}) in api.calls and ("/match/500", {}) not in api.calls  # history only: the ticks keep the recent ones
    assert left == 0 and lines and "1/1 partite storiche" in lines[0]
    assert waits and max(waits) <= 1.0  # one page a second
    assert not col.history_due()  # a tick of the same day leaves the history alone
