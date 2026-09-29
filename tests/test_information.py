import json
from datetime import timedelta

import numpy as np
import pytest

from algowinbet.backtest import run_info_value, run_stale_backtest
from algowinbet.config import Config
from algowinbet.domain import InformationEvent, LineupSnapshot, NewsItem, OpportunityStatus, Player, Position, utc
from algowinbet.engine import Engine
from algowinbet.information import (LLMNewsParser, PlayerImpactModel, RuleBasedParser, base_rates, build_availability,
                                    parse_lineup_names, resolve)
from algowinbet.providers import ManualInfoOverlay, MockProvider
from algowinbet.state import build_state
from algowinbet.store import Store

T0 = utc(2025, 4, 20, 12, 0)


def roster(team="Inter"):
    spec = [("Sommer", "GK"), ("Pavard", "DEF"), ("Acerbi", "DEF"), ("Bastoni", "DEF"), ("Darmian", "DEF"), ("Barella", "MID"),
            ("Calhanoglu", "MID"), ("Mkhitaryan", "MID"), ("Thuram", "FWD"), ("Lautaro Martinez", "FWD"), ("Arnautovic", "FWD"),
            ("Martinez Sub", "GK"), ("Sanchez", "FWD")]
    return [Player(id=f"{team}-{n}", name=n, team=team, position=Position(p)) for n, p in spec]


def item(text, level="A", team="Inter", at=T0):
    return NewsItem(source="test", source_level=level, published_at=at, observed_at=at, text=text, team=team)


# ------------------------------------------------------------------ parser
def test_parser_classifies_status_and_confidence_by_source():
    p = RuleBasedParser()
    ev = p.parse(item("Thuram indisponibile per infortunio."), roster())
    assert [(e.event_type, e.payload["status"], e.player) for e in ev] == [("PLAYER_STATUS", "OUT", "Inter-Thuram")]
    assert ev[0].confidence == pytest.approx(0.98 * 0.92)
    assert p.parse(item("Barella squalificato per un turno"), roster())[0].payload["status"] == "SUSPENDED"
    assert p.parse(item("Thuram non è infortunato, torna in gruppo"), roster())[0].payload["status"] == "AVAILABLE"
    assert p.parse(item("Sommer non convocato"), roster())[0].payload["status"] == "OUT"


def test_status_is_assigned_per_clause_when_several_players_share_a_sentence():
    ev = RuleBasedParser().parse(item("Forse Thuram in dubbio, Barella recuperato."), roster())
    by = {e.player: e for e in ev}
    assert by["Inter-Thuram"].payload["status"] == "DOUBTFUL" and by["Inter-Thuram"].payload["hedged"]
    assert by["Inter-Barella"].payload["status"] == "AVAILABLE" and not by["Inter-Barella"].payload["hedged"]


def test_rumours_are_low_confidence_and_flagged():
    e = RuleBasedParser().parse(item("Forse Thuram potrebbe essere in dubbio", level="E"), roster())[0]
    assert e.payload["hedged"] and e.payload["status"] == "DOUBTFUL"
    assert e.confidence < 0.1


def test_unparsed_text_is_kept_not_dropped_and_unknown_players_ignored():
    ev = RuleBasedParser().parse(item("Grande atmosfera a San Siro"), roster())
    assert len(ev) == 1 and ev[0].event_type == "OTHER" and ev[0].payload["unparsed"] and ev[0].confidence == 0
    ev = RuleBasedParser().parse(item("Mbappe indisponibile per infortunio"), roster())
    assert all(e.event_type == "OTHER" for e in ev)


def test_other_event_types_are_classified():
    ev = RuleBasedParser().parse(item("Allenatore esonerato dopo la sconfitta."), roster())
    assert ev[0].event_type == "COACH_CHANGE"


def test_same_item_gives_same_event_ids():
    a = RuleBasedParser().parse(item("Thuram out"), roster())
    b = RuleBasedParser().parse(item("Thuram out"), roster())
    assert [e.id for e in a] == [e.id for e in b]


def test_llm_parser_validates_output():
    r = roster()
    good = json.dumps([{"event_type": "PLAYER_STATUS", "player_id": "Inter-Thuram", "status": "OUT", "confidence": 0.9}])
    ev = LLMNewsParser(lambda _p: good).parse(item("x"), r)
    assert ev[0].player == "Inter-Thuram" and ev[0].confidence == pytest.approx(0.98 * 0.9)
    bad_player = json.dumps([{"event_type": "PLAYER_STATUS", "player_id": "Nobody", "status": "OUT", "confidence": 0.9}])
    assert LLMNewsParser(lambda _p: bad_player).parse(item("x"), r) == []
    assert LLMNewsParser(lambda _p: "not json").parse(item("x"), r)[0].payload["unparsed"]
    bad_status = json.dumps([{"event_type": "PLAYER_STATUS", "player_id": "Inter-Thuram", "status": "DEAD", "confidence": 0.9}])
    assert LLMNewsParser(lambda _p: bad_status).parse(item("x"), r) == []


