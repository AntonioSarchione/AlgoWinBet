from datetime import datetime, timedelta, timezone

from algowinbet.domain import MatchResult
from algowinbet.models.dixon_coles import DixonColes

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _rows():
    # A scores lots without making chances (lucky), B makes chances without scoring
    out, xg = [], {}
    teams = ["A", "B", "C", "D"]
    k = 0
    for wk in range(30):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                k += 1
                fid = f"f{k}"
                hg = 3 if h == "A" else 0 if h == "B" else 1
                ag = 3 if a == "A" else 0 if a == "B" else 1
                out.append(MatchResult(fixture_id=fid, competition="L", home=h, away=a, kickoff=T0 + timedelta(days=wk), home_goals=hg, away_goals=ag))
                xg[fid] = (1.0 if h == "A" else 2.5 if h == "B" else 1.2, 1.0 if a == "A" else 2.5 if a == "B" else 1.2)
    return out, xg


def test_xg_weight_moves_strength_toward_chances():
    rows, xg = _rows()
    at = T0 + timedelta(days=40)
    goals = DixonColes().fit(rows, at)
    mixed = DixonColes(xg_weight=0.5).fit(rows, at, xg=xg)
    g = goals.attack[goals.teams["A"]] - goals.attack[goals.teams["B"]]
    m = mixed.attack[mixed.teams["A"]] - mixed.attack[mixed.teams["B"]]
    assert m < g  # A's lucky goals count less, B's chances more
    assert DixonColes(xg_weight=0.5).fit(rows, at).attack.tolist() == goals.attack.tolist()  # no xG given: goals only
