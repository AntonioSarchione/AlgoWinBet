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
