import math
from datetime import datetime, timedelta, timezone

import numpy as np

from algowinbet.config import Config
from algowinbet.domain import MatchResult
from algowinbet.elo import (EloMatch, EloTimeline, club_timeline, elo_prior, goal_multiplier, national_timeline, sync_international,
                            tournament_k)
from algowinbet.engine import Engine
from algowinbet.models import DixonColes
from algowinbet.snapshots import SnapshotProvider, SnapshotStore

UTC = timezone.utc
T0 = datetime(2025, 8, 1, tzinfo=UTC)

INTL = b"""date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
2025-03-20,Netherlands,Greece,3,0,UEFA Nations League,Amsterdam,Netherlands,FALSE
2025-06-10,Greece,Netherlands,0,2,FIFA World Cup qualification,Athens,Greece,FALSE
2025-09-05,Netherlands,Malta,8,0,FIFA World Cup qualification,Rotterdam,Netherlands,FALSE
2026-11-15,Greece,Netherlands,NA,NA,UEFA Nations League,Athens,Greece,FALSE
"""


def test_elo_updates_and_reads_only_known_results():
    tl = EloTimeline([EloMatch(T0, "A", "B", 2, 0, 20, 0.0)])
    assert tl.at("A", T0 - timedelta(seconds=1)) is None
    assert tl.at("A", T0) == 1500 + 20 * 1.5 * 0.5 and tl.at("B", T0) == 1500 - 15
    assert goal_multiplier(1) == 1 and goal_multiplier(-2) == 1.5 and goal_multiplier(4) == 15 / 8
    assert tournament_k("FIFA World Cup") == 60 and tournament_k("UEFA Euro qualification") == 40 and tournament_k("Friendly") == 20


def test_national_ratings_from_the_international_csv():
    tl = national_timeline(INTL)
    after = datetime(2025, 9, 7, tzinfo=UTC)
    assert tl.at("Netherlands", after) > 1500 > tl.at("Greece", after)
    assert tl.at("Netherlands", datetime(2025, 3, 20, 12, tzinfo=UTC)) is None  # dated results count from the next day
    assert "Malta" in tl.teams()


def test_club_newcomers_start_below_average():
    res = [MatchResult(fixture_id=f"g{k}", competition="L", home=h, away=a, kickoff=T0 + timedelta(days=d), home_goals=1, away_goals=1)
           for k, (h, a, d) in enumerate([("A", "B", 0), ("C", "A", 200)])]
    tl = club_timeline(res)
    later = T0 + timedelta(days=300)
    assert tl.at("B", later) > 1500 and tl.at("C", later) < 1450  # B drew away (gains); C first seen 200 days after the start


def test_elo_prior_is_relative_to_teams_of_the_same_kind():
    nations = national_timeline(INTL)
    clubs = EloTimeline([EloMatch(T0, "Inter", "Lecce", 3, 0, 20, 0.0)])
    when = datetime(2025, 9, 7, tzinfo=UTC)
    pr = elo_prior(["Netherlands", "Greece", "Inter", "Lecce"], when, clubs, nations, club_per_100=0.1, nation_per_100=0.2)
    assert pr["Netherlands"][0] > 0 > pr["Greece"][0]
    assert math.isclose(pr["Netherlands"][0] + pr["Greece"][0], 0, abs_tol=1e-9)  # centred on the nations' mean
    assert pr["Inter"][0] > 0 > pr["Lecce"][0]
    assert elo_prior(["Netherlands", "Greece"], when, clubs, nations, 0.1, 0.0) == {}  # nations off, not known as clubs


def _league(comp, n_teams, home_rate, away_rate, rounds, seed, start=T0):
    rng = np.random.default_rng(seed)
    teams = [f"{comp}{k}" for k in range(n_teams)]
    out, k = [], 0
    for r in range(rounds):
        for i in range(0, n_teams, 2):
            h, a = (teams[i], teams[i + 1]) if r % 2 else (teams[i + 1], teams[i])
            out.append(MatchResult(fixture_id=f"{comp}{k}", competition=comp, home=h, away=a, kickoff=start + timedelta(days=r),
                                   home_goals=int(rng.poisson(home_rate)), away_goals=int(rng.poisson(away_rate))))
            k += 1
        rng.shuffle(teams)
    return out


def test_league_home_advantage_and_goal_level_are_learnt_and_shrunk():
    big = _league("Big", 10, 2.0, 1.0, 60, 1)    # strong home advantage
    small = _league("Small", 6, 1.2, 1.2, 4, 2)  # no home advantage, few matches
    as_of = T0 + timedelta(days=61)
    shared = DixonColes(comp_mu=True).fit(big + small, as_of)
    assert shared.home_comp == {}
    m = DixonColes(comp_mu=True, l2_comp_home=5.0, l2_comp_mu=5.0).fit(big + small, as_of)
    hb, hs = m.home_adv + m.home_comp["Big"], m.home_adv + m.home_comp["Small"]
    assert hb > hs
    loose = DixonColes(comp_mu=True, l2_comp_home=0.01).fit(big + small, as_of)
    assert abs(loose.home_comp["Small"] - loose.home_comp["Big"]) > abs(m.home_comp["Small"] - m.home_comp["Big"])  # shrinkage
    lh, la = m.expected_goals("Big0", "Big1", "Big")
    lh2, la2 = m.expected_goals("Big0", "Big1", "Small")
    assert lh / la > lh2 / la2


