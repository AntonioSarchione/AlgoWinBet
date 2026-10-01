"""All tunables in one serialisable object (spec 45). Load overrides from JSON."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class ModelCfg(BaseModel):
    xi_half_life_days: float = 365.0
    l2: float = 1.0
    n_bootstrap: int = 0  # >0 = parametric uncertainty by bootstrap (slow); 0 = analytic proxy
    min_history: int = 40
    pooled: bool = True  # one model over all competitions (needed for European cups); False = one model per competition
    newcomer_prior: float = -0.2  # log-strength prior (attack and defence) for clubs with no match before this season
    history_seasons: int = 2  # results used by the model: current season + this many previous seasons (seasons start 1 July)
    # Elo prior (0 = off): log-goal strength per 100 Elo above the average of the teams of the same kind, used as the centre
    # of the ridge penalty instead of 0. Clubs: Elo from our results; national teams: Elo from the international results.
    club_elo_per_100: float = 0.0
    nation_elo_per_100: float = 0.15  # model-eval 2026-10-01: national matches 1X2 log loss 1.0095 -> 0.9850 (n=259)
    # league effects (pooled model): home advantage per competition shrunk to the shared one (None = shared only), goal
    # level per competition shrunk to the overall one (0 = free)
    # national teams: years of international results (every friendly and competitive match, github martj42) added to the
    # history of the national teams our competitions involve (0 = only our own results)
    national_history_years: float = 4.0  # model-eval 2026-10-01, 190 internationals: 1X2 LL 0.898 -> 0.842, O2.5 0.717 -> 0.664
    l2_comp_home: float | None = None
    l2_comp_mu: float = 0.0
    aliases_path: str = "configs/team_aliases.json"  # team names shared by our feeds and the international results
    version: str = "dc-poisson-v3"  # v2: national-team Elo prior; v3: + 4 years of international results


class EnsembleCfg(BaseModel):
    """mode=adaptive: the devigged market price is the prior; the structural model is noisy evidence.
    w_struct = tau^2/(tau^2+sigma^2), tau = how far the true prob may sit from the market price (market_prior_sd),
    sigma = model error (bootstrap or sqrt(p(1-p)/n) proxy, measured on synthetic data: rmse ~ sqrt(pq/n_matches)).
    mode=fixed: constant w_struct (learn it with `backtest`, column w_struct*)."""

    mode: str = "adaptive"
    market_prior_sd: float = 0.025
    w_struct: float = 0.5  # used only in fixed mode
    devig_method: str = "power"
    quote_window_hours: float | None = None  # only prices observed in the last N hours before the cutoff (live: 24, set by the CLI)


class Thresholds(BaseModel):
    min_ev: float = 0.03
    min_edge: float = 0.01
    max_uncertainty: float = 0.10
    min_dq_candidate: float = 0.60
    min_dq_strong: float = 0.80
    min_dq_valid: float = 0.40
    avoid_ev: float = -0.03
    z: float = 1.645  # one-sided 95% for lower bounds
    min_odds: float = 1.05
    # below this probability a selection is never proposed, whatever its EV: long shots (exact scores, 7+ goals) carry the
    # model's largest errors exactly where no sharp price can check it, and the tool is after the slip most likely to win
    min_probability: float = 0.25


class OptimizerCfg(BaseModel):
    odds_min: float = 2.0
    odds_max: float = 15.0
    min_probability: float = 0.0  # min joint probability of slip
    max_legs: int = 8
    max_legs_per_fixture: int = 1  # SGP needs a bookmaker combo quote; see joint pricing
    max_legs_per_competition: int = 99
    min_leg_probability: float = 0.0
    beam_width: int = 200
    output_count: int = 3
    max_overlap: float = 0.5  # Jaccard overlap allowed between output slips
    correlation_limit: float = 0.25
    min_slip_ev: float = 0.0
    risk_profile: str = "balanced"  # conservative | balanced | dynamic
    cross_match_rho: float = 0.005
    same_competition_rho: float = 0.02
    include_watch: bool = False
    w_ev: float = 1.0
    w_prob: float = 0.5
    w_div: float = 0.1
    w_unc: float = 1.5
    w_corr: float = 1.0
    w_disagree: float = 0.5


class RiskCfg(BaseModel):
    bankroll: float = 1000.0
    mode: str = "paper"  # paper only: the tool never places bets
    stake_method: str = "kelly"  # flat | pct | kelly
    flat_stake: float = 10.0
    pct: float = 0.01
    kelly_fraction: float = 0.25
    max_stake_pct_per_slip: float = 0.02
    max_exposure_total_pct: float = 0.10
    max_exposure_per_fixture_pct: float = 0.03
    max_exposure_per_team_pct: float = 0.04


class Config(BaseModel):
    model: ModelCfg = Field(default_factory=ModelCfg)
    ensemble: EnsembleCfg = Field(default_factory=EnsembleCfg)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    optimizer: OptimizerCfg = Field(default_factory=OptimizerCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    bet_bookmakers: list[str] = Field(default_factory=list)  # empty: any bookmaker; e.g. ["sisal"]: only its prices are playable
    calibration_path: str = "configs/calibration.json"
    db_path: str = "algowinbet.db"

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path and Path(path).exists():
            return cls.model_validate(json.loads(Path(path).read_text()))
        return cls()

    def apply_profile(self) -> "Config":
        """Risk profile presets: conservative | balanced | dynamic (spec 17.1)."""
        o, t = self.optimizer, self.thresholds
        if o.risk_profile == "conservative":
            t.min_ev, t.max_uncertainty, o.max_legs = max(t.min_ev, 0.05), min(t.max_uncertainty, 0.07), min(o.max_legs, 4)
            o.w_unc, o.w_corr = 2.5, 1.5
        elif o.risk_profile == "dynamic":
            t.min_ev, t.max_uncertainty = min(t.min_ev, 0.02), max(t.max_uncertainty, 0.13)
            o.w_unc, o.w_ev = 1.0, 1.5
        return self
