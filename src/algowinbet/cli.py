"""CLI: analyze | backtest | timeline | news | event | info-value. Paper-only: nothing here places bets."""
from __future__ import annotations

import argparse
import json
import os
import time
import sys
from datetime import datetime, timedelta, timezone

from .backtest import run_backtest, run_info_value, run_stale_backtest
from .calibration import CalibrationSet, PlattCalibrator
from .config import Config
from .domain import NewsItem, Opportunity
from .engine import AnalysisResult, Engine
from .information import RuleBasedParser
from .autorun import AutoConfig, run_tick
from .collector import GoalCollector
from .oddscollector import OddsCollector
from .providers.oddspapi import MONTHLY_LIMIT, OddsPapiClient, OddsPapiError
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


def cmd_fair(a, cfg: Config, prov, cutoff: datetime) -> None:
    """Model-only view: fair probabilities and odds per fixture, no bookmaker price needed. Validates the model on real
    data before any quote is collected (it is NOT a betting signal: value needs the market price)."""
    from .markets import probability
    from .domain import SelectionRef
    eng = Engine(prov, cfg, use_lineups=False)
    refs = [("1", SelectionRef(market_code="MATCH_1X2", selection="HOME")), ("X", SelectionRef(market_code="MATCH_1X2", selection="DRAW")),
            ("2", SelectionRef(market_code="MATCH_1X2", selection="AWAY")),
            ("O2.5", SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=2.5)), ("GG", SelectionRef(market_code="BTTS", selection="YES"))]
    fixtures = [f for f in prov.list_fixtures(a.competitions, cutoff, cutoff + timedelta(days=a.days)) if f.kickoff > cutoff]
    print(f"Probabilità del modello (quota equa) — {len(fixtures)} partite, storico: stagione corrente + {cfg.model.history_seasons} precedenti")
    missing: dict[str, int] = {}
    for f in fixtures:
        fitted = eng.fit(f.competition, cutoff)
        if fitted is None or not (fitted[0].knows(f.home) and fitted[0].knows(f.away)):
            missing[f.competition] = missing.get(f.competition, 0) + 1
            continue
        m = fitted[0].score_matrix(f.home, f.away)
        cells = "  ".join(f"{k} {probability(m, r):.0%} ({1 / max(probability(m, r), 1e-9):.2f})" for k, r in refs)
        print(f"  {_local_short(f.kickoff)}  {f.home} - {f.away}  [{f.competition}]  {cells}")
    for c, n in sorted(missing.items()):
        print(f"  ! {c}: {n} partite senza storico sufficiente per una o entrambe le squadre")


