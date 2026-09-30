"""OddsPapi client/mapper/collector and the online scheduler tick.

Payload shapes follow the OddsPapi v4 documentation examples; they are NOT yet verified against live responses."""
import json
from datetime import timedelta

import pytest

from algowinbet.autorun import AutoConfig, plan_tick, run_tick
from algowinbet.collector import GoalCollector
from algowinbet.domain import Fixture, utc
from algowinbet.names import TeamNames
from algowinbet.oddscollector import HIST_SOURCE, OddsCollector
from algowinbet.providers.goalapi import GoalApiClient
from algowinbet.providers.oddspapi import OddsPapiClient, OddsPapiError, OddsPapiMapper
from algowinbet.snapshots import BudgetExceeded, BudgetGuard, SnapshotProvider, SnapshotStore

NOW = utc(2026, 10, 10, 11, 30)
NAMES = TeamNames({"Genoa": ["Genoa CFC"], "Fiorentina": ["ACF Fiorentina"]})

MARKETS = [{"marketId": 101, "marketName": "1X2", "marketType": "1x2", "period": "fulltime", "sportId": 10,
            "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"}, {"outcomeId": 103, "outcomeName": "2"}]},
           {"marketId": 1010, "marketName": "Over/Under", "marketType": "totals", "handicap": 2.5, "period": "fulltime", "sportId": 10,
            "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
           {"marketId": 104, "marketName": "Both Teams To Score", "marketType": "btts", "period": "fulltime", "sportId": 10,
            "outcomes": [{"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
           {"marketId": 1200, "marketName": "Corners Over/Under", "marketType": "totals", "handicap": 9.5, "period": "fulltime",
            "sportId": 10, "outcomes": [{"outcomeId": 1200, "outcomeName": "Over"}]}]
BOOKS = [{"slug": "sisal.it", "bookmakerName": "Sisal"}, {"slug": "pinnacle", "bookmakerName": "Pinnacle"},
         {"slug": "snai.it", "bookmakerName": "Snai"}, {"slug": "bet365", "bookmakerName": "bet365"}]


def p(price, **kw):
    return {"0": {"price": price, "active": True, **kw}}


# v4 fixtures carry only participant ids; names come from /participants
PARTICIPANTS = [{"participantId": 1, "participantName": "Genoa CFC"}, {"participantId": 2, "participantName": "ACF Fiorentina"},
                {"participantId": 3, "participantName": "Inter Milan"}, {"participantId": 4, "participantName": "Parma Calcio 1913"}]


def fixture_odds(fid="op-1", home=1, away=2, start="2026-10-10T13:00:00Z", only=None):
    fx = _fixture_odds(fid, home, away, start)
    if only:
        fx["bookmakerOdds"] = {k: v for k, v in fx["bookmakerOdds"].items() if k == only}
    return fx


def _fixture_odds(fid, home, away, start):
    return {"fixtureId": fid, "participant1Id": home, "participant2Id": away, "startTime": start, "bookmakerOdds": {
        "sisal.it": {"markets": {"101": {"outcomes": {"101": {"players": p(2.4)}, "102": {"players": p(3.2)}, "103": {"players": p(3.0)}}},
                                 "1010": {"outcomes": {"1010": {"players": p(1.95)}, "1011": {"players": p(1.85)}}},
                                 "1200": {"outcomes": {"1200": {"players": p(1.8)}}}}},
        "pinnacle": {"markets": {"101": {"outcomes": {"101": {"players": p(2.5)}, "102": {"players": p(3.3)}, "103": {"players": p(3.1)}}},
                                 "104": {"outcomes": {"104": {"players": p(1.9)}, "105": {"players": p(1.95, active=False)}}}}},
        "snai.it": {"suspended": True, "markets": {"101": {"outcomes": {"101": {"players": p(9.0)}}}}}}}


class Fake:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers):
        self.calls.append(url)
        path, _, query = url.split("/v4", 1)[1].partition("?")
        route = self.routes.get(path, (404, {"error": "nope"}))
        status, body = route(dict(kv.split("=", 1) for kv in query.split("&"))) if callable(route) else route
        return status, {}, json.dumps(body).encode()


def mk_client(routes, store, monthly=250, reserve=0, now=NOW):
    t = Fake(routes)
    clock = iter(range(0, 10_000, 10))  # monotonic seconds, 10s apart: cooldowns never trigger unless a test wants them
    c = OddsPapiClient(api_key="OP-SECRET", store=store, budget=BudgetGuard(store, "oddspapi", monthly=monthly, reserve=reserve, now=lambda: now),
                       transport=t, sleep=lambda s: None, now=lambda: now, clock=lambda: next(clock))
    return c, t


def seed_calendar(store):
    fx = [Fixture(id="goal:g1", competition="Serie A", home="Genoa", away="Fiorentina", kickoff=utc(2026, 10, 10, 13), provider="goal-api",
                  provider_event_id="g1"),
          Fixture(id="goal:g2", competition="Serie A", home="Inter", away="Parma", kickoff=utc(2026, 10, 10, 16), provider="goal-api",
                  provider_event_id="g2")]
    store.save_fixtures("goal-api", fx, NOW - timedelta(days=2))
    return fx


# ------------------------------------------------------------------ client
def test_key_only_on_the_wire_and_free_endpoints_not_charged():
    store = SnapshotStore(":memory:")
    acc = {"current_subscription_id": 1, "subscriptions": [{"subscription_id": 1, "request_limit": 250, "request_count": 40,
                                                              "bookmakers": {"sisal.it": {}, "pinnacle": {}}}]}
    c, t = mk_client({"/account": (200, acc), "/fixtures": (200, [])}, store)
    info = c.account()
    assert info["bookmakers"] == ["pinnacle", "sisal.it"] and c.budget.used()[1] == 40  # synced from the provider counter
    c.get("/fixtures", {"tournamentId": 17})
    assert c.budget.used()[1] == 41 and "apiKey=OP-SECRET" in t.calls[-1]
    dump = "".join(str(r) for r in store.db.execute("SELECT * FROM raw_requests").fetchall())
    assert "OP-SECRET" not in dump and "OP-SECRET" not in repr(c)


def test_errors_are_billed_except_auth_and_429_and_budget_blocks_first():
    store = SnapshotStore(":memory:")
    c, t = mk_client({"/fixtures": (422, {"error": "bad"}), "/odds": (401, {"error": "key"})}, store, monthly=3, reserve=1)
    with pytest.raises(OddsPapiError):
        c.get("/fixtures")
    with pytest.raises(OddsPapiError):
        c.get("/odds")
    assert c.budget.used()[1] == 1  # 422 counted, 401 not
    with pytest.raises(OddsPapiError):
        c.get("/fixtures")  # 2nd billable: still allowed (limit 3 - reserve 1 = 2)
    with pytest.raises(BudgetExceeded):
        c.get("/fixtures")
    assert len(t.calls) == 3


def test_metadata_is_served_from_the_store_for_a_week():
    store = SnapshotStore(":memory:")
    c, t = mk_client({"/markets": (200, MARKETS)}, store)
    c.get("/markets")
    c.get("/markets")
    assert len(t.calls) == 1
    c2, t2 = mk_client({"/markets": (200, MARKETS)}, store, now=NOW + timedelta(days=8))
    c2.get("/markets")
    assert len(t2.calls) == 1


def test_bookmaker_resolution_prefers_italian_slug():
    store = SnapshotStore(":memory:")
    books = BOOKS + [{"slug": "sisal-es", "bookmakerName": "Sisal Spain"}]
    c, _ = mk_client({"/bookmakers": (200, books)}, store)
    assert c.resolve_bookmakers(["sisal", "Pinnacle", "snai"]) == ["sisal.it", "pinnacle", "snai.it"]
    with pytest.raises(OddsPapiError):
        c.resolve_bookmakers(["betfair"])


# ------------------------------------------------------------------ mapper
def test_mapper_links_fixture_and_maps_goal_markets_only():
    m = OddsPapiMapper(NAMES, MARKETS, PARTICIPANTS)
    cal = [Fixture(id="goal:g1", competition="Serie A", home="Genoa", away="Fiorentina", kickoff=utc(2026, 10, 10, 13))]
    fx = m.match_fixture(fixture_odds(), cal)
    assert fx.id == "goal:g1"
    qs = {(q.bookmaker, q.market_code, q.selection, q.line): q.odds for q in m.odds(fixture_odds(), fx, NOW)}
    assert qs[("sisal.it", "MATCH_1X2", "HOME", None)] == 2.4 and qs[("sisal.it", "TOTAL_GOALS", "UNDER", 2.5)] == 1.85
    assert qs[("pinnacle", "BTTS", "YES", None)] == 1.9 and ("pinnacle", "BTTS", "NO", None) not in qs  # inactive price
    assert not any(b == "snai.it" for b, *_ in qs)  # suspended bookmaker
    assert m.report.unmapped_markets == {"Corners Over/Under": 1}
    assert m.match_fixture(fixture_odds(start="2026-10-11T13:00:00Z"), cal) is None and m.report.gaps


def test_history_keeps_only_prekickoff_points_and_marks_close():
    m = OddsPapiMapper(NAMES, MARKETS)
    fx = Fixture(id="goal:g1", competition="Serie A", home="Genoa", away="Fiorentina", kickoff=utc(2026, 10, 10, 13))
    series = [{"createdAt": "2026-10-08T09:00:00Z", "price": 2.6}, {"createdAt": "2026-10-10T12:10:00Z", "price": 2.3},
              {"createdAt": "2026-10-10T13:20:00Z", "price": 1.5}]
    hist = {"fixtureId": "op-1", "bookmakers": {"sisal.it": {"markets": {"101": {"outcomes": {"101": {"players": {"0": series}}}}}}}}
    qs = m.history(hist, fx)
    assert [(q.odds, q.kind) for q in qs] == [(2.6, "current"), (2.3, "close")]
    assert qs[-1].observed_at == utc(2026, 10, 10, 12, 10)


# --------------------------------------------------------------- collector
def test_snapshot_then_free_closing_under_the_goal_fixture_id():
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    hist = {"fixtureId": "op-1", "bookmakers": {"sisal.it": {"markets": {"101": {"outcomes": {
        "101": {"players": {"0": [{"createdAt": "2026-10-10T12:50:00Z", "price": 2.2}]}}}}}}}}
    by_book = lambda q: (200, [fixture_odds(only=q["bookmaker"]), fixture_odds("op-2", 3, 4, "2026-10-10T16:00:00Z", only=q["bookmaker"])])
    routes = {"/markets": (200, MARKETS), "/bookmakers": (200, BOOKS), "/participants": (200, PARTICIPANTS),
              "/odds-by-tournaments": by_book, "/historical-odds": (200, hist)}
    c, t = mk_client(routes, store)
    col = OddsCollector(c, store, ["17"], ["sisal", "pinnacle", "snai"], NAMES, now=lambda: NOW)
    st = col.sync_odds()
    # both fixtures linked (Inter Milan / Parma Calcio 1913 by fuzzy names); sisal 5+5, pinnacle 4+4, snai suspended
    assert st.saved["quotes"] == 18 and not st.errors and c.budget.used()[1] == 6  # markets + bookmakers + participants + 3 books
    assert sorted(u.split("bookmaker=")[1].split("&")[0] for u in t.calls if "odds-by-tournaments" in u) == ["pinnacle", "sisal.it", "snai.it"]
    assert {q.bookmaker for q in SnapshotProvider(store).get_quotes("goal:g1")} == {"sisal.it", "pinnacle"}
    assert col.sync_closing().requests == 0  # match not played yet
    later, _ = mk_client(routes, store, now=NOW + timedelta(hours=4))
    col2 = OddsCollector(later, store, ["17"], ["sisal", "pinnacle", "snai"], NAMES, now=lambda: NOW + timedelta(hours=4))
    st2 = col2.sync_closing()
    assert st2.saved == {"quotes": 1} and later.budget.used()[1] == 6  # historical odds are free
    assert store.db.execute("SELECT kind, source FROM quotes WHERE source=?", (HIST_SOURCE,)).fetchall() == [("close", HIST_SOURCE)]
    assert col2.sync_closing().requests == 0


# ------------------------------------------------------------------ tick
def test_plan_tick_schedules_by_staleness_and_kickoff_slots():
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    cfg = AutoConfig(goal_leagues=["L"], oddspapi_tournaments=["17"])
    assert plan_tick(store, cfg, NOW, None) == ["fixtures", "results", "stats", "lineups", "backfill", "odds", "closing"]
    store.mark_job("backfill:goal:L", NOW)
    store.put_raw("goal-api", "/leagues/L/fixtures", {}, 200, b"{}", NOW - timedelta(hours=2))
    store.put_raw("goal-api", "/leagues/L/results", {}, 200, b"{}", NOW - timedelta(hours=2))
    t = utc(2026, 10, 10, 12, 10)  # 50 min before Genoa-Fiorentina: pre-kickoff slot
    assert plan_tick(store, cfg, t, t - timedelta(hours=3)) == ["lineups", "odds", "closing"]
    assert plan_tick(store, cfg, t, t - timedelta(minutes=20)) == ["lineups", "closing"]  # slot already covered
    assert plan_tick(store, cfg, utc(2026, 10, 10, 9), utc(2026, 10, 10, 1)) == ["lineups", "closing"]  # daily done, no slot


def test_odds_budget_is_paced_over_the_month():
    from algowinbet.autorun import odds_allowed_today
    store = SnapshotStore(":memory:")
    cfg = AutoConfig(oddspapi_tournaments=["17"], oddspapi_monthly_limit=250, oddspapi_reserve=20)
    day = utc(2026, 10, 1, 9)  # 31 days left, 230 usable: today may spend up to ~14
    assert odds_allowed_today(store, cfg, day, 2)
    store.add_usage("oddspapi", "D2026-10-01", 14)
    store.add_usage("oddspapi", "M2026-10", 14)
    assert not odds_allowed_today(store, cfg, day, 2)
    store.add_usage("oddspapi", "M2026-10", 215)  # month almost gone: the reserve is never touched
    assert not odds_allowed_today(store, cfg, utc(2026, 10, 2, 9), 2)


def test_tick_with_nothing_due_sends_nothing():
    store = SnapshotStore(":memory:")
    cfg = AutoConfig(goal_leagues=["L"])
    for ep in ("/leagues/L/fixtures", "/leagues/L/results"):
        store.put_raw("goal-api", ep, {}, 200, b"{}", NOW - timedelta(hours=1))
    store.mark_job("backfill:goal:L", NOW - timedelta(days=3))
    sent = []
    gc = GoalApiClient(api_key="k", store=store, transport=lambda u, h: sent.append(u) or (200, {}, b'{"success":true,"data":[]}'),
                       now=lambda: NOW)
    res = run_tick(store, cfg, GoalCollector(gc, store, ["L"], NAMES, now=lambda: NOW), None, now=lambda: NOW)
    assert [r.mode for r in res] == ["lineups"] and sent == []


def test_auto_config_file_loads_saved_ids():
    cfg = AutoConfig.load("configs/collect.json")
    assert cfg.goal_leagues and cfg.oddspapi_monthly_limit == 250 and cfg.prekick_min == (30, 75)
    assert cfg.oddspapi_tournaments == ["23", "17", "35", "34", "8", "238", "37", "7", "679"] and len(cfg.leagues) == 9


def test_history_backfill_runs_once_per_league_without_csv():
    store = SnapshotStore(":memory:")
    rows = [{"id": f"r{i}", "matchStatus": "FINISHED", "kickoffUtc": f"2025-0{1 + i % 8}-1{i % 9}T18:00:00.000Z", "league": {"name": "Serie A"},
             "homeTeam": {"name": f"H{i}"}, "awayTeam": {"name": f"A{i}"}, "homeTeamScore": "1", "awayTeamScore": "0"} for i in range(5)]
    sent = []

    def transport(url, headers):
        sent.append(url)
        return 200, {}, json.dumps({"success": True, "data": rows, "pagination": {"hasMore": False}}).encode()
    gc = GoalApiClient(api_key="k", store=store, transport=transport, now=lambda: NOW)
    col = GoalCollector(gc, store, ["L1", "L2"], NAMES, now=lambda: NOW)
    st = col.backfill_next()
    assert st.mode == "backfill L1" and st.saved == {"results": 5} and "/leagues/L1/results" in sent[0] and "from=2024-" in sent[0]
    assert col.backfill_next().mode == "backfill L2" and col.backfill_next() is None and len(sent) == 2
    assert len(SnapshotProvider(store).list_history(None, NOW)) == 5


def test_tick_stops_starting_steps_after_time_budget_and_reports_each_step():
    store = SnapshotStore(":memory:")
    cfg = AutoConfig(goal_leagues=["L"])
    gc = GoalApiClient(api_key="k", store=store, transport=lambda u, h: (200, {}, b'{"success":true,"data":[],"pagination":{"hasMore":false}}'),
                       now=lambda: NOW)
    ticks = iter([0, 1, 500, 501, 502, 503, 504])
    seen = []
    res = run_tick(store, cfg, GoalCollector(gc, store, ["L"], NAMES, now=lambda: NOW), None, now=lambda: NOW,
                   on_step=lambda st: seen.append(st.mode), max_seconds=100, clock=lambda: next(ticks))
    assert seen[0] == "fixtures" and all("tempo del giro esaurito" in r.skipped[0] for r in res[1:]) and len(seen) == len(res)


def test_bulk_writes_use_few_statements():
    store = SnapshotStore(":memory:")
    from algowinbet.domain import MatchResult
    rs = [MatchResult(fixture_id=f"goal:{i}", competition="Serie A", home=f"H{i}", away=f"A{i}", kickoff=NOW - timedelta(days=i),
                      home_goals=1, away_goals=0) for i in range(1000)]
    calls = []
    real = store.db
    class Spy:
        def __getattr__(self, k):
            return getattr(real, k)
        def execute(self, *a):
            calls.append(a[0][:20])
            return real.execute(*a)
    store.db = Spy()
    assert store.save_results("x", rs, NOW) == 1000 and len(calls) <= 10
