from datetime import datetime, timedelta, timezone

import numpy as np

from algowinbet.domain import MatchResult, SelectionRef
from algowinbet.markets import stat_outcome, stat_probability
from algowinbet.models.counts import CountModel

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEAMS = ["A", "B", "C", "D", "E", "F"]
RATE = {"A": 7.0, "B": 5.5, "C": 5.0, "D": 4.5, "E": 4.0, "F": 3.5}  # corners won per match, before home advantage


def _season(rng, size: float | None, n_rounds: int = 12):
    rows, k = [], 0
    for rnd in range(n_rounds):
        for h in TEAMS:
            for a in TEAMS:
                if h == a or rng.random() > 0.5:
                    continue
                lh, la = RATE[h] * 1.1, RATE[a] * 0.9
                if size is None:
                    ch, ca = rng.poisson(lh), rng.poisson(la)
                else:  # gamma-mixed Poisson = negative binomial with this size
                    ch, ca = rng.poisson(rng.gamma(size, lh / size)), rng.poisson(rng.gamma(size, la / size))
                rows.append(MatchResult(fixture_id=f"m{k}", competition="L", home=h, away=a, kickoff=T0 + timedelta(days=3 * rnd),
                                        home_goals=int(ch), away_goals=int(ca)))
                k += 1
    return rows


def test_count_model_recovers_team_strengths_and_overdispersion():
    rng = np.random.default_rng(3)
    rows = _season(rng, size=6.0)
    cm = CountModel("corners", l2=0.1).fit(rows, T0 + timedelta(days=40))
    lh, la = cm.expected("A", "F", "L")
    assert lh > 6 and la < 4.5  # the strong corner side at home against the weak one
    assert 2 < cm.size < 30  # over-dispersion found (true size 6)
    m = cm.matrix("A", "F", "L")
    assert m.shape == (31, 31) and abs(m.sum() - 1) < 1e-9
    over = stat_probability(m, SelectionRef(market_code="CORNERS_TOTAL", selection="OVER", line=9.5))
    under = stat_probability(m, SelectionRef(market_code="CORNERS_TOTAL", selection="UNDER", line=9.5))
    assert abs(over + under - 1) < 1e-9 and over > 0.5
    home = stat_probability(m, SelectionRef(market_code="CORNERS_1X2", selection="HOME"))
    assert home > 0.6
    # an unknown cup competition takes the overall level, not a missing one
    assert cm.expected("A", "F", "Coppa") == cm.expected("A", "F", None)


def test_poisson_data_gives_no_dispersion_and_cards_grid():
    rng = np.random.default_rng(5)
    rows = [r.model_copy(update={"home_goals": min(r.home_goals // 2, 14), "away_goals": min(r.away_goals // 2, 14)})
            for r in _season(rng, size=None)]
    cm = CountModel("cards").fit(rows, T0 + timedelta(days=40))
    assert cm.matrix("B", "C", "L").shape == (16, 16)
    assert cm.size > 20 or cm.size == float("inf")  # halved Poisson counts are under-dispersed: (near) Poisson


def test_statistic_markets_settle_from_the_counts():
    ref = SelectionRef(market_code="CARDS_TOTAL", selection="OVER", line=4.5)
    assert stat_outcome(3, 2, ref) and not stat_outcome(2, 2, ref)
    assert stat_outcome(5, 5, SelectionRef(market_code="CORNERS_1X2", selection="DRAW"))
    assert stat_outcome(2, 4, SelectionRef(market_code="CORNERS_TEAM_AWAY", selection="OVER", line=3.5))


def test_shared_factor_finds_correlated_counts():
    rng = np.random.default_rng(1)
    rows = []
    for r in _season(rng, size=None, n_rounds=20):  # one gamma multiplier per match on both sides (referee): k_s = 4
        g = rng.gamma(4.0, 1 / 4.0)
        rows.append(r.model_copy(update={"home_goals": int(rng.poisson(2.2 * g)), "away_goals": int(rng.poisson(2.0 * g))}))
    cm = CountModel("cards", shared=True).fit(rows, T0 + timedelta(days=70))
    assert 2.5 < cm.shared_size < 6.5
    m = cm.matrix("A", "B", "L")
    assert abs(m.sum() - 1) < 1e-9
    h = np.arange(16)
    cov = (m * np.outer(h, h)).sum() - (m.sum(1) @ h) * (m.sum(0) @ h)
    assert cov > 0.5  # the two sides move together
    plain = CountModel("cards").fit(rows, T0 + timedelta(days=70))
    assert plain.shared_size == float("inf")


def test_cards_1x2_is_priced_and_card_totals_are_not():
    from algowinbet.config import Config
    from algowinbet.domain import Fixture, OddsQuote
    from algowinbet.models.dixon_coles import DixonColes
    from algowinbet.opportunity import analyze_fixture
    from algowinbet.state import MatchState
    rng = np.random.default_rng(3)
    rows = _season(rng, size=6.0)
    goals = DixonColes().fit([r.model_copy(update={"home_goals": r.home_goals % 4, "away_goals": r.away_goals % 3}) for r in rows],
                             T0 + timedelta(days=40))
    cards = CountModel("cards", shared=True).fit([r.model_copy(update={"home_goals": r.home_goals // 2, "away_goals": r.away_goals // 2})
                                                  for r in rows], T0 + timedelta(days=40))
    ko = T0 + timedelta(days=41)
    fx = Fixture(id="x", competition="L", home="A", away="F", kickoff=ko)
    at = ko - timedelta(hours=5)
    q = [OddsQuote(fixture_id="x", market_code="CARDS_1X2", selection=s, bookmaker=b, odds=o, observed_at=at)
         for b in ("sisal", "pinnacle") for s, o in (("HOME", 2.4), ("DRAW", 4.2), ("AWAY", 2.6))]
    q += [OddsQuote(fixture_id="x", market_code="CARDS_TOTAL", selection=s, line=4.5, bookmaker=b, odds=1.9, observed_at=at)
          for b in ("sisal", "pinnacle") for s in ("OVER", "UNDER")]
    res = analyze_fixture(MatchState(fixture=fx, cutoff=at, quotes=q), goals, [], Config(),
                          stats={"cards": (cards.matrix("A", "F", "L"), 30)})
    codes = {o.ref.market_code for o in res.opportunities}
    assert "CARDS_1X2" in codes and "CARDS_TOTAL" not in codes


def test_referee_key_and_factor():
    from algowinbet.snapshots import referee_key
    assert referee_key("M Oliver") == referee_key("Michael Oliver, England") == "m oliver"
    assert referee_key("") is None
    rng = np.random.default_rng(7)
    rows, refs = [], {}
    for k, r in enumerate(_season(rng, size=None, n_rounds=24)):
        strict = k % 4 == 0  # one referee in four shows 60% more cards
        g = 1.6 if strict else 1.0
        rows.append(r.model_copy(update={"home_goals": int(rng.poisson(2.0 * g)), "away_goals": int(rng.poisson(1.8 * g))}))
        refs[r.fixture_id] = "a strict" if strict else f"r {k % 4}"
    cm = CountModel("cards", l2=5.0).fit(rows, T0 + timedelta(days=80), referees=refs)
    assert cm.ref_adj["a strict"] > 1.3 > cm.ref_adj["r 1"]
    hs, as_ = cm.expected("A", "B", "L", referee="a strict")
    hn, an = cm.expected("A", "B", "L", referee="nobody")
    assert hs / hn == cm.ref_adj["a strict"] and hn == cm.expected("A", "B", "L")[0]
