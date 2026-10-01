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
           {"marketId": 1200, "marketName": "Corners Over/Under", "marketType": "totals-corners", "handicap": 9.5, "period": "fulltime",
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
    assert m.match_fixture(fixture_odds(start="2026-10-11T13:00:00Z"), cal).id == "goal:g1"  # moved by a day: same match
    assert m.match_fixture(fixture_odds(start="2026-10-16T13:00:00Z"), cal) is None and m.report.gaps  # too far apart
    twice = cal + [Fixture(id="goal:g9", competition="Coppa Italia", home="Genoa", away="Fiorentina", kickoff=utc(2026, 10, 13, 19))]
    assert m.match_fixture(fixture_odds(start="2026-10-11T19:00:00Z"), twice) is None  # two candidates: never guess


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


def _counter_account(counter):
    return lambda q: (200, {"current_subscription_id": 1, "subscriptions": [{"subscription_id": 1, "request_limit": 250,
                                                                            "request_count": counter[0]}]})


def test_prematch_history_saves_thinned_price_paths_and_syncs_the_budget():
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    early = utc(2026, 10, 10, 9)  # both matches still to play
    series = {"101": {"players": {"0": [{"createdAt": "2026-10-08T09:00:00Z", "price": 2.6}, {"createdAt": "2026-10-10T08:00:00Z", "price": 2.4}]}}}
    hist = {"bookmakers": {"sisal.it": {"markets": {"101": {"outcomes": series}}}}}
    by_book = lambda q: (200, [fixture_odds(only=q["bookmaker"]), fixture_odds("op-2", 3, 4, "2026-10-10T16:00:00Z", only=q["bookmaker"])])
    routes = {"/markets": (200, MARKETS), "/bookmakers": (200, BOOKS), "/participants": (200, PARTICIPANTS),
              "/odds-by-tournaments": by_book, "/account": _counter_account([7]), "/historical-odds": (200, hist)}
    c, t = mk_client(routes, store, now=early)
    col = OddsCollector(c, store, ["17"], ["sisal", "pinnacle"], NAMES, now=lambda: early)
    col.sync_odds()
    st = col.sync_prematch_history()
    assert sum("historical-odds" in u for u in t.calls) == 2 and st.saved["quotes"] == 4 and not st.skipped
    assert {r[0] for r in store.db.execute("SELECT kind FROM quotes WHERE observed_at < '2026-10-10T09'").fetchall()} == {"current"}
    assert "/account" in t.calls[-1] and c.budget.used()[1] == 7  # provider counter synced after the free calls
    col.sync_prematch_history()  # a newer path replaces the older payload of the same fixture
    assert store.db.execute("SELECT COUNT(*) FROM raw_requests WHERE endpoint='/historical-odds'").fetchone()[0] == 2


def test_thin_history_keeps_opening_checkpoints_and_latest():
    from algowinbet.domain import OddsQuote
    from algowinbet.oddscollector import thin_history
    ko = utc(2026, 10, 10, 20)
    qs = [OddsQuote(fixture_id="f", market_code="MATCH_1X2", selection="HOME", bookmaker="pinnacle", odds=2.0 + i / 100,
                    observed_at=ko - timedelta(hours=100 - i), kind="current") for i in range(95)]  # hourly from 100h to 6h before
    kept = thin_history(qs, ko, ko - timedelta(hours=5))
    hours = [round((ko - q.observed_at).total_seconds() / 3600) for q in kept]
    assert hours == [100, 72, 48, 24, 12, 6]  # 3h and 1h not reached yet; the 6h checkpoint is also the latest price


# ------------------------------------------------------------------ tick
def test_snapshot_splits_tournaments_in_blocks_of_five():
    store = SnapshotStore(":memory:")
    routes = {"/markets": (200, MARKETS), "/bookmakers": (200, BOOKS), "/participants": (200, PARTICIPANTS), "/odds-by-tournaments": (200, [])}
    c, t = mk_client(routes, store)
    col = OddsCollector(c, store, [str(i) for i in range(1, 11)] + ["99"], ["sisal", "pinnacle"], NAMES, now=lambda: NOW)
    assert col.snapshot_cost() == 6
    col.sync_odds()
    sent = [u.split("tournamentIds=")[1].split("&")[0] for u in t.calls if "odds-by-tournaments" in u]
    assert len(sent) == 6 and max(s.count("%2C") + 1 for s in sent) == 5


def test_manual_refresh_is_capped_per_month_and_never_repeated_within_minutes():
    from algowinbet.autorun import MANUAL_REFRESHES
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    cfg = AutoConfig(oddspapi_tournaments=["17"])
    t = utc(2026, 10, 10, 9)
    assert plan_tick(store, cfg, t, t - timedelta(hours=1)) == ["closing"]  # daily snapshot already taken today
    assert plan_tick(store, cfg, t, t - timedelta(hours=1), manual=True) == ["odds", "history", "closing"]
    assert plan_tick(store, cfg, t, t - timedelta(minutes=5), manual=True) == ["history", "closing"]  # double click
    store.add_usage(MANUAL_REFRESHES, "M2026-10", 5)
    assert plan_tick(store, cfg, t, t - timedelta(hours=1), manual=True) == ["history", "closing"]  # 5/5 used: free history only
    assert plan_tick(store, cfg, t, t - timedelta(hours=1), history=True) == ["history", "closing"]


def test_manual_refresh_is_counted_apart_from_the_automatic_plan():
    from algowinbet.autorun import MANUAL_REQUESTS, manual_used, odds_allowed_today
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    routes = {"/markets": (200, MARKETS), "/bookmakers": (200, BOOKS), "/participants": (200, PARTICIPANTS),
              "/odds-by-tournaments": lambda q: (200, [fixture_odds(only=q["bookmaker"])]), "/account": _counter_account([0]),
              "/historical-odds": (200, {"bookmakers": {}})}
    t = utc(2026, 10, 10, 9)
    c, sent = mk_client(routes, store, now=t)
    cfg = AutoConfig(oddspapi_tournaments=["17"])
    col = OddsCollector(c, store, ["17"], ["sisal"], NAMES, now=lambda: t, history_books=["sisal", "pinnacle"])
    res = run_tick(store, cfg, None, col, now=lambda: t, manual=True)
    assert [r.mode for r in res] == ["odds", "history", "closing"]
    assert sum("odds-by-tournaments" in u for u in sent.calls) == 1  # Sisal only
    assert all("bookmakers=sisal.it%2Cpinnacle" in u for u in sent.calls if "historical-odds" in u)
    assert manual_used(store, t) == 1 and store.usage(MANUAL_REQUESTS, "M2026-10") == res[0].requests > 0
    store.add_usage("oddspapi", "M2026-10", 150)
    store.add_usage(MANUAL_REQUESTS, "M2026-10", 150)
    assert odds_allowed_today(store, cfg, t, 2)  # manual requests do not eat the automatic plan


def test_plan_tick_schedules_daily_crowded_slots_and_busy_day_refreshes():
    store = SnapshotStore(":memory:")
    seed_calendar(store)  # Genoa-Fiorentina 13:00, Inter-Parma 16:00 on 10/10
    cfg = AutoConfig(goal_leagues=["L"], oddspapi_tournaments=["17"])
    assert plan_tick(store, cfg, NOW, None) == ["fixtures", "results", "stats", "lineups", "backfill", "odds", "closing"]
    store.mark_job("backfill:goal:L", NOW)
    store.put_raw("goal-api", "/leagues/L/fixtures", {"from": "x"}, 200, b"{}", NOW - timedelta(hours=2))
    store.put_raw("goal-api", "/leagues/L/results", {"from": "x"}, 200, b"{}", NOW - timedelta(hours=2))
    t = utc(2026, 10, 10, 12, 10)  # 50 min before Genoa-Fiorentina, alone in its slot
    assert plan_tick(store, cfg, t, t - timedelta(hours=3)) == ["lineups", "closing"]  # daily done, slot not crowded
    crowded = AutoConfig(goal_leagues=["L"], oddspapi_tournaments=["17"], crowded_slot=1)
    assert plan_tick(store, crowded, t, t - timedelta(hours=3)) == ["lineups", "odds", "closing"]
    assert plan_tick(store, crowded, t, t - timedelta(minutes=20)) == ["lineups", "closing"]  # slot already covered
    assert plan_tick(store, cfg, utc(2026, 10, 10, 9), utc(2026, 10, 9, 22)) == ["lineups", "odds", "closing"]  # first of the day
    busy = AutoConfig(goal_leagues=["L"], oddspapi_tournaments=["17"], busy_day=2)
    nine = utc(2026, 10, 10, 9)
    assert plan_tick(store, busy, nine, utc(2026, 10, 10, 3)) == ["lineups", "odds", "closing"]  # refresh 6h after the daily one
    assert plan_tick(store, busy, nine, utc(2026, 10, 10, 6)) == ["lineups", "closing"]  # too soon
    store.add_usage("oddspapi", "M2026-10", 70)  # ahead of the month's share (200 * 10/31): optional refreshes stop
    assert plan_tick(store, busy, nine, utc(2026, 10, 10, 3)) == ["lineups", "closing"]


def test_free_history_is_due_at_checkpoints_daily_and_after_the_lineup():
    from algowinbet.autorun import history_due
    from algowinbet.oddscollector import LINKS_SCHEMA
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    cfg = AutoConfig(oddspapi_tournaments=["17"])
    assert history_due(store, cfg, NOW) == []  # nothing linked yet
    store.db.executescript(LINKS_SCHEMA)
    store.db.executemany("INSERT INTO fixture_links VALUES('oddspapi', ?, ?, '')", [("op-1", "goal:g1"), ("op-2", "goal:g2")])
    assert history_due(store, cfg, NOW) == ["goal:g1", "goal:g2"]  # never fetched
    for ext in ("op-1", "op-2"):
        store.put_raw("oddspapi", "/historical-odds", {"fixtureId": ext, "bookmakers": "sisal.it,pinnacle"}, 200, b"{}", utc(2026, 10, 10, 11))
    # g1 (13:00): the 1.5h checkpoint (11:30) passed after the fetch; g2 (16:00): last checkpoint 6h (10:00) already covered
    assert history_due(store, cfg, NOW) == ["goal:g1"]
    store.db.execute("INSERT INTO lineups(fixture_id, team, status, observed_at) VALUES('goal:g2', 'Inter', 'confirmed', ?)",
                     ("2026-10-10T11:20:00+00:00",))
    assert history_due(store, cfg, NOW) == ["goal:g1", "goal:g2"]  # confirmed XI after the last fetch


def test_odds_budget_is_paced_over_the_month():
    from algowinbet.autorun import odds_allowed_today
    store = SnapshotStore(":memory:")
    cfg = AutoConfig(oddspapi_tournaments=["17"], oddspapi_monthly_limit=250, oddspapi_reserve=20)
    day = utc(2026, 10, 1, 9)  # 31 days left, plan of 200: today may spend up to ~12
    assert odds_allowed_today(store, cfg, day, 2)
    store.add_usage("oddspapi", "D2026-10-01", 12)
    store.add_usage("oddspapi", "M2026-10", 12)
    assert not odds_allowed_today(store, cfg, day, 2)
    store.add_usage("oddspapi", "M2026-10", 188)  # plan spent: nothing more automatic this month
    assert not odds_allowed_today(store, cfg, utc(2026, 10, 2, 9), 2)


def test_tick_with_nothing_due_sends_nothing():
    store = SnapshotStore(":memory:")
    cfg = AutoConfig(goal_leagues=["L"])
    for ep in ("/leagues/L/fixtures", "/leagues/L/results"):
        store.put_raw("goal-api", ep, {"from": "x"}, 200, b"{}", NOW - timedelta(hours=1))
    store.mark_job("backfill:goal:L", NOW - timedelta(days=3))
    sent = []
    gc = GoalApiClient(api_key="k", store=store, transport=lambda u, h: sent.append(u) or (200, {}, b'{"success":true,"data":[]}'),
                       now=lambda: NOW)
    res = run_tick(store, cfg, GoalCollector(gc, store, ["L"], NAMES, now=lambda: NOW), None, now=lambda: NOW)
    assert [r.mode for r in res] == ["lineups"] and sent == []


def test_auto_config_file_loads_saved_ids():
    cfg = AutoConfig.load("configs/collect.json")
    assert cfg.goal_leagues and cfg.oddspapi_monthly_limit == 250 and cfg.prekick_min == (30, 75)
    assert cfg.bookmakers == ["sisal"] and cfg.history_bookmakers == ["sisal", "pinnacle"] and cfg.manual_monthly == 5
    assert cfg.oddspapi_tournaments == ["23", "17", "35", "34", "8", "238", "37", "7", "679", "23755"] and len(cfg.leagues) == 10


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


def test_interrupted_tick_leaves_no_league_behind():
    store = SnapshotStore(":memory:")
    store.put_raw("goal-api", "/leagues/A/fixtures", {"from": "x"}, 200, b"{}", NOW - timedelta(hours=1))  # A done, B never fetched
    store.put_raw("goal-api", "/leagues/B/fixtures", {"limit": "3"}, 200, b"{}", NOW)  # a manual probe on B is not a sync
    cfg = AutoConfig(goal_leagues=["A", "B"])
    assert "fixtures" in plan_tick(store, cfg, NOW, None)
    gc = GoalApiClient(api_key="k", store=store, transport=lambda u, h: (200, {}, b"{}"), now=lambda: NOW)
    assert GoalCollector(gc, store, ["A", "B"], NAMES, now=lambda: NOW).stale_leagues("fixtures") == ["B"]


def test_publish_writes_dashboard_tables_and_prunes():
    from algowinbet.publish import analyze_and_publish, last_publication
    from algowinbet.providers import MockProvider
    from algowinbet.config import Config
    mock = MockProvider(seed=7)
    s = SnapshotStore(":memory:")
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)
    fxs = mock.list_fixtures(None, mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    cfg = Config()
    cfg.ensemble.quote_window_hours = 72  # mock prices are timed relative to kickoff, not to "now"
    cfg.bet_bookmakers = ["BookA", "BookB", "BookC"]  # mock books stand in for Sisal
    rid, res = analyze_and_publish(s, cfg, now=mock.as_of)
    n_fx = s.db.execute("SELECT COUNT(*), SUM(p_home IS NOT NULL) FROM pub_fixtures WHERE run_id=?", (rid,)).fetchone()
    assert n_fx[0] == len(res.fixtures) > 0 and n_fx[1] == n_fx[0]
    run = s.db.execute("SELECT no_bet, n_fixtures FROM pub_runs WHERE id=?", (rid,)).fetchone()
    assert run[1] == len(res.fixtures) and last_publication(s) is not None
    assert s.db.execute("SELECT COUNT(*) FROM pub_slips WHERE run_id=?", (rid,)).fetchone()[0] == len(res.optimizer.slips)
    settings = json.loads(s.db.execute("SELECT optimizer FROM pub_runs WHERE id=?", (rid,)).fetchone()[0])
    assert settings["optimizer"]["max_legs"] == cfg.optimizer.max_legs and settings["z"] == cfg.thresholds.z and "kelly_fraction" in settings["risk"]
    row = s.db.execute("SELECT sel_key, home, away, score, disagreement FROM pub_opportunities WHERE run_id=? LIMIT 1", (rid,)).fetchone()
    assert row is None or (row[0] and row[1] and row[2] and row[3] is not None)
    xg_h, raw = s.db.execute("SELECT xg_home, markets FROM pub_fixtures WHERE run_id=? LIMIT 1", (rid,)).fetchone()
    mk = {(m["g"], m["l"]): m["p"] for m in json.loads(raw)}
    assert xg_h > 0 and abs(mk[("1X2", "1")] + mk[("1X2", "X")] + mk[("1X2", "2")] - 1) < 0.01
    assert mk[("Combo", "1 + Over 2.5")] <= min(mk[("1X2", "1")], mk[("Under/Over", "Over 2.5")])
    assert mk[("Multigol", "Multigol 1-3")] >= mk[("Multigol", "Multigol 2-3")]
    # bookmaker side of the headline selections: playable price, market probability, final (blended) probability
    books = [json.loads(b) for (b,) in s.db.execute("SELECT book FROM pub_fixtures WHERE run_id=? AND book IS NOT NULL", (rid,))]
    assert books and all(v["odds"] > 1 and 0 < v["pf"] < 1 for b in books for v in b.values())
    assert set().union(*books) <= {"p_home", "p_draw", "p_away", "p_over25", "p_btts"}


def test_publish_adds_new_columns_to_an_existing_database():
    from algowinbet.publish import _migrate
    s = SnapshotStore(":memory:")
    s.db.execute("CREATE TABLE pub_fixtures(run_id INTEGER, fixture_id TEXT, kickoff TEXT, competition TEXT, home TEXT, away TEXT, "
                 "p_home REAL, p_draw REAL, p_away REAL, p_over25 REAL, p_btts REAL, lineup_state TEXT, n_quotes INTEGER, "
                 "PRIMARY KEY(run_id, fixture_id))")
    _migrate(s)
    _migrate(s)  # idempotent
    cols = {r[1] for r in s.db.execute("PRAGMA table_info(pub_fixtures)").fetchall()}
    assert {"xg_home", "xg_away", "markets"} <= cols


def test_should_publish_on_fresh_data_or_stale_publication():
    from algowinbet.autorun import should_publish
    from algowinbet.collector import CollectStats
    q = CollectStats("odds")
    q.add("quotes", 5)
    assert should_publish([q], NOW, NOW) and should_publish([], None, NOW)
    assert not should_publish([CollectStats("lineups")], NOW - timedelta(hours=1), NOW)
    assert should_publish([], NOW - timedelta(hours=7), NOW)


# Rows copied from the real /markets catalogue (2026-09-30): ids, marketType, outcome names as OddsPapi returns them.
REAL_MARKETS = [
    {"marketId": 101902, "marketType": "doublechance", "marketName": "Double Chance Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 101902, "outcomeName": "1X"}, {"outcomeId": 101903, "outcomeName": "12"}, {"outcomeId": 101904, "outcomeName": "2X"}]},
    {"marketId": 1060, "marketType": "spreads", "marketName": "Asian Handicap", "period": "fulltime", "handicap": -0.5,
     "outcomes": [{"outcomeId": 1060, "outcomeName": "1"}, {"outcomeId": 1061, "outcomeName": "2"}]},
    {"marketId": 1062, "marketType": "spreads", "marketName": "Asian Handicap", "period": "fulltime", "handicap": -0.25,
     "outcomes": [{"outcomeId": 1062, "outcomeName": "1"}, {"outcomeId": 1063, "outcomeName": "2"}]},
    {"marketId": 10140, "marketType": "spreads-european", "marketName": "European Handicap", "period": "fulltime", "handicap": -1.0,
     "outcomes": [{"outcomeId": 10140, "outcomeName": "1"}, {"outcomeId": 10141, "outcomeName": "X"}, {"outcomeId": 10142, "outcomeName": "2"}]},
    {"marketId": 10284, "marketType": "toscore-team1", "marketName": "Team 1 To Score", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10284, "outcomeName": "Yes"}, {"outcomeId": 10285, "outcomeName": "No"}]},
    {"marketId": 10312, "marketType": "cleansheet-team1", "marketName": "Team 1 Clean Sheet", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10312, "outcomeName": "Yes"}, {"outcomeId": 10313, "outcomeName": "No"}]},
    {"marketId": 10336, "marketType": "correctscore", "marketName": "Correct Score Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10354, "outcomeName": "2:2"}, {"outcomeId": 10347, "outcomeName": "3:1"}]},
    {"marketId": 101936, "marketType": "winningmargin", "marketName": "Winning Margin Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 101938, "outcomeName": "Draw"}, {"outcomeId": 101936, "outcomeName": "No Goal"},
                  {"outcomeId": 101941, "outcomeName": "1 By 3"}, {"outcomeId": 101963, "outcomeName": "1 By 5+"}]},
    {"marketId": 102053, "marketType": "exactscore", "marketName": "Exact Score Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 102056, "outcomeName": "3"}, {"outcomeId": 102066, "outcomeName": "3+"}]},
    {"marketId": 10222, "marketType": "oddeven", "marketName": "Odd Even Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10222, "outcomeName": "Odd"}, {"outcomeId": 10223, "outcomeName": "Even"}]},
    {"marketId": 10316, "marketType": "wintonil-team1", "marketName": "Team 1 Win To Nil", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10316, "outcomeName": "Yes"}, {"outcomeId": 10317, "outcomeName": "No"}]},
    {"marketId": 10208, "marketType": "1x2", "marketName": "First Half Result", "period": "p1", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10208, "outcomeName": "1"}, {"outcomeId": 10209, "outcomeName": "X"}, {"outcomeId": 10210, "outcomeName": "2"}]},
    {"marketId": 10214, "marketType": "drawnobet", "marketName": "Draw No Bet", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10214, "outcomeName": "1"}, {"outcomeId": 10215, "outcomeName": "2"}]},
    {"marketId": 10322, "marketType": "drawnobet", "marketName": "Draw No Bet Second Half", "period": "p2", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10322, "outcomeName": "1"}, {"outcomeId": 10323, "outcomeName": "2"}]},
    {"marketId": 10216, "marketType": "firstgoal", "marketName": "First Goal Full Time", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10216, "outcomeName": "1"}, {"outcomeId": 10217, "outcomeName": "No Goal"}, {"outcomeId": 10218, "outcomeName": "2"}]},
    {"marketId": 10332, "marketType": "oddeven-team1", "marketName": "Team 1 Odd Even", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10332, "outcomeName": "Odd"}, {"outcomeId": 10333, "outcomeName": "Even"}]},
    {"marketId": 101919, "marketType": "halftime-fulltime", "marketName": "Half Time / Full Time", "period": None, "handicap": 0.0,
     "outcomes": [{"outcomeId": 101919, "outcomeName": "1/1"}, {"outcomeId": 101924, "outcomeName": "X/2"}]},
    {"marketId": 102041, "marketType": "highestscoringh", "marketName": "Highest Scoring Half", "period": None, "handicap": 0.0,
     "outcomes": [{"outcomeId": 102041, "outcomeName": "1st"}, {"outcomeId": 102042, "outcomeName": "X"}, {"outcomeId": 102043, "outcomeName": "2nd"}]},
    {"marketId": 10304, "marketType": "wineitherh-team1", "marketName": "Team 1 To Win Either Halves", "period": None, "handicap": 0.0,
     "outcomes": [{"outcomeId": 10304, "outcomeName": "Yes"}, {"outcomeId": 10305, "outcomeName": "No"}]},
    {"marketId": 10300, "marketType": "bothteamsscore", "marketName": "Both Teams To Score First Half", "period": "p1", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10300, "outcomeName": "Yes"}, {"outcomeId": 10301, "outcomeName": "No"}]},
    {"marketId": 10292, "marketType": "toscore-team1", "marketName": "Team 1 To Score Second Half", "period": "p2", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10292, "outcomeName": "Yes"}, {"outcomeId": 10293, "outcomeName": "No"}]},
    {"marketId": 101979, "marketType": "winningmargin", "marketName": "Winning Margin First Half", "period": "p1", "handicap": 0.0,
     "outcomes": [{"outcomeId": 101980, "outcomeName": "Draw (incl 0:0)"}, {"outcomeId": 102003, "outcomeName": "2 By 1+"}]},
    {"marketId": 10761, "marketType": "2up", "marketName": "2Up - Full Time Result", "period": "fulltime", "handicap": 0.0,
     "outcomes": [{"outcomeId": 10761, "outcomeName": "1"}]},
]