def test_lineup_name_resolution():
    ids, bad = parse_lineup_names("Sommer, Pavard, Lautaro Martinez, Nessuno", roster())
    assert ids == ["Inter-Sommer", "Inter-Pavard", "Inter-Lautaro Martinez"] and bad == ["Nessuno"]


# ------------------------------------------------------------ availability
def _av(events=(), lineups=(), cutoff=T0):
    r = roster()
    base = {p.id: v for p, v in zip(r, [0.9, 0.9, 0.8, 0.85, 0.7, 0.9, 0.85, 0.7, 0.9, 0.85, 0.6, 0.1, 0.3])}
    return build_availability("Inter", r, base, list(events), list(lineups), cutoff, "fx"), base


def _ev(pid, status, level="A", conf=0.9, at=T0 - timedelta(hours=5), fixture=None):
    return InformationEvent(id=f"{pid}{status}{at}", fixture_id=fixture, team="Inter", player=pid, event_type="PLAYER_STATUS",
                            source_level=level, published_at=at, observed_at=at, confidence=conf, payload={"status": status})


def test_official_out_removes_player_and_redistributes_minutes():
    av0, base = _av()
    av, _ = _av([_ev("Inter-Thuram", "OUT")])
    assert av.p_start["Inter-Thuram"] == pytest.approx(base["Inter-Thuram"] * 0.1)
    assert av.p_start["Inter-Arnautovic"] > base["Inter-Arnautovic"]  # replacement gets more minutes
    fwd = [p.id for p in roster() if p.position == Position.FWD]
    assert sum(av.p_start[i] for i in fwd) == pytest.approx(sum(av0.p_start[i] for i in fwd), abs=0.05)


def test_rumour_barely_moves_probabilities():
    av, base = _av([_ev("Inter-Thuram", "OUT", level="E", conf=0.15 * 0.55)])
    assert av.p_start["Inter-Thuram"] > 0.9 * base["Inter-Thuram"]


def test_information_after_cutoff_is_invisible():
    av, base = _av([_ev("Inter-Thuram", "OUT", at=T0 + timedelta(minutes=1))])
    assert av.p_start["Inter-Thuram"] == pytest.approx(base["Inter-Thuram"])


def test_confirmed_lineup_is_certain_and_overrides_news():
    starters = [p.id for p in roster()][:11]
    lu = LineupSnapshot(fixture_id="fx", team="Inter", status="confirmed", starters=starters, published_at=T0 - timedelta(hours=1),
                        observed_at=T0 - timedelta(hours=1))
    av, _ = _av([_ev("Inter-Sommer", "OUT")], [lu])
    assert av.source == "confirmed"
    assert av.p_start[starters[0]] == 1.0 and av.p_start[roster()[12].id] == 0.0


def test_probable_lineup_then_out_news_lowers_starter():
    starters = [p.id for p in roster()][:11]
    at = T0 - timedelta(hours=10)
    lu = LineupSnapshot(fixture_id="fx", team="Inter", status="probable", starters=starters, published_at=at, observed_at=at, source_level="B")
    av, _ = _av([_ev("Inter-Thuram", "OUT", at=T0 - timedelta(hours=2))], [lu])
    assert av.source == "probable" and av.p_start["Inter-Thuram"] < 0.2 and av.p_start["Inter-Sommer"] > 0.7


# ----------------------------------------------------------- impact + routing
def test_impact_direction_and_uncertainty():
    ri, ra = roster("Inter"), roster("Empoli")
    base = {p.id: 0.8 for p in ri + ra}
    imp = PlayerImpactModel(ri + ra, base)
    av_full, _ = build_availability("Inter", ri, base, [], [], T0, "fx"), None
    home = build_availability("Inter", ri, base, [], [], T0, "fx")
    away = build_availability("Empoli", ra, base, [], [], T0, "fx")
    assert imp.adjustment(home, away).is_zero or abs(imp.adjustment(home, away).d_home) < 1e-9
    home_out = build_availability("Inter", ri, base, [_ev("Inter-Thuram", "OUT")], [], T0, "fx")
    assert imp.adjustment(home_out, away).d_home < 0  # own striker out: fewer home goals
    away_gk_out = build_availability("Empoli", ra, base, [InformationEvent(id="g", team="Empoli", player="Empoli-Sommer", event_type="PLAYER_STATUS",
                                     source_level="A", published_at=T0, observed_at=T0 - timedelta(hours=1), confidence=0.9,
                                     payload={"status": "OUT"})], [], T0, "fx")
    assert imp.adjustment(home, away_gk_out).d_home > 0  # opposing keeper out: more home goals
    assert imp.adjustment(home_out, away).sd_home > 0