def _local_short(dt: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return f"{dt.astimezone(ZoneInfo('Europe/Rome')):%d/%m %H:%M}"
    except Exception:
        return f"{dt:%d/%m %H:%M}Z"


def cmd_analyze(a) -> None:
    cfg = _cfg(a)
    if getattr(a, "fair", False):
        prov = _provider(a)
        cmd_fair(a, cfg, prov, _cutoff(prov))
        return
    if a.provider == "snapshots" and cfg.ensemble.quote_window_hours is None:
        cfg.ensemble.quote_window_hours = 24.0  # live data: analyse only prices observed in the last 24h
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


def _odds_client(store: SnapshotStore, monthly: int = MONTHLY_LIMIT, reserve: int = 20) -> OddsPapiClient:
    try:
        return OddsPapiClient(store=store, budget=BudgetGuard(store, "oddspapi", monthly=monthly, reserve=reserve))
    except OddsPapiError as e:
        sys.exit(str(e))


def cmd_odds_account(a) -> None:
    store = SnapshotStore(a.db)
    c = _odds_client(store)
    try:
        acc = c.account()
    except OddsPapiError as e:
        sys.exit(f"errore: {e}")
    for k, v in acc.items():
        print(f"  {k}: {v}")
    print(f"  budget locale: {c.budget.remaining()}")
    store.close()


def cmd_odds_probe(a) -> None:
    store = SnapshotStore(a.db)
    c = _odds_client(store)
    params = dict(kv.split("=", 1) for kv in a.param or [])
    try:
        env = c.get(a.path, params, use_store_cache=False)
    except (OddsPapiError, BudgetExceeded) as e:
        sys.exit(f"errore: {e}")
    print(f"OK {a.path} {params} — richieste fatturabili in questo run: {c.billable_sent}; budget {c.budget.remaining()}")
    for line in shape_summary(env.get("data"))[: a.max_lines]:
        print("  " + line)
    store.close()


def cmd_odds_tournaments(a) -> None:
    store = SnapshotStore(a.db)
    c = _odds_client(store)
    try:
        rows = c.get("/tournaments", {"sportId": 10})["data"]
    except (OddsPapiError, BudgetExceeded) as e:
        sys.exit(f"errore: {e}")
    s = a.search.lower()
    for t in rows:
        if s in str(t.get("tournamentName", "")).lower() or s in str(t.get("categoryName", "")).lower():
            print(f"  id={t.get('tournamentId')}  {t.get('tournamentName')}  ({t.get('categoryName')})  future={t.get('futureFixtures')}")
    store.close()


def cmd_odds_markets(a) -> None:
    """Soccer market catalogue, compact (1 request, then cached 7 days and reused by the collector)."""
    store = SnapshotStore(a.db)
    c = _odds_client(store)
    try:
        rows = c.get("/markets")["data"]
    except (OddsPapiError, BudgetExceeded) as e:
        sys.exit(f"errore: {e}")
    rows = [m for m in rows if str(m.get("sportId", 10)) == "10" and not m.get("playerProp")]
    print(f"{len(rows)} mercati calcio (senza player props); chiavi: {sorted(rows[0]) if rows else []}")
    by_type: dict[str, int] = {}
    for m in rows:
        by_type[str(m.get("marketType"))] = by_type.get(str(m.get("marketType")), 0) + 1
    print("per tipo: " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])))
    s = (a.search or "").lower()
    for m in rows:
        line = (f"{m.get('marketId')}|{m.get('marketType')}|{m.get('marketName')}|{m.get('period')}|{m.get('handicap')}|"
                + ";".join(f"{o.get('outcomeId')}={o.get('outcomeName')}" for o in m.get("outcomes") or []))
        if s in line.lower():
            print(line)
    store.close()


def cmd_teams(a) -> None:
    """Every team name in the database (calendar + results), with its competitions. No API request."""
    store = SnapshotStore(a.db)
    rows = store.db.execute(
        "SELECT team, competition, COUNT(*) FROM (SELECT home AS team, competition FROM results UNION ALL SELECT away, competition FROM results "
        "UNION ALL SELECT home, competition FROM fixtures UNION ALL SELECT away, competition FROM fixtures) GROUP BY team, competition").fetchall()
    teams: dict[str, list[str]] = {}
    for team, comp, _ in rows:
        teams.setdefault(team, []).append(comp)
    print(f"{len(teams)} squadre")
    for t in sorted(teams):
        print(f"TEAM|{t}|{';'.join(sorted(set(teams[t])))}")
    store.close()


