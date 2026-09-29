"""CLI: `algowinbet analyze|backtest|combo`. Paper-only: nothing here places bets."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .backtest import run_backtest
from .calibration import CalibrationSet, PlattCalibrator
from .config import Config
from .domain import Opportunity
from .engine import AnalysisResult, Engine
from .providers import FootballDataCSV, ManualOverlay, MockProvider
from .store import Store


def _provider(a):
    if a.provider == "mock":
        p = MockProvider(seed=a.seed, bias_over=a.mock_bias_over, open_noise=a.mock_noise, close_noise=a.mock_noise / 2)
    elif a.provider == "csv":
        if not a.csv:
            sys.exit("--csv richiesto (uno o più file football-data.co.uk, scaricati a mano)")
        p = FootballDataCSV(a.csv)
    else:
        sys.exit(f"provider sconosciuto {a.provider}")
    if a.manual:
        p = ManualOverlay(p, a.manual)
    return p


def _cfg(a) -> Config:
    cfg = Config.load(a.config)
    o = cfg.optimizer
    for name in ("odds_min", "odds_max", "min_probability", "max_legs", "output_count", "risk_profile"):
        v = getattr(a, name, None)
        if v is not None:
            setattr(o, name, v)
    if getattr(a, "min_ev", None) is not None:
        cfg.thresholds.min_ev = a.min_ev
    if getattr(a, "w_struct", None) is not None:
        cfg.ensemble.w_struct, cfg.ensemble.mode = a.w_struct, "fixed"
    if getattr(a, "market_prior_sd", None) is not None:
        cfg.ensemble.market_prior_sd = a.market_prior_sd
    if getattr(a, "bootstrap", None):
        cfg.model.n_bootstrap = a.bootstrap
    if getattr(a, "bankroll", None):
        cfg.risk.bankroll = a.bankroll
    if getattr(a, "include_watch", False):
        o.include_watch = True
    return cfg


def _fmt_leg(o: Opportunity) -> str:
    edge = f"{o.edge:+.1%}" if o.edge is not None else "n/d"
    return (f"  {o.kickoff:%d/%m %H:%M} {o.home} - {o.away} [{o.competition}]\n"
            f"      {o.description:<32} quota {o.odds:5.2f} ({o.bookmaker}, {o.n_books} book)  "
            f"P {o.p_final:.1%} [{o.p_low:.1%}-{o.p_high:.1%}]  edge {edge}  EV {o.ev:+.1%}  "
            f"DQ {o.data_quality:.0%}  {o.status.value}")


def print_result(r: AnalysisResult, cfg: Config) -> None:
    opt = r.optimizer
    print(f"\nAnalisi al {r.cutoff:%d/%m/%Y %H:%M} UTC — {len(r.fixtures)} partite, {r.n_markets} mercati valutati")
    print("Stati opportunità: " + ", ".join(f"{k} {v}" for k, v in r.status_counts().items() if v))
    for n in r.notes:
        print(f"  ! {n}")
    if opt.no_bet:
        print("\n=== NO BET ===")
        for reason in opt.reasons:
            print("  " + reason)
        if opt.nearest_miss:
            s = opt.nearest_miss
            print(f"\n  Più vicina (NON proposta): quota {s.total_odds:.2f}, P {s.joint_probability:.1%}, EV {s.ev:+.1%} — "
                  f"violazioni: {'; '.join(opt.nearest_miss_violations)}")
        top = sorted(r.opportunities, key=lambda o: -o.score)[:5]
        if top:
            print("\n  Migliori opportunità singole (informativo):")
            for o in top:
                print(_fmt_leg(o))
        return
    for k, s in enumerate(opt.slips, 1):
        print(f"\n=== SCHEDINA {k} — quota {s.total_odds:.2f}  P congiunta {s.joint_probability:.1%}  "
              f"fair {s.fair_odds:.2f}  EV {s.ev:+.1%} (limite inf. {s.ev_lower:+.1%})  "
              f"correlazione {s.correlation_penalty:.3f}  stake paper {s.stake:.2f} € ===")
        for l in s.legs:
            print(_fmt_leg(l))
        ex = s.explanation
        print("  Perché:")
        for x in ex.get("positive_factors", []):
            print(f"    + {x}")
        for x in ex.get("negative_factors", []):
            print(f"    - {x}")
        print("  Cosa la cambierebbe:")
        for x in ex.get("what_would_change_it", []):
            print(f"    · {x}")
    print("\nNota: probabilità stimate, non certezze. Modalità paper: lo strumento non piazza scommesse.")


def cmd_analyze(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    cutoff = prov.as_of if hasattr(prov, "as_of") else datetime.now(timezone.utc)
    if getattr(prov, "base", None) is not None and hasattr(prov.base, "as_of"):
        cutoff = prov.base.as_of
    start, end = cutoff, cutoff + timedelta(days=a.days)
    eng = Engine(prov, cfg)
    r = eng.analyze(a.competitions, start, end, cutoff, set(a.markets) if a.markets else None)
    print_result(r, cfg)
    if a.save:
        st = Store(cfg.db_path)
        rid = st.save_run(cutoff, cfg.model_dump(), r.opportunities, r.optimizer.slips, r.status_counts())
        print(f"Salvato run {rid} in {cfg.db_path}")


def cmd_backtest(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    rep = run_backtest(prov, cfg, a.competitions, a.max_rounds, set(a.markets) if a.markets else None)
    print(f"\nBacktest walk-forward: {rep.n_rounds} turni, {rep.n_predictions} previsioni con prezzo di mercato")
    print("\nQualità probabilistica (più basso = meglio):")
    print(f"  {'famiglia':<12}{'n':>6} {'Brier S':>9}{'Brier M':>9}{'Brier F':>9} {'LL S':>8}{'LL M':>8}{'LL F':>8} {'ECE F':>7} {'w_struct*':>10}")
    for fam, m in rep.families.items():
        print(f"  {fam:<12}{m['n']:>6} {m['brier_struct']:>9.4f}{m['brier_market']:>9.4f}{m['brier_final']:>9.4f} "
              f"{m['logloss_struct']:>8.4f}{m['logloss_market']:>8.4f}{m['logloss_final']:>8.4f} {m['ece_final']:>7.3f} {m['best_w_struct']:>10.2f}")
    print("  (S=modello strutturale, M=mercato sgonfiato, F=ensemble; w_struct*=peso ottimo del modello: ~0 => non aggiunge info al prezzo)")
    s, ab = rep.single, rep.all_bets
    print(f"\nSingole paper (status STRONG/CANDIDATE, stake 1): n={s['n_bets']}  hit={s['hit_rate']:.1%}  ROI={s['roi']:+.1%}  "
          f"EV atteso={s['mean_predicted_ev']:+.1%}  CLV={s['mean_clv']:+.1%}  max drawdown={s['max_drawdown_units']:.1f} unità")
    print(f"Riferimento — scommettere TUTTO il mercato valutato: n={ab['n']}  ROI={ab['roi']:+.1%}")
    sl = rep.slips
    print(f"Schedine paper: n={sl['n']}  vinte={sl['hits']}  ROI={sl['roi']:+.1%}  turni NO BET={sl['no_bet_rounds']}/{rep.n_rounds}")
    if a.save_calibration:
        cs = CalibrationSet({fam: PlattCalibrator.fit(*data) for fam, data in rep.calibration_data.items()})
        cs.save(cfg.calibration_path)
        print(f"\nCalibrazione Platt salvata in {cfg.calibration_path} "
              "(fittata in-sample sull'intero backtest: ottimistica, rivalidare su dati nuovi)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="algowinbet", description="AlgoWinBet — analisi probabilistica dei mercati (solo paper)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--provider", default="mock", choices=["mock", "csv"])
        sp.add_argument("--csv", nargs="*", help="file CSV football-data.co.uk (risultati e/o fixtures)")
        sp.add_argument("--manual", help="JSON con quote inserite a mano (es. Sisal)")
        sp.add_argument("--competitions", nargs="*")
        sp.add_argument("--markets", nargs="*", help="es. MATCH_1X2 TOTAL_GOALS BTTS")
        sp.add_argument("--config")
        sp.add_argument("--seed", type=int, default=7)
        sp.add_argument("--mock-bias-over", type=float, default=0.0, help="inefficienza pianificata nel mock (gol sottostimati dai book)")
        sp.add_argument("--mock-noise", type=float, default=0.02, help="rumore dei book sul ritmo gol reale (0=book perfetti)")
        sp.add_argument("--odds-min", type=float)
        sp.add_argument("--odds-max", type=float)
        sp.add_argument("--min-probability", type=float)
        sp.add_argument("--max-legs", type=int)
        sp.add_argument("--output-count", type=int)
        sp.add_argument("--risk-profile", choices=["conservative", "balanced", "dynamic"])
        sp.add_argument("--min-ev", type=float)
        sp.add_argument("--w-struct", type=float, help="peso fisso del modello (disattiva la modalità adattiva)")
        sp.add_argument("--market-prior-sd", type=float, help="quanto il vero può discostarsi dal prezzo di mercato (default 0.025)")
        sp.add_argument("--bootstrap", type=int, help="ricampionamenti per l'incertezza del modello (lento)")

    a = sub.add_parser("analyze", help="analizza il palinsesto e propone schedine o NO BET")
    common(a)
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--bankroll", type=float)
    a.add_argument("--include-watch", action="store_true")
    a.add_argument("--save", action="store_true", help="salva run/opportunità/schedine in SQLite")
    a.set_defaults(fn=cmd_analyze)
    b = sub.add_parser("backtest", help="walk-forward + paper betting")
    common(b)
    b.add_argument("--max-rounds", type=int)
    b.add_argument("--save-calibration", action="store_true")
    b.set_defaults(fn=cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