def test_mapper_covers_the_real_goal_market_catalogue():
    from algowinbet.markets import is_supported
    m = OddsPapiMapper(NAMES, REAL_MARKETS, PARTICIPANTS)
    fx = Fixture(id="goal:g1", competition="Serie A", home="Genoa", away="Fiorentina", kickoff=utc(2026, 10, 10, 13))
    mk = {str(r["marketId"]): {"outcomes": {str(o["outcomeId"]): {"players": p(2.0)} for o in r["outcomes"]}} for r in REAL_MARKETS}
    qs = {(q.market_code, q.selection, q.line) for q in m.odds({"bookmakerOdds": {"sisal.it": {"markets": mk}}}, fx, NOW)}
    assert {("DOUBLE_CHANCE", "X2", None), ("DOUBLE_CHANCE", "1X", None), ("DOUBLE_CHANCE", "12", None)} <= qs  # "2X" is X2
    assert ("ASIAN_HANDICAP", "HOME", -0.5) in qs and not any(l == -0.25 for _, _, l in qs)  # quarter lines refund: skipped
    assert {("EURO_HANDICAP", s, -1.0) for s in ("HOME", "DRAW", "AWAY")} <= qs
    assert {("TEAM_TOTAL_HOME", "OVER", 0.5), ("TEAM_TOTAL_HOME", "UNDER", 0.5)} <= qs  # to score yes/no
    assert {("TEAM_TOTAL_AWAY", "UNDER", 0.5), ("TEAM_TOTAL_AWAY", "OVER", 0.5)} <= qs  # home clean sheet yes/no
    assert {("CORRECT_SCORE", "2-2", None), ("WINNING_MARGIN", "D", None), ("WINNING_MARGIN", "NG", None),
            ("WINNING_MARGIN", "H3", None), ("WINNING_MARGIN", "H5+", None), ("TOTAL_EXACT", "3+", None),
            ("ODD_EVEN", "ODD", None), ("WIN_TO_NIL_HOME", "YES", None)} <= qs
    assert not any(c == "MATCH_1X2" for c, *_ in qs)  # first-half 1X2 is not a full-time market...
    assert {("MATCH_1X2@H1", s, None) for s in ("HOME", "DRAW", "AWAY")} <= qs  # ...it is its own half market
    assert {("DRAW_NO_BET", "HOME", None), ("DRAW_NO_BET@H2", "AWAY", None), ("FIRST_GOAL", "NONE", None), ("FIRST_GOAL", "AWAY", None),
            ("TEAM_ODD_EVEN_HOME", "ODD", None), ("HT_FT", "1/1", None), ("HT_FT", "X/2", None), ("HIGHEST_HALF", "EQUAL", None),
            ("WIN_EITHER_HALF_HOME", "YES", None), ("BTTS@H1", "NO", None), ("TEAM_TOTAL_HOME@H2", "OVER", 0.5),
            ("WINNING_MARGIN@H1", "DI", None), ("WINNING_MARGIN@H1", "A1+", None)} <= qs
    assert not any(c.startswith("2UP") or c == "2up" for c, *_ in qs)  # early payout markets are not priced
    from algowinbet.domain import SelectionRef
    assert all(is_supported(SelectionRef(market_code=c, selection=s, line=l)) for c, s, l in qs)