def cmd_inspect(a) -> None:
    """Debug a fixture's goal-total prices from the stored raw payloads (no API request): for each total-goals market of the
    catalogue, the raw OddsPapi row (market id, name, type, line, period, outcome, price) next to what we stored."""
    store = SnapshotStore(a.db)
    like = f"%{a.team}%"
    fxs = store.db.execute("SELECT DISTINCT fixture_id, home, away, kickoff FROM fixtures WHERE (home LIKE ? OR away LIKE ?) AND kickoff > ? ORDER BY kickoff",
                       (like, like, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())).fetchall()
    cat_raw = store.last_raw("oddspapi", "/markets")
    catalogue = {int(m["marketId"]): m for m in (json.loads(store.raw_body(cat_raw[0])) if cat_raw else []) if "marketId" in m}
    raws = store.db.execute("SELECT id, params FROM raw_requests WHERE source='oddspapi' AND endpoint='/odds-by-tournaments' AND status=200 "
                            "ORDER BY id DESC LIMIT 8").fetchall()
    for fid, home, away, ko in fxs:
        links = [r[0] for r in store.db.execute("SELECT ext_id FROM fixture_links WHERE fixture_id=?", (fid,)).fetchall()]
        if not links:
            continue
        print(f"== {home} - {away} {ko} ({fid}) oddspapi {links}")
        for rid, params in raws:
            body = json.loads(store.raw_body(rid))
            for row in body if isinstance(body, list) else [body]:
                if str(row.get("fixtureId")) not in links:
                    continue
                for book, bd in (row.get("bookmakerOdds") or {}).items():
                    for mid, md in ((bd or {}).get("markets") or {}).items():
                        m = catalogue.get(int(mid), {})
                        mtype = str(m.get("marketType") or "")
                        if a.all_markets or "total" in mtype.lower() or "total" in str(m.get("marketName") or "").lower():
                            outs = {int(o["outcomeId"]): o.get("outcomeName") for o in m.get("outcomes") or []}
                            prices = "; ".join(f"{outs.get(int(oid), oid)}={next(iter((od.get('players') or {}).values()), {}).get('price')}"
                                               for oid, od in (md.get("outcomes") or {}).items())
                            print(f"  RAW {book} m{mid} '{m.get('marketName')}' type={mtype} line={m.get('handicap')} "
                                  f"period={m.get('period')} sport={m.get('sportId')} :: {prices}")
                break
            else:
                continue
            break
        for row in store.db.execute(
                "SELECT market_code, selection, line, bookmaker, odds, observed_at FROM quotes q WHERE fixture_id=? AND market_code LIKE 'TOTAL%' "
                "AND observed_at = (SELECT MAX(observed_at) FROM quotes q2 WHERE q2.fixture_id=q.fixture_id AND q2.market_code=q.market_code "
                "AND q2.selection=q.selection AND q2.line_key=q.line_key AND q2.bookmaker=q.bookmaker) ORDER BY bookmaker, line, selection",
                (fid,)).fetchall():
            print(f"  DB  {row}")
    store.close()


def cmd_market_coverage(a) -> None:
    """Every market a bookmaker prices in the stored snapshots (no API request): per catalogue marketType and period, how many
    fixtures and lines it covers and whether our mapper reads it (used) or skips it."""
    from .providers.oddspapi import OddsPapiMapper
    store = SnapshotStore(a.db)
    cat_raw = store.last_raw("oddspapi", "/markets")
    catalogue = {int(m["marketId"]): m for m in (cat_raw[1] if cat_raw else []) if "marketId" in m}
    mapper = OddsPapiMapper(TeamNames(), list(catalogue.values()), [])
    raws = store.db.execute("SELECT id, params FROM raw_requests WHERE source='oddspapi' AND endpoint='/odds-by-tournaments' AND status=200 "
                            "AND params LIKE ? ORDER BY id DESC LIMIT ?", (f"%{a.book}%", a.snapshots)).fetchall()
    cov: dict[tuple[str, str], dict] = {}
    seen_fx: set[str] = set()
    for rid, _ in raws:
        body = json.loads(store.raw_body(rid))
        for row in body if isinstance(body, list) else [body]:
            fx = str(row.get("fixtureId"))
            if fx in seen_fx:
                continue  # newest snapshot of each fixture only
            seen_fx.add(fx)
            for book, bd in (row.get("bookmakerOdds") or {}).items():
                if a.book not in book:
                    continue
                for mid, md in ((bd or {}).get("markets") or {}).items():
                    m = catalogue.get(int(mid))
                    key = (str(m.get("marketType")) if m else f"? id {mid}", str(m.get("period")) if m else "?")
                    c = cov.setdefault(key, {"fixtures": set(), "lines": set(), "name": (m or {}).get("marketName", "fuori catalogo"),
                                             "used": mapper._market(int(mid)) is not None, "prop": bool((m or {}).get("playerProp"))})
                    c["fixtures"].add(fx)
                    c["lines"].add((m or {}).get("handicap"))
    print(f"{a.book}: {len(seen_fx)} partite nelle ultime {len(raws)} fotografie, {len(cov)} tipi di mercato")
    for used in (True, False):
        print("\n== USATI" if used else "\n== NON USATI")
        for (mtype, period), c in sorted(cov.items(), key=lambda kv: -len(kv[1]["fixtures"])):
            if c["used"] != used:
                continue
            lines = sorted(x for x in c["lines"] if isinstance(x, (int, float)))
            ln = f" linee {lines[0]}..{lines[-1]} ({len(lines)})" if len(lines) > 1 else ""
            print(f"  {mtype:32} {period:10} {len(c['fixtures']):4} partite{ln}  '{c['name']}'{' [giocatore]' if c['prop'] else ''}")


