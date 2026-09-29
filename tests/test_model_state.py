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
    assert st.quotes and all(q.kind == "open" for q in st.quotes)  # closing quotes are not visible yet
    assert all(q.observed_at <= st.cutoff for q in st.quotes)


def test_unknown_team_falls_back_without_crashing():
    prov = MockProvider(seed=3)
    comp = prov.list_competitions()[0]
    m = DixonColes().fit(history_at(prov, comp, prov.as_of), prov.as_of)
    assert not m.knows("Nobody FC")
    assert abs(m.score_matrix("Nobody FC", "Other FC").sum() - 1) < 1e-9
