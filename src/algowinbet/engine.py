"""Orchestrator: universe -> state at cutoff -> model -> opportunities -> optimizer -> stakes -> explanations."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .calibration import CalibrationSet
from .config import Config
from .domain import Fixture, Opportunity, OpportunityStatus
from .explain import explain_slip
from .models import DixonColes
from .opportunity import FixtureAnalysis, analyze_fixture
from .optimizer import OptimizerResult, optimize
from .risk import assign_stakes
from .state import build_state, history_at


@dataclass
class AnalysisResult:
    cutoff: datetime
    fixtures: list[Fixture]
    analyses: dict[str, FixtureAnalysis]
    opportunities: list[Opportunity]
    optimizer: OptimizerResult
    notes: list[str] = field(default_factory=list)

    @property
    def n_markets(self) -> int:
        return len(self.opportunities)

    def status_counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in OpportunityStatus}
        for o in self.opportunities:
            c[o.status.value] += 1
        return c


class Engine:
    def __init__(self, provider, cfg: Config, calib: CalibrationSet | None = None):
        self.provider, self.cfg = provider, cfg.apply_profile()
        self.calib = calib if calib is not None else CalibrationSet.load(cfg.calibration_path)
        self._models: dict[tuple[str, datetime], tuple[DixonColes, list[DixonColes]] | None] = {}

    def fit(self, competition: str, cutoff: datetime):
        key = (competition, cutoff)
        if key not in self._models:
            hist = history_at(self.provider, competition, cutoff)
            if len(hist) < self.cfg.model.min_history:
                self._models[key] = None
            else:
                m = self.cfg.model
                kw = dict(xi=0.6931471805599453 / m.xi_half_life_days, l2=m.l2)
                model = DixonColes(**kw).fit(hist, cutoff)
                boots = DixonColes.bootstrap(hist, cutoff, m.n_bootstrap, seed=1, **kw) if m.n_bootstrap else []
                self._models[key] = (model, boots)
        return self._models[key]

    def analyze_fixtures(self, fixtures: list[Fixture], cutoff: datetime, markets: set[str] | None = None,
                         notes: list[str] | None = None) -> tuple[dict[str, FixtureAnalysis], list[Opportunity]]:
        analyses: dict[str, FixtureAnalysis] = {}
        opps: list[Opportunity] = []
        for f in fixtures:
            if f.kickoff <= cutoff:
                continue  # already started at cutoff
            fitted = self.fit(f.competition, cutoff)
            if fitted is None:
                if notes is not None:
                    notes.append(f"{f.competition}: storico insufficiente (<{self.cfg.model.min_history} partite), competizione saltata")
                continue
            state = build_state(self.provider, f, cutoff)
            a = analyze_fixture(state, fitted[0], fitted[1], self.cfg, self.calib)
            if markets:
                a.opportunities = [o for o in a.opportunities if o.ref.market_code in markets]
            if a.opportunities:
                analyses[f.id] = a
                opps.extend(a.opportunities)
        return analyses, opps

    def analyze(self, competitions: list[str] | None, start: datetime, end: datetime, cutoff: datetime,
                markets: set[str] | None = None) -> AnalysisResult:
        notes: list[str] = []
        fixtures = self.provider.list_fixtures(competitions, start, end)
        fixtures = [f for f in fixtures if f.kickoff > cutoff]
        analyses, opps = self.analyze_fixtures(fixtures, cutoff, markets, notes)
        notes = sorted(set(notes))
        result = optimize(opps, analyses, self.cfg)
        assign_stakes(result.slips, self.cfg.risk)
        for s in result.slips:
            s.explanation.update(explain_slip(s, self.cfg))
        return AnalysisResult(cutoff, fixtures, analyses, opps, result, notes)
