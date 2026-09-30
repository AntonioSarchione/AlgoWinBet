"""Orchestrator: universe -> state at cutoff -> model (+ lineup impact) -> opportunities -> optimizer -> stakes -> explanations.
Also event-driven selective recalculation (spec 19) and per-fixture information timelines (spec 8)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .calibration import CalibrationSet
from .config import Config
from .domain import Fixture, InformationEvent, Opportunity, OpportunityStatus, Player, SelectionRef
from .explain import explain_slip
from .information import PlayerImpactModel, base_rates, resolve
from .markets import family_of
from .models import DixonColes
from .opportunity import FixtureAnalysis, analyze_fixture
from .optimizer import OptimizerResult, optimize
from .risk import assign_stakes
from .state import _dedupe, build_state, history_at


@dataclass
class AnalysisResult:
    cutoff: datetime
    fixtures: list[Fixture]
    analyses: dict[str, FixtureAnalysis]
    opportunities: list[Opportunity]
    optimizer: OptimizerResult
    notes: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)  # filled by handle_event

    @property
    def n_markets(self) -> int:
        return len(self.opportunities)

    def status_counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in OpportunityStatus}
        for o in self.opportunities:
            c[o.status.value] += 1
        return c


@dataclass
class TimelineEntry:
    cutoff: datetime
    label: str
    lineup_state: str
    expected_goals: tuple[float, float]
    lambda_shift: tuple[float, float]
    stale_quotes: bool
    rows: dict[str, dict]  # ref.key -> {p_struct, p_blind, p_final, odds, ev, status}
    notes: list[str]


class Engine:
    def __init__(self, provider, cfg: Config, calib: CalibrationSet | None = None, use_lineups: bool = True):
        self.provider, self.cfg = provider, cfg.apply_profile()
        self.calib = calib if calib is not None else CalibrationSet.load(cfg.calibration_path)
        self.use_lineups = use_lineups
        self._models: dict[tuple[str, datetime], tuple[DixonColes, list[DixonColes]] | None] = {}
        self._impact: dict[tuple[str, datetime], PlayerImpactModel | None] = {}
        self._rosters: dict[str, list[Player]] = {}

    # ---------------------------------------------------------------- models
    def roster(self, competition: str) -> list[Player]:
        if competition not in self._rosters:
            fn = getattr(self.provider, "list_players", None)
            self._rosters[competition] = list(fn(competition)) if fn else []
        return self._rosters[competition]

    def roster_by_id(self) -> dict[str, Player]:
        return {p.id: p for c in self._rosters.values() for p in c}

    def fit(self, competition: str, cutoff: datetime):
        key = (competition, cutoff)
        if key not in self._models:
            hist = history_at(self.provider, competition, cutoff, self.cfg.model.history_seasons)
            if len(hist) < self.cfg.model.min_history:
                self._models[key] = None
            else:
                m = self.cfg.model
                kw = dict(xi=math.log(2) / m.xi_half_life_days, l2=m.l2)
                model = DixonColes(**kw).fit(hist, cutoff)
                boots = DixonColes.bootstrap(hist, cutoff, m.n_bootstrap, seed=1, **kw) if m.n_bootstrap else []
                self._models[key] = (model, boots)
        return self._models[key]

    def impact(self, competition: str, cutoff: datetime) -> PlayerImpactModel | None:
        if not self.use_lineups:
            return None
        key = (competition, cutoff)
        if key not in self._impact:
            roster = self.roster(competition)
            fitted = self.fit(competition, cutoff)
            if not roster or fitted is None:
                self._impact[key] = None
            else:
                hist = history_at(self.provider, competition, cutoff, self.cfg.model.history_seasons)
                hist_ids = {r.fixture_id for r in hist}
                fn = getattr(self.provider, "list_lineup_history", None)
                lus = [l for l in (fn(competition, cutoff) if fn else []) if l.fixture_id in hist_ids]
                team_matches: dict[str, int] = {}
                starts: dict[str, int] = {}
                for l in lus:
                    team_matches[l.team] = team_matches.get(l.team, 0) + 1
                    for pid in l.starters:
                        starts[pid] = starts.get(pid, 0) + 1
                team_of = {p.id: p.team for p in roster}
                learned = {pid: n / team_matches[team_of[pid]] for pid, n in starts.items()
                           if pid in team_of and team_matches.get(team_of[pid])}
                imp = PlayerImpactModel(roster, base_rates(roster, learned))
                model = fitted[0]
                offsets = {}
                for r in hist:
                    lh, la = model.expected_goals(r.home, r.away)
                    offsets[(r.fixture_id, "home")], offsets[(r.fixture_id, "away")] = math.log(lh), math.log(la)
                self._impact[key] = imp.fit(lus, hist, offsets)
        return self._impact[key]

    # -------------------------------------------------------------- analysis
    def analyze_fixtures(self, fixtures: list[Fixture], cutoff: datetime, markets: set[str] | None = None,
                         notes: list[str] | None = None, model_cutoff: datetime | None = None,
                         extra_events: list[InformationEvent] | None = None) -> tuple[dict[str, FixtureAnalysis], list[Opportunity]]:
        analyses: dict[str, FixtureAnalysis] = {}
        opps: list[Opportunity] = []
        mc = model_cutoff or cutoff
        for f in fixtures:
            if f.kickoff <= cutoff:
                continue  # already started at cutoff
            fitted = self.fit(f.competition, mc)
            if fitted is None:
                if notes is not None:
                    notes.append(f"{f.competition}: storico insufficiente (<{self.cfg.model.min_history} partite), competizione saltata")
                continue
            roster = self.roster(f.competition) if self.use_lineups else []
            state = build_state(self.provider, f, cutoff, roster, quote_window_hours=self.cfg.ensemble.quote_window_hours)
            if extra_events:
                state.events = _dedupe(state.events + [e for e in extra_events if e.observed_at <= cutoff])
            a = analyze_fixture(state, fitted[0], fitted[1], self.cfg, self.calib, self.impact(f.competition, mc), roster)
            if markets:
                a.opportunities = [o for o in a.opportunities if o.ref.market_code in markets]
            if a.opportunities:
                analyses[f.id] = a
                opps.extend(a.opportunities)
        return analyses, opps

    def _finish(self, cutoff, fixtures, analyses, opps, notes, changes=None) -> AnalysisResult:
        result = optimize(opps, analyses, self.cfg)
        assign_stakes(result.slips, self.cfg.risk)
        for s in result.slips:
            s.explanation.update(explain_slip(s, self.cfg))
        return AnalysisResult(cutoff, fixtures, analyses, opps, result, sorted(set(notes)), changes or [])

    def analyze(self, competitions: list[str] | None, start: datetime, end: datetime, cutoff: datetime,
                markets: set[str] | None = None) -> AnalysisResult:
        notes: list[str] = []
        fixtures = [f for f in self.provider.list_fixtures(competitions, start, end) if f.kickoff > cutoff]
        analyses, opps = self.analyze_fixtures(fixtures, cutoff, markets, notes)
        return self._finish(cutoff, fixtures, analyses, opps, notes)

    # ----------------------------------------------------- event-driven update
    def handle_event(self, prev: AnalysisResult, event: InformationEvent, new_cutoff: datetime | None = None) -> AnalysisResult:
        """Selective recalculation: only fixtures of the affected team/fixture and only the market families routed for the event."""
        new_cutoff = new_cutoff or max(event.observed_at, prev.cutoff)
        route = resolve(event, self.roster_by_id())
        affected = [f for f in prev.fixtures if (event.fixture_id and f.id == event.fixture_id)
                    or (not event.fixture_id and event.team in (f.home, f.away))]
        notes = list(prev.notes)
        new_an, new_opps = self.analyze_fixtures(affected, new_cutoff, None, notes, model_cutoff=prev.cutoff, extra_events=[event])
        aff_ids = {f.id for f in affected}
        old = {(o.fixture_id, o.ref.key): o for o in prev.opportunities}
        merged: list[Opportunity] = [o for o in prev.opportunities if o.fixture_id not in aff_ids]
        changes = []
        for o in new_opps:
            k = (o.fixture_id, o.ref.key)
            if family_of(o.ref.market_code) in route.affected or k not in old:
                merged.append(o)
                if k in old:
                    b = old[k]
                    if abs(o.p_final - b.p_final) > 1e-9 or o.status != b.status:
                        changes.append({"fixture": f"{o.home} - {o.away}", "market": o.description, "p_before": b.p_final,
                                        "p_after": o.p_final, "ev_before": b.ev, "ev_after": o.ev,
                                        "status_before": b.status.value, "status_after": o.status.value,
                                        "priority": "high" if family_of(o.ref.market_code) in route.high else "monitor"})
            else:
                merged.append(old[k])
        analyses = {fid: a for fid, a in prev.analyses.items() if fid not in aff_ids}
        analyses.update(new_an)
        res = self._finish(new_cutoff, prev.fixtures, analyses, merged, notes, changes)
        res.notes.append(f"evento {event.event_type} ({route.reason}): ricalcolate famiglie {sorted(route.affected) or 'nessuna'}")
        return res

    # ---------------------------------------------------------------- timeline
    def timeline(self, fixture: Fixture, offsets: list[timedelta] | None = None, refs: list[SelectionRef] | None = None,
                 model_cutoff: datetime | None = None) -> list[TimelineEntry]:
        offsets = offsets or [timedelta(hours=72), timedelta(hours=24), timedelta(hours=6), timedelta(minutes=75), timedelta(minutes=30)]
        refs = refs or [SelectionRef(market_code="MATCH_1X2", selection=s) for s in ("HOME", "DRAW", "AWAY")] + [
            SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5), SelectionRef(market_code="BTTS", selection="YES")]
        keys = {r.key for r in refs}
        mc = model_cutoff or (fixture.kickoff - max(offsets) - timedelta(hours=1))
        out = []
        for off in sorted(offsets, reverse=True):
            cutoff = fixture.kickoff - off
            an, opps = self.analyze_fixtures([fixture], cutoff, model_cutoff=mc)
            a = an.get(fixture.id)
            if not a:
                continue
            rows = {o.ref.key: {"desc": o.description, "p_struct": o.p_struct, "p_blind": o.p_struct_blind, "p_final": o.p_final,
                                "p_market": o.p_market, "odds": o.odds, "ev": o.ev, "status": o.status.value, "stale": o.odds_stale}
                    for o in a.opportunities if o.ref.key in keys}
            label = f"T-{int(off.total_seconds() // 3600)}h" if off >= timedelta(hours=3) else f"T-{int(off.total_seconds() // 60)}m"
            out.append(TimelineEntry(cutoff, label, a.state.lineup_state, a.expected_goals,
                                     (a.adjustment.d_home, a.adjustment.d_away),
                                     any(o.odds_stale for o in a.opportunities), rows, a.opportunities[0].lineup_notes if a.opportunities else []))
        return out
