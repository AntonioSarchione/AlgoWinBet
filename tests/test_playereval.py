"""Replay of the player numbers: each start predicted from earlier days only."""
from datetime import datetime, timedelta, timezone

from algowinbet.playereval import PlayerParams, at_least, evaluate_players

T0 = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)


def test_at_least_poisson_and_negative_binomial():
    assert abs(at_least(1.0, 1) - (1 - 2.718281828 ** -1)) < 1e-6
    assert abs(at_least(1.0, 2) - (1 - 2 * 2.718281828 ** -1)) < 1e-6
    assert at_least(1.0, 1, disp=2.0) < at_least(1.0, 1)  # same mean, more spread: more zeros
    assert at_least(0.0, 1) == 0.0


def test_replay_uses_only_earlier_days_and_scores_every_line():
    apps, roles = [], {}
    for d in range(80):
        ko = (T0 + timedelta(days=d)).isoformat()
        for p in range(4):
            pid = f"fotmob:{p}"
            roles[pid] = "MID"  # one role: only the player's own history tells the striker apart
            shots = 4 if p == 0 else 0
            apps.append((ko, pid, 90, 1, shots, shots // 2, 1, 1, int(d % 4 == 0), 0, f"m{d}", "A" if p < 2 else "B"))
    fixtures = {f"m{d}": ("Serie A", "A", "B") for d in range(80)}
    rep = evaluate_players(None, T0 + timedelta(days=60), T0 + timedelta(days=80),
                           variants={"modello": PlayerParams(), "ruolo": PlayerParams(prior_90=1e9), "contesto": PlayerParams(opp=1.0, venue=1.0)},
                           data=(apps, roles, fixtures, {}))
    assert rep.starts == 80  # 4 players x 20 days
    s = rep.scores["modello"]["tiri 1+"]
    assert s.hits == 20 and len(s.terms) == 80
    # the striker who always shoots gets a high probability, the defenders who never do a low one
    assert sum(s.terms) / len(s.terms) < sum(rep.scores["ruolo"]["tiri 1+"].terms) / 80


def test_context_factors_follow_the_opponent_the_venue_and_the_referee():
    from algowinbet.playercontext import MatchContext, apply
    ctx = MatchContext()
    for d in range(30):  # "Leaky" lets 20+ shots in, "Wall" 6; home teams shoot more; referee "R" books more players than "S"
        c, ref = (4, "R") if d % 2 else (1, "S")
        ctx.add("Serie A", "Leaky", "Wall", {"Leaky": {"shots": 6, "cards": c}, "Wall": {"shots": 20, "cards": c}}, ref)
        ctx.add("Serie A", "Wall", "Leaky", {"Wall": {"shots": 24, "cards": c}, "Leaky": {"shots": 6, "cards": c}}, ref)
    f = ctx.factors("Leaky", "Serie A", True, "R")
    assert f["opp"]["shots"] > 1.2 and ctx.factors("Wall", "Serie A", True)["opp"]["shots"] < 0.8
    assert f["venue"]["shots"] > 1.0 and ctx.factors("Leaky", "Serie A", False)["venue"]["shots"] < 1.0
    assert f["ref"]["cards"] > 1.2 and ctx.factors("Leaky", "Serie A", True, "S")["ref"]["cards"] < 0.8
    assert ctx.factors("Leaky", "Serie A", True, "new")["ref"] == {}  # a referee never seen: no factor
    out = apply({"shots": 2.0, "cards": 0.2}, f, opp=1.0)
    assert out["shots"] == 2.0 * f["opp"]["shots"] and out["cards"] == 0.2 * f["opp"]["cards"]


def test_keeper_replay_reads_shots_faced_from_the_opponents_rows():
    from algowinbet.playereval import KeeperParams, evaluate_keepers
    apps, roles, fixtures = [], {"fotmob:gA": "GK", "fotmob:gB": "GK", "fotmob:sA": "FWD", "fotmob:sB": "FWD"}, {}
    for d in range(80):
        ko, fid = (T0 + timedelta(days=d)).isoformat(), f"m{d}"
        fixtures[fid] = ("Serie A", "A", "B", 1, 0)  # A scores once a match, B never
        # (ko, player, minutes, starter, shots, on target, fouls, fouls won, booked, assists, fixture, team, goals, saves, conceded)
        apps += [(ko, "fotmob:gA", 90, 1, 0, 0, 0, 0, 0, 0, fid, "A", 0, None, None), (ko, "fotmob:gB", 90, 1, 0, 0, 0, 0, 0, 0, fid, "B", 0, None, None),
                 (ko, "fotmob:sA", 90, 1, 6, 5, 1, 1, 0, 0, fid, "A", 1, None, None), (ko, "fotmob:sB", 90, 1, 2, 1, 1, 1, 0, 0, fid, "B", 0, None, None)]
    rep = evaluate_keepers(None, T0 + timedelta(days=60), T0 + timedelta(days=80), {"k5": KeeperParams()}, data=(apps, roles, fixtures, {}))
    assert rep.starts == 40  # both keepers, 20 days
    s = rep.scores["k5"]["parate 4+"]
    assert s.hits == 20  # B's keeper faces 5 on target and concedes 1: 4 saves; A's keeper saves 1
    assert rep.scores["k5"]["porta inviolata"].hits == 20  # A never concedes
