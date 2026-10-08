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
            apps.append((ko, pid, 90, 1, shots, shots // 2, 1, 1, int(d % 4 == 0), 0))
    rep = evaluate_players(None, T0 + timedelta(days=60), T0 + timedelta(days=80),
                           variants={"modello": PlayerParams(), "ruolo": PlayerParams(prior_90=1e9)}, data=(apps, roles))
    assert rep.starts == 80  # 4 players x 20 days
    s = rep.scores["modello"]["tiri 1+"]
    assert s.hits == 20 and len(s.terms) == 80
    # the striker who always shoots gets a high probability, the defenders who never do a low one
    assert sum(s.terms) / len(s.terms) < sum(rep.scores["ruolo"]["tiri 1+"].terms) / 80
