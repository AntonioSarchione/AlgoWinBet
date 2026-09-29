"""Walk-forward backtest with paper bets (spec 20). Each round is predicted at cutoff = first kickoff - 24h using only
information observable then (history, opening quotes); closing quotes are used only afterwards for CLV."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from .calibration import brier, ece, fit_ensemble_weight, log_loss
from .config import Config
from .engine import Engine
from .markets import family_of, outcome
from .optimizer import optimize
from .pricing import build_market_views


@dataclass
class BacktestReport:
    n_rounds: int = 0
    n_predictions: int = 0
    families: dict[str, dict] = field(default_factory=dict)
    single: dict = field(default_factory=dict)
    all_bets: dict = field(default_factory=dict)
    slips: dict = field(default_factory=dict)
    curve: list[float] = field(default_factory=list)
    calibration_data: dict[str, tuple[list[float], list[float]]] = field(default_factory=dict)


def _max_drawdown(curve: list[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def run_backtest(provider, cfg: Config, competitions: list[str] | None = None, max_rounds: int | None = None,
                 markets: set[str] | None = None) -> BacktestReport:
    engine = Engine(provider, cfg)
    finished = provider.list_history(competitions, provider_horizon(provider))
    if not finished:
        return BacktestReport()
    groups: dict[tuple, list] = defaultdict(list)
    for r in finished:
        iso = r.kickoff.isocalendar()
        groups[(iso.year, iso.week)].append(r)
    rounds = [groups[k] for k in sorted(groups)]

    rep = BacktestReport()
    fam_rows: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)  # p_s, p_m, p_f, y
    single_pnl, all_pnl, clvs, evs, slip_pnl = [], [], [], [], []
    hits = 0
    slip_hits = 0
    no_bet_rounds = 0
    used = 0
    for grp in rounds:
        if max_rounds and used >= max_rounds:
            break
        first = min(r.kickoff for r in grp)
        cutoff = first - timedelta(hours=24)
        fx = []
        res_by_fid = {r.fixture_id: r for r in grp}
        for comp in {r.competition for r in grp}:
            fx += [f for f in provider.list_fixtures([comp], first, max(r.kickoff for r in grp)) if f.id in res_by_fid]
        analyses, opps = engine.analyze_fixtures(fx, cutoff, markets)
        if not opps:
            continue
        used += 1
        for o in opps:
            res = res_by_fid[o.fixture_id]
            y = 1.0 if outcome(res.home_goals, res.away_goals, o.ref) else 0.0
            fam = family_of(o.ref.market_code)
            if o.p_market is not None:
                fam_rows[fam].append((o.p_struct, o.p_market, o.p_final, y))
            pnl = (o.odds - 1) if y else -1.0
            all_pnl.append(pnl)
            if o.status.value in ("STRONG", "CANDIDATE"):
                single_pnl.append(pnl)
                evs.append(o.ev)
                hits += int(y)
                closing = build_market_views([q for q in provider.get_quotes(o.fixture_id) if q.observed_at > cutoff],
                                             cfg.ensemble.devig_method)
                cv = next((v for v in closing if v.ref == o.ref and v.p_market is not None), None)
                if cv:
                    clvs.append(cv.p_market * o.odds - 1)
        # slips for this round (paper, flat 1 unit)
        opt = optimize(opps, analyses, cfg)
        if opt.no_bet:
            no_bet_rounds += 1
        for s in opt.slips:
            win = all(outcome(res_by_fid[l.fixture_id].home_goals, res_by_fid[l.fixture_id].away_goals, l.ref) for l in s.legs)
            slip_pnl.append((s.total_odds - 1) if win else -1.0)
            slip_hits += int(win)
    rep.n_rounds = used
    for fam, rows in fam_rows.items():
        a = np.array(rows)
        ps, pm, pf, y = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        w, _ = fit_ensemble_weight(ps, pm, y)
        rep.families[fam] = {
            "n": len(rows), "brier_struct": brier(ps, y), "brier_market": brier(pm, y), "brier_final": brier(pf, y),
            "logloss_struct": log_loss(ps, y), "logloss_market": log_loss(pm, y), "logloss_final": log_loss(pf, y),
            "ece_final": ece(pf, y), "best_w_struct": w,
        }
        rep.calibration_data[fam] = (pf.tolist(), y.tolist())
        rep.n_predictions += len(rows)
    curve = np.cumsum(single_pnl).tolist() if single_pnl else []
    rep.curve = curve
    rep.single = {
        "n_bets": len(single_pnl), "hit_rate": hits / len(single_pnl) if single_pnl else float("nan"),
        "roi": float(np.mean(single_pnl)) if single_pnl else float("nan"),
        "mean_predicted_ev": float(np.mean(evs)) if evs else float("nan"),
        "mean_clv": float(np.mean(clvs)) if clvs else float("nan"), "max_drawdown_units": _max_drawdown(curve),
    }
    rep.all_bets = {"n": len(all_pnl), "roi": float(np.mean(all_pnl)) if all_pnl else float("nan")}
    rep.slips = {"n": len(slip_pnl), "hits": slip_hits, "roi": float(np.mean(slip_pnl)) if slip_pnl else float("nan"),
                 "no_bet_rounds": no_bet_rounds}
    return rep


def provider_horizon(provider):
    from .domain import utc
    return getattr(provider, "as_of", utc(2100, 1, 1))


@dataclass
class InfoValueReport:
    """Spec 10 'information value': does adding news / official lineups improve the structural probabilities?"""
    stages: dict[str, dict[str, dict]] = field(default_factory=dict)  # stage -> family -> metrics
    n_rounds: int = 0


def run_info_value(provider, cfg: Config, competitions: list[str] | None = None,
                   stages: list[tuple[str, timedelta]] | None = None, max_rounds: int | None = None) -> InfoValueReport:
    stages = stages or [("T-24h (news)", timedelta(hours=24)), ("T-30m (lineups)", timedelta(minutes=30))]
    blind, aware = Engine(provider, cfg, use_lineups=False), Engine(provider, cfg, use_lineups=True)
    finished = provider.list_history(competitions, provider_horizon(provider))
    groups: dict[tuple, list] = defaultdict(list)
    for r in finished:
        iso = r.kickoff.isocalendar()
        groups[(iso.year, iso.week)].append(r)
    rep = InfoValueReport()
    rows: dict[str, dict[str, list[tuple[float, float, float, float]]]] = {n: defaultdict(list) for n, _ in stages}
    used = 0
    for k in sorted(groups):
        grp = groups[k]
        if max_rounds and used >= max_rounds:
            break
        first = min(r.kickoff for r in grp)
        res = {r.fixture_id: r for r in grp}
        fx = []
        for comp in {r.competition for r in grp}:
            fx += [f for f in provider.list_fixtures([comp], first, max(r.kickoff for r in grp)) if f.id in res]
        got = False
        for name, off in stages:
            cutoff = first - off
            _, ob = blind.analyze_fixtures(fx, cutoff)
            _, oa = aware.analyze_fixtures(fx, cutoff)
            blind_by = {(o.fixture_id, o.ref.key): o for o in ob}
            for o in oa:
                b = blind_by.get((o.fixture_id, o.ref.key))
                if b is None or o.p_market is None:
                    continue
                r = res[o.fixture_id]
                y = 1.0 if outcome(r.home_goals, r.away_goals, o.ref) else 0.0
                rows[name][family_of(o.ref.market_code)].append((b.p_struct, o.p_struct, o.p_market, y))
                got = True
        used += int(got)
    rep.n_rounds = used
    for name, fams in rows.items():
        rep.stages[name] = {}
        for fam, lst in fams.items():
            a = np.array(lst)
            pb, pa, pm, y = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
            w_b, _ = fit_ensemble_weight(pb, pm, y)
            w_a, _ = fit_ensemble_weight(pa, pm, y)
            rep.stages[name][fam] = {
                "n": len(lst), "brier_blind": brier(pb, y), "brier_aware": brier(pa, y), "brier_market": brier(pm, y),
                "logloss_blind": log_loss(pb, y), "logloss_aware": log_loss(pa, y), "logloss_market": log_loss(pm, y),
                "w_struct_blind": w_b, "w_struct_aware": w_a,
                "mean_abs_shift": float(np.mean(np.abs(pa - pb))),
            }
    return rep


@dataclass
class StaleReport:
    n_rounds: int = 0
    n_bets: int = 0
    hit_rate: float = float("nan")
    roi: float = float("nan")
    mean_predicted_ev: float = float("nan")
    mean_clv: float = float("nan")
    baseline_roi: float = float("nan")  # same universe (stale-flagged), no EV filter
    baseline_n: int = 0


def run_stale_backtest(provider, cfg: Config, competitions: list[str] | None = None, minutes_after_lineup: int = 5,
                       max_rounds: int | None = None) -> StaleReport:
    """Bet, at (lineup publication + minutes_after_lineup), the selections whose last quote predates the official XI and
    whose model-updated EV clears the threshold. Settles on results; CLV is measured against the later closing quotes.
    Tests the one place where fast information can legitimately beat a slow price."""
    engine = Engine(provider, cfg)
    finished = provider.list_history(competitions, provider_horizon(provider))
    groups: dict[tuple, list] = defaultdict(list)
    for r in finished:
        iso = r.kickoff.isocalendar()
        groups[(iso.year, iso.week)].append(r)
    t = cfg.thresholds
    pnl, evs, clvs, base = [], [], [], []
    hits, used = 0, 0
    for k in sorted(groups):
        grp = groups[k]
        if max_rounds and used >= max_rounds:
            break
        first = min(r.kickoff for r in grp)
        cutoff = first - timedelta(minutes=75 - minutes_after_lineup)
        res = {r.fixture_id: r for r in grp}
        fx = []
        for comp in {r.competition for r in grp}:
            fx += [f for f in provider.list_fixtures([comp], first, max(r.kickoff for r in grp)) if f.id in res]
        # visible quotes must exclude the closing snapshot: enforced by the cutoff (close is observed at kickoff-1h)
        _, opps = engine.analyze_fixtures(fx, cutoff)
        used += 1
        for o in opps:
            if not o.odds_stale or o.p_market is None:
                continue
            r = res[o.fixture_id]
            y = 1.0 if outcome(r.home_goals, r.away_goals, o.ref) else 0.0
            p = (o.odds - 1) if y else -1.0
            base.append(p)
            if o.ev >= t.min_ev and o.uncertainty <= t.max_uncertainty and o.data_quality >= t.min_dq_candidate:
                pnl.append(p)
                evs.append(o.ev)
                hits += int(y)
                closing = build_market_views([q for q in provider.get_quotes(o.fixture_id) if q.observed_at > cutoff],
                                             cfg.ensemble.devig_method)
                cv = next((v for v in closing if v.ref == o.ref and v.p_market is not None), None)
                if cv:
                    clvs.append(cv.p_market * o.odds - 1)
    rep = StaleReport(n_rounds=used, n_bets=len(pnl), baseline_n=len(base))
    if pnl:
        rep.hit_rate, rep.roi, rep.mean_predicted_ev = hits / len(pnl), float(np.mean(pnl)), float(np.mean(evs))
    if clvs:
        rep.mean_clv = float(np.mean(clvs))
    if base:
        rep.baseline_roi = float(np.mean(base))
    return rep
