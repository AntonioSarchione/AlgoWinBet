from datetime import datetime, timedelta, timezone

from algowinbet.collector import CollectStats
from algowinbet.domain import Fixture, FixtureStatus, MatchResult
from algowinbet.fdcollector import FootballDataCollector, opening_time, season_code, seasons_back
from algowinbet.snapshots import SnapshotStore

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

HEAD = ("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,HxG,AxG,HS,AS,HST,AST,HC,AC,HY,AY,HR,AR,"
        "PSH,PSD,PSA,P>2.5,P<2.5,AHh,PAHH,PAHA,BFEH,BFED,BFEA,PSCH,PSCD,PSCA,PC>2.5,PC<2.5,AHCh,PCAHH,PCAHA")


def row(date, time, home, away, ftg=(2, 1), htg=(1, 0), ah="-0.5", ahc="-0.25"):
    return (f"I1,{date},{time},{home},{away},{ftg[0]},{ftg[1]},{htg[0]},{htg[1]},1.7,0.9,14,8,6,3,7,2,1,2,0,0,"
            f"1.80,3.70,4.60,1.90,1.95,{ah},1.95,1.93,1.82,3.80,4.80,1.75,3.80,5.00,1.85,2.00,{ahc},1.90,2.00")


def csv_body(*rows):
    return ("﻿" + HEAD + "\n" + "\n".join(rows) + "\n").encode()


def store_with(*matches):
    st = SnapshotStore(":memory:")
    st.save_results("goal", [MatchResult(fixture_id=fid, competition="Serie A", home=h, away=a, kickoff=ko, home_goals=0, away_goals=0)
                             for fid, h, a, ko in matches], NOW)
    return st


def test_season_codes_and_opening_time():
    assert season_code(NOW) == "2627" and season_code(datetime(2026, 6, 1, tzinfo=UTC)) == "2526"
    assert seasons_back(NOW, 2) == ["2627", "2526", "2425"]
    sunday = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)
    assert opening_time(sunday) == datetime(2026, 10, 2, 14, 0, tzinfo=UTC)  # Friday 15:00 UK (BST)
    wednesday = datetime(2026, 9, 30, 19, 0, tzinfo=UTC)
    assert opening_time(wednesday) == datetime(2026, 9, 29, 14, 0, tzinfo=UTC)  # Tuesday 15:00 UK
    friday_early = datetime(2026, 10, 2, 14, 30, tzinfo=UTC)
    assert opening_time(friday_early) == friday_early - timedelta(hours=1)  # never later than 1h before kickoff


def test_load_links_rows_saves_stats_and_timed_quotes():
    ko = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)  # 14:00 UK
    st = store_with(("goal:1", "Internazionale", "AC Milan", ko), ("goal:2", "Juventus FC", "Napoli", ko + timedelta(hours=3)))
    c = FootballDataCollector(st, {"I1": "Serie A"}, now=lambda: NOW)
    from algowinbet.names import TeamNames
    c.names = TeamNames({"Inter": ["Internazionale"], "Milan": ["AC Milan"], "Juventus": ["Juventus FC"]})
    stats = CollectStats("t")
    rep = c.load(csv_body(row("27/09/2026", "14:00", "Inter", "Milan"), row("27/09/2026", "17:00", "Juventus", "Napoli")),
                 "I1", None, stats)
    assert (rep.rows, rep.linked, rep.unmatched) == (2, 2, [])
    assert st.stats_of("goal:1")["goals"] == (2, 1)
    assert st.stats_of("goal:1", "1H")["goals"] == (1, 0)
    assert st.stats_of("goal:1")["expected_goals"] == (1.7, 0.9)
    assert st.stats_of("goal:1")["corners"] == (7, 2)
    q = {(r[0], r[1], r[2], r[3], r[4]): (r[5], r[6]) for r in st.db.execute(
        "SELECT market_code, selection, line_key, bookmaker, kind, odds, observed_at FROM quotes WHERE fixture_id='goal:1'")}
    assert q[("MATCH_1X2", "HOME", "", "pinnacle", "open")] == (1.80, "2026-09-25T14:00:00+00:00")
    assert q[("MATCH_1X2", "HOME", "", "pinnacle", "close")] == (1.75, "2026-09-27T12:55:00+00:00")
    assert q[("TOTAL_GOALS", "OVER", "2.5", "pinnacle", "open")][0] == 1.90
    assert q[("TOTAL_GOALS", "UNDER", "2.5", "pinnacle", "close")][0] == 2.00
    assert q[("ASIAN_HANDICAP", "HOME", "-0.5", "pinnacle", "open")][0] == 1.95
    assert q[("MATCH_1X2", "AWAY", "", "betfair-ex", "open")][0] == 4.80
    assert not any(k[0] == "ASIAN_HANDICAP" and k[4] == "close" for k in q)  # quarter line -0.25 refunds: skipped


