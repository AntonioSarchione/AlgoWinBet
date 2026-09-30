from datetime import timedelta

from algowinbet.models import DixonColes
from algowinbet.providers import MockProvider
from algowinbet.state import build_state, history_at


def test_dixon_coles_fit_is_a_valid_distribution_and_finds_home_advantage():
    prov = MockProvider(seed=3)
    comp = prov.list_competitions()[0]
    m = DixonColes().fit(history_at(prov, comp, prov.as_of), prov.as_of)
    fx = prov.list_fixtures([comp], prov.as_of, prov.as_of + timedelta(days=9))[0]
    mat = m.score_matrix(fx.home, fx.away)
    assert abs(mat.sum() - 1) < 1e-9
    assert (mat >= 0).all()
    assert m.home_adv > 0  # generator uses +0.25
    assert abs(m.rho) <= 0.2


def test_history_and_quotes_respect_cutoff():
    prov = MockProvider(seed=3)
    comp = prov.list_competitions()[0]
    victim = prov.list_history([comp], prov.as_of)[-1]
    cutoff = victim.kickoff + timedelta(hours=1)  # match not over yet
    assert all(r.fixture_id != victim.fixture_id for r in history_at(prov, comp, cutoff))
    fx = next(f for f in prov.list_fixtures([comp], victim.kickoff, victim.kickoff) if f.id == victim.fixture_id)
    st = build_state(prov, fx, victim.kickoff - timedelta(hours=24))
    assert st.quotes and all(q.kind != "close" for q in st.quotes)  # closing quotes are not visible yet
    assert all(q.observed_at <= st.cutoff for q in st.quotes)


def test_unknown_team_falls_back_without_crashing():
    prov = MockProvider(seed=3)
    comp = prov.list_competitions()[0]
    m = DixonColes().fit(history_at(prov, comp, prov.as_of), prov.as_of)
    assert not m.knows("Nobody FC")
    assert abs(m.score_matrix("Nobody FC", "Other FC").sum() - 1) < 1e-9


def test_pooled_model_puts_clubs_from_different_leagues_on_one_scale():
    """League A clubs are truly stronger than league B clubs. Domestic games alone cannot reveal it (each league is
    centred on itself); cup games between the leagues do, and only the pooled model uses them."""
    import numpy as np
    from datetime import timedelta
    from algowinbet.domain import MatchResult, utc
    from algowinbet.models.dixon_coles import DixonColes
    rng = np.random.default_rng(3)
    strength = {**{f"A{i}": 0.35 for i in range(8)}, **{f"B{i}": -0.35 for i in range(8)}}
    res, t0, k = [], utc(2025, 8, 1), 0

    def play(h, a, comp):
        nonlocal k
        lh, la = np.exp(0.2 + 0.25 + strength[h] - strength[a]), np.exp(0.2 + strength[a] - strength[h])
        res.append(MatchResult(fixture_id=f"m{k}", competition=comp, home=h, away=a, kickoff=t0 + timedelta(hours=k),
                               home_goals=int(rng.poisson(lh)), away_goals=int(rng.poisson(la))))
        k += 1
    for lg in "AB":
        for _ in range(2):
            for i in range(8):
                for j in range(8):
                    if i != j:
                        play(f"{lg}{i}", f"{lg}{j}", f"League {lg}")
    for _ in range(6):
        for i in range(8):
            play(f"A{i}", f"B{i}", "Cup")
            play(f"B{i}", f"A{i}", "Cup")
    pooled = DixonColes(comp_mu=True).fit(res, t0 + timedelta(days=400)).for_competition("Cup")
    a_home = np.mean([pooled.expected_goals(f"A{i}", f"B{i}")[0] - pooled.expected_goals(f"B{i}", f"A{i}")[0] for i in range(8)])
    assert a_home > 0.5  # A clubs score clearly more against B clubs than vice versa
    solo_a = DixonColes().fit([r for r in res if r.competition == "League A"], t0 + timedelta(days=400))
    solo_b = DixonColes().fit([r for r in res if r.competition == "League B"], t0 + timedelta(days=400))
    assert abs(np.mean(solo_a.attack) - np.mean(solo_b.attack)) < 0.1  # per-league models see two equal leagues


def test_newcomers_start_below_average_not_at_league_average():
    from datetime import timedelta
    from algowinbet.domain import MatchResult, utc
    from algowinbet.engine import newcomer_prior
    from algowinbet.models.dixon_coles import DixonColes
    old = [MatchResult(fixture_id=f"o{k}", competition="L", home=f"T{k % 6}", away=f"T{(k + 1) % 6}", kickoff=utc(2025, 9, 1) + timedelta(days=k),
                       home_goals=1, away_goals=1) for k in range(60)]
    new = [MatchResult(fixture_id="n1", competition="L", home="Promoted", away="T0", kickoff=utc(2026, 8, 20), home_goals=1, away_goals=1)]
    cutoff = utc(2026, 9, 30)
    prior = newcomer_prior(old + new, cutoff, -0.2)
    assert prior == {"Promoted": (-0.2, -0.2)}
    with_p = DixonColes().fit(old + new, cutoff, prior=prior)
    without = DixonColes().fit(old + new, cutoff)
    assert with_p.expected_goals("T1", "Promoted")[0] > without.expected_goals("T1", "Promoted")[0]
