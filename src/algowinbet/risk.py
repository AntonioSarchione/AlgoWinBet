"""Risk + stake engine (spec 18). Separate from prediction: bankroll never changes probabilities.
No function here reads past results, so stakes can never chase losses. Paper-only by design."""
from __future__ import annotations

from .config import RiskCfg
from .domain import Slip


def raw_stake(s: Slip, r: RiskCfg) -> float:
    if r.stake_method == "flat":
        return r.flat_stake
    if r.stake_method == "pct":
        return r.bankroll * r.pct
    b = (s.total_odds - 1) * (1 + s.bonus)  # net winnings per unit, Sisal multiple bonus included
    f = s.ev_lower / b if b > 0 else 0.0  # Kelly at the conservative joint probability: (p_low * (1 + b) - 1) / b
    return max(0.0, f) * r.kelly_fraction * r.bankroll


def assign_stakes(slips: list[Slip], r: RiskCfg) -> list[Slip]:
    total_cap = r.bankroll * r.max_exposure_total_pct
    per_fix_cap = r.bankroll * r.max_exposure_per_fixture_pct
    per_team_cap = r.bankroll * r.max_exposure_per_team_pct
    used_total, used_fix, used_team = 0.0, {}, {}
    for s in slips:
        stake = min(raw_stake(s, r), r.bankroll * r.max_stake_pct_per_slip, total_cap - used_total)
        notes = []
        for l in s.legs:
            stake = min(stake, per_fix_cap - used_fix.get(l.fixture_id, 0.0))
            for team in (l.home, l.away):
                stake = min(stake, per_team_cap - used_team.get(team, 0.0))
        stake = max(0.0, round(stake, 2))
        if stake == 0.0:
            notes.append("stake 0: nessun vantaggio conservativo o limite di esposizione raggiunto")
        s.stake = stake
        s.explanation.setdefault("risk", {})["notes"] = notes
        used_total += stake
        for l in s.legs:
            used_fix[l.fixture_id] = used_fix.get(l.fixture_id, 0.0) + stake
            for team in (l.home, l.away):
                used_team[team] = used_team.get(team, 0.0) + stake
    return slips
