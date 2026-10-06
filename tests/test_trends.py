"""Statistical streaks ("ritardi"): runs and counts against the base rates of each competition."""
from datetime import datetime, timedelta, timezone

from algowinbet.trends import EVENTS, View, base_rates, streaks

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _v(k, gf, ga, comp="Serie A", opp="X", xg=None):
    st = {"expected_goals": xg} if xg else {}
    return View(f"f{k}", comp, T - timedelta(days=7 * k), opp, k % 2 == 0, gf, ga, st)


def test_drought_and_count_are_scored_against_the_league_rate():
    base = {("Serie A", e.key): 0.5 for e in EVENTS}
    base[("Serie A", "win")] = 0.4
    vs = [_v(k, 0, 1, xg=(1.9, 0.8)) for k in range(6)] + [_v(k, 2, 0) for k in range(6, 10)]  # newest first: 6 defeats
    out = {s["e"]: s for s in streaks(vs, base, True, {"1": 0.55, "Over 0.5 casa": 0.8})}
    win = out["win"]
    assert win["t"] == "Senza vittorie da 6 partite" and abs(win["p"] - 0.6 ** 6) < 1e-12 and win["m"] == 0.55
    assert out["scores"]["t"] == "Senza segnare da 6 partite" and "xG medio 1.90 fatti" in out["scores"]["x"]
    assert all(s["p"] <= 0.15 for s in out.values())


def test_head_to_head_names_the_side_and_short_histories_stay_quiet():
    base = {("Serie A", e.key): 0.5 for e in EVENTS}
    vs = [_v(k, 3, 1) for k in range(4)]
    texts = [s["t"] for s in streaks(vs, base, True, {}, "Milan")]
    assert "Milan: Vince da 4 partite di fila" in texts and "Over 2.5 da 4 partite di fila" in texts
    assert streaks(vs[:2], base, True, {}) == []


def test_base_rates_need_enough_matches():
    views = {"A": [_v(k, 1, 0) for k in range(50)]}
    rates = base_rates(views, T - timedelta(days=3650))
    assert abs(rates[("Serie A", "win")] - 51 / 52) < 1e-12 and ("Serie A", "corners") not in rates


def test_match_trends_from_the_store():
    import json
    from algowinbet.domain import Fixture
    from algowinbet.snapshots import SnapshotProvider
    from algowinbet.trends import match_trends
    from test_scorers import T0, _store
    st = _store(20)  # Milan beat Roma 2-0 every week, the striker scored every time
    prov = SnapshotProvider(st)
    ko = T0 + timedelta(days=7 * 20)
    fx = Fixture(id="goal:next", competition="Serie A", home="Milan", away="Roma", kickoff=ko)
    got = json.loads(match_trends(st, prov, [fx], {"goal:next": [{"g": "1X2", "l": "1", "p": 0.6}]}, ko - timedelta(hours=2))["goal:next"])
    assert any(s["t"] == "Vince da 10 partite di fila" and s["m"] == 0.6 for s in got["home"])
    assert any(s["t"].startswith("Milan: Vince") for s in got["h2h"]) and got["n_h2h"] == 10
    assert any(s["t"].startswith("Striker One: a segno da") for s in got["scorers"])