def test_bookings_and_corners_totals_are_never_goal_totals():
    cat = MARKETS + [
        {"marketId": 10946, "marketName": "Bookings - Over Under Full Time", "marketType": "totals-bookings", "handicap": 8.5,
         "period": "fulltime", "sportId": 10, "outcomes": [{"outcomeId": 10946, "outcomeName": "Over"}, {"outcomeId": 10947, "outcomeName": "Under"}]},
        {"marketId": 777, "marketName": "Bookings Over/Under 4.5", "sportId": 10, "outcomes": []},  # no type: name guess must refuse
        {"marketId": 778, "marketName": "Over/Under 2.5", "sportId": 10, "outcomes": []},  # no type, plain goals: still guessed
    ]
    m = OddsPapiMapper(NAMES, cat, PARTICIPANTS)
    assert m._market(10946) is None and m._market(777) is None and m._market(1200) is None
    assert m._market(778) == ("TOTAL_GOALS", 2.5) and m._market(1010) == ("TOTAL_GOALS", 2.5)


def test_remap_rebuilds_quotes_from_stored_payloads_without_requests():
    from algowinbet.oddscollector import remap_stored_odds
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    by_book = lambda q: (200, [fixture_odds(only=q["bookmaker"])])
    routes = {"/markets": (200, MARKETS), "/bookmakers": (200, BOOKS), "/participants": (200, PARTICIPANTS), "/odds-by-tournaments": by_book}
    c, t = mk_client(routes, store)
    OddsCollector(c, store, ["17"], ["sisal", "pinnacle"], NAMES, now=lambda: NOW).sync_odds()
    n = store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    store.db.execute("UPDATE quotes SET odds = 99 WHERE market_code='MATCH_1X2'")  # a wrong mapping to repair
    sent = len(t.calls)
    st = remap_stored_odds(store, NAMES)
    assert st.saved["quotes"] == n and len(t.calls) == sent
    assert store.db.execute("SELECT MAX(odds) FROM quotes WHERE market_code='MATCH_1X2'").fetchone()[0] < 99


def test_only_sisal_selections_are_playable_and_pinnacle_still_feeds_the_market_probability():
    from algowinbet.domain import OddsQuote
    from algowinbet.pricing import build_market_views
    q = lambda book, sel, odds, line=2.5: OddsQuote(fixture_id="f", market_code="TOTAL_GOALS", selection=sel, line=line, bookmaker=book,
                                                    odds=odds, observed_at=NOW, kind="current")
    quotes = [q("sisal.it", "OVER", 1.90), q("sisal.it", "UNDER", 1.85), q("pinnacle", "OVER", 2.02), q("pinnacle", "UNDER", 1.88),
              q("pinnacle", "OVER", 60.0, 8.5), q("pinnacle", "UNDER", 1.001, 8.5)]  # line Sisal does not offer
    views = {(v.ref.selection, v.ref.line): v for v in build_market_views(quotes, bettable=["sisal"])}
    assert set(views) == {("OVER", 2.5), ("UNDER", 2.5)}
    assert views[("OVER", 2.5)].best_book == "sisal.it" and views[("OVER", 2.5)].best_odds == 1.90
    assert set(views[("OVER", 2.5)].per_book) == {"sisal.it", "pinnacle"}