def cmd_remap_odds(a) -> None:
    """Re-map every stored OddsPapi payload with the current mapper (no API request)."""
    from .oddscollector import remap_stored_odds
    store = SnapshotStore(a.db)
    _print_stats(remap_stored_odds(store, TeamNames.load(a.aliases)))
    store.close()


def _print_stats(st, budget=None) -> None:
    print(f"{st.mode}: {st.requests} richieste, salvati {st.saved or '{}'}" + (f"; budget {budget.remaining()}" if budget else ""))
    for x in st.skipped[:10]:
        print(f"  = {x}")
    for e in st.errors:
        print(f"  ! {e}")
    r = st.report
    for k, v in sorted(r.gaps.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  lacuna {v}x {k}")
    if r.unmapped_markets:
        print("  mercati non mappati: " + ", ".join(f"{k} ({v})" for k, v in sorted(r.unmapped_markets.items(), key=lambda kv: -kv[1])[:15]))


def cmd_odds_collect(a) -> None:
    store = SnapshotStore(a.db)
    c = _odds_client(store, reserve=a.reserve)
    col = OddsCollector(c, store, a.tournaments, a.bookmakers, TeamNames.load(a.aliases))
    _print_stats(col.sync_odds() if a.mode == "odds" else col.sync_closing(), c.budget)
    store.close()


def cmd_collect_auto(a) -> None:
    """One scheduler tick (GitHub Actions): runs only what is due; keys and DB credentials come from the environment."""
    cfg = AutoConfig.load(a.config)
    try:
        store = SnapshotStore(a.db)
    except RuntimeError as e:
        sys.exit(str(e))
    names = TeamNames.load(a.aliases)
    goal = odds = None
    if cfg.goal_leagues and (os.environ.get("GOALAPI_KEY") or os.environ.get("GOAL_API_KEY")):
        gc = GoalApiClient(store=store, budget=BudgetGuard(store, "goal-api", daily=cfg.goal_daily_limit, reserve=cfg.goal_reserve))
        goal = GoalCollector(gc, store, cfg.goal_leagues, names)
    if cfg.oddspapi_tournaments and (os.environ.get("ODDSPAPI_API_KEY") or os.environ.get("ODDSPAPI_KEY")):
        oc = OddsPapiClient(store=store, budget=BudgetGuard(store, "oddspapi", monthly=cfg.oddspapi_monthly_limit, reserve=cfg.oddspapi_reserve))
        odds = OddsCollector(oc, store, cfg.oddspapi_tournaments, cfg.bookmakers, names, history_books=cfg.history_bookmakers)
    t0 = time.monotonic()

    def show(st):
        _print_stats(st)
        print(f"  ({time.monotonic() - t0:.0f}s dall'inizio)", flush=True)
    try:
        results = run_tick(store, cfg, goal, odds, on_step=show, max_seconds=a.max_seconds, manual=a.manual, history=a.history)
        if not results:
            print("tick: niente da fare")
        from .autorun import should_publish
        from .publish import analyze_and_publish, last_publication
        if a.publish and (a.force_publish or should_publish(results, last_publication(store), datetime.now(timezone.utc))):
            if time.monotonic() - t0 > a.max_seconds + 60:
                print("analisi: rimandata (tempo del giro esaurito)")
            else:
                rid, res = analyze_and_publish(store)
                print(f"analisi pubblicata (run {rid}): {len(res.fixtures)} partite, {len(res.opportunities)} mercati, "
                      f"{len(res.optimizer.slips)} schedine" + (" — NO BET" if res.optimizer.no_bet else "") +
                      f" ({time.monotonic() - t0:.0f}s dall'inizio)")
        now = datetime.now(timezone.utc)
        print("Riepilogo database: " + ", ".join(f"{k}={v}" for k, v in store.stats().items()))
        by_comp: dict[str, int] = {}
        for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=cfg.fixtures_days)):
            by_comp[f.competition] = by_comp.get(f.competition, 0) + 1
        print("Partite in calendario: " + (", ".join(f"{k} {v}" for k, v in sorted(by_comp.items())) or "nessuna"))
        print("Budget: " + ", ".join(f"{s} {store.usage(s, f'D{now:%Y-%m-%d}')} oggi / {store.usage(s, f'M{now:%Y-%m}')} mese"
                                     for s in ("goal-api", "oddspapi")))
    finally:
        store.close()


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
    for q in a.search:  # several names in one run: 1 GOAL request each
        try:
            env = client.get("/leagues", {"search": q, "limit": 20})
        except (GoalApiError, BudgetExceeded) as e:
            sys.exit(f"errore: {e}")
        print(f"== {q}")
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
    elif a.mode == "stats":
        st = col.sync_stats(a.days, a.max_fixtures or 30)
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


