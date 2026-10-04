"""Matches Sisal does not price on the feed: linked through /fixtures, priced with an estimated Sisal price from Pinnacle."""
from datetime import timedelta

from algowinbet.domain import OddsQuote, utc
from algowinbet.oddscollector import OddsCollector
from algowinbet.pricing import ESTIMATED_BOOK, build_market_views, payout_ratios
from algowinbet.snapshots import SnapshotStore

from test_oddspapi_auto import MARKETS, NAMES, NOW, PARTICIPANTS, mk_client, seed_calendar

T = utc(2026, 10, 10, 10)


def q(book, code, sel, odds, line=None):
    return OddsQuote(fixture_id="goal:g1", market_code=code, selection=sel, line=line, bookmaker=book, odds=odds, observed_at=T)


PIN_1X2 = [q("pinnacle", "MATCH_1X2", "HOME", 2.0), q("pinnacle", "MATCH_1X2", "DRAW", 3.6), q("pinnacle", "MATCH_1X2", "AWAY", 4.2)]


def test_no_sisal_at_all_gives_estimated_prices_from_pinnacle_fair():
    views = build_market_views(PIN_1X2, bettable=["sisal"], estimate={"*": 0.95})
    assert {v.best_book for v in views} == {ESTIMATED_BOOK} and len(views) == 3
    for v in views:
        assert v.p_market is not None and abs(v.best_odds - round(0.95 / v.p_market, 2)) < 1e-9
    assert build_market_views(PIN_1X2, bettable=["sisal"]) == []  # without estimate: not playable, as before


def test_sisal_on_the_match_means_no_estimate_for_what_it_does_not_offer():
    quotes = PIN_1X2 + [q("pinnacle", "BTTS", "YES", 1.8), q("pinnacle", "BTTS", "NO", 2.0), q("sisal.it", "MATCH_1X2", "HOME", 1.9)]
    views = build_market_views(quotes, bettable=["sisal"], estimate={"*": 0.95})
    assert [(v.ref.market_code, v.ref.selection, v.best_book) for v in views] == [("MATCH_1X2", "HOME", "sisal.it")]


def test_market_payout_needs_enough_pairs_and_never_exceeds_fair():
    assert payout_ratios([("MATCH_1X2", 0.95)] * 19) == {}
    r = payout_ratios([("MATCH_1X2", 0.95)] * 20 + [("BTTS", 0.9)] * 5 + [("BTTS", 3.0)])  # 3.0: feed error, dropped
    assert r["MATCH_1X2"] == 0.95 and "BTTS" not in r and r["*"] == 0.95
    assert payout_ratios([("MATCH_1X2", 1.05)] * 25)["*"] == 1.0


def test_unlinked_match_is_linked_with_one_fixtures_request_and_tried_once():
    store = SnapshotStore(":memory:")
    seed_calendar(store)
    rows = [{"fixtureId": "op-1", "participant1Id": 1, "participant2Id": 2, "startTime": "2026-10-10T13:00:00Z", "hasOdds": True}]
    routes = {"/markets": (200, MARKETS), "/participants": (200, PARTICIPANTS), "/fixtures": (200, rows)}
    c, t = mk_client(routes, store)
    col = OddsCollector(c, store, ["17"], ["sisal"], NAMES, now=lambda: NOW)
    st = col.link_unquoted({"Serie A": "17"})
    assert st.saved == {"links": 1} and sum("/fixtures?" in u for u in t.calls) == 1
    assert store.db.execute("SELECT ext_id, fixture_id FROM fixture_links").fetchall() == [("op-1", "goal:g1")]
    # g2 (Inter - Parma) is not on OddsPapi: tried once, no request on the next ticks
    again = col.link_unquoted({"Serie A": "17"})
    assert again.requests == 0 and sum("/fixtures?" in u for u in t.calls) == 1
    # a competition without a tournament id, or a match beyond the window, costs nothing
    assert OddsCollector(c, store, ["17"], ["sisal"], NAMES, now=lambda: NOW - timedelta(days=5)).link_unquoted({"Serie A": "17"}).requests == 0


class _SisalPinnacle:
    """MockProvider with BookA as Sisal and BookB as Pinnacle; one match without any Sisal price."""

    def __init__(self, base, unquoted: str):
        self.base, self.unquoted = base, unquoted

    def __getattr__(self, name):
        return getattr(self.base, name)

    def get_quotes(self, fixture_id):
        names = {"BookA": "sisal.it", "BookB": "pinnacle"}
        out = [x.model_copy(update={"bookmaker": names.get(x.bookmaker, x.bookmaker)}) for x in self.base.get_quotes(fixture_id)]
        return [x for x in out if not (fixture_id == self.unquoted and x.bookmaker == "sisal.it")]


def test_engine_estimates_only_the_unquoted_match_and_keeps_it_out_of_slips_and_registry():
    from algowinbet.config import Config
    from algowinbet.engine import Engine
    from algowinbet.providers import MockProvider

    base = MockProvider(seed=11, open_noise=0.0, close_noise=0.0)
    t = base.as_of
    first = base.list_fixtures(None, t, t + timedelta(days=8))[0]
    prov = _SisalPinnacle(base, first.id)
    cfg = Config()
    cfg.bet_bookmakers = ["sisal"]
    res = Engine(prov, cfg).analyze(None, t, t + timedelta(days=8), t)
    assert res.estimated and {o.fixture_id for o in res.estimated} == {first.id}
    assert {o.bookmaker for o in res.estimated} == {ESTIMATED_BOOK}
    assert first.id not in {o.fixture_id for o in res.opportunities}
    assert all(l.fixture_id != first.id for s in res.optimizer.slips for l in s.legs)
    assert any("stimate" in n for n in res.notes)
    cfg.estimate_unquoted = False
    assert not Engine(prov, cfg).analyze(None, t, t + timedelta(days=8), t).estimated
