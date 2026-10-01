"""GOAL client + snapshot store + collector.

Payload shapes for lineups/odds/kickoff below are ASSUMED (the official SDK does not document row schemas); tests pin the
behaviour of the plumbing (auth, budget, retries, pagination, storage, timestamps) and of the tolerant mappers, and must be
re-pinned against a real captured payload (`algowinbet goal probe`) once one exists."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from algowinbet.collector import GoalCollector
from algowinbet.config import Config
from algowinbet.domain import Fixture, FixtureStatus, LineupSnapshot, OddsQuote, Player, Position, utc
from algowinbet.engine import Engine
from algowinbet.names import TeamNames
from algowinbet.providers import MockProvider
from algowinbet.providers.goalapi import (AuthError, GoalApiClient, GoalApiError, GoalMapper, NotFound, PlanUpgradeRequired, RateLimited, ServerError,
                                          ValidationFailed, shape_summary)
from algowinbet.snapshots import BudgetExceeded, BudgetGuard, MergedProvider, SnapshotProvider, SnapshotStore
from algowinbet.state import build_state

NOW = utc(2026, 9, 30, 17, 0)


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes  # path-prefix -> list of (status, headers, body-dict|bytes); last one repeats
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        path = url.split("/v1", 1)[1].split("?")[0]
        for prefix, queue in self.routes.items():
            if path.startswith(prefix):
                status, hdr, body = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                return status, {k.lower(): v for k, v in hdr.items()}, body
        return 404, {}, json.dumps({"success": False, "code": "ROUTE_NOT_FOUND", "message": "nope"}).encode()


def ok(data, **extra):
    return 200, {}, {"success": True, "data": data, **extra}


def client(routes, store=None, budget=None, sleeps=None, now=lambda: NOW):
    t = FakeTransport(routes)
    c = GoalApiClient(api_key="SECRET-KEY-123", store=store, budget=budget, transport=t,
                      sleep=(sleeps.append if sleeps is not None else (lambda s: None)), now=now)
    return c, t


# ------------------------------------------------------------------ client
def test_bearer_auth_sorted_params_and_key_never_stored_or_printed():
    store = SnapshotStore(":memory:")
    c, t = client({"/fixtures": [ok([{"id": 1}])]}, store=store)
    c.get("/fixtures", {"to": "2026-10-01", "from": "2026-09-30", "leagueId": None})
    url, headers = t.calls[0]
    assert headers["Authorization"] == "Bearer SECRET-KEY-123"
    assert url.endswith("/fixtures?from=2026-09-30&to=2026-10-01")  # sorted, None dropped
    assert "SECRET" not in repr(c) and "SECRET" not in url
    dump = "".join(str(r) for r in store.db.execute("SELECT * FROM raw_requests").fetchall())
    assert "SECRET" not in dump and store.stats()["raw_requests"] == 1


def test_missing_key_is_refused(monkeypatch):
    monkeypatch.delenv("GOALAPI_KEY", raising=False)
    with pytest.raises(AuthError):
        GoalApiClient()


def test_budget_blocks_before_sending_and_keeps_reserve():
    store = SnapshotStore(":memory:")
    budget = BudgetGuard(store, "goal-api", daily=3, reserve=1, now=lambda: NOW)
    c, t = client({"/x": [ok([])]}, store=store, budget=budget)
    c.get("/x")
    c.get("/x")
    with pytest.raises(BudgetExceeded):
        c.get("/x")
    assert len(t.calls) == 2 and budget.used() == (2, 2)


def test_provider_headers_can_raise_our_counter():
    store = SnapshotStore(":memory:")
    budget = BudgetGuard(store, "goal-api", daily=1000, now=lambda: NOW)
    c, _ = client({"/x": [(200, {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "640", "X-RateLimit-Type": "DAILY"},
                           {"success": True, "data": []})]}, store=store, budget=budget)
    c.get("/x")
    assert budget.used()[0] == 360 and c.last_rate["remaining"] == 640


def test_429_waits_retry_after_then_succeeds_and_daily_exhaustion_is_not_retried():
    sleeps: list[float] = []
    c, t = client({"/x": [(429, {"Retry-After": "2"}, {"success": False, "code": "BURST_LIMIT_EXCEEDED"}), ok([1])]}, sleeps=sleeps)
    assert c.get("/x")["data"] == [1] and sleeps == [2.0] and len(t.calls) == 2
    sleeps2: list[float] = []
    c2, t2 = client({"/x": [(429, {"X-RateLimit-Remaining": "0", "X-RateLimit-Type": "DAILY", "Retry-After": "3600"},
                             {"success": False, "code": "RATE_LIMIT_EXCEEDED"})]}, sleeps=sleeps2)
    with pytest.raises(RateLimited) as e:
        c2.get("/x")
    assert e.value.daily_exhausted and sleeps2 == [] and len(t2.calls) == 1


def test_server_errors_back_off_then_give_up():
    sleeps: list[float] = []
    c, t = client({"/x": [(503, {}, {"success": False, "code": "SERVICE_UNAVAILABLE"}), (503, {}, {"success": False}), ok([])]}, sleeps=sleeps)
    assert c.get("/x")["success"] and sleeps == [1.0, 2.0]
    c2, t2 = client({"/y": [(500, {}, {"success": False, "code": "INTERNAL_ERROR"})]}, sleeps=[])
    with pytest.raises(ServerError):
        c2.get("/y")
    assert len(t2.calls) == 4  # first try + 3 retries


@pytest.mark.parametrize("status,code,exc", [(401, "INVALID_API_KEY", AuthError), (402, "PLAN_UPGRADE_REQUIRED", PlanUpgradeRequired),
                                              (404, "FIXTURE_NOT_FOUND", NotFound), (422, "VALIDATION_ERROR", ValidationFailed)])
def test_error_codes_map_to_typed_errors(status, code, exc):
    c, _ = client({"/x": [(status, {}, {"success": False, "code": code, "message": "m"})]})
    with pytest.raises(exc):
        c.get("/x")


def test_success_false_in_200_envelope_is_an_error():
    c, t = client({"/x": [(200, {}, {"success": False, "error": "boom", "code": "DATABASE_ERROR"})]}, sleeps=[])
    with pytest.raises(GoalApiError, match="boom"):
        c.get("/x")
    assert len(t.calls) == 1


def test_pagination_follows_has_more_with_offsets():
    pages = [ok([{"i": 1}, {"i": 2}], pagination={"total": 5, "limit": 2, "offset": 0, "hasMore": True}),
             ok([{"i": 3}, {"i": 4}], pagination={"hasMore": True}),
             ok([{"i": 5}], pagination={"hasMore": False})]
    c, t = client({"/leagues/1/fixtures": pages})
    rows = list(c.pages("/leagues/1/fixtures", {"status": "SCHEDULED"}, limit=2))
    assert [r["i"] for r in rows] == [1, 2, 3, 4, 5]
    assert [u.split("offset=")[1].split("&")[0] for u, _ in t.calls] == ["0", "2", "4"]


# ------------------------------------------------------------------ mappers
def fixture_row(**kw):
    row = {"fixtureId": 501, "status": "SCHEDULED", "homeTeam": {"name": "Internazionale"}, "awayTeam": {"name": "Empoli FC"},
           "startTime": "2026-09-30T18:45:00Z", "league": {"name": "Serie A"}}
    row.update(kw)
    return row


NAMES = TeamNames({"Inter": ["Internazionale"], "Empoli": ["Empoli FC"]})


def test_fixture_and_result_mapping_with_aliases_and_status_enum():
    m = GoalMapper(NAMES)
    f = m.fixture(fixture_row())
    assert (f.id, f.home, f.away, f.competition, f.status) == ("goal:501", "Inter", "Empoli", "Serie A", FixtureStatus.SCHEDULED)
    assert f.kickoff == utc(2026, 9, 30, 18, 45)
    r = m.result(fixture_row(status="AFTER_PEN", homeScore=2, awayScore=2))
    assert r and (r.home_goals, r.away_goals) == (2, 2)
    assert m.result(fixture_row(status="SCHEDULED", homeScore=None, awayScore=None)) is None
    assert m.fixture(fixture_row(status="POSTPONED")).status == FixtureStatus.POSTPONED


def test_mapper_reports_gaps_instead_of_guessing():
    m = GoalMapper(NAMES)
    row = fixture_row()
    del row["startTime"]
    assert m.fixture(row) is None
    assert m.fixture(fixture_row(status="WEIRD")).status == FixtureStatus.UNKNOWN
    assert any("kickoff=False" in k for k in m.report.gaps) and any("status sconosciuto" in k for k in m.report.gaps)


def _fx():
    return Fixture(id="goal:501", competition="Serie A", home="Inter", away="Empoli", kickoff=utc(2026, 9, 30, 18, 45), provider="goal-api",
                   provider_event_id="501")


def _side(prefix, n=11, confirmed=True):
    return {"formation": "3-5-2", "isConfirmed": confirmed,
            "startXI": [{"player": {"id": f"{prefix}{k}", "name": f"{prefix} Player{k}"}} for k in range(n)],
            "substitutes": [{"player": {"id": f"{prefix}s{k}", "name": f"{prefix} Sub{k}"}} for k in range(3)]}


def test_lineup_mapping_and_incomplete_xi_is_a_gap():
    m = GoalMapper(NAMES)
    lus = m.lineups({"home": _side("h"), "away": _side("a", n=10)}, _fx(), NOW)
    assert len(lus) == 1 and lus[0].team == "Inter" and lus[0].status == "confirmed" and len(lus[0].starters) == 11
    assert lus[0].observed_at == NOW and lus[0].formation == "3-5-2"
    assert any("10 titolari" in k for k in m.report.gaps)
    assert m.lineups({"home": _side("h", confirmed=False)}, _fx(), NOW)[0].status == "probable"


def test_odds_mapping_markets_lines_and_unknowns():
    payload = [{"bookmakers": [{"name": "Sisal", "markets": [
        {"name": "Match Winner", "outcomes": [{"name": "Home", "price": 1.8}, {"name": "Draw", "price": 3.6}, {"name": "Away", "price": 4.5}]},
        {"name": "Goals Over/Under", "outcomes": [{"name": "Over", "point": 2.5, "price": 1.9}, {"name": "Under 2.5", "price": 1.9}]},
        {"name": "Corners Over Under", "outcomes": [{"name": "Over 9.5", "price": 1.8}]},
        {"name": "Both Teams Score", "outcomes": [{"name": "Yes", "price": 1.7}, {"name": "No", "price": 2.1}, {"name": "Maybe", "price": 9}]},
    ]}]}]
    m = GoalMapper(NAMES)
    qs = m.odds(payload, _fx(), NOW)
    got = {(q.market_code, q.selection, q.line): q.odds for q in qs}
    assert got[("MATCH_1X2", "HOME", None)] == 1.8 and got[("TOTAL_GOALS", "OVER", 2.5)] == 1.9 and got[("TOTAL_GOALS", "UNDER", 2.5)] == 1.9
    assert got[("BTTS", "NO", None)] == 2.1 and all(q.bookmaker == "Sisal" and q.observed_at == NOW for q in qs)
    assert m.report.unmapped_markets == {"Corners Over Under": 1} and any("Maybe" in k for k in m.report.gaps)


def test_shape_summary_lists_paths():
    lines = shape_summary({"a": {"b": [{"c": 1}]}, "d": "x"})
    assert "a.b: list[1]" in lines and any(l.startswith("a.b[0].c: int") for l in lines) and any(l.startswith("d: str") for l in lines)


# ------------------------------------------------------------------- store
def test_raw_payloads_are_deduplicated_and_recoverable():
    s = SnapshotStore(":memory:")
    a = s.put_raw("goal-api", "/x", {"q": 1}, 200, b'{"success":true}', NOW)
    b = s.put_raw("goal-api", "/x", {"q": 1}, 200, b'{"success":true}', NOW + timedelta(minutes=5))
    assert a != b and s.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1 and s.raw_body(b) == b'{"success":true}'


def test_quotes_lineups_fixtures_are_idempotent_and_fixture_history_keeps_changes_only():
    s = SnapshotStore(":memory:")
    q = OddsQuote(fixture_id="f", market_code="TOTAL_GOALS", selection="OVER", line=2.5, bookmaker="Sisal", odds=1.9, observed_at=NOW)
    assert s.save_quotes("x", [q, q]) == 1 and s.save_quotes("x", [q]) == 0
    assert s.save_quotes("x", [q.model_copy(update={"observed_at": NOW + timedelta(hours=1)})]) == 1  # a new observation of the same price
    f = _fx()
    assert s.save_fixtures("x", [f], NOW) == 1 and s.save_fixtures("x", [f], NOW + timedelta(hours=1)) == 0
    moved = f.model_copy(update={"kickoff": f.kickoff + timedelta(days=1), "status": FixtureStatus.POSTPONED})
    assert s.save_fixtures("x", [moved], NOW + timedelta(hours=2)) == 1
    assert SnapshotProvider(s).list_fixtures(None, utc(2026, 1, 1), utc(2027, 1, 1))[0].status == FixtureStatus.POSTPONED


def test_snapshot_provider_replays_exactly_what_was_known_at_a_cutoff():
    s = SnapshotStore(":memory:")
    f = _fx()
    s.save_fixtures("x", [f], NOW - timedelta(days=2))
    mk = lambda odds, at: OddsQuote(fixture_id=f.id, market_code="MATCH_1X2", selection="HOME", bookmaker="Sisal", odds=odds, observed_at=at)
    s.save_quotes("x", [mk(2.0, NOW - timedelta(hours=30)), mk(1.8, NOW - timedelta(minutes=10))])
    lu = LineupSnapshot(fixture_id=f.id, team="Inter", status="confirmed", starters=[f"p{i}" for i in range(11)], published_at=NOW - timedelta(minutes=20),
                        observed_at=NOW - timedelta(minutes=15))
    s.save_lineups("x", [lu])
    prov = SnapshotProvider(s)
    early = build_state(prov, f, NOW - timedelta(hours=1))
    late = build_state(prov, f, NOW)
    assert [q.odds for q in early.quotes] == [2.0] and early.lineups == []
    assert [q.odds for q in late.quotes] == [2.0, 1.8] and len(late.lineups) == 1


def test_history_uses_result_availability_and_merge_dedupes():
    s = SnapshotStore(":memory:")
    from algowinbet.domain import MatchResult
    r = MatchResult(fixture_id="goal:1", competition="Serie A", home="Inter", away="Empoli", kickoff=NOW, home_goals=1, away_goals=0)
    s.save_results("x", [r], NOW + timedelta(hours=4))
    p = SnapshotProvider(s)
    assert p.list_history(None, NOW + timedelta(hours=2)) == [] and len(p.list_history(None, NOW + timedelta(hours=4))) == 1

    class Extra:
        name = "csv"

        def list_history(self, comps, until):
            return [r.model_copy(update={"fixture_id": "fd-1"}),
                    r.model_copy(update={"fixture_id": "fd-2", "home": "Roma", "away": "Lazio"})]

        def list_competitions(self):
            return ["Serie A"]
    merged = MergedProvider(p, Extra())
    assert len(merged.list_history(None, NOW + timedelta(days=1))) == 2


# --------------------------------------------------------------- collector
def _collector(routes, store, now=NOW, budget=None):
    c, t = client(routes, store=store, budget=budget, now=lambda: now)
    return GoalCollector(c, store, ["1"], NAMES, now=lambda: now), t


def _seed_fixtures(store, kickoffs):
    fx = [Fixture(id=f"goal:{i}", competition="Serie A", home=f"H{i}", away=f"A{i}", kickoff=k, provider="goal-api", provider_event_id=str(i))
          for i, k in enumerate(kickoffs)]
    store.save_fixtures("goal-api", fx, NOW - timedelta(days=1))
    return fx


def test_lineup_polling_only_in_window_skips_confirmed_and_stamps_fetch_time():
    store = SnapshotStore(":memory:")
    fx = _seed_fixtures(store, [NOW + timedelta(minutes=60), NOW + timedelta(minutes=80), NOW + timedelta(hours=5)])
    store.save_lineups("goal-api", [LineupSnapshot(fixture_id=fx[1].id, team=t, status="confirmed", starters=["a"] * 11, published_at=NOW,
                                                   observed_at=NOW - timedelta(minutes=5)) for t in ("H1", "A1")])
    body = {"home": _side("h"), "away": _side("a")}
    col, t = _collector({"/fixtures/0/lineups": [ok(body)]}, store)
    st = col.sync_lineups(window_minutes=95)
    assert st.requests == 1 and st.saved == {"lineups": 2}  # fixture 0 fetched; 1 already confirmed; 2 outside the window
    assert any("già confermate" in s for s in st.skipped)
    got = SnapshotProvider(store).get_lineups("goal:0")
    assert {l.observed_at for l in got} == {NOW} and {l.team for l in got} == {"H0", "A0"}
    st2 = col.sync_lineups(window_minutes=95)  # second run: nothing left to fetch
    assert st2.requests == 0


def test_lineup_names_are_reconciled_to_roster_ids():
    store = SnapshotStore(":memory:")
    _seed_fixtures(store, [NOW + timedelta(minutes=60)])
    store.save_players("x", [Player(id="R-7", name="Marco Rossi", team="H0", position=Position.FWD)], NOW)
    side = {"startXI": ["Marco Rossi"] + [f"Other {k}" for k in range(10)], "isConfirmed": True}
    col, _ = _collector({"/fixtures/0/lineups": [ok({"home": side, "away": _side("a")})]}, store)
    col.sync_lineups()
    home = next(l for l in SnapshotProvider(store).get_lineups("goal:0") if l.team == "H0")
    assert "R-7" in home.starters and "H0::Other 0" in home.starters


def test_collector_stops_cleanly_when_budget_runs_out():
    store = SnapshotStore(":memory:")
    _seed_fixtures(store, [NOW + timedelta(minutes=30 + k) for k in range(3)])
    budget = BudgetGuard(store, "goal-api", daily=2, now=lambda: NOW)
    body = ok({"home": _side("h"), "away": _side("a")})
    col, t = _collector({"/fixtures/": [body]}, store, budget=budget)
    st = col.sync_lineups()
    assert st.stopped_by_budget and len(t.calls) == 2 and st.saved == {"lineups": 4}


def test_fixture_and_results_sync_store_rows_and_report_gaps():
    store = SnapshotStore(":memory:")
    rows = [fixture_row(fixtureId=1), fixture_row(fixtureId=2, startTime=None)]
    done = [fixture_row(fixtureId=3, status="FINISHED", homeScore=3, awayScore=1, startTime="2026-09-28T18:45:00Z")]
    col, t = _collector({"/leagues/1/fixtures": [ok(rows, pagination={"hasMore": False})],
                         "/leagues/1/results": [ok(done, pagination={"hasMore": False})]}, store)
    st = col.sync_fixtures(7)
    assert st.saved == {"fixtures": 1} and any("kickoff=False" in k for k in st.report.gaps)
    assert "status=SCHEDULED" in t.calls[0][0] and "from=2026-09-30" in t.calls[0][0]
    st = col.sync_results(3)
    assert st.saved == {"results": 1}
    assert SnapshotProvider(store).list_history(None, NOW)[0].home_goals == 3


# Real pair from /leagues/{LaLiga}/results (2026-09-30): one match, two GOAL ids from two upstream feeds (apiId 610628 / 687733).
def _betis_alaves(fid, api_id, away_name):
    return {"id": fid, "apiId": api_id, "leagueName": "LaLiga", "kickoffUtc": "2025-08-22T19:30:00.000Z", "matchStatus": "FINISHED",
            "homeTeamName": "Betis", "homeTeamScore": "1", "awayTeamName": away_name, "awayTeamScore": "0",
            "homeTeam": {"id": "cmr7m55uo5eg8rx06hlsjolr6", "name": "Real Betis"}, "awayTeam": {"id": "cmr7m55ue5eg4rx06vdyle9qd", "name": "Alavés"}}


def test_match_listed_twice_by_goal_is_stored_and_read_once():
    store = SnapshotStore(":memory:")
    rows = [_betis_alaves("cmrjhshs6t0n7o8070iny6gwe", "687733", "Alaves"), _betis_alaves("cmrjhshrkt0mxo8074kvog6tn", "610628", "Alavés")]
    col, _ = _collector({"/leagues/1/results": [ok(rows, pagination={"hasMore": False})]}, store, now=utc(2025, 8, 23, 9, 0))
    assert col.sync_results(3).saved == {"results": 1}
    assert store.db.execute("SELECT fixture_id FROM results").fetchall() == [("goal:cmrjhshrkt0mxo8074kvog6tn",)]  # smallest id kept
    assert store.db.execute("SELECT COUNT(DISTINCT fixture_id) FROM fixtures").fetchone()[0] == 2  # both ids stay in the calendar
    # a later page with the other id, or the same id again, adds nothing; the same clubs days later are another match
    from algowinbet.domain import MatchResult
    r = MatchResult(fixture_id="goal:zzz", competition="LaLiga", home="Real Betis", away="Alavés", kickoff=utc(2025, 8, 22, 21, 0),
                    home_goals=1, away_goals=0)
    assert store.save_results("x", [r], NOW) == 0
    assert store.save_results("x", [r.model_copy(update={"kickoff": utc(2025, 8, 26, 19, 30)})], NOW) == 1


def test_history_reads_count_a_match_stored_under_two_ids_once():
    """Pairs stored before save_results deduplicated (37 on Turso): the model must not fit them twice."""
    from algowinbet.domain import MatchResult
    s = SnapshotStore(":memory:")
    ko = utc(2025, 8, 24, 13, 30)
    rows = [("goal:b", "Mainz 05", "Köln", ko, 0, 1), ("goal:a", "Mainz 05", "Koln", ko + timedelta(hours=1), 0, 1),
            ("goal:c", "Mainz 05", "Köln", ko + timedelta(days=200), 2, 2), ("goal:d", "Köln", "Mainz 05", ko, 1, 1)]
    for fid, h, a, k, hg, ag in rows:  # straight into the table, as the old save_results did
        s.db.execute("INSERT INTO results(fixture_id,source,competition,home,away,kickoff,home_goals,away_goals,observed_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (fid, "goal-api", "Bundesliga", h, a, k.isoformat(), hg, ag, NOW.isoformat()))
    hist = SnapshotProvider(s).list_history(None, NOW)
    assert [r.fixture_id for r in hist] == ["goal:b", "goal:d", "goal:c"]  # earliest kickoff of the pair kept; reverse fixture is another match
    merged = MergedProvider(SnapshotProvider(s), type("E", (), {"name": "e", "list_history": lambda *a: [], "list_competitions": lambda *a: []})())
    assert len(merged.list_history(None, NOW)) == 3


def test_plan_upgrade_required_is_reported_not_crashing():
    store = SnapshotStore(":memory:")
    _seed_fixtures(store, [NOW + timedelta(hours=10)])
    col, _ = _collector({"/fixtures/0/odds": [(402, {}, {"success": False, "code": "PLAN_UPGRADE_REQUIRED", "message": "upgrade"})]}, store)
    st = col.sync_odds(hours_ahead=48)
    assert st.errors and "piano" in st.errors[0]


# ------------------------------------------------------------- end-to-end
def test_engine_runs_on_collected_snapshots_only():
    mock = MockProvider(seed=7, open_noise=0.0, close_noise=0.0)
    s = SnapshotStore(":memory:")
    comp = mock.list_competitions()[0]
    s.save_results("mock", mock.list_history(None, mock.as_of), mock.as_of)  # pooled model: every competition
    fxs = mock.list_fixtures([comp], mock.as_of, mock.as_of + timedelta(days=3))
    s.save_fixtures("mock", fxs, mock.as_of - timedelta(days=1))
    for f in fxs:
        s.save_quotes("mock", mock.get_quotes(f.id))
    live = Engine(SnapshotProvider(s), Config(), use_lineups=False).analyze(None, mock.as_of, mock.as_of + timedelta(days=3), mock.as_of)
    ref = Engine(mock, Config(), use_lineups=False).analyze([comp], mock.as_of, mock.as_of + timedelta(days=3), mock.as_of)
    assert len(live.opportunities) == len(ref.opportunities) > 0
    a = {(o.fixture_id, o.ref.key): o.p_struct for o in live.opportunities}
    b = {(o.fixture_id, o.ref.key): o.p_struct for o in ref.opportunities}
    assert a.keys() == b.keys() and all(abs(a[k] - b[k]) < 1e-9 for k in a)


# ------------------------------------------------------------------- names
def test_team_names_canonicalise_and_flag_unknowns():
    n = TeamNames.load("configs/team_aliases.json")
    assert n.canon("FC Internazionale Milano") == n.canon("Inter") == "Inter"
    assert n.canon("Atlético Madrid") == n.canon("Atletico Madrid")  # accents never matter
    assert n.unmatched({"Inter", "Sconosciuta United"}, {"Inter", "Milan"}) == {"Sconosciuta United"}


# Real GOAL row captured with `goal probe` (2026-09-29): flat fields + nested homeTeam/awayTeam/league; kickoffUtc is the UTC datetime.
REAL_FIXTURE_ROW = {"id": "cmu75rli66xhikm08d7xr86al", "apiId": "808223", "leagueName": "Serie A", "leagueYear": "2026/2027",
                    "matchDate": "2027-05-30", "matchTime": "16:30", "kickoffUtc": "2027-05-30T16:30:00.000Z", "matchStatus": "SCHEDULED",
                    "homeTeamName": "AC Milan", "homeTeamScore": None, "awayTeamName": "Udinese", "awayTeamScore": None,
                    "league": {"id": "cmr77dvpd006yrx06zig7907g", "name": "Serie A"},
                    "homeTeam": {"id": "cmr7fp1wj2n8trx061joikfn8", "name": "Milan"}, "awayTeam": {"id": "cmr7m6br95t2qrx068uc0kscj", "name": "Udinese"}}


def test_real_goal_fixture_row_maps_with_utc_kickoff():
    m = GoalMapper(TeamNames.load("configs/team_aliases.json"))
    f = m.fixture(REAL_FIXTURE_ROW)
    assert (f.id, f.home, f.away, f.competition) == ("goal:cmu75rli66xhikm08d7xr86al", "Milan", "Udinese", "Serie A")
    assert f.kickoff == utc(2027, 5, 30, 16, 30) and f.status == FixtureStatus.SCHEDULED and not m.report.gaps
    done = {**REAL_FIXTURE_ROW, "matchStatus": "FINISHED", "homeTeamScore": "2", "awayTeamScore": "1"}
    r = m.result(done)
    assert (r.home_goals, r.away_goals) == (2, 1)


# Real /fixtures/{id}/lineups response 11 days before kickoff (captured 2026-09-29): lists empty, hasLineups false.
REAL_LINEUPS_EMPTY = {"home": {"startingLineups": [], "substitutes": [], "missingPlayers": [],
                               "coach": [{"playerId": "c1", "lineupPlayer": "Cristian Chivu", "type": "coach", "team": "home"}]},
                      "away": {"startingLineups": [], "substitutes": [], "missingPlayers": [], "coach": []},
                      "homeFormation": None, "awayFormation": None, "hasLineups": False}


def test_real_empty_lineups_is_not_a_gap_and_stores_nothing():
    m = GoalMapper(NAMES)
    assert m.lineups(REAL_LINEUPS_EMPTY, _fx(), NOW) == [] and not m.report.gaps and m.report.ok["lineup: non ancora pubblicate (hasLineups=false)"] == 2


def test_lineups_with_goal_field_names_ASSUMED_row_shape():
    # Row keys (playerId, lineupPlayer, lineupPosition) are copied from the real coach rows; starters rows not yet seen on a match day.
    row = lambda side, k: {"playerId": f"{side}{k}", "lineupPlayer": f"{side} P{k}", "lineupPosition": "M", "team": side, "type": "starter"}
    data = {"home": {"startingLineups": [row("h", k) for k in range(11)], "substitutes": [row("h", 20 + k) for k in range(5)], "missingPlayers": []},
            "away": {"startingLineups": [row("a", k) for k in range(11)], "substitutes": [], "missingPlayers": []},
            "homeFormation": "3-5-2", "awayFormation": "4-3-3", "hasLineups": True}
    lus = GoalMapper(NAMES).lineups(data, _fx(), NOW)
    assert [(l.team, l.status, l.formation, len(l.starters), len(l.bench)) for l in lus] == [("Inter", "confirmed", "3-5-2", 11, 5), ("Empoli", "confirmed", "4-3-3", 11, 0)]
    assert lus[0].starters[0] == "goal:h0"


# Real /fixtures/{id}/statistics payload (finished match, captured 2026-09-29).
def _real_stats():
    import pathlib
    return json.loads((pathlib.Path(__file__).parent / "data" / "goal_statistics.json").read_text())


def test_real_statistics_payload_maps_and_resolves_duplicates():
    m = GoalMapper(NAMES)
    st = {(s.period, s.stat): (s.home, s.away) for s in m.stats(_real_stats(), _fx(), NOW)}
    assert st[("FT", "ball_possession")] == (69.0, 31.0)  # the 0%/0% duplicate is dropped
    assert st[("FT", "corners")] == (2.0, 3.0) and st[("FT", "yellow_cards")] == (0.0, 1.0) and st[("FT", "shots_total")] == (10.0, 6.0)
    assert ("FT", "red_cards") not in st and not any(p == "2H" for p, _ in st) and not m.report.gaps


def test_stats_collection_only_for_finished_matches_without_stats():
    store = SnapshotStore(":memory:")
    from algowinbet.domain import MatchResult
    r = MatchResult(fixture_id="goal:77", competition="Serie A", home="Inter", away="Empoli", kickoff=NOW - timedelta(hours=20),
                    home_goals=1, away_goals=0)
    store.save_results("goal-api", [r], NOW - timedelta(hours=17))
    col, t = _collector({"/fixtures/77/statistics": [ok({**_real_stats(), "hasStatistics": True})]}, store)
    st = col.sync_stats(days_back=2)
    assert st.requests == 1 and st.saved["stats"] > 20 and store.stats_of("goal:77")["corners"] == (2.0, 3.0)
    assert col.sync_stats(days_back=2).requests == 0  # already stored


def test_live_windows_quotes_24h_and_history_two_previous_seasons():
    from algowinbet.state import history_at, season_start
    from algowinbet.domain import MatchResult
    s = SnapshotStore(":memory:")
    f = _fx()
    mk = lambda odds, at: OddsQuote(fixture_id=f.id, market_code="MATCH_1X2", selection="HOME", bookmaker="Sisal", odds=odds, observed_at=at)
    s.save_quotes("x", [mk(2.0, NOW - timedelta(hours=30)), mk(1.8, NOW - timedelta(hours=2))])
    assert [q.odds for q in build_state(SnapshotProvider(s), f, NOW, quote_window_hours=24).quotes] == [1.8]
    assert season_start(NOW) == utc(2026, 7, 1) and season_start(utc(2026, 3, 1)) == utc(2025, 7, 1)
    olds = [MatchResult(fixture_id=f"goal:{y}", competition="Serie A", home="Inter", away="Empoli", kickoff=utc(y, 1, 10),
                        home_goals=1, away_goals=0) for y in (2024, 2025, 2026)]
    s.save_results("x", olds, NOW)
    # current season 2026/27 + two previous (from 2024-07-01): the January 2024 match (2023/24) is excluded
    assert [r.kickoff.year for r in history_at(SnapshotProvider(s), "Serie A", NOW, seasons=2)] == [2025, 2026]
