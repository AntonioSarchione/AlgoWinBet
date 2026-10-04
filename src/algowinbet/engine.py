"""Orchestrator: universe -> state at cutoff -> model (+ lineup impact) -> opportunities -> optimizer -> stakes -> explanations.
Also event-driven selective recalculation (spec 19) and per-fixture information timelines (spec 8)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .calibration import CalibrationSet
from .meta import MetaSet
from .elo import EloTimeline, club_timeline, elo_prior, nation_weights, national_timeline
from .config import Config
from .domain import Fixture, InformationEvent, Opportunity, OpportunityStatus, Player, SelectionRef
from .explain import explain_slip
from .information import PlayerImpactModel, base_rates, resolve
from .markets import family_of, stat_of
from .models import DixonColes
from .opportunity import FixtureAnalysis, analyze_fixture
from .optimizer import OptimizerResult, optimize
from .pricing import payout_ratios
from .risk import assign_stakes
from .models.counts import CountModel
from .state import _dedupe, build_state, history_at, season_start


@dataclass
class AnalysisResult:
    cutoff: datetime
    fixtures: list[Fixture]
    analyses: dict[str, FixtureAnalysis]
    opportunities: list[Opportunity]
    optimizer: OptimizerResult
    notes: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)  # filled by handle_event
    estimated: list[Opportunity] = field(default_factory=list)  # estimated Sisal prices (manual slips only, see analyze)

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


def newcomer_prior(hist, cutoff: datetime, value: float) -> dict[str, tuple[float, float]]:
    """Clubs with no match before the current season in the stored history (promoted sides, cup-only opponents) start
    below average instead of at the league average. Only applies when the history does reach back before this season."""
    start = season_start(cutoff)
    before = {t for r in hist if r.kickoff < start for t in (r.home, r.away)}
    if not before or not value:
        return {}
    return {t: (value, value) for r in hist if r.kickoff >= start for t in (r.home, r.away) if t not in before}


RECENT_XI = 8  # matches that define a player's usual start rate today


def start_rates(roster, lineups, kickoff: dict) -> tuple[dict[str, float], dict[str, float]]:
    """Start rate of every roster player of a team with lineup history: over the whole window (the fit) and over the team's
    last RECENT_XI matches (prediction). A player never in those XI gets 0, not the generic default: a player who left must
    not count as a missing regular."""
    by_team: dict[str, list] = {}
    for l in lineups:
        by_team.setdefault(l.team, []).append(l)
    learned: dict[str, float] = {}
    recent: dict[str, float] = {}
    for team, ls in by_team.items():
        ls = sorted(ls, key=lambda l: kickoff.get(l.fixture_id) or datetime.min.replace(tzinfo=timezone.utc))
        last = ls[-RECENT_XI:]
        mine = [p for p in roster if p.team == team]
        for rates, xs in ((learned, ls), (recent, last)):
            r = {p.id: sum(p.id in l.starters for l in xs) / len(xs) for p in mine}
            # starters outside today's roster (moved, unknown ids) would leave the usual XI short of 11 and make every
            # confirmed XI look stronger than usual: the rates are scaled to 11 starters (each capped at 1)
            tot = sum(r.values())
            k = 11.0 / tot if tot > 0 else 1.0
            rates.update({i: min(1.0, v * k) for i, v in r.items()})
    return learned, recent


class Engine:
    def __init__(self, provider, cfg: Config, calib: CalibrationSet | None = None, use_lineups: bool = True,
                 meta: MetaSet | None = None):
        self.provider, self.cfg = provider, cfg.apply_profile()
        self.meta = meta  # learned model/market blend (meta.py); None = the fixed adaptive weight
        self.calib = calib if calib is not None else CalibrationSet.load(cfg.calibration_path)
        self.use_lineups = use_lineups
        self._models: dict[tuple[str, datetime], tuple[DixonColes, list[DixonColes]] | None] = {}
        self._impact: dict[tuple[str, datetime], PlayerImpactModel | None] = {}
        self._rosters: dict[str, list[Player]] = {}
        self._elo: tuple[EloTimeline | None, EloTimeline | None] | None = None
        self._intl: list | None = None
        self._counts: dict[tuple[str, datetime], CountModel | None] = {}
        self._stat_rows: dict[str, dict[str, tuple[float, float]]] = {}

    # ---------------------------------------------------------------- models
    def roster(self, competition: str) -> list[Player]:
        if competition not in self._rosters:
            fn = getattr(self.provider, "list_players", None)
            self._rosters[competition] = list(fn(competition)) if fn else []
        return self._rosters[competition]

    def roster_by_id(self) -> dict[str, Player]:
        return {p.id: p for c in self._rosters.values() for p in c}

    def fit(self, competition: str, cutoff: datetime):
        m = self.cfg.model
        if m.pooled:
            key = ("*", cutoff)
            if key not in self._models:
                hist = history_at(self.provider, None, cutoff, m.history_seasons)
                rows = nation_weights(hist + self.national_extra(hist, cutoff), m.nation_importance, m.nation_half_life_days)
                self._models[key] = self._fit_hist(rows, cutoff, comp_mu=True)
            pooled = self._models[key]
            if pooled is None or competition not in pooled[0].mu_comp:
                return None  # no history for this competition at all: no model, no bet
            model, boots = pooled
            return model.for_competition(competition), [b.for_competition(competition) for b in boots]
        key = (competition, cutoff)
        if key not in self._models:
            self._models[key] = self._fit_hist(history_at(self.provider, competition, cutoff, m.history_seasons), cutoff, comp_mu=False)
        return self._models[key]

    def count_model(self, stat: str, cutoff: datetime) -> CountModel | None:
        """Corners / cards model on every match with that statistic known at cutoff (same seasons and decay as goals)."""
        key = (stat, cutoff)
        if key not in self._counts:
            m = self.cfg.model
            fn = getattr(self.provider, "stat_counts", None)
            if stat not in self._stat_rows:
                self._stat_rows[stat] = fn(stat) if fn else {}
            counts = self._stat_rows[stat]
            rows = [r.model_copy(update={"home_goals": int(counts[r.fixture_id][0]), "away_goals": int(counts[r.fixture_id][1])})
                    for r in history_at(self.provider, None, cutoff, m.history_seasons) if r.fixture_id in counts]
            o = m.stat_options.get(stat, {})
            lh = o.get("level_half_life_days")
            cm = CountModel(stat, xi=math.log(2) / m.xi_half_life_days, l2=o.get("l2", m.l2), shared=bool(o.get("shared", False)),
                            level_xi=math.log(2) / lh if lh else None)
            self._counts[key] = cm.fit(rows, cutoff) if len(rows) >= m.stat_min_history else None
        return self._counts[key]

    def stat_matrices(self, f: Fixture, cutoff: datetime) -> dict[str, tuple]:
        """statistic -> (count matrix, matches of the less-known team) for one match, for the statistics with a model."""
        out = {}
        for stat in self.cfg.model.stat_models:
            cm = self.count_model(stat, cutoff)
            if cm is not None and cm.knows(f.home) and cm.knows(f.away) and cm.sample_size(f.home, f.away) >= self.cfg.model.stat_min_team_matches:
                out[stat] = (cm.matrix(f.home, f.away, f.competition, getattr(f, "neutral", False)), cm.sample_size(f.home, f.away))
        return out

    def elo(self) -> tuple[EloTimeline | None, EloTimeline | None]:
        """(clubs, national teams) rating timelines, built once: lookups read the rating known at the cutoff."""
        if self._elo is None:
            m = self.cfg.model
            clubs = nations = None
            if m.club_elo_per_100:
                from .domain import utc
                clubs = club_timeline(self.provider.list_history(None, utc(2100, 1, 1)))
            if m.nation_elo_per_100:
                from .names import TeamNames
                fn = getattr(self.provider, "international_results", None)
                body = fn() if fn else None
                nations = national_timeline(body, TeamNames.load(m.aliases_path), m.nation_k) if body else None
            self._elo = (clubs, nations)
        return self._elo

    def international(self) -> list:
        if self._intl is None:
            fn = getattr(self.provider, "international_results", None)
            body = fn() if fn else None
            if body:
                from .elo import international_results
                from .names import TeamNames
                self._intl = international_results(body, TeamNames.load(self.cfg.model.aliases_path))
            else:
                self._intl = []
        return self._intl

    def national_extra(self, hist, cutoff: datetime) -> list:
        """International results of the national teams in our history (pooled model only): a national side plays a few
        competitive matches a year in our feed, ten or more in all."""
        years = self.cfg.model.national_history_years
        intl = self.international() if years else []
        if not intl:
            return []
        from .elo import national_history
        known = {t for r in intl for t in (r.home, r.away)}
        nations = {t for r in hist for t in (r.home, r.away) if t in known}
        return national_history(intl, hist, nations, cutoff - timedelta(days=365.25 * years), cutoff)

    def _fit_hist(self, hist, cutoff: datetime, comp_mu: bool):
        m = self.cfg.model
        if len(hist) < m.min_history:
            return None
        kw = dict(xi=math.log(2) / m.xi_half_life_days, l2=m.l2, comp_mu=comp_mu, l2_comp_home=m.l2_comp_home, l2_comp_mu=m.l2_comp_mu)
        prior = newcomer_prior(hist, cutoff, m.newcomer_prior)
        if m.club_elo_per_100 or m.nation_elo_per_100:
            clubs, nations = self.elo()
            teams = {t for r in hist for t in (r.home, r.away)}
            prior.update(elo_prior(teams, cutoff, clubs, nations, m.club_elo_per_100, m.nation_elo_per_100))
        model = DixonColes(**kw).fit(hist, cutoff, prior=prior)
        boots = DixonColes.bootstrap(hist, cutoff, m.n_bootstrap, seed=1, **kw) if m.n_bootstrap else []
        return model, boots

    def impact(self, competition: str, cutoff: datetime) -> PlayerImpactModel | None:
        if not self.use_lineups or self.cfg.model.lineup_impact == "off":
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
                learned, recent = start_rates(roster, lus, {r.fixture_id: r.kickoff for r in hist})
                imp = PlayerImpactModel(roster, base_rates(roster, learned))
                model = fitted[0]
                offsets = {}
                for r in hist:
                    lh, la = model.expected_goals(r.home, r.away)
                    offsets[(r.fixture_id, "home")], offsets[(r.fixture_id, "away")] = math.log(lh), math.log(la)
                if self.cfg.model.lineup_impact == "learned":
                    imp.fit(lus, hist, offsets)
                imp.set_current(base_rates(roster, recent)).scale = self.cfg.model.lineup_scale
                self._impact[key] = imp
        return self._impact[key]

    # -------------------------------------------------------------- analysis
    def analyze_fixtures(self, fixtures: list[Fixture], cutoff: datetime, markets: set[str] | None = None,
                         notes: list[str] | None = None, model_cutoff: datetime | None = None,
                         extra_events: list[InformationEvent] | None = None,
                         estimate: dict[str, float] | None = None) -> tuple[dict[str, FixtureAnalysis], list[Opportunity]]:
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
            a = analyze_fixture(state, fitted[0], fitted[1], self.cfg, self.calib, self.impact(f.competition, mc), roster, self.meta,
                                stats=self.stat_matrices(f, mc) if any(stat_of(q.market_code) for q in state.quotes) else None,
                                estimate=estimate)
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
        estimated = self._estimate_unquoted(fixtures, analyses, cutoff, markets, notes)
        res = self._finish(cutoff, fixtures, analyses, opps, notes)
        res.estimated = estimated
        return res

    def _estimate_unquoted(self, fixtures, analyses, cutoff, markets, notes) -> list[Opportunity]:
        """Second pass over the matches no bet bookmaker prices: estimated prices from the usual payout of the priced ones.
        Kept apart from the opportunities, so they never reach the automatic slips or the registry."""
        if not (self.cfg.bet_bookmakers and self.cfg.estimate_unquoted):
            return []
        missing = [f for f in fixtures if f.id not in analyses]
        ratios = payout_ratios([s for a in analyses.values() for s in a.payouts])
        if not missing or not ratios:
            return []
        _, est = self.analyze_fixtures(missing, cutoff, markets, None, estimate=ratios)
        if est:
            notes.append(f"quote Sisal stimate da Pinnacle per {len({o.fixture_id for o in est})} partite non quotate da Sisal sul feed "
                         f"(rendimento Sisal abituale {ratios['*']:.1%} della quota equa)")
        return est

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