def test_engine_uses_the_national_elo_prior_from_the_stored_csv():
    st = SnapshotStore(":memory:")
    res = _league("Liga", 10, 1.5, 1.1, 30, 3, start=T0 - timedelta(days=60))
    res += [MatchResult(fixture_id="n1", competition="UEFA Nations League", home="Netherlands", away="Greece",
                        kickoff=datetime(2025, 9, 1, tzinfo=UTC), home_goals=1, away_goals=1),
            MatchResult(fixture_id="n2", competition="UEFA Nations League", home="Greece", away="Netherlands",
                        kickoff=datetime(2025, 9, 4, tzinfo=UTC), home_goals=1, away_goals=1)]
    st.save_results("goal", res, T0)
    from algowinbet.elo import INTL_ENDPOINT, INTL_SOURCE
    st.put_raw(INTL_SOURCE, INTL_ENDPOINT, {}, 200, INTL, T0, cost=0)
    when = datetime(2025, 9, 10, tzinfo=UTC)
    base = Engine(SnapshotProvider(st), Config(), use_lineups=False).fit("UEFA Nations League", when)[0]
    cfg = Config()
    cfg.model.nation_elo_per_100 = 0.3
    elo = Engine(SnapshotProvider(st), cfg, use_lineups=False).fit("UEFA Nations League", when)[0]
    gap = lambda m: math.log(m.expected_goals("Netherlands", "Greece")[0] / m.expected_goals("Netherlands", "Greece")[1])
    assert gap(elo) > gap(base) + 0.05  # two draws say little; the Elo gap moves the Netherlands ahead


def test_international_csv_downloaded_weekly_with_etag():
    st = SnapshotStore(":memory:")
    calls = []

    def fetch(url, headers):
        calls.append(headers)
        return 200, INTL

    r = sync_international(st, T0, fetch=fetch)
    assert r.requests == 1 and r.saved == {"partite internazionali": 3}
    assert sync_international(st, T0 + timedelta(days=3), fetch=fetch) is None
    assert SnapshotProvider(st).international_results() == INTL
    assert sync_international(st, T0 + timedelta(days=8), fetch=lambda u, h: (304, b"")).skipped == ["risultati internazionali: invariati"]


def test_model_eval_scores_variants_walk_forward():
    from algowinbet.modeleval import evaluate
    st = SnapshotStore(":memory:")
    st.save_results("goal", _league("Liga", 10, 1.6, 1.0, 80, 4, start=T0), T0)
    rep = evaluate(SnapshotProvider(st), Config(), T0 + timedelta(days=50), T0 + timedelta(days=80),
                   {"base": {}, "campionati": {"l2_comp_home": 50.0, "l2_comp_mu": 20.0}})
    s = rep.scores["base"]["campionati"]
    assert rep.weeks >= 4 and s["n"] > 100 and 0.8 < s["ll_1x2"] < 1.3 and s["n_ref"] == 0
    assert set(rep.scores) == {"base", "campionati"}


def test_international_history_adds_matches_not_in_our_feed_and_skips_home_advantage_on_neutral_ground():
    from algowinbet.elo import INTL_FRIENDLY, INTL_OFFICIAL, international_results, national_history
    intl = international_results(INTL + b"2025-06-14,Spain,Malta,4,0,Friendly,Cadiz,Spain,TRUE\n")
    assert [r.competition for r in intl] == [INTL_OFFICIAL, INTL_OFFICIAL, INTL_OFFICIAL, INTL_FRIENDLY]
    assert intl[-1].neutral and not intl[0].neutral
    ours = [MatchResult(fixture_id="goal:1", competition="UEFA Nations League", home="Netherlands", away="Greece",
                        kickoff=datetime(2025, 3, 20, 19, 45, tzinfo=UTC), home_goals=3, away_goals=0)]
    got = national_history(intl, ours, {"Netherlands"}, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC))
    assert [(r.home, r.away) for r in got] == [("Greece", "Netherlands"), ("Netherlands", "Malta")]  # ours kept, Spain not involved


def test_neutral_ground_has_no_home_advantage():
    res = _league("L", 8, 1.8, 1.0, 40, 5)
    m = DixonColes().fit(res, T0 + timedelta(days=41))
    lh, la = m.expected_goals("L0", "L1")
    nh, na = m.expected_goals("L0", "L1", neutral=True)
    assert math.isclose(lh / nh, math.exp(m.home_adv)) and math.isclose(la, na)
    neutral = [r.model_copy(update={"neutral": True}) for r in res]
    assert abs(DixonColes().fit(neutral, T0 + timedelta(days=41)).home_adv - 0.25) < 1e-6  # no data on it: stays at the start


def test_engine_adds_international_history_for_our_national_teams():
    st = SnapshotStore(":memory:")
    res = _league("Liga", 10, 1.5, 1.1, 30, 3, start=T0 - timedelta(days=60))
    res.append(MatchResult(fixture_id="n1", competition="UEFA Nations League", home="Netherlands", away="Greece",
                           kickoff=datetime(2025, 9, 1, tzinfo=UTC), home_goals=1, away_goals=1))
    st.save_results("goal", res, T0)
    from algowinbet.elo import INTL_ENDPOINT, INTL_SOURCE
    st.put_raw(INTL_SOURCE, INTL_ENDPOINT, {}, 200, INTL, T0, cost=0)
    cfg = Config()
    cfg.model.national_history_years = 4
    m = Engine(SnapshotProvider(st), cfg, use_lineups=False).fit("UEFA Nations League", datetime(2025, 9, 10, tzinfo=UTC))[0]
    assert m.knows("Malta") and m.n_matches["Netherlands"] == 4  # ours + 3 internationals
