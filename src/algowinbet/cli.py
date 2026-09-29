"""CLI: analyze | backtest | timeline | news | event | info-value. Paper-only: nothing here places bets."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .backtest import run_backtest, run_info_value, run_stale_backtest
from .calibration import CalibrationSet, PlattCalibrator
from .config import Config
from .domain import NewsItem, Opportunity
from .engine import AnalysisResult, Engine
from .information import RuleBasedParser
from .collector import GoalCollector
from .names import TeamNames
from .providers import FootballDataCSV, ManualInfoOverlay, ManualOverlay, MockProvider
from .providers.goalapi import GoalApiClient, GoalApiError, shape_summary
from .snapshots import BudgetExceeded, BudgetGuard, MergedProvider, SnapshotProvider, SnapshotStore
from .store import Store


def _provider(a):
    if a.provider == "mock":
        p = MockProvider(seed=a.seed, bias_over=a.mock_bias_over, open_noise=a.mock_noise, close_noise=a.mock_noise / 2,
                         stage=a.mock_stage, fresh_quotes=a.mock_fresh_quotes, player_effect_scale=a.mock_effect_scale)
    elif a.provider == "csv":
        if not a.csv:
            sys.exit("--csv richiesto (uno o più file football-data.co.uk, scaricati a mano)")
        p = FootballDataCSV(a.csv, TeamNames.load(a.aliases))
    elif a.provider == "snapshots":
        p = SnapshotProvider(SnapshotStore(a.db))
        if a.csv:  # multi-season history from CSV + fixtures/quotes/lineups collected forward
            p = MergedProvider(p, FootballDataCSV(a.csv, TeamNames.load(a.aliases)))
    else:
        sys.exit(f"provider sconosciuto {a.provider}")
    if a.manual:
        p = ManualOverlay(p, a.manual)
    if a.info:
        p = ManualInfoOverlay(p, a.info)
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
    flags = f"  formazioni:{o.lineup_state}" + ("  !! QUOTA VECCHIA: verifica prezzo attuale" if o.odds_stale else "")
    return (f"  {o.kickoff:%d/%m %H:%M} {o.home} - {o.away} [{o.competition}]\n"
            f"      {o.description:<32} quota {o.odds:5.2f} ({o.bookmaker}, {o.n_books} book)  "
            f"P {o.p_final:.1%} [{o.p_low:.1%}-{o.p_high:.1%}]  edge {edge}  EV {o.ev:+.1%}  "
            f"DQ {o.data_quality:.0%}  {o.status.value}{flags}")


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


def _cutoff(prov) -> datetime:
    return prov.as_of if hasattr(prov, "as_of") else datetime.now(timezone.utc)


def cmd_analyze(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    cutoff = _cutoff(prov)
    start, end = cutoff, cutoff + timedelta(days=a.days)
    eng = Engine(prov, cfg, use_lineups=not a.no_lineups)
    r = eng.analyze(a.competitions, start, end, cutoff, set(a.markets) if a.markets else None)
    for w in getattr(prov, "warnings", []):
        print(f"  ! info: {w}")
    print_result(r, cfg)
    for an in r.analyses.values():
        if an.availability and (an.state.lineup_state != "none" or any(av.notes for av in an.availability.values())):
            f = an.state.fixture
            print(f"\n  Formazioni/notizie {f.home} - {f.away} (stato {an.state.lineup_state}, shift gol attesi "
                  f"casa {an.adjustment.d_home:+.3f} / ospite {an.adjustment.d_away:+.3f} in log):")
            for n in (an.opportunities[0].lineup_notes if an.opportunities else [])[:6]:
                print(f"    · {n}")
    if a.save:
        st = Store(cfg.db_path)
        rid = st.save_run(cutoff, cfg.model_dump(), r.opportunities, r.optimizer.slips, r.status_counts())
        ev = [e for an in r.analyses.values() for e in an.state.events]
        lu = [l for an in r.analyses.values() for l in an.state.lineups]
        ne, nl = st.save_information(ev, lu)
        print(f"Salvato run {rid} in {cfg.db_path} (+{ne} eventi, +{nl} formazioni nuovi)")


def _find_fixture(prov, needle: str, start, end):
    fx = prov.list_fixtures(None, start, end)
    hit = [f for f in fx if needle == f.id or needle.lower() in f"{f.home} {f.away}".lower()]
    if not hit:
        sys.exit(f"nessuna partita trovata per '{needle}' (prova un pezzo del nome squadra o l'id)")
    return hit[0]


def cmd_timeline(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    cutoff = _cutoff(prov)
    fx = _find_fixture(prov, a.fixture, cutoff - timedelta(days=1), cutoff + timedelta(days=a.days))
    eng = Engine(prov, cfg, use_lineups=not a.no_lineups)
    print(f"\nTimeline informativa {fx.home} - {fx.away} ({fx.kickoff:%d/%m/%Y %H:%M} UTC)")
    prev: dict[str, float] = {}
    for e in eng.timeline(fx, model_cutoff=cutoff - timedelta(days=1)):
        stale = "  !! quote più vecchie dell'ultima informazione" if e.stale_quotes else ""
        print(f"\n[{e.label}] {e.cutoff:%d/%m %H:%M}  formazioni: {e.lineup_state}  gol attesi {e.expected_goals[0]:.2f}-{e.expected_goals[1]:.2f} "
              f"(shift log {e.lambda_shift[0]:+.3f}/{e.lambda_shift[1]:+.3f}){stale}")
        for k, r in e.rows.items():
            d = f"  Δp_finale {r['p_final'] - prev[k]:+.3f}" if k in prev else ""
            mk = f"{r['p_market']:.1%}" if r["p_market"] is not None else "n/d"
            print(f"   {r['desc']:<28} p_cieca {r['p_blind']:.1%} -> p_modello {r['p_struct']:.1%} | mercato {mk} | finale {r['p_final']:.1%} | "
                  f"quota {r['odds']:.2f} EV {r['ev']:+.1%} {r['status']}{'*' if r['stale'] else ''}{d}")
            prev[k] = r["p_final"]
        for n in e.notes[:5]:
            print(f"      · {n}")


def _roster_all(prov):
    out = []
    for c in prov.list_competitions():
        out += list(getattr(prov, "list_players", lambda _c: [])(c))
    return out


def cmd_news(a) -> None:
    prov = _provider(a)
    roster = _roster_all(prov)
    if not roster:
        sys.exit("nessuna rosa disponibile: usa --provider mock oppure --info rose.json")
    now = _cutoff(prov)
    item = NewsItem(source=a.source, source_level=a.level, published_at=now, observed_at=now, text=a.text, team=a.team)
    for e in RuleBasedParser().parse(item, roster):
        who = next((p.name for p in roster if p.id == e.player), "-")
        print(f"{e.event_type:<14} {who:<14} status={e.payload.get('status', '-'):<10} conf={e.confidence:.2f} livello={e.source_level}"
              f"{' (ipotesi)' if e.payload.get('hedged') else ''}{' NON PARSATA' if e.payload.get('unparsed') else ''}")


def cmd_event(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    cutoff = _cutoff(prov)
    eng = Engine(prov, cfg)
    r0 = eng.analyze(a.competitions, cutoff, cutoff + timedelta(days=a.days), cutoff)
    roster = _roster_all(prov)
    later = cutoff + timedelta(minutes=a.minutes_later)
    item = NewsItem(source=a.source, source_level=a.level, published_at=later, observed_at=later, text=a.text, team=a.team)
    events = [e for e in RuleBasedParser().parse(item, roster) if e.event_type != "OTHER"]
    if not events:
        sys.exit("nessun evento strutturato estratto dal testo (nessun giocatore/stato riconosciuto)")
    res = r0
    for e in events:
        res = eng.handle_event(res, e, later)
    print(f"\nEvento: {a.text!r} (livello {a.level}) -> {len(events)} evento/i strutturato/i")
    for n in res.notes[-len(events):]:
        print(f"  {n}")
    print(f"\nVariazioni ({len(res.changes)}):")
    for c in sorted(res.changes, key=lambda c: -abs(c["p_after"] - c["p_before"]))[:12]:
        print(f"  {c['fixture']:<34} {c['market']:<28} p {c['p_before']:.1%} -> {c['p_after']:.1%}  EV {c['ev_before']:+.1%} -> {c['ev_after']:+.1%}  "
              f"{c['status_before']} -> {c['status_after']} [{c['priority']}]")
    print_result(res, cfg)


def cmd_info_value(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    rep = run_info_value(prov, cfg, a.competitions, max_rounds=a.max_rounds)
    print(f"\nValore informativo di notizie/formazioni ({rep.n_rounds} turni). Log loss: più basso = meglio; 'cieco' = senza formazioni/notizie.")
    for st, fams in rep.stages.items():
        print(f"\n{st}")
        print(f"  {'famiglia':<10}{'n':>6}  {'LL cieco':>9}{'LL con info':>12}{'LL mercato':>11}  {'Δ':>8}  {'shift medio p':>13}")
        for fam, m in fams.items():
            print(f"  {fam:<10}{m['n']:>6}  {m['logloss_blind']:>9.4f}{m['logloss_aware']:>12.4f}{m['logloss_market']:>11.4f}  "
                  f"{m['logloss_aware'] - m['logloss_blind']:>+8.4f}  {m['mean_abs_shift']:>13.3f}")
    print("\nΔ < 0: le informazioni migliorano le probabilità del modello strutturale. Il mercato resta il riferimento: se supera ancora il modello,")
    print("il vantaggio va cercato solo dove il prezzo è vecchio rispetto all'informazione (flag QUOTA VECCHIA).")


def cmd_stale_edge(a) -> None:
    cfg = _cfg(a)
    prov = _provider(a)
    r = run_stale_backtest(prov, cfg, a.competitions, a.minutes_after_lineup, a.max_rounds)
    print(f"\nBacktest 'quota ferma dopo la formazione ufficiale' ({r.n_rounds} turni, decisione {a.minutes_after_lineup} min dopo la pubblicazione XI)")
    print(f"  Universo (tutte le selezioni con quota più vecchia dell'informazione): n={r.baseline_n}  ROI={r.baseline_roi:+.1%}")
    print(f"  Con filtro EV/qualità: n={r.n_bets}  hit={r.hit_rate:.1%}  ROI={r.roi:+.1%}  EV atteso={r.mean_predicted_ev:+.1%}  CLV={r.mean_clv:+.1%}")
    print("  CLV > 0 = il prezzo preso era migliore della chiusura. Serve un campione grande: un ROI positivo con poche scommesse è rumore.")


def _goal_client(a, store: SnapshotStore) -> GoalApiClient:
    budget = BudgetGuard(store, "goal-api", daily=a.daily_limit, monthly=None, reserve=a.reserve)
    try:
        return GoalApiClient(store=store, budget=budget)
    except GoalApiError as e:
        sys.exit(str(e))


def cmd_goal_probe(a) -> None:
    store = SnapshotStore(a.db)
    client = _goal_client(a, store)
    params = dict(kv.split("=", 1) for kv in a.param or [])
    try:
        env = client.get(a.path, params)
    except (GoalApiError, BudgetExceeded) as e:
        sys.exit(f"errore: {e}")
    print(f"OK {a.path} {params} — 1 richiesta. Rate limit: {client.last_rate}. Risposta salvata in {a.db} (raw_requests).")
    for line in shape_summary(env.get("data"))[: a.max_lines]:
        print("  " + line)
    if env.get("pagination"):
        print(f"  pagination: {env['pagination']}")


def cmd_goal_leagues(a) -> None:
    store = SnapshotStore(a.db)
    client = _goal_client(a, store)
    try:
        env = client.get("/leagues", {"search": a.search, "limit": 20})
    except (GoalApiError, BudgetExceeded) as e:
        sys.exit(f"errore: {e}")
    for r in env.get("data") or []:
        c = r.get("country") or r.get("countryName") or "-"
        print(f"  id={r.get('id') or r.get('leagueId')}  {r.get('name')}  ({c.get('name', '-') if isinstance(c, dict) else c})")


def cmd_goal_collect(a) -> None:
    store = SnapshotStore(a.db)
    client = _goal_client(a, store)
    col = GoalCollector(client, store, a.leagues, TeamNames.load(a.aliases))
    if a.dry_run:
        print(f"[dry-run] modo {a.mode}: circa {col.plan(a.mode)} richieste; budget residuo {client.budget.remaining()}")
        return
    if a.mode == "fixtures":
        st = col.sync_fixtures(a.days)
    elif a.mode == "results":
        st = col.sync_results(a.days)
    elif a.mode == "lineups":
        st = col.sync_lineups(a.window, a.max_fixtures)
    elif a.mode == "odds":
        st = col.sync_odds(a.hours, a.max_fixtures or 20)
    else:
        st = col.sync_players()
    print(f"collect {st.mode}: {st.requests} richieste, salvati {st.saved or '{}'}; budget residuo {client.budget.remaining()}")
    for x in st.skipped[:10]:
        print(f"  = saltato: {x}")
    for e in st.errors:
        print(f"  ! {e}")
    r = st.report
    if r.gaps or r.unmapped_markets:
        print("  Lacune di mapping (i payload grezzi sono salvati: correggi il mapper e rilancia senza altre richieste):")
        for k, v in sorted(r.gaps.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {v}x {k}")
        if r.unmapped_markets:
            print("    mercati non mappati: " + ", ".join(f"{k} ({v})" for k, v in sorted(r.unmapped_markets.items(), key=lambda kv: -kv[1])[:15]))
    if a.csv:
        miss = col.name_check(FootballDataCSV(a.csv, TeamNames.load(a.aliases)))
        if miss:
            print(f"  ! nomi squadra senza storico (aggiungi alias in {a.aliases}): {sorted(miss)}")


def cmd_snapshots_stats(a) -> None:
    store = SnapshotStore(a.db)
    print(f"Snapshot store {a.db}:")
    for k, v in store.stats().items():
        print(f"  {k:<14}{v}")
    for r in store.db.execute("SELECT source, period, used FROM api_usage ORDER BY period DESC LIMIT 6").fetchall():
        print(f"  uso API {r[0]} {r[1]}: {r[2]}")
    if a.upcoming:
        now = datetime.now(timezone.utc)
        for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=a.upcoming))[:40]:
            print(f"  {f.kickoff:%Y-%m-%d %H:%M} UTC  {f.home} - {f.away}  [{f.competition}] {f.status.value}")


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
        sp.add_argument("--provider", default="mock", choices=["mock", "csv", "snapshots"])
        sp.add_argument("--db", default="data/snapshots.db", help="database snapshot (provider snapshots / comando goal)")
        sp.add_argument("--aliases", default="configs/team_aliases.json", help="alias nomi squadre/competizioni tra fonti diverse")
        sp.add_argument("--csv", nargs="*", help="file CSV football-data.co.uk (risultati e/o fixtures)")
        sp.add_argument("--manual", help="JSON con quote inserite a mano (es. Sisal)")
        sp.add_argument("--info", help="JSON con rose, formazioni e notizie copiate da fonti ufficiali")
        sp.add_argument("--no-lineups", action="store_true", help="ignora formazioni/notizie (modello cieco)")
        sp.add_argument("--competitions", nargs="*")
        sp.add_argument("--markets", nargs="*", help="es. MATCH_1X2 TOTAL_GOALS BTTS")
        sp.add_argument("--config")
        sp.add_argument("--seed", type=int, default=7)
        sp.add_argument("--mock-stage", default="early", choices=["early", "pre_lineup", "post_lineup"])
        sp.add_argument("--mock-fresh-quotes", action="store_true", help="post_lineup: i book hanno già aggiornato le quote")
        sp.add_argument("--mock-effect-scale", type=float, default=1.0, help="peso reale dei giocatori nel mock")
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
    a.add_argument("--save", action="store_true", help="salva run/opportunità/schedine/eventi in SQLite")
    a.set_defaults(fn=cmd_analyze)
    b = sub.add_parser("backtest", help="walk-forward + paper betting")
    common(b)
    b.add_argument("--max-rounds", type=int)
    b.add_argument("--save-calibration", action="store_true")
    b.set_defaults(fn=cmd_backtest)
    t = sub.add_parser("timeline", help="evoluzione delle stime di una partita nel tempo (T-72h ... T-30m)")
    common(t)
    t.add_argument("fixture", help="id o parte del nome di una squadra")
    t.add_argument("--days", type=int, default=7)
    t.set_defaults(fn=cmd_timeline)
    n = sub.add_parser("news", help="parsa un testo di notizia in eventi strutturati (regole IT/EN)")
    common(n)
    n.add_argument("--text", required=True)
    n.add_argument("--team")
    n.add_argument("--level", default="C", choices=list("ABCDE"))
    n.add_argument("--source", default="manual")
    n.set_defaults(fn=cmd_news)
    ev = sub.add_parser("event", help="simula una notizia in arrivo e mostra il ricalcolo selettivo")
    common(ev)
    ev.add_argument("--text", required=True)
    ev.add_argument("--team")
    ev.add_argument("--level", default="A", choices=list("ABCDE"))
    ev.add_argument("--source", default="manual")
    ev.add_argument("--days", type=int, default=7)
    ev.add_argument("--minutes-later", type=int, default=30)
    ev.set_defaults(fn=cmd_event)
    se = sub.add_parser("stale-edge", help="backtest: scommesse quando la quota è ferma dopo la formazione ufficiale")
    common(se)
    se.add_argument("--minutes-after-lineup", type=int, default=5)
    se.add_argument("--max-rounds", type=int)
    se.set_defaults(fn=cmd_stale_edge)
    g = sub.add_parser("goal", help="GOAL API: probe | leagues | collect (chiave in GOALAPI_KEY)")
    gs = g.add_subparsers(dest="goal_cmd", required=True)

    def gcommon(sp):
        sp.add_argument("--db", default="data/snapshots.db")
        sp.add_argument("--aliases", default="configs/team_aliases.json")
        sp.add_argument("--daily-limit", type=int, default=1000, help="richieste/giorno del piano (free: 1000)")
        sp.add_argument("--reserve", type=int, default=50, help="richieste tenute di scorta")
    pr = gs.add_parser("probe", help="1 richiesta a un endpoint: salva il grezzo e mostra la struttura dei campi")
    gcommon(pr)
    pr.add_argument("path", help="es. /fixtures/date/2026-09-30")
    pr.add_argument("--param", action="append", help="k=v (ripetibile)")
    pr.add_argument("--max-lines", type=int, default=80)
    pr.set_defaults(fn=cmd_goal_probe)
    lg = gs.add_parser("leagues", help="cerca gli id delle leghe")
    gcommon(lg)
    lg.add_argument("search")
    lg.set_defaults(fn=cmd_goal_leagues)
    co = gs.add_parser("collect", help="raccoglie dati in avanti nello snapshot store (idempotente, budget-aware)")
    gcommon(co)
    co.add_argument("--mode", required=True, choices=["fixtures", "results", "lineups", "odds", "players"])
    co.add_argument("--leagues", nargs="+", required=True, help="id lega GOAL (vedi: goal leagues Serie A)")
    co.add_argument("--days", type=int, default=7)
    co.add_argument("--window", type=int, default=95, help="lineups: minuti prima del calcio d'inizio")
    co.add_argument("--hours", type=int, default=48, help="odds: ore in avanti")
    co.add_argument("--max-fixtures", type=int)
    co.add_argument("--csv", nargs="*", help="storico CSV per il controllo dei nomi squadra")
    co.add_argument("--dry-run", action="store_true")
    co.set_defaults(fn=cmd_goal_collect)
    sn = sub.add_parser("snapshots", help="statistiche dello snapshot store")
    sn.add_argument("--db", default="data/snapshots.db")
    sn.add_argument("--upcoming", type=int, metavar="GIORNI", help="elenca le partite salvate nei prossimi GIORNI")
    sn.set_defaults(fn=cmd_snapshots_stats)
    iv = sub.add_parser("info-value", help="quanto migliorano le stime notizie e formazioni (walk-forward)")
    common(iv)
    iv.add_argument("--max-rounds", type=int)
    iv.set_defaults(fn=cmd_info_value)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