def test_unknown_spelling_learnt_from_known_opponents():
    """'Wolves' never matches 'Wolverhampton Wanderers FC' by spelling: the clubs it met pin it down."""
    d0 = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
    matches, rows = [], []
    others = ["Arsenal", "Chelsea", "Everton", "Fulham"]
    for k, o in enumerate(others):
        ko = d0 + timedelta(days=7 * k)
        home = k % 2 == 0
        matches.append((f"goal:{k}", "Wolverhampton Wanderers FC" if home else o, o if home else "Wolverhampton Wanderers FC", ko))
        rows.append(row(f"{ko + timedelta(hours=1):%d/%m/%Y}", "15:00", "Wolves" if home else o, o if home else "Wolves"))
        matches.append((f"goal:x{k}", "Brentford", "Burnley", ko))  # another match the same day
        rows.append(row(f"{ko + timedelta(hours=1):%d/%m/%Y}", "15:00", "Brentford", "Burnley"))
    c = FootballDataCollector(store_with(*matches), {"E0": "Premier League"}, now=lambda: NOW)
    rep = c.load(csv_body(*rows), "E0", None, CollectStats("t"))
    assert rep.linked == 8 and rep.unmatched == []


def test_row_without_our_match_is_reported_not_guessed():
    ko = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)
    c = FootballDataCollector(store_with(("goal:1", "Inter", "Milan", ko)), {"I1": "Serie A"}, now=lambda: NOW)
    rep = c.load(csv_body(row("27/09/2026", "14:00", "Inter", "Milan"), row("20/09/2026", "14:00", "Roma", "Lazio")),
                 "I1", None, CollectStats("t"))
    assert rep.linked == 1 and rep.unmatched == ["Roma-Lazio 20/09/2026"]


def test_same_match_under_two_ids_gets_the_data_on_both():
    ko = datetime(2025, 8, 24, 13, 30, tzinfo=UTC)
    st = store_with(("goal:a", "Mainz 05", "Köln", ko))
    # results keeps one id per match; the GOAL calendar (fixtures) keeps both, and the CSV data goes on both
    st.save_fixtures("goal", [Fixture(id=fid, competition="Bundesliga", home="Mainz 05", away="Köln", kickoff=ko, status=FixtureStatus.FINISHED)
                              for fid in ("goal:a", "goal:b")], NOW)
    c = FootballDataCollector(st, {"D1": "Bundesliga"}, now=lambda: NOW)
    rep = c.load(csv_body(row("24/08/2025", "14:30", "Mainz", "FC Koln")), "D1", None, CollectStats("t"))
    assert rep.linked == 1 and st.stats_of("goal:a")["goals"] == st.stats_of("goal:b")["goals"] == (2, 1)


class FakeSite:
    def __init__(self, body):
        self.body, self.calls, self.status = body, [], 200

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return (304, b"") if self.status == 304 else (self.status, self.body)


def test_sync_downloads_past_seasons_once_and_current_every_few_days():
    ko = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)
    st = store_with(("goal:1", "Inter", "Milan", ko))
    site = FakeSite(csv_body(row("27/09/2026", "14:00", "Inter", "Milan")))
    t = [NOW]
    c = FootballDataCollector(st, {"I1": "Serie A"}, now=lambda: t[0], fetch=site)
    r = c.sync(previous_seasons=2)
    assert r.requests == 3 and [u.split("/")[-2] for u, _ in site.calls] == ["2627", "2526", "2425"]
    assert c.due() == []
    t[0] = NOW + timedelta(days=1)
    assert c.due() == []  # current season checked yesterday
    t[0] = NOW + timedelta(days=4)
    assert c.due() == [("2627", "I1")]  # past seasons are never downloaded again
    site.status = 304
    r = c.sync()
    assert r.requests == 1 and "If-Modified-Since" in site.calls[-1][1] and r.skipped == ["I1 2627: invariato"]
    t[0] = NOW + timedelta(days=8)
    site.status = 200  # same bytes again: stored, not parsed again
    r = c.sync()
    assert r.skipped == ["I1 2627: invariato"] and r.saved == {}
    import json
    detail = json.loads(st.db.execute("SELECT detail FROM jobs WHERE name='dataset-report:football-data:I1:2627'").fetchone()[0])
    assert (detail["rows"], detail["linked"], detail["competition"]) == (1, 1, "Serie A")


def test_past_season_with_few_links_is_read_again_later():
    site = FakeSite(csv_body(row("27/09/2025", "14:00", "Inter", "Milan")))  # no match of ours that day
    t = [NOW]
    c = FootballDataCollector(SnapshotStore(":memory:"), {"I1": "Serie A"}, now=lambda: t[0], fetch=site)
    r = c.sync(previous_seasons=1)
    assert "I1 2526: abbinate 0/1, riprovo tra 3 giorni" in r.skipped
    t[0] = NOW + timedelta(days=4)
    assert ("2526", "I1") in c.due(previous_seasons=1)