def test_impact_model_learns_from_lineup_history():
    prov = MockProvider(seed=7, past_rounds=24, player_effect_scale=3.0, stats_informed=False)
    eng = Engine(prov, Config())
    comp = prov.list_competitions()[0]
    imp = eng.impact(comp, prov.as_of)
    assert imp is not None and imp.learned and imp.n_obs.sum() > 0
    truth_a = np.array([prov._effects[i][0] for i in imp.ids])
    assert np.corrcoef(imp.a, truth_a)[0, 1] > np.corrcoef(imp.prior_a, truth_a)[0, 1] - 0.02  # learning does not degrade the prior


def test_routing_by_position_and_event_type():
    r = {p.id: p for p in roster()}
    gk = resolve(_ev("Inter-Sommer", "OUT"), r)
    fwd = resolve(_ev("Inter-Thuram", "OUT"), r)
    assert {"1X2", "BTTS"} <= gk.high and {"TEAM_TOTALS", "TOTALS"} <= fwd.high and "BTTS" in fwd.monitor
    lu = InformationEvent(id="l", event_type="LINEUP_CONFIRMED", published_at=T0, observed_at=T0)
    assert {"1X2", "TOTALS", "BTTS", "TEAM_TOTALS"} <= resolve(lu).high
    assert resolve(InformationEvent(id="o", event_type="ODDS_MOVE", published_at=T0, observed_at=T0)).affected == set()


# ------------------------------------------------------------- state / engine
def test_lineups_after_cutoff_not_visible_and_confirmed_needs_both_teams():
    prov = MockProvider(seed=3)
    comp = prov.list_competitions()[0]
    fx = prov.list_fixtures([comp], prov.as_of, prov.as_of + timedelta(days=3))[0]
    before = build_state(prov, fx, fx.kickoff - timedelta(hours=2), prov.list_players(comp))
    assert before.lineups == [] and before.lineup_state == "none"
    after = build_state(prov, fx, fx.kickoff - timedelta(minutes=60), prov.list_players(comp))
    assert after.lineup_state == "confirmed"
    one = build_state(prov, fx, fx.kickoff - timedelta(minutes=60), prov.list_players(comp))
    one.lineups = one.lineups[:1]
    assert one.lineup_state == "probable"


def test_pre_lineup_news_flows_through_parser_into_state():
    prov = MockProvider(seed=7, stage="pre_lineup")
    comp = prov.list_competitions()[0]
    fx = [f for f in prov.list_fixtures([comp], prov.as_of, prov.as_of + timedelta(days=2))]
    found = 0
    for f in fx:
        st = build_state(prov, f, prov.as_of, prov.list_players(comp))
        found += sum(1 for e in st.events if e.event_type == "PLAYER_STATUS" and e.payload["status"] in ("OUT", "DOUBTFUL"))
    assert found > 0


def test_stale_quotes_flagged_and_never_candidates_then_cleared_by_fresh_quotes():
    stale = MockProvider(seed=7, stage="post_lineup", open_noise=0.0, close_noise=0.0)
    cfg = Config()
    res = Engine(stale, cfg).analyze(None, stale.as_of, stale.as_of + timedelta(days=1), stale.as_of)
    flagged = [o for o in res.opportunities if o.odds_stale]
    assert flagged and all(o.status not in (OpportunityStatus.STRONG, OpportunityStatus.CANDIDATE) for o in flagged)
    assert any(o.status == OpportunityStatus.WATCH for o in flagged)
    fresh = MockProvider(seed=7, stage="post_lineup", fresh_quotes=True, open_noise=0.0, close_noise=0.0)
    res2 = Engine(fresh, cfg).analyze(None, fresh.as_of, fresh.as_of + timedelta(days=1), fresh.as_of)
    assert not any(o.odds_stale for o in res2.opportunities)
    assert all(o.lineup_state == "confirmed" for o in res2.opportunities)