def _local(dt: datetime) -> str:
    """Display only: storage and cutoffs stay in UTC. Italian time when tzdata is available."""
    try:
        from zoneinfo import ZoneInfo
        return f"{dt.astimezone(ZoneInfo('Europe/Rome')):%Y-%m-%d %H:%M} ora IT"
    except Exception:
        return f"{dt:%Y-%m-%d %H:%M} UTC"


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
            print(f"  {_local(f.kickoff)}  {f.home} - {f.away}  [{f.competition}] {f.status.value}")


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
    a.add_argument("--fair", action="store_true", help="solo modello: probabilità e quote eque, senza quote del bookmaker")
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
    lg.add_argument("search", nargs="+", help="uno o più nomi, es. \"Premier League\" Bundesliga")
    lg.set_defaults(fn=cmd_goal_leagues)
    co = gs.add_parser("collect", help="raccoglie dati in avanti nello snapshot store (idempotente, budget-aware)")
    gcommon(co)
    co.add_argument("--mode", required=True, choices=["fixtures", "results", "lineups", "odds", "stats", "players"])
    co.add_argument("--leagues", nargs="+", required=True, help="id lega GOAL (vedi: goal leagues Serie A)")
    co.add_argument("--days", type=int, default=7)
    co.add_argument("--window", type=int, default=95, help="lineups: minuti prima del calcio d'inizio")
    co.add_argument("--hours", type=int, default=48, help="odds: ore in avanti")
    co.add_argument("--max-fixtures", type=int)
    co.add_argument("--csv", nargs="*", help="storico CSV per il controllo dei nomi squadra")
    co.add_argument("--dry-run", action="store_true")
    co.set_defaults(fn=cmd_goal_collect)
    od = sub.add_parser("odds", help="OddsPapi: account | probe | tournaments | collect (chiave in ODDSPAPI_API_KEY)")
    os_ = od.add_subparsers(dest="odds_cmd", required=True)

    def ocommon(sp):
        sp.add_argument("--db", default="data/snapshots.db", help="file SQLite oppure 'turso' (TURSO_DATABASE_URL/TURSO_AUTH_TOKEN)")
        sp.add_argument("--aliases", default="configs/team_aliases.json")
        sp.add_argument("--reserve", type=int, default=20, help="richieste mensili tenute di scorta")
    oa = os_.add_parser("account", help="quota e bookmaker del piano (gratis)")
    ocommon(oa)
    oa.set_defaults(fn=cmd_odds_account)
    op = os_.add_parser("probe", help="1 richiesta: salva il grezzo e mostra la struttura")
    ocommon(op)
    op.add_argument("path", help="es. /fixtures")
    op.add_argument("--param", action="append", help="k=v (ripetibile)")
    op.add_argument("--max-lines", type=int, default=80)
    op.set_defaults(fn=cmd_odds_probe)
    ot = os_.add_parser("tournaments", help="cerca gli id dei campionati (1 richiesta, poi cache 24 h)")
    ocommon(ot)
    ot.add_argument("search")
    ot.set_defaults(fn=cmd_odds_tournaments)
    om = os_.add_parser("markets", help="catalogo mercati calcio (1 richiesta, poi cache 7 giorni)")
    ocommon(om)
    om.add_argument("search", nargs="?", default="")
    om.set_defaults(fn=cmd_odds_markets)
    oc = os_.add_parser("collect", help="quote correnti (1 richiesta per tutti i campionati) o chiusure gratuite")
    ocommon(oc)
    oc.add_argument("--mode", required=True, choices=["odds", "closing"])
    oc.add_argument("--tournaments", nargs="+", required=True)
    oc.add_argument("--bookmakers", nargs="+", default=["sisal", "pinnacle", "snai"], help="massimo 3")
    oc.set_defaults(fn=cmd_odds_collect)
    rm = sub.add_parser("remap-odds", help="ricostruisce le quote OddsPapi dai dati grezzi salvati (nessuna richiesta API)")
    rm.add_argument("--db", default="algowinbet.db")
    rm.add_argument("--aliases", default="configs/team_aliases.json")
    rm.set_defaults(fn=cmd_remap_odds)
    mc = sub.add_parser("market-coverage", help="mercati quotati da un bookmaker nelle fotografie salvate (nessuna richiesta API)")
    mc.add_argument("--db", default="turso")
    mc.add_argument("--book", default="sisal")
    mc.add_argument("--snapshots", type=int, default=4)
    mc.set_defaults(fn=cmd_market_coverage)
    ins = sub.add_parser("inspect", help="quote grezze OddsPapi vs quote salvate per le partite di una squadra (nessuna richiesta API)")
    ins.add_argument("team")
    ins.add_argument("--db", default="algowinbet.db")
    ins.add_argument("--all-markets", action="store_true")
    ins.set_defaults(fn=cmd_inspect)
    tm = sub.add_parser("teams", help="elenco squadre nel database (nessuna richiesta API)")
    tm.add_argument("--db", default="turso")
    tm.set_defaults(fn=cmd_teams)
    ca = sub.add_parser("collect-auto", help="un giro dello scheduler online: esegue solo ciò che serve adesso")
    ca.add_argument("--config", default="configs/collect.json")
    ca.add_argument("--db", default="turso")
    ca.add_argument("--aliases", default="configs/team_aliases.json")
    ca.add_argument("--max-seconds", type=float, default=360, help="nessun nuovo passo dopo N secondi (il job CI ha un timeout)")
    ca.add_argument("--no-publish", dest="publish", action="store_false", help="non rifare l'analisi per la dashboard")
    ca.add_argument("--force-publish", action="store_true", help="rifai l'analisi per la dashboard anche senza dati nuovi")
    ca.add_argument("--manual", action="store_true",
                    help="aggiornamento manuale: fotografia Sisal adesso (richieste conteggiate, massimo manual_monthly al mese) + storico gratuito")
    ca.add_argument("--history", action="store_true", help="storico prezzi gratuito /historical-odds di tutte le partite future")
    ca.set_defaults(fn=cmd_collect_auto)
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