def test_past_season_read_again_once_when_the_linker_improves():
    st = SnapshotStore(":memory:")
    st.mark_job("dataset:football-data:I1:2526", NOW, '{"rows": 380, "linked": 370, "n_unmatched": 10}')  # old linker
    st.mark_job("dataset:football-data:I1:2425", NOW, '{"rows": 380, "linked": 380, "n_unmatched": 0}')
    c = FootballDataCollector(st, {"I1": "Serie A"}, now=lambda: NOW + timedelta(days=4))
    assert c.due() == [("2627", "I1"), ("2526", "I1")]


def test_sync_stops_starting_files_when_time_is_up():
    site = FakeSite(csv_body())
    ticks = iter([0, 0, 100, 100, 100])
    c = FootballDataCollector(SnapshotStore(":memory:"), {"I1": "Serie A", "E0": "Premier League"}, now=lambda: NOW, fetch=site)
    r = c.sync(previous_seasons=2, max_seconds=50, clock=lambda: next(ticks))
    assert r.requests == 1 and r.skipped == ["5 file rimandati al prossimo giro (tempo)"]


def test_http_error_is_reported_and_retried_next_time():
    site = FakeSite(b"")
    site.status = 404
    c = FootballDataCollector(SnapshotStore(":memory:"), {"I1": "Serie A"}, now=lambda: NOW, fetch=site)
    r = c.sync(previous_seasons=1)
    assert r.errors == ["I1 2627: HTTP 404", "I1 2526: HTTP 404"]
    assert c.due(previous_seasons=1) == []  # no retry at every tick
    c.now = lambda: NOW + timedelta(days=4)
    assert c.due(previous_seasons=1) == [("2627", "I1"), ("2526", "I1")]


def test_scheduler_runs_datasets_after_the_backfill():
    from algowinbet.autorun import AutoConfig, League, plan_tick, run_tick
    cfg = AutoConfig(leagues=[League("Serie A", goal="g1", fd="I1")], goal_leagues=["g1"], divisions={"I1": "Serie A"})
    st = SnapshotStore(":memory:")
    assert "datasets" not in plan_tick(st, cfg, NOW, None)  # results history first: the files link to it
    st.mark_job("backfill:goal:g1", NOW)
    assert "datasets" in plan_tick(st, cfg, NOW, None)
    site = FakeSite(csv_body())
    out = run_tick(st, cfg, None, None, now=lambda: NOW, datasets=FootballDataCollector(st, cfg.divisions, now=lambda: NOW, fetch=site))
    assert [r.mode for r in out] == ["datasets"] and len(site.calls) == 3
    assert "datasets" not in plan_tick(st, cfg, NOW, None)


def test_config_maps_divisions():
    from algowinbet.autorun import AutoConfig
    cfg = AutoConfig.load("configs/collect.json")
    assert cfg.divisions == {"I1": "Serie A", "E0": "Premier League", "D1": "Bundesliga", "F1": "Ligue 1", "SP1": "LaLiga",
                             "P1": "Liga Portugal", "N1": "Eredivisie"}


def test_a_finished_match_without_result_triggers_the_results_step_early():
    from algowinbet.autorun import AutoConfig, League, late_result_leagues, plan_tick
    from algowinbet.domain import Fixture, FixtureStatus, MatchResult
    st = SnapshotStore(":memory:")
    cfg = AutoConfig(leagues=[League("UEFA Nations League", goal="nl"), League("Serie A", goal="sa")])
    cfg.goal_leagues = ["nl", "sa"]
    ko = NOW - timedelta(hours=12)
    st.save_fixtures("goal-api", [Fixture(id="goal:1", competition="UEFA Nations League", home="Denmark", away="Portugal", kickoff=ko,
                                          status=FixtureStatus.SCHEDULED)], ko - timedelta(days=2))
    for lid in ("nl", "sa"):  # daily syncs done 6 hours ago: not stale yet
        for kind in ("fixtures", "results"):
            st.db.execute("INSERT INTO raw_requests(source, endpoint, params, status, fetched_at) VALUES('goal-api', ?, '{\"from\": 1}', 200, ?)",
                          (f"/leagues/{lid}/{kind}", (NOW - timedelta(hours=6)).isoformat()))
    st.db.commit()
    assert late_result_leagues(st, cfg, NOW) == ["nl"]
    assert "results" not in plan_tick(st, cfg, NOW - timedelta(hours=10), None)  # 2 hours after kickoff: not over yet
    assert "results" in plan_tick(st, cfg, NOW, None)
    st.save_results("goal-api", [MatchResult(fixture_id="goal:1", competition="UEFA Nations League", home="Denmark", away="Portugal",
                                             kickoff=ko, home_goals=1, away_goals=1)], NOW)
    assert late_result_leagues(st, cfg, NOW) == [] and "results" not in plan_tick(st, cfg, NOW, None)