def test_handle_event_recalculates_only_affected_fixture_and_routed_families():
    prov = MockProvider(seed=7, stage="pre_lineup", open_noise=0.0, close_noise=0.0)
    eng = Engine(prov, Config())
    r0 = eng.analyze(None, prov.as_of, prov.as_of + timedelta(days=2), prov.as_of)
    fx = r0.fixtures[0]
    team = fx.home
    star = next(p for p in prov.list_players(fx.competition) if p.team == team and p.position == Position.FWD and p.id.endswith("FWD0"))
    at = prov.as_of + timedelta(minutes=10)
    ev = InformationEvent(id="e1", fixture_id=fx.id, team=team, player=star.id, event_type="PLAYER_STATUS", source_level="A",
                          published_at=at, observed_at=at, confidence=0.95, payload={"status": "OUT"})
    r1 = eng.handle_event(r0, ev, at)
    before = {(o.fixture_id, o.ref.key): o for o in r0.opportunities}
    for o in r1.opportunities:
        if o.fixture_id != fx.id:
            assert o.p_final == before[(o.fixture_id, o.ref.key)].p_final  # untouched fixtures are not recomputed
    mine_before = {k: v for k, v in before.items() if k[0] == fx.id}
    assert any(o.fixture_id == fx.id and o.p_final != mine_before[(o.fixture_id, o.ref.key)].p_final for o in r1.opportunities)
    assert r1.changes and all(c["priority"] in ("high", "monitor") for c in r1.changes)
    over = next(c for c in r1.changes if c["market"].startswith("Over 2.5"))
    assert over["p_after"] < over["p_before"]  # top striker out => fewer goals expected


def test_timeline_shows_information_arriving_over_time():
    prov = MockProvider(seed=7, stage="post_lineup", open_noise=0.0, close_noise=0.0)
    eng = Engine(prov, Config())
    fx = prov.list_fixtures(None, prov.as_of, prov.as_of + timedelta(days=1))[0]
    tl = eng.timeline(fx, model_cutoff=prov.as_of - timedelta(days=1))
    states = [e.lineup_state for e in tl]
    assert states[0] == "none" and states[-1] == "confirmed"
    assert tl[-1].stale_quotes


def test_info_value_lineups_do_not_hurt_and_help_when_effects_are_large():
    prov = MockProvider(seed=7, past_rounds=24, player_effect_scale=3.0)
    rep = run_info_value(prov, Config())
    lu = rep.stages["T-30m (lineups)"]
    for fam in ("1X2", "TOTALS"):
        assert lu[fam]["logloss_aware"] <= lu[fam]["logloss_blind"] + 0.001
    assert sum(m["logloss_aware"] < m["logloss_blind"] for m in lu.values()) >= 2


def test_stale_backtest_beats_the_closing_line_on_mock():
    prov = MockProvider(seed=7, past_rounds=24, open_noise=0.0, close_noise=0.0)
    r = run_stale_backtest(prov, Config())
    assert r.n_bets > 50
    assert r.mean_clv > 0  # bought better than the eventual closing price (ROI itself is too noisy at this n to assert)


# --------------------------------------------------------------- store/manual
def test_store_information_is_idempotent(tmp_path):
    st = Store(tmp_path / "t.db")
    e = _ev("Inter-Thuram", "OUT")
    lu = LineupSnapshot(fixture_id="fx", team="Inter", status="confirmed", starters=["a"], published_at=T0, observed_at=T0)
    assert st.save_information([e, e], [lu, lu]) == (1, 1)
    assert st.save_information([e], [lu]) == (0, 0)


def test_manual_info_overlay_resolves_lineups_and_news(tmp_path):
    prov = MockProvider(seed=7, stage="pre_lineup")
    comp = prov.list_competitions()[0]
    fx = prov.list_fixtures([comp], prov.as_of, prov.as_of + timedelta(days=2))[0]
    squad = [p for p in prov.list_players(comp) if p.team == fx.home]
    info = {
        "players": [{"name": p.name, "team": p.team, "position": p.position.value} for p in squad],
        "lineups": [{"team": fx.home, "date": fx.kickoff.date().isoformat(), "status": "confirmed", "formation": "4-3-3",
                     "starters": [p.name for p in squad[:11]], "published_at": (fx.kickoff - timedelta(minutes=75)).isoformat()},
                    {"team": fx.home, "date": "1999-01-01", "status": "confirmed", "starters": []}],
        "news": [{"source": "club", "level": "A", "published_at": prov.as_of.isoformat(), "team": fx.home,
                  "text": f"{squad[9].name} indisponibile per infortunio."}],
    }
    f = tmp_path / "info.json"
    f.write_text(json.dumps(info))
    ov = ManualInfoOverlay(prov, f)
    assert len(ov.get_lineups(fx.id)) >= 1 and any("nessuna partita" in w for w in ov.warnings)
    assert ov.list_players(comp)  # roster available
    st = build_state(ov, fx, fx.kickoff - timedelta(minutes=30), ov.list_players(comp))
    assert st.lineup_state in ("probable", "confirmed")
    assert any(e.payload.get("status") == "OUT" and e.team == fx.home for e in st.events)
