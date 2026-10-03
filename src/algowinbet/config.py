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
    # national teams (2026-10-02): Elo K per tournament class (user's table; model-eval: same scores as the old one); in the
    # goal model each national match weighs importance x 0.5^(age / nation_half_life_days). model-eval 2026-10-02, 190
    # internationals: importance off (None) and a 3-year half-life is best on 1X2 / O2.5 / GG (0.8370 / 0.6600 / 0.6766, was
    # 0.8424 / 0.6635 / 0.6779); importance on (1 / 0.95 / 0.7 / 0.25 / 0.1) was worse on all three (0.8452 / 0.6684 / 0.6845)
    nation_k: dict[str, float] = Field(default_factory=lambda: {
        "world_cup": 65.0, "continental": 60.0, "qualification": 45.0, "other": 20.0, "friendly": 10.0})
    nation_importance: dict[str, float] | None = None
    nation_half_life_days: float | None = 1095.0
    # Fase 7: corners and cards from count models (models/counts.py); a statistic with fewer matches than this is not priced
    # stat-eval 2026-10-03 (26 weeks, 852 league matches): corners beat the league average (1X2 corners log loss 0.888 vs 0.929,
    # Over 9.5 0.687 vs 0.695).
    # Cards (Sisal: yellow 1, red 1, second yellow before a red not counted; what we store): stat-eval 2026-10-03 over 52 weeks
    # (2,322 matches) with a shared match factor, a 60-day level and 5x shrinkage: 1X2 cards 1.0605 vs 1.0698 for the league
    # average (clear), totals no better than the league average (O3.5 0.6694 vs 0.6728, O4.5 0.6422 vs 0.6436, O5.5 0.5220
    # vs 0.5224): only the 1X2 is priced; totals wait for the referee.
    stat_models: list[str] = Field(default_factory=lambda: ["corners", "cards"])
    stat_options: dict[str, dict] = Field(default_factory=lambda: {"cards": {"shared": True, "level_half_life_days": 60.0, "l2": 5.0}})
    stat_markets_off: list[str] = Field(default_factory=lambda: ["CARDS_TOTAL", "CARDS_TEAM_HOME", "CARDS_TEAM_AWAY"])
    stat_min_history: int = 300
    l2_comp_home: float | None = None
    l2_comp_mu: float = 0.0
    aliases_path: str = "configs/team_aliases.json"  # team names shared by our feeds and the international results
    version: str = "dc-poisson-v5"  # v2: national-team Elo prior; v3: + 4 years of international results; v4: national
    # matches weighted by tournament importance (Elo K and goal model); v5: Elo K table, national half-life 3 years. Not bumped for corners / cards: the meta-model
    # and calibration are keyed by this version and fitted on the goal model, which those statistics do not touch


class EnsembleCfg(BaseModel):
    """mode=adaptive: the devigged market price is the prior; the structural model is noisy evidence.
    w_struct = tau^2/(tau^2+sigma^2), tau = how far the true prob may sit from the market price (market_prior_sd),
    sigma = model error (bootstrap or sqrt(p(1-p)/n) proxy, measured on synthetic data: rmse ~ sqrt(pq/n_matches)).
    mode=fixed: constant w_struct (learn it with `backtest`, column w_struct*)."""

    mode: str = "adaptive"
    market_prior_sd: float = 0.025
    w_struct: float = 0.5  # used only in fixed mode
    devig_method: str = "power"
    use_meta: bool = True  # live analysis: the meta-model fitted by the weekly quality run, when there is one
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
    # "Equa": a likely selection (>= min_probability) priced against a sharp reference whose EV at Sisal is at least fair_ev:
    # not a bet on its own, but in a slip it costs (almost) no margin, and the Sisal multiple bonus can turn the slip positive
    fair_ev: float = -0.02


class OptimizerCfg(BaseModel):
    odds_min: float = 2.0
    odds_max: float = 15.0
    min_probability: float = 0.0  # min joint probability of slip
    max_legs: int = 8
    min_legs: int = 1  # fewer selections than this: not a valid slip (e.g. 5 to always reach the Sisal multiple bonus)
    max_legs_per_fixture: int = 1  # SGP needs a bookmaker combo quote; see joint pricing
    max_legs_per_competition: int = 99
    min_leg_probability: float = 0.0
    # no selection under these odds enters a slip (user's rule, 2026-10-03): it adds noise and almost nothing to the slip
    min_leg_odds: float = 1.2
    beam_width: int = 200
    output_count: int = 3
    max_overlap: float = 0.5  # Jaccard overlap allowed between output slips
    correlation_limit: float = 0.25
    min_slip_ev: float = 0.0
    risk_profile: str = "balanced"  # conservative | balanced | dynamic
    cross_match_rho: float = 0.005
    same_competition_rho: float = 0.02
    include_watch: bool = False
    include_fair: bool = True  # "Equa" selections may enter slips (the slip itself must still clear min_slip_ev)
    # Sisal "Bonus Multipla": +x% on the net winnings of a multiple with >= 5 selections, each at odds >= 1.25
    # (5 -> 4%, 6 -> 8%, ... 30 -> 177%), index 0 = 5 selections
    multi_bonus: list[float] = Field(default_factory=lambda: [
        0.04, 0.08, 0.12, 0.17, 0.22, 0.27, 0.32, 0.37, 0.42, 0.48, 0.54, 0.60, 0.67, 0.73, 0.80, 0.87, 0.95, 1.03, 1.11, 1.19,
        1.28, 1.37, 1.46, 1.56, 1.67, 1.77])
    multi_bonus_min_odds: float = 1.25
    # Prudent EV: share of the model's edge over the sharp market price that is trusted (1 = all of it, 0 = none: the
    # market is assumed right whenever the model is more optimistic). Set from the weekly replay: the edges of the selections
    # picked in the past, compared with how often they won (picking the best of thousands inflates their edges).
    edge_shrink: float = 1.0
    # Slip profiles shown together on the dashboard: objective weights per profile (every profile keeps the same hard limits,
    # minimum slip EV included). The published slips and the stakes use `profile`.
    profile: str = "equilibrata"
    profiles: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "probabilita": {"w_ev": 0.5, "w_prob": 3.0, "w_unc": 1.0},
        "equilibrata": {},
        "value": {"w_ev": 2.0, "w_prob": 0.1},
    })
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

    def with_slip_profile(self, name: str) -> "Config":
        """A copy whose optimizer weights are those of slip profile `name` (Massima probabilità, Equilibrata, Value)."""
        c = self.model_copy(deep=True)
        for k, v in self.optimizer.profiles.get(name, {}).items():
            setattr(c.optimizer, k, v)
        c.optimizer.profile = name
        return c

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
