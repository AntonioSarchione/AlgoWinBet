"""CLI: analyze | backtest | timeline | news | event | info-value. Paper-only: nothing here places bets."""
from __future__ import annotations

import argparse
import json
import re
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


def cmd_results_day(a) -> None:
    """Fixtures of one day (optionally one competition) and whether their result is stored (no API request)."""
    store = SnapshotStore(a.db)
    try:
        day = datetime.fromisoformat(a.day).replace(tzinfo=timezone.utc)
        lo, hi = day.isoformat(), (day + timedelta(days=1)).isoformat()
        like = f"%{a.comp or ''}%"
        fxs = store.db.execute(
            "SELECT fixture_id, competition, home, away, kickoff, status FROM fixtures f WHERE kickoff >= ? AND kickoff < ? AND competition LIKE ? "
            "AND observed_at = (SELECT MAX(observed_at) FROM fixtures g WHERE g.fixture_id = f.fixture_id) ORDER BY kickoff, home",
            (lo, hi, like)).fetchall()
        res = {r[0]: r for r in store.db.execute(
            "SELECT fixture_id, home, away, home_goals, away_goals, observed_at, competition FROM results WHERE kickoff >= ? AND kickoff < ? AND competition LIKE ?",
            (lo, hi, like)).fetchall()}
        quotes: dict[str, str] = {}
        for fid, book, kind, n in store.db.execute(
                "SELECT fixture_id, bookmaker, kind, COUNT(*) FROM quotes WHERE fixture_id IN (SELECT fixture_id FROM fixtures WHERE kickoff >= ? AND kickoff < ?) "
                "GROUP BY fixture_id, bookmaker, kind", (lo, hi)).fetchall():
            quotes[fid] = (quotes.get(fid, "") + f" {book}/{kind}={n}").strip()
        print(f"{a.day} {a.comp or 'tutte le competizioni'}: {len(fxs)} partite in calendario, {len(res)} risultati salvati")
        try:
            last = store.db.execute("SELECT id, created_at FROM pub_runs ORDER BY id DESC LIMIT 1").fetchone()
            legs = store.db.execute("SELECT COUNT(*), SUM(result IS NOT NULL) FROM paper_legs").fetchone()
            print(f"ultima analisi pubblicata: run {last[0]} ({last[1][:16]}) · registro: {legs[0]} selezioni, {legs[1] or 0} chiuse")
        except Exception:  # noqa: BLE001 - tables not created yet
            pass
        seen = set()
        for fid, comp, home, away, ko, status in fxs:
            r = res.get(fid)
            seen.add(fid)
            score = f"{r[3]}-{r[4]} (salvato {r[5][:16]})" if r else "NESSUN RISULTATO"
            print(f"  {ko[11:16]} {comp:<28} {home} - {away}: {score} · stato {status} · quote: {quotes.get(fid, 'nessuna')}")
        for fid, r in res.items():
            if fid not in seen:
                print(f"  (solo risultato) {r[6]} {r[1]} - {r[2]}: {r[3]}-{r[4]}")
    finally:
        store.close()


def cmd_db_bench(a) -> None:
    """Time of writes on Turso: embedded replica vs direct connection, one commit per statement vs one transaction."""
    import time as _t
    import libsql
    url, tok = os.environ["TURSO_DATABASE_URL"], os.environ.get("TURSO_AUTH_TOKEN", "")
    rows = [(f"k{i}", "x" * 50) for i in range(a.rows)]

    def run(conn, label):
        conn.execute("CREATE TABLE IF NOT EXISTS bench(k TEXT PRIMARY KEY, v TEXT)")
        conn.commit()
        t0 = _t.monotonic()
        for i in range(5):
            conn.execute("INSERT OR REPLACE INTO bench VALUES(?, ?)", (f"one{i}", "x"))
            conn.commit()
        t1 = _t.monotonic()
        conn.execute("BEGIN")
        for i in range(5):
            conn.execute("INSERT OR REPLACE INTO bench VALUES(?, ?)", (f"tx{i}", "x"))
        conn.execute("COMMIT")
        t2 = _t.monotonic()
        values = ",".join(["(?,?)"] * len(rows))
        conn.execute(f"INSERT OR REPLACE INTO bench VALUES {values}", [v for r in rows for v in r])
        conn.commit()
        t3 = _t.monotonic()
        conn.execute("SELECT COUNT(*) FROM bench").fetchall()
        t4 = _t.monotonic()
        print(f"{label}: 5 scritture con commit {(t1 - t0) / 5:.2f}s ciascuna · 5 in una transazione {t2 - t1:.2f}s in tutto · "
              f"{len(rows)} righe in 1 istruzione {t3 - t2:.2f}s · 1 lettura {t4 - t3:.3f}s", flush=True)

    t = _t.monotonic()
    rep = libsql.connect(a.replica, sync_url=url, auth_token=tok)
    rep.sync()
    print(f"sync iniziale della replica {_t.monotonic() - t:.1f}s", flush=True)
    run(rep, "replica")
    t = _t.monotonic()
    rep.sync()
    print(f"sync dopo le scritture {_t.monotonic() - t:.2f}s", flush=True)
    run(libsql.connect(database=url, auth_token=tok), "diretta")
    rem = libsql.connect(database=url, auth_token=tok)
    rem.execute("DROP TABLE IF EXISTS bench")
    rem.commit()


def cmd_raw_last(a) -> None:
    """Latest saved raw responses of an endpoint (no API request): when, status, and the field structure of the newest."""
    import json as _json
    from .providers.goalapi import shape_summary
    store = SnapshotStore(a.db)
    rows = store.db.execute("SELECT id, endpoint, params, fetched_at, status FROM raw_requests WHERE source LIKE ? AND endpoint LIKE ? "
                            "ORDER BY id DESC LIMIT ?", (f"%{a.source}%", f"%{a.endpoint}%", a.n)).fetchall()
    if not rows:
        print(f"nessuna risposta salvata per {a.source} {a.endpoint}")
        return
    for rid, ep, params, at, status in rows:
        body = store.raw_body(rid)
        try:
            n = _json.loads(body).get("results")
        except (ValueError, AttributeError):
            n = None
        print(f"#{rid} {at} {status} {ep} {params} {len(body)} byte" + (f", results {n}" if n is not None else ""))
    body = store.raw_body(rows[0][0])
    try:
        data = _json.loads(body)
    except ValueError:
        print(body[:2000].decode("utf-8", "replace"))
        return
    print(f"\nstruttura della più recente (#{rows[0][0]}, {len(body)} byte):")
    for line in shape_summary(data)[: a.max_lines]:
        print("  " + line)
    if a.dump:
        print("\n" + _json.dumps(data, ensure_ascii=False)[: a.dump])


def cmd_lineup_timing(a) -> None:
    """When the XI of recent matches arrived (no API request): for each match, every lineup request sent (GOAL and API-Football)
    with the minutes before kickoff and how many players it returned, and the first confirmed XI stored per source."""
    import json as _json
    store = SnapshotStore(a.db)
    now = datetime.now(timezone.utc)
    rows = store.db.execute(
        "SELECT fixture_id, competition, home, away, MAX(kickoff) FROM fixtures WHERE kickoff >= ? AND kickoff <= ? "
        "GROUP BY fixture_id ORDER BY MAX(kickoff)", ((now - timedelta(days=a.days)).isoformat(), now.isoformat())).fetchall()
    links = {fid: ext for ext, fid in store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source='api-football'").fetchall()}

    def mins(ko, at):
        return int((ko - datetime.fromisoformat(at)).total_seconds() // 60)

    def players(rid, src):
        try:
            d = _json.loads(store.raw_body(rid))
        except ValueError:
            return "?"
        if src == "goal":
            d = d.get("data") if isinstance(d, dict) else None
            if not isinstance(d, dict):
                return "vuota"
            n = [len((d.get(side) or {}).get("startingLineups") or []) for side in ("home", "away")]
            return f"hasLineups={d.get('hasLineups')} " + "+".join(map(str, n))
        resp = d.get("response") or []
        return "+".join(str(len(t.get("startXI") or [])) for t in resp) if resp else "vuota"

    summary = {}
    for fid, comp, home, away, ko in rows:
        ko = datetime.fromisoformat(ko)
        reqs = []
        goal_id = fid.split(":", 1)[-1]
        for rid, at, status in store.db.execute("SELECT id, fetched_at, status FROM raw_requests WHERE source='goal' AND endpoint=? ORDER BY id",
                                                (f"/fixtures/{goal_id}/lineups",)).fetchall():
            reqs.append(f"GOAL {mins(ko, at)}' {status} {players(rid, 'goal')}")
        if fid in links:
            for rid, at, status in store.db.execute("SELECT id, fetched_at, status FROM raw_requests WHERE source='api-football' "
                                                    "AND endpoint='/fixtures/lineups' AND params LIKE ? ORDER BY id",
                                                    (f'%"{links[fid]}"%',)).fetchall():
                reqs.append(f"APIF {mins(ko, at)}' {status} {players(rid, 'apif')}")
        first = {src: mins(ko, at) for src, at in store.db.execute(
            "SELECT source, MIN(observed_at) FROM lineups WHERE fixture_id=? AND status='confirmed' GROUP BY source", (fid,)).fetchall()}
        best = max(first.values()) if first else None
        key = comp
        s = summary.setdefault(key, [0, 0, 0])
        s[0] += 1
        s[1] += best is not None and best >= 30
        s[2] += best is None
        print(f"{ko:%d/%m %H:%M} {comp[:18]:<18} {home}-{away}: XI " +
              (", ".join(f"{k} {v}' prima" for k, v in first.items()) if first else "MAI") + (f" | {'; '.join(reqs)}" if reqs else " | nessuna richiesta"))
    print("\nper competizione: partite, XI almeno 30' prima, XI mai")
    for comp, (n, ok, never) in sorted(summary.items()):
        print(f"  {comp}: {n}, {ok}, {never}")


def cmd_scorer_check(a) -> None:
    """Fase 9 survey (no request): which goalscorer data the stored raw responses already hold. OddsPapi player-prop markets
    per bookmaker, GOAL lineups / events fields, API-Football goal events, players table."""
    import json as _json
    from collections import Counter, defaultdict
    from .providers.goalapi import shape_summary
    store = SnapshotStore(a.db)
    since = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat()
    cat = store.last_raw("oddspapi", "/markets")
    props = {}
    if cat:
        for m in cat[1] if isinstance(cat[1], list) else cat[1].get("data") or []:
            if m.get("playerProp"):
                props[int(m["marketId"])] = f"{m.get('marketName')} [{m.get('marketType')}|{m.get('period')}]"
    for m in (cat[1] if cat and isinstance(cat[1], list) else (cat[1].get("data") or []) if cat else []):
        if int(m.get("marketId") or 0) in (10730, 10731, 10732, 10733):
            print(_json.dumps(m, ensure_ascii=False)[:800])
    print(f"OddsPapi: {len(props)} mercati giocatore nel catalogo")
    for mid, name in sorted(props.items())[:60]:
        print(f"  {mid}: {name}")
    seen = defaultdict(lambda: defaultdict(set))  # book -> market id -> fixture ids
    players = defaultdict(Counter)  # book -> number of player entries per market
    any_fx = defaultdict(set)  # book -> every fixture with prices
    sample = None
    raws = store.db.execute("SELECT id, endpoint FROM raw_requests WHERE source='oddspapi' AND status=200 AND fetched_at >= ? AND endpoint IN "
                            "('/odds-by-tournaments', '/historical-odds') ORDER BY id DESC LIMIT ?", (since, a.max_raw)).fetchall()
    for rid, ep in raws:
        body = _json.loads(store.raw_body(rid))
        rows = body if isinstance(body, list) else body.get("data", body) if isinstance(body, dict) else []
        for row in rows if isinstance(rows, list) else [rows]:
            books = row.get("bookmakerOdds") or row.get("bookmakers") or {}
            for book, bdata in books.items():
                if (bdata or {}).get("markets"):
                    any_fx[book].add(row.get("fixtureId"))
                for mid_s, mdata in ((bdata or {}).get("markets") or {}).items():
                    try:
                        mid = int(mid_s)
                    except ValueError:
                        continue
                    if mid not in props:
                        continue
                    seen[book][mid].add(row.get("fixtureId"))
                    for odata in (mdata.get("outcomes") or {}).values():
                        keys = [k for k in (odata.get("players") or {}) if k != "0"]
                        players[book][mid] += len(keys)
                        if keys and book.startswith("sisal") and (sample is None or (ep == "/odds-by-tournaments" and sample[0] != ep)):
                            sample = (ep, mid, row.get("fixtureId"), {k: odata["players"][k] for k in keys[:2]})
    print(f"\nrisposte quote lette: {len(raws)} (ultimi {a.days} giorni)")
    from .oddscollector import LINKS_SCHEMA
    store.db.executescript(LINKS_SCHEMA)
    comp_of = dict(store.db.execute("SELECT l.ext_id, r.competition FROM fixture_links l JOIN (SELECT fixture_id, competition FROM fixtures "
                                    "GROUP BY fixture_id) r ON r.fixture_id = l.fixture_id WHERE l.source = 'oddspapi'").fetchall())
    for book in sorted(seen):
        fx = set().union(*seen[book].values())
        tot = Counter(comp_of.get(str(f), "?") for f in any_fx[book])
        print(f"  {book}: {len(fx)} partite con mercati giocatore su {len(any_fx[book])} quotate ({', '.join(f'{c} {n}' for c, n in tot.most_common())}): "
              + ", ".join(f"{c} {n}" for c, n in Counter(comp_of.get(str(f), "?") for f in fx).most_common()))
        for mid, fids in sorted(seen[book].items(), key=lambda kv: -len(kv[1]))[:12]:
            print(f"    {mid} {props.get(mid)}: {len(fids)} partite, {players[book][mid]} voci giocatore")
    if sample:
        print(f"\nesempio Sisal ({sample[0]}, mercato {sample[1]}, partita {sample[2]}):\n  {_json.dumps(sample[3], ensure_ascii=False)[:1500]}")
    for src, ep in (("goal-api", "%/lineups"), ("goal-api", "%/events"), ("goal-api", "/teams/%/players"), ("api-football", "/fixtures/events"),
                    ("api-football", "/players%"), ("api-football", "/fixtures/players")):
        n = store.db.execute("SELECT COUNT(*) FROM raw_requests WHERE source=? AND endpoint LIKE ? AND status=200", (src, ep)).fetchone()[0]
        print(f"\n{src} {ep}: {n} risposte salvate")
        last = store.last_raw(src, ep) if n else None
        if last:
            for line in shape_summary(last[1], max_depth=7)[: a.max_lines]:
                print("  " + line)
    miss, total, sample_m = 0, 0, None
    for rid, ep, at in store.db.execute("SELECT id, endpoint, fetched_at FROM raw_requests WHERE source='goal-api' AND endpoint LIKE '%/lineups' "
                                        "AND status=200 ORDER BY id DESC LIMIT 300").fetchall():
        d = (_json.loads(store.raw_body(rid)) or {}).get("data") or {}
        total += 1
        got = [p for side in ("home", "away") for p in ((d.get(side) or {}).get("missingPlayers") or [])]
        if got:
            miss += 1
            sample_m = sample_m or (ep, at, got[:3])
    print(f"\nGOAL formazioni con missingPlayers: {miss} su {total}" + (f"; esempio {sample_m}" if sample_m else ""))
    print("\ntabella players:")
    for row in store.db.execute("SELECT source, COUNT(*), SUM(start_rate IS NOT NULL), SUM(importance IS NOT NULL) FROM players GROUP BY source").fetchall():
        print(f"  {row}")
    print("lineups:", store.db.execute("SELECT source, COUNT(DISTINCT fixture_id) FROM lineups GROUP BY source").fetchall())
    print("eventi letti:", store.db.execute("SELECT COUNT(*), SUM(n) FROM event_reads").fetchone())
    for kind, n, ok in store.db.execute("SELECT kind, COUNT(*), SUM(player_id IS NOT NULL) FROM match_events GROUP BY kind").fetchall():
        print(f"  {kind}: {n} eventi, giocatore riconosciuto {ok}")
    for row in store.db.execute("SELECT fixture_id, minute, team, kind, player_name, player_id FROM match_events WHERE player_id IS NULL "
                                "AND kind IN ('GOAL', 'PENALTY', 'OWN_GOAL') LIMIT 15").fetchall():
        print(f"  non riconosciuto: {row}")


def cmd_analysis_preview(a) -> None:
    """The next week's analysis as the next publication would run it, without publishing (no request): opportunities per
    market family and status, corners / cards ones listed."""
    from collections import Counter
    from .markets import family_of
    from .meta import load_meta
    from .publish import live_config
    store = SnapshotStore(a.db)
    try:
        cfg = live_config(None)
        t = datetime.now(timezone.utc)
        meta = load_meta(store, cfg.model.version) if cfg.ensemble.use_meta else None
        print(f"modello {cfg.model.version}, meta {meta.version if meta else 'spento'}")
        res = Engine(SnapshotProvider(store), cfg, use_lineups=True, meta=meta).analyze(None, t, t + timedelta(days=a.days), t)
        print(f"{len(res.fixtures)} partite, {len(res.opportunities)} mercati, {len(res.optimizer.slips)} schedine")
        c = Counter((family_of(o.ref.market_code), o.status.value) for o in res.opportunities)
        for (fam, status), n in sorted(c.items()):
            print(f"  {fam:12} {status:10} {n}")
        for o in sorted(res.opportunities, key=lambda o: -o.ev):
            if o.ref.market_code.startswith(("CARDS_", "CORNERS_")) and o.status.value in ("STRONG", "CANDIDATE", "FAIR"):
                print(f"  {o.home}-{o.away} {o.description}: quota {o.odds:.2f}, p {o.p_final:.3f}, EV {o.ev:+.3f} ({o.status.value})")
    finally:
        store.close()


def cmd_registry_purge_stats(a) -> None:
    """One-off cleanup (user's decision, 2026-10-03): corner / card selections recorded for matches outside the club
    competitions (national teams, priced before the 20-match rule) and the slips that contain them. Dry run unless --apply."""
    import json as _json
    store = SnapshotStore(a.db)
    try:
        cfg = AutoConfig.load(a.config)
        clubs = {l.name for l in cfg.leagues if l.fd} | {"UEFA Champions League", "UEFA Europa League"}
        legs = [r for r in store.db.execute("SELECT id, fixture_id, sel_key, competition, match, result FROM paper_legs "
                                            "WHERE sel_key LIKE 'CORNERS_%' OR sel_key LIKE 'CARDS_%'").fetchall() if r[3] not in clubs]
        keys = {f"{fid}|{key}" for _, fid, key, *_ in legs}
        slips = [(sid, res) for sid, legs_json, res in store.db.execute("SELECT id, legs, result FROM paper_slips").fetchall()
                 if any(f"{l['fixture_id']}|{l['sel_key']}" in keys for l in _json.loads(legs_json))]
        for _, _, key, comp, match, res in legs:
            print(f"  {comp} | {match} | {key} | {res}")
        print(f"giocate da cancellare: {len(legs)}; schedine che le contengono: {len(slips)} {[s for s, _ in slips]}")
        if a.apply and legs:
            store.db.executemany("DELETE FROM paper_legs WHERE id = ?", [(r[0],) for r in legs])
            store.db.executemany("DELETE FROM paper_slips WHERE id = ?", [(s,) for s, _ in slips])
            store.db.commit()
            print("cancellate")
    finally:
        store.close()


def cmd_referees_backfill(a) -> None:
    """Referees of past matches from payloads already stored (football-data season files, API-Football day reads)."""
    import json as _json
    from .fdcollector import FootballDataCollector
    from .collector import CollectStats
    store = SnapshotStore(a.db)
    try:
        cfg = AutoConfig.load(a.config)
        st = CollectStats("arbitri")
        fd = FootballDataCollector(store, cfg.divisions, TeamNames.load(a.aliases))
        for endpoint, rid in store.db.execute("SELECT endpoint, MAX(id) FROM raw_requests WHERE source = 'football-data' AND status = 200 "
                                              "AND endpoint LIKE '/mmz4281/%' GROUP BY endpoint").fetchall():
            div = endpoint.rsplit("/", 1)[-1].removesuffix(".csv")
            if div in cfg.divisions:
                fd.load(store.raw_body(rid), div, rid, st, only_referees=True)
        links = {ext: fid for ext, fid in store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source = 'api-football'").fetchall()}
        refs = []
        for (rid,) in store.db.execute("SELECT id FROM raw_requests WHERE source = 'api-football' AND endpoint = '/fixtures' "
                                       "AND status = 200 ORDER BY id").fetchall():
            for r in (_json.loads(store.raw_body(rid)).get("response") or []):
                fid = links.get(str((r.get("fixture") or {}).get("id")))
                if fid and (r.get("fixture") or {}).get("referee"):
                    refs.append((fid, r["fixture"]["referee"]))
        st.add("arbitri api-football", store.save_referees("api-football", refs, datetime.now(timezone.utc)))
        store.db.commit()
        _print_stats(st)
        by = store.db.execute("SELECT source, COUNT(*), COUNT(DISTINCT name) FROM referees GROUP BY source").fetchall()
        print("arbitri salvati: " + ", ".join(f"{s} {n} partite / {k} arbitri" for s, n, k in by))
    finally:
        store.close()


def cmd_snapshot_check(a) -> None:
    """The newest OddsPapi snapshots as stored (no request): every fixture row, the team names resolved through the cached
    /participants, and the GOAL match it links to (or why not)."""
    import json as _json
    from .providers.oddspapi import OddsPapiMapper
    store = SnapshotStore(a.db)
    try:
        def latest(endpoint):
            r = store.db.execute("SELECT id, fetched_at FROM raw_requests WHERE source = 'oddspapi' AND endpoint = ? AND status = 200 "
                                 "ORDER BY id DESC LIMIT 1", (endpoint,)).fetchone()
            return (_json.loads(store.raw_body(r[0])), r[1]) if r else (None, None)
        parts, p_at = latest("/participants")
        markets, _ = latest("/markets")
        def data(x):
            return x.get("data", x) if isinstance(x, dict) else x
        m = OddsPapiMapper(TeamNames.load(a.aliases), data(markets) or [], data(parts))
        print(f"/participants letto {p_at}: {len(m.participants)} squadre")
        now = datetime.now(timezone.utc)
        cal = SnapshotProvider(store).list_fixtures(None, now - timedelta(hours=3), now + timedelta(days=10))
        rows = store.db.execute("SELECT id, fetched_at FROM raw_requests WHERE source = 'oddspapi' AND endpoint = '/odds-by-tournaments' "
                                "AND status = 200 AND fetched_at >= ? ORDER BY id", ((now - timedelta(days=1)).isoformat(),)).fetchall()
        for rid, at in rows:
            body = _json.loads(store.raw_body(rid))
            body = data(body)
            for row in (body if isinstance(body, list) else [body]):
                if a.text and a.text not in str(row.get("tournamentId")):
                    continue
                fx = m.match_fixture(row, cal)
                print(f"  {at[:16]} t{row.get('tournamentId')} {row.get('startTime')} ids {row.get('participant1Id')}/{row.get('participant2Id')} "
                      f"'{m._name(row, 1)}'-'{m._name(row, 2)}' -> {fx.home + '-' + fx.away if fx else 'NESSUNA'}")
        for g in sorted(set(m.report.gaps))[:30] if hasattr(m.report, "gaps") else []:
            print("  gap:", g)
        fx_raw, fx_at = latest("/fixtures")
        if fx_raw is not None:
            links = {e for (e,) in store.db.execute("SELECT ext_id FROM fixture_links WHERE source = 'oddspapi'").fetchall()}
            rows = [r for r in (data(fx_raw) or []) if (r.get("startTime") or "") >= now.isoformat()[:10]]
            print(f"/fixtures letto {fx_at}: {len(rows)} partite future")
            for r in sorted(rows, key=lambda r: r.get("startTime") or ""):
                print(f"  {r.get('startTime', '')[:16]} {r.get('participant1Name')}-{r.get('participant2Name')} hasOdds={r.get('hasOdds')} "
                      f"collegata={'sì' if str(r.get('fixtureId')) in links else 'no'} {r.get('fixtureId')}")
    finally:
        store.close()


def cmd_quotes_check(a) -> None:
    """Upcoming matches (7 days, optionally of one competition / team): OddsPapi link, Sisal quotes by kind with the newest
    observation, and the playable selections in the latest publication (no request)."""
    store = SnapshotStore(a.db)
    try:
        now = datetime.now(timezone.utc)
        run = store.db.execute("SELECT MAX(id) FROM pub_runs").fetchone()[0]
        links = {fid for (fid,) in store.db.execute("SELECT fixture_id FROM fixture_links WHERE source = 'oddspapi'").fetchall()}
        for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=7)):
            if a.text and a.text.lower() not in f"{f.competition} {f.home} {f.away}".lower():
                continue
            kinds = store.db.execute("SELECT kind, COUNT(*), MAX(observed_at) FROM quotes WHERE fixture_id = ? AND bookmaker LIKE 'sisal%' "
                                     "GROUP BY kind", (f.id,)).fetchall()
            pub = store.db.execute("SELECT n_quotes FROM pub_fixtures WHERE run_id = ? AND fixture_id = ?", (run, f.id)).fetchone()
            print(f"{f.kickoff:%d/%m %H:%M} {f.competition} | {f.home}-{f.away} | oddspapi {'sì' if f.id in links else 'NO'} | "
                  f"sisal {', '.join(f'{k} {n} (ultima {m[5:16]})' for k, n, m in kinds) or 'nessuna'} | pubblicate {pub[0] if pub else '-'}")
    finally:
        store.close()


DOMESTIC = ["Serie A", "Premier League", "La Liga", "Bundesliga", "Ligue 1", "Primeira Liga", "Eredivisie"]


LATE_FROM_HOUR = 20  # UTC: evening runs after this hour (the pinger's results runs for the evening matches) may empty the day
LATE_KEEP = 20  # GOAL requests a late run leaves for a later one the same night (results of the last matches)


def late_backfill_due(provider, now: datetime) -> bool:
    """An evening run after which no match of the calendar kicks off before the daily reset (00:00 UTC): no XI left to read
    today, so the GOAL requests still unused can go to the history instead of being lost."""
    if now.hour < LATE_FROM_HOUR:
        return False
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return not provider.list_fixtures(None, now, midnight)


def lineup_backfill(store, names, now: datetime, since: str, max_requests: int, max_seconds: float, keep: int):
    """GOAL XI and goal events of finished domestic matches, within the day's GOAL budget minus `keep` requests left for the
    normal ticks. Returns the stats and (matches still without XI, matches still without events)."""
    from .collector import GoalCollector
    guard = BudgetGuard(store, "goal-api", daily=1000, reserve=50)
    left = (guard.remaining()["daily"] or 0) - keep
    if left <= 0:
        return None, 0
    # no raw payloads and no per-request budget write: the backfill charges the budget once per batch
    coll = GoalCollector(GoalApiClient(), store, [], names)
    st = coll.backfill_lineups(DOMESTIC, datetime.fromisoformat(since).replace(tzinfo=timezone.utc), min(max_requests, left), max_seconds,
                               guard=guard)
    left = coll.pending_history(DOMESTIC, datetime.fromisoformat(since).replace(tzinfo=timezone.utc))
    return st, (sum(bool(r[5]) for r in left), sum(bool(r[6]) for r in left))


def cmd_lineups_backfill(a) -> None:
    """Historical XI of the 7 domestic leagues from GOAL (1 request per match, within the daily budget)."""
    store = SnapshotStore(a.db)
    try:
        st, left = lineup_backfill(store, TeamNames.load(a.aliases), datetime.now(timezone.utc), a.since, a.max, a.max_seconds, a.keep)
        if st is None:
            print("budget GOAL di oggi esaurito (tolta la quota lasciata ai giri normali)")
            return
        print(f"richieste {st.requests} · salvate {st.saved} · {'; '.join(st.skipped + st.errors)}")
        print(f"dal {a.since} ancora da leggere: formazioni {left[0]}, eventi {left[1]}")
    finally:
        store.close()


def cmd_events_remap(a) -> None:
    """Goal events mapped again from the stored payloads (no request), then the kinds found."""
    from .collector import GoalCollector
    store = SnapshotStore(a.db)
    try:
        n = GoalCollector(None, store, []).remap_events()
        print(f"eventi riscritti: {n}")
        for kind, cnt, ok in store.db.execute("SELECT kind, COUNT(*), SUM(player_id IS NOT NULL) FROM match_events GROUP BY kind").fetchall():
            print(f"  {kind}: {cnt}, giocatore riconosciuto {ok}")
    finally:
        store.close()


def cmd_slip_review(a) -> None:
    """Settled paper slips (no request): how often they won against their joint probability, which legs sank them, and the
    legs of the slips by probability band (won rate against the model, the Pinnacle price at decision and the close)."""
    from collections import defaultdict as _dd
    store = SnapshotStore(a.db)
    try:
        legs = {(f, k): (r, pm, cf, st) for f, k, r, pm, cf, st in store.db.execute(
            "SELECT fixture_id, sel_key, result, p_market, close_fair, status FROM paper_legs").fetchall()}
        slips = store.db.execute("SELECT id, legs, joint, total_odds, result, profile FROM paper_slips WHERE result IN ('won', 'lost')").fetchall()
        if not slips:
            print("nessuna schedina chiusa")
            return
        by_prof = _dd(lambda: [0, 0, 0.0])
        bands = _dd(lambda: [0, 0, 0.0, 0.0, 0, 0.0, 0])  # n, won, sum p, sum p_market, n p_market, sum close, n close
        losers = _dd(int)
        killers = []
        for sid, lj, joint, odds, res, prof in slips:
            b = by_prof[prof or "-"]
            b[0] += 1
            b[1] += res == "won"
            b[2] += joint or 0
            lost_here = []
            for l in json.loads(lj):
                r, pm, cf, st = legs.get((l["fixture_id"], l["sel_key"]), (None, None, None, l.get("status")))
                if r not in ("won", "lost"):
                    continue
                p = l["p"]
                band = "<40%" if p < .4 else "40-50%" if p < .5 else "50-60%" if p < .6 else "60-70%" if p < .7 else "70-80%" if p < .8 else "80%+"
                x = bands[band]
                x[0] += 1
                x[1] += r == "won"
                x[2] += p
                if pm:
                    x[3] += pm
                    x[4] += 1
                if cf:
                    x[5] += cf
                    x[6] += 1
                if r == "lost":
                    lost_here.append((p, l["market"], l["match"], st, l["odds"]))
            if res == "lost":
                losers[len(lost_here)] += 1
                killers += lost_here
        print("schedine chiuse per profilo: profilo, schedine, vinte, vinte attese (somma delle probabilità)")
        for prof, (n, w, e) in sorted(by_prof.items()):
            print(f"  {prof:12} {n:4}  {w:4}  {e:6.1f}")
        print("schedine perse per numero di selezioni sbagliate: " + ", ".join(f"{k}: {v}" for k, v in sorted(losers.items())))
        print("selezioni nelle schedine chiuse per fascia di probabilità: n, vinte, prob. modello media, Pinnacle alla scelta, Pinnacle in chiusura")
        for band in ("<40%", "40-50%", "50-60%", "60-70%", "70-80%", "80%+"):
            n, w, sp, spm, npm, sc, nc = bands[band]
            if n:
                print(f"  {band:7} {n:4}  {w / n:6.1%}  {sp / n:6.1%}  {(spm / npm if npm else float('nan')):6.1%}  {(sc / nc if nc else float('nan')):6.1%}")
        print("selezioni perse più frequenti nelle schedine perse (prob. modello, mercato, partita, stato, quota):")
        seen = _dd(int)
        for k in killers:
            seen[k] += 1
        for (p, m, match, st, o), n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0][0]))[:25]:
            print(f"  {n:3}x  {p:5.1%}  {m}  |  {match}  |  {st}  |  {o:.2f}")
    finally:
        store.close()


def cmd_xi_eval(a) -> None:
    """Our probable lineups replayed against the official XI (no API request)."""
    from .probable import evaluate_xi, load_xi_data, print_xi_eval
    store = SnapshotStore(a.db)
    try:
        data = load_xi_data(store, SnapshotProvider(store))
        start = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        print(f"formazioni salvate: {sum(len(v) for v in data.sheets.values())} di {len(data.sheets)} squadre; righe infortuni {len(data.status)}")
        print_xi_eval(evaluate_xi(data, start, end))
    finally:
        store.close()


def cmd_scorer_eval(a) -> None:
    """Fase 9 acceptance test: goalscorer probabilities replayed week by week against who really scored (no API request)."""
    from .scorers import evaluate_scorers, print_scorer_eval
    store = SnapshotStore(a.db)
    try:
        end = datetime.now(timezone.utc)
        start = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
        print_scorer_eval(evaluate_scorers(store, SnapshotProvider(store), _cfg(a), start, end, DOMESTIC))
    finally:
        store.close()


def cmd_lineup_eval(a) -> None:
    """Fase 6-bis acceptance test: log loss with and without the official XI, walk-forward (no API request)."""
    from .lineupeval import evaluate_lineups, print_lineup_eval
    from .modeleval import default_window
    store = SnapshotStore(a.db)
    try:
        start, end = default_window(weeks=a.weeks)
        print_lineup_eval(evaluate_lineups(SnapshotProvider(store), _cfg(a), start, end + timedelta(days=7), DOMESTIC))
    finally:
        store.close()


def cmd_lineup_history_check(a) -> None:
    """One-shot check (a few GOAL requests): does GOAL return the XI of finished matches, recent and a season ago, and do
    their players resolve to the roster? Decides whether the player impact model can be trained on GOAL lineups."""
    from .collector import GoalCollector
    from .domain import Fixture, FixtureStatus
    from .providers.goalapi import shape_summary
    store = SnapshotStore(a.db)
    try:
        client = GoalApiClient(store=store, budget=BudgetGuard(store, "goal-api", daily=1000, reserve=50))
        coll = GoalCollector(client, store, [], TeamNames.load(a.aliases))
        now = datetime.now(timezone.utc)
        print("risultati salvati per competizione e stagione (fixture GOAL):")
        for comp, n, lo, hi in store.db.execute(
                "SELECT competition, COUNT(*), MIN(kickoff), MAX(kickoff) FROM results WHERE fixture_id LIKE 'goal:%' GROUP BY competition ORDER BY 2 DESC LIMIT 12").fetchall():
            print(f"  {comp:<28} {n:>5}  {lo[:10]} .. {hi[:10]}")
        picks = []
        for label, until in (("recente", now), ("un anno fa", now - timedelta(days=330))):
            row = store.db.execute("SELECT fixture_id, competition, home, away, kickoff FROM results WHERE fixture_id LIKE 'goal:%' "
                                   "AND competition = ? AND kickoff <= ? ORDER BY kickoff DESC LIMIT 1", (a.comp, until.isoformat())).fetchone()
            if row:
                picks.append((label, row))
        for label, (fid, comp, home, away, ko) in picks:
            print(f"\n{label}: {home} - {away} {ko[:10]} ({fid})")
            env = client.get(f"/fixtures/{fid.split(':', 1)[1]}/lineups")
            data = env.get("data")
            for line in shape_summary(data)[:25]:
                print("  " + line)
            fx = Fixture(id=fid, competition=comp, home=home, away=away, kickoff=datetime.fromisoformat(ko), status=FixtureStatus.FINISHED)
            roster = SnapshotProvider(store).list_players(comp)
            for l in coll.mapper.lineups(data, fx, now):
                ids = coll._reconcile_players(l.starters, l.team, roster)
                known = {p.id for p in roster}
                print(f"  {l.team}: stato {l.status}, titolari {len(l.starters)}, riconosciuti nella rosa {sum(i in known for i in ids)}")
    finally:
        store.close()


def cmd_quotes_prune(a) -> None:
    """Quotes retention by hand: --dry-run counts what the morning run would delete (no API request)."""
    from .retention import prune_quotes
    store = SnapshotStore(a.db)
    try:
        pr = prune_quotes(store, datetime.now(timezone.utc), max_matches=a.max, max_seconds=a.max_seconds, dry_run=a.dry_run)
        print(f"{'da togliere' if a.dry_run else 'tolti'}: {pr['deleted']} prezzi su {pr['before']} di {pr['matches']} partite finite "
              f"(ultima {pr['last_kickoff'] or '-'})")
    finally:
        store.close()


def cmd_registry_calibration(a) -> None:
    """Registry calibration against Pinnacle and Sisal closing prices: model error or luck of the matches (no API request)."""
    from .registrycal import load_rows, print_report, report
    store = SnapshotStore(a.db)
    try:
        print_report(report(load_rows(store)))
    finally:
        store.close()


def cmd_registry_check(a) -> None:
    """Registry diagnosis (no API request): slips recorded per day and per run, and for the matches of a team its recorded
    selections, the half-time / goal-order details stored and the API-Football day reads."""
    store = SnapshotStore(a.db)
    print("schedine registrate per giorno (ultimi 7):")
    for day, n in store.db.execute("SELECT substr(created_at, 1, 10) d, COUNT(*) FROM paper_slips GROUP BY d ORDER BY d DESC LIMIT 7").fetchall():
        print(f"  {day}: {n}")
    print("ultime pubblicazioni e schedine proposte:")
    for rid, at in store.db.execute("SELECT id, created_at FROM pub_runs ORDER BY id DESC LIMIT 6").fetchall():
        keys = store.db.execute("SELECT COUNT(*) FROM pub_slips WHERE run_id = ?", (rid,)).fetchone()[0]
        mine = store.db.execute("SELECT COUNT(*) FROM paper_slips WHERE run_id = ?", (rid,)).fetchone()[0]
        print(f"  run {rid} {at}: {keys} proposte, {mine} registrate per la prima volta in questo run")
    if not a.team:
        return
    legs = store.db.execute("SELECT fixture_id, sel_key, kickoff, result, settled_at, score FROM paper_legs WHERE match LIKE ? "
                            "ORDER BY kickoff DESC LIMIT 40", (f"%{a.team}%",)).fetchall()
    fids = sorted({l[0] for l in legs})
    for fid in fids:
        link = store.db.execute("SELECT ext_id FROM fixture_links WHERE source = 'api-football' AND fixture_id = ?", (fid,)).fetchone()
        print(f"partita {fid}: api-football {link[0] if link else 'NON COLLEGATA'}; 1H {store.stats_of(fid, '1H')}; "
              f"FT {store.stats_of(fid, 'FT')}")
    for fid, key, ko, res, at, score in legs:
        print(f"  {ko} {key}: {res} ({score}, chiusa {at})")
    for at, params, status in store.db.execute("SELECT fetched_at, params, status FROM raw_requests WHERE source = 'api-football' "
                                               "AND endpoint IN ('/fixtures', '/fixtures/events') ORDER BY id DESC LIMIT 10").fetchall():
        print(f"  api-football {at} {params} {status}")


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


def cmd_dataset_report(a) -> None:
    """Season CSV link report: per file the link rate, and for each unlinked row our matches of those clubs within 3 days."""
    store = SnapshotStore(a.db)
    try:
        for name, detail in store.db.execute("SELECT name, detail FROM jobs WHERE name LIKE 'dataset-report:%' ORDER BY name").fetchall():
            d = json.loads(detail or "{}")
            print(f"{name.split(':', 2)[2]}: {d.get('linked')}/{d.get('rows')} abbinate")
            for u in d.get("unmatched", [])[:a.examples]:
                teams, day = u.rsplit(" ", 1)
                when = datetime.strptime(day, "%d/%m/%Y").replace(tzinfo=timezone.utc)
                home, away = teams.split("-", 1)
                near = store.db.execute(
                    "SELECT home, away, kickoff FROM results WHERE kickoff BETWEEN ? AND ? AND (home LIKE ? OR away LIKE ? OR home LIKE ? OR away LIKE ?) "
                    "ORDER BY kickoff", ((when - timedelta(days=3)).isoformat(), (when + timedelta(days=4)).isoformat(),
                                         f"%{home[:5]}%", f"%{home[:5]}%", f"%{away[:5]}%", f"%{away[:5]}%")).fetchall()
                n_day = store.db.execute("SELECT COUNT(*) FROM results WHERE substr(kickoff, 1, 10) = ?", (f"{when:%Y-%m-%d}",)).fetchone()[0]
                print(f"  - {u}: nostre partite quel giorno {n_day}; vicine: " + ("; ".join(f"{h}-{w} {k[:16]}" for h, w, k in near) or "nessuna"))
        _duplicate_report(store, a.examples)
        _national_names_report(store)
    finally:
        store.close()


def cmd_stat_coverage(a) -> None:
    """Fase 7 data check (no request): per stat name and source the matches with a full-time figure and the mean per team,
    then per competition the share of results with corners and with cards, and the Sisal corner/booking quotes stored."""
    store = SnapshotStore(a.db)
    try:
        rows = store.db.execute("SELECT stat, source, COUNT(*), AVG(home), AVG(away) FROM match_stats WHERE period='FT' "
                                "GROUP BY stat, source ORDER BY COUNT(*) DESC").fetchall()
        print("statistiche (FT): nome, fonte, partite, media casa / ospite")
        for stat, src, n, h, w in rows:
            if a.all or re.search(r"corner|card|yellow|red|booking|foul", stat):
                print(f"  {stat:28s} {src:14s} {n:6d}  {h or 0:5.2f} / {w or 0:5.2f}")
        print("copertura per competizione: risultati, con corner, con cartellini")
        for comp, n, nc, nk in store.db.execute(
                "SELECT r.competition, COUNT(*), "
                "SUM(EXISTS(SELECT 1 FROM match_stats s WHERE s.fixture_id=r.fixture_id AND s.period='FT' AND s.stat LIKE '%corner%')), "
                "SUM(EXISTS(SELECT 1 FROM match_stats s WHERE s.fixture_id=r.fixture_id AND s.period='FT' AND s.stat LIKE '%yellow%')) "
                "FROM results r GROUP BY r.competition ORDER BY COUNT(*) DESC").fetchall():
            print(f"  {comp:34s} {n:6d}  corner {nc:6d} ({100 * nc / max(n, 1):3.0f}%)  cartellini {nk:6d} ({100 * nk / max(n, 1):3.0f}%)")
        print("quote Sisal corner / cartellini salvate: mercato, quote, partite")
        for code, n, nf in store.db.execute(
                "SELECT market_code, COUNT(*), COUNT(DISTINCT fixture_id) FROM quotes WHERE bookmaker LIKE 'sisal%' "
                "AND (market_code LIKE 'CORNERS%' OR market_code LIKE 'CARDS%') GROUP BY market_code").fetchall():
            print(f"  {code:24s} {n:7d} {nf:5d}")
    finally:
        store.close()


def cmd_stat_eval(a) -> None:
    """Fase 7: walk-forward log loss of the corners / cards models against simple baselines (no request)."""
    from .stateval import evaluate_stat, print_stat_report
    store = SnapshotStore(a.db)
    try:
        prov = SnapshotProvider(store)
        end = datetime.now(timezone.utc)
        for stat in a.stats:
            print_stat_report(stat, evaluate_stat(prov, stat, end - timedelta(weeks=a.weeks), end))
    finally:
        store.close()


def _duplicate_report(store: SnapshotStore, examples: int) -> None:
    """Results stored twice (same clubs, kickoff within 3h, different ids): count, and for a few pairs the raw GOAL rows of
    both ids with the request that returned them, to see where the second id comes from."""
    rows = store.db.execute(
        "SELECT a.fixture_id, b.fixture_id FROM results a JOIN results b ON a.home=b.home AND a.away=b.away AND a.fixture_id<b.fixture_id "
        "AND abs(julianday(a.kickoff)-julianday(b.kickoff)) <= 0.125 ORDER BY a.kickoff").fetchall()
    n_rows = store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    n_read = len(SnapshotProvider(store).list_history(None, datetime(2100, 1, 1, tzinfo=timezone.utc)))
    print(f"results duplicati: {len(rows)} coppie; righe {n_rows}, partite lette dal modello {n_read}")
    pairs = rows[:examples]
    want = {fid.split(":", 1)[-1]: fid for p in pairs for fid in p}
    seen: dict[str, list[str]] = {}
    first: dict[str, tuple] = {}  # one read per distinct payload (pages fetched again unchanged share a blob)
    for rid, ep, params, fetched, h in store.db.execute(
            "SELECT id, endpoint, params, fetched_at, hash FROM raw_requests WHERE source='goal-api' AND endpoint LIKE '/leagues/%' "
            "AND status=200 ORDER BY id").fetchall():
        first.setdefault(h, (rid, ep, params, fetched))
    for h, (rid, ep, params, fetched) in first.items():
        try:
            data = json.loads(store.raw_body(rid)).get("data") or []
        except (ValueError, AttributeError):
            continue
        for row in data if isinstance(data, list) else [data]:
            key = next((str(row[k]) for k in ("fixtureId", "id", "matchId", "matchApiId") if isinstance(row, dict) and row.get(k) is not None), None)
            if key in want:
                hits = seen.setdefault(want[key], [])
                if len(hits) < 3:
                    hits.append(f"raw {rid} {ep} {params} {fetched[:16]}: {json.dumps(row, ensure_ascii=False, sort_keys=True)[:900]}")
    for pair in pairs:
        print("==")
        for fid in pair:
            r = store.db.execute("SELECT competition, home, away, kickoff, home_goals, away_goals, observed_at FROM results WHERE fixture_id=?",
                                 (fid,)).fetchone()
            n_fx = store.db.execute("SELECT COUNT(*), MIN(observed_at), MAX(status) FROM fixtures WHERE fixture_id=?", (fid,)).fetchone()
            n_q = store.db.execute("SELECT COUNT(*) FROM quotes WHERE fixture_id=?", (fid,)).fetchone()[0]
            n_l = store.db.execute("SELECT COUNT(*) FROM fixture_links WHERE fixture_id=?", (fid,)).fetchone()[0] if _has_table(store, "fixture_links") else "-"
            print(f"  {fid}: {r} | fixtures {n_fx} | quotes {n_q} | links {n_l}")
            for h in seen.get(fid, ["nessun grezzo trovato"]):
                print(f"    {h}")


def _national_names_report(store: SnapshotStore) -> None:
    """National teams of our competitions that the international results do not know (they get no Elo prior): add an alias."""
    from .elo import latest_international, national_timeline
    from .modeleval import group_of
    body = latest_international(store)
    if not body:
        print("risultati internazionali: non ancora scaricati")
        return
    known = national_timeline(body, TeamNames.load("configs/team_aliases.json")).teams()
    teams = {t for c, h, w in store.db.execute("SELECT competition, home, away FROM results UNION SELECT competition, home, away FROM fixtures")
             .fetchall() if group_of(c) == "nazionali" for t in (h, w)}
    missing = sorted(teams - known)
    print(f"nazionali: {len(teams) - len(missing)}/{len(teams)} con Elo" + (f"; senza: {', '.join(missing)}" if missing else ""))


def _has_table(store: SnapshotStore, name: str) -> bool:
    return store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def cmd_model_check(a) -> None:
    """Expected goals of the upcoming matches of a team, current model vs variants (no API request): to see why the model
    prices a match the way it does."""
    import copy
    from .engine import Engine
    from .markets import probability
    from .domain import SelectionRef
    store = SnapshotStore(a.db)
    try:
        prov = SnapshotProvider(store)
        now = datetime.now(timezone.utc)
        fx = [f for f in prov.list_fixtures(None, now, now + timedelta(days=14)) if a.team.lower() in (f.home + " " + f.away).lower()]
        base = _cfg(a)
        variants = {"attuale": {}, "senza-storico-nazionali": {"national_history_years": 0.0}}
        o75 = SelectionRef(market_code="TOTAL_GOALS", selection="OVER", line=7.5)
        for name, ch in variants.items():
            c = copy.deepcopy(base)
            for k, v in ch.items():
                setattr(c.model, k, v)
            eng = Engine(prov, c, use_lineups=False)
            for f in fx[:6]:
                fitted = eng.fit(f.competition, now)
                if not fitted:
                    print(f"[{name}] {f.home}-{f.away}: nessun modello")
                    continue
                m = fitted[0]
                lh, la = m.expected_goals(f.home, f.away)
                mat = m.score_matrix(f.home, f.away)
                print(f"[{name}] {f.home}-{f.away} ({f.competition}): xG {lh:.2f}-{la:.2f}, Over 7.5 {probability(mat, o75):.1%}, "
                      f"partite nello storico {m.n_matches.get(f.home, 0)}/{m.n_matches.get(f.away, 0)}, "
                      f"livello gol {m.mu_comp.get(f.competition, m.mu):.2f}, casa {m.home_adv:.2f}")
                for t in (f.home, f.away):
                    if t in m.teams:
                        k = m.teams[t]
                        print(f"    {t}: attacco {m.attack[k]:+.2f} difesa {m.defence[k]:+.2f}")
            if name == "attuale":
                clubs, nations = eng.elo()
                for f in fx[:6]:
                    print("    Elo nazionali: " + ", ".join(f"{t} {nations.at(t, now):.0f}" if nations and nations.at(t, now) else f"{t} -"
                                                       for t in (f.home, f.away)))
        for f in fx[:2]:
            for t in (f.home, f.away):
                rows = [r for r in prov.list_history(None, now) if t in (r.home, r.away)][-8:]
                print(f"  ultime di {t}: " + "; ".join(f"{r.home} {r.home_goals}-{r.away_goals} {r.away} ({r.kickoff:%d/%m/%y})" for r in rows))
    finally:
        store.close()


def cmd_quality(a) -> None:
    """Weekly model quality replay (no API request); --save stores it for the "Qualità del modello" page."""
    from .modeleval import default_window
    from .quality import print_quality, run_quality, save_quality
    store = SnapshotStore(a.db)
    try:
        start, end = default_window(weeks=a.weeks)
        cfg = _cfg(a)
        rep = run_quality(SnapshotProvider(store), cfg, start, end)
        print_quality(rep)
        if a.save:
            print(f"salvato (quality run {save_quality(store, rep, start, end, cfg.model.version)})")
            if rep["_params"]:
                from .meta import save_meta
                print(f"meta-modello salvato (id {save_meta(store, rep['_params'], start, end, cfg.model.version)})")
    finally:
        store.close()


def cmd_boost_eval(a) -> None:
    """Fase 10: walk-forward replay of the LightGBM correction on top of Dixon-Coles (no API request)."""
    from .boost import evaluate_boost, print_boost
    from .modeleval import default_window
    store = SnapshotStore(a.db)
    try:
        start, end = default_window(weeks=a.weeks)
        print_boost(evaluate_boost(SnapshotProvider(store), _cfg(a), start, end, history_weeks=a.history, retrain_every=a.retrain))
    finally:
        store.close()


def cmd_boost_train(a) -> None:
    """Fase 10 shadow test: train the booster once on the matches before boost.SHADOW_FROM and freeze it (no API request)."""
    from .boost import DESIGN, load_booster, train_frozen
    store = SnapshotStore(a.db)
    try:
        if load_booster(store) and not a.force:
            print(f"booster {DESIGN} già congelato: il test in ombra usa quello (--force per sostituirlo e ricominciare)")
            return
        rid, b, n = train_frozen(SnapshotProvider(store), _cfg(a), store, datetime.now(timezone.utc))
        print(f"booster {DESIGN} congelato (id {rid}): {n} partite di addestramento, {b.best} giri utili")
        if b.importance():
            print("peso delle variabili: " + ", ".join(f"{k} {v:.0%}" for k, v in list(b.importance().items())[:8]))
    finally:
        store.close()


def cmd_boost_shadow(a) -> None:
    """Fase 10 shadow test: the frozen booster against Dixon-Coles on the cup matches since it was frozen (no API request)."""
    from .boost import print_shadow, save_shadow, shadow_report
    from .modeleval import default_window
    store = SnapshotStore(a.db)
    try:
        now = datetime.now(timezone.utc)
        rep = shadow_report(SnapshotProvider(store), _cfg(a), store, default_window(now, 1)[1])
        if rep is None:
            print("nessun booster congelato: prima boost-train")
            return
        print_shadow(rep)
        if a.save:
            save_shadow(store, rep, now)
    finally:
        store.close()


def cmd_model_eval(a) -> None:
    """Walk-forward comparison of model variants on the stored results (no API request)."""
    from .modeleval import VARIANTS, default_window, evaluate, print_report
    store = SnapshotStore(a.db)
    try:
        start, end = default_window(weeks=a.weeks)
        chosen = {k: v for k, v in VARIANTS.items() if not a.variants or k in a.variants}
        rep = evaluate(SnapshotProvider(store), _cfg(a), start, end, chosen)
        print_report(rep)
    finally:
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
    _print_stats(remap_stored_odds(store, TeamNames.load(a.aliases), only_stats=a.only_stats))
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
    from . import actionsminutes as am
    started = datetime.now(timezone.utc)
    daily = started.hour == 6 and started.minute < 30
    if daily or not store.usage(am.SOURCE, am.month_key(started)):  # every morning, and at once when the month has no count
        try:
            print(f"minuti GitHub del mese (da GitHub): {am.sync_month(store, started)}")
        except Exception as e:  # noqa: BLE001 - the count is a guard, never a reason to lose the morning run
            print(f"minuti GitHub: conteggio non riuscito ({type(e).__name__}: {e})")
    lvl, used = am.month_level(store, started)
    if daily:
        try:
            msg = am.dispatch_quality(store, Config.load(None).model.version, started)
            if msg:
                print(msg)
        except Exception as e:  # noqa: BLE001 - retried next morning, never a reason to lose this run
            print(f"verifica qualità: avvio non riuscito ({type(e).__name__}: {e})")
    if lvl == 2 and not daily and not (a.manual or a.history or a.force_publish):
        print(f"minuti GitHub: {used}/{am.BUDGET} usati, solo il giro del mattino fino a fine mese")
        am.record_run(store, started)
        store.close()
        return
    goal = odds = None
    if cfg.goal_leagues and (os.environ.get("GOALAPI_KEY") or os.environ.get("GOAL_API_KEY")):
        gc = GoalApiClient(store=store, budget=BudgetGuard(store, "goal-api", daily=cfg.goal_daily_limit, reserve=cfg.goal_reserve))
        goal = GoalCollector(gc, store, cfg.goal_leagues, names)
    if cfg.oddspapi_tournaments and (os.environ.get("ODDSPAPI_API_KEY") or os.environ.get("ODDSPAPI_KEY")):
        oc = OddsPapiClient(store=store, budget=BudgetGuard(store, "oddspapi", monthly=cfg.oddspapi_monthly_limit, reserve=cfg.oddspapi_reserve))
        odds = OddsCollector(oc, store, cfg.oddspapi_tournaments, cfg.bookmakers, names, history_books=cfg.history_bookmakers)
    apif = None
    if any(l.apif for l in cfg.leagues) and os.environ.get("APIFOOTBALL_KEY"):
        from .apifcollector import ApifLeague, ApiFootballCollector
        from .providers.apifootball import ApiFootballClient
        ac = ApiFootballClient(store=store, budget=BudgetGuard(store, "api-football", daily=cfg.apif_daily_limit, reserve=cfg.apif_reserve))
        apif = ApiFootballCollector(ac, store, [ApifLeague(l.apif, l.name, bool(l.fd)) for l in cfg.leagues if l.apif], names,
                                    squads_per_day=cfg.apif_squads_per_day)
    from .fdcollector import FootballDataCollector
    datasets = FootballDataCollector(store, cfg.divisions, names) if cfg.divisions else None  # public files, no key
    t0 = time.monotonic()

    def show(st):
        _print_stats(st)
        print(f"  ({time.monotonic() - t0:.0f}s dall'inizio)", flush=True)
    try:
        # season files and international results run after the publication: they are never worth a late analysis
        results = run_tick(store, cfg, goal, odds, on_step=show, max_seconds=a.max_seconds, manual=a.manual, history=a.history,
                           datasets=datasets, skip=("datasets",), apif=apif)
        if not results:
            print("tick: niente da fare")
        from .autorun import should_publish, transient_db_error
        from .publish import analyze_and_publish, last_publication
        if a.publish and (a.force_publish or should_publish(results, last_publication(store), datetime.now(timezone.utc))):
            if time.monotonic() - t0 > a.max_seconds + 60:
                print("analisi: rimandata (tempo del giro esaurito)")
            else:
                try:
                    rid, res = analyze_and_publish(store)
                    print(f"analisi pubblicata (run {rid}): {len(res.fixtures)} partite, {len(res.opportunities)} mercati, "
                          f"{len(res.optimizer.slips)} schedine" + (" — NO BET" if res.optimizer.no_bet else "") +
                          f" ({time.monotonic() - t0:.0f}s dall'inizio)")
                except Exception as e:  # noqa: BLE001 - a dropped connection: the next tick publishes (the analysis is older)
                    if not transient_db_error(e):
                        raise
                    store.recover()
                    print(f"analisi: rimandata al prossimo giro (connessione a Turso caduta: {e})", flush=True)
        from .paper import settle
        try:
            paper = settle(store, SnapshotProvider(store))
        except Exception as e:  # noqa: BLE001 - settling is idempotent: the next tick closes what is left
            if not transient_db_error(e):
                raise
            store.recover()
            paper = {}
            print(f"registro: chiusura rimandata al prossimo giro (connessione a Turso caduta: {e})", flush=True)
        if any(paper.values()):
            print("registro: chiuse " + ", ".join(f"{v} {k}" for k, v in paper.items() if v), flush=True)
        from .autorun import DATASETS_SECONDS, run_datasets
        left = min(DATASETS_SECONDS, a.max_seconds + 300 - (time.monotonic() - t0))  # the job has 20 minutes
        if left >= 60:
            run_datasets(store, cfg, datasets, datetime.now(timezone.utc), left, on_step=show)
        # the minute this job already pays for: its unused seconds read the most overdue Sisal prices (free requests)
        free = am.free_seconds()
        if odds is not None and free >= 14:
            from .autorun import freshness_due
            due = freshness_due(store, datetime.now(timezone.utc))
            if due:
                st = odds.sync_prematch_history(only=due, max_seconds=free - 8)
                print(f"riempimento del minuto pagato ({free:.0f}s liberi): {st.requests} storici letti su {len(due)} da aggiornare", flush=True)
        now = datetime.now(timezone.utc)
        if a.summary:  # row counts over the whole database: about a minute on Turso, so not on every tick
            print("Riepilogo database: " + ", ".join(f"{k}={v}" for k, v in store.stats().items()))
            by_comp: dict[str, int] = {}
            for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=cfg.fixtures_days)):
                by_comp[f.competition] = by_comp.get(f.competition, 0) + 1
            print("Partite in calendario: " + (", ".join(f"{k} {v}" for k, v in sorted(by_comp.items())) or "nessuna"))
        print("Budget: " + ", ".join(f"{s} {store.usage(s, f'D{now:%Y-%m-%d}')} oggi / {store.usage(s, f'M{now:%Y-%m}')} mese"
                                     for s in ("goal-api", "oddspapi", "api-football")))
        late = not daily and goal is not None and late_backfill_due(SnapshotProvider(store), now)
        if (daily or late) and goal is not None:
            # Fase 6-bis / 9: XI and goal events of finished domestic matches with the GOAL requests the day leaves over (newest
            # first: 2025/26, then 2024/25, then every new matchday), 8 requests at a time. In the morning 120 requests stay for
            # the day's ticks; in the last evening run (no kickoff left before the reset at 00:00 UTC) only LATE_KEEP do, so the
            # day's unused requests are not lost.
            try:
                lst, left = lineup_backfill(store, names, now, "2024-07-01", 900, 240 if daily else 120, 120 if daily else LATE_KEEP)
                if lst is not None:
                    print(f"storico formazioni ed eventi{' (sera)' if late else ''}: {lst.requests} richieste, {lst.saved}; ancora da leggere: formazioni {left[0]}, "
                          f"eventi {left[1]}", flush=True)
            except Exception as e:  # noqa: BLE001 - history only: never a reason to lose the morning run
                print(f"storico formazioni ed eventi: non riuscito ({type(e).__name__}: {e})")
        if daily:
            # quotes retention: finished matches keep only the price points anything reads again (Turso free plan: 5 GB)
            try:
                from .retention import prune_quotes
                pr = prune_quotes(store, now, max_seconds=90)
                if pr["matches"]:
                    print(f"quote sfoltite: {pr['matches']} partite finite, tolti {pr['deleted']} prezzi su {pr['before']}", flush=True)
            except Exception as e:  # noqa: BLE001 - housekeeping: never a reason to lose the morning run
                print(f"sfoltimento quote: non riuscito ({type(e).__name__}: {e})")
        failed: list[str] = []
        if daily or a.health:
            from .health import print_health, run_health, save_health
            checks, extra = run_health(store, now, Config.load(None).model.version)
            print_health(checks)
            save_health(store, checks, extra, now)
            failed = [f"{c.label}: {c.detail}" for c in checks if c.level == "error"]
        n = am.record_run(store, now)
        if n:
            print(f"minuti GitHub: questo run {n}, mese {store.usage(am.SOURCE, am.month_key(now))}/{am.BUDGET}")
    finally:
        store.close()
    if failed:  # all the work is done: failing now only sends GitHub's e-mail to the owner
        for f in failed:
            print(f"::error::{f}")
        sys.exit(1)


def cmd_health(a) -> None:
    """Health check of the whole chain plus database size per table (no API request)."""
    from .health import print_health, run_health, save_health
    store = SnapshotStore(a.db)
    try:
        now = datetime.now(timezone.utc)
        checks, extra = run_health(store, now, Config.load(None).model.version)
        print_health(checks)
        if extra.get("tables"):
            print("Spazio per tabella:")
            for t, b in extra["tables"].items():
                print(f"  {t:<22} {b / 1e6:8.1f} MB")
        if a.save:
            save_health(store, checks, extra, now)
    finally:
        store.close()


def cmd_db_backup(a) -> None:
    """Compact gzipped copy of the database for the weekly backup (read from the local replica, no API request)."""
    from pathlib import Path
    from .health import backup, replica_path
    src = Path(a.src) if a.src else replica_path()
    if src is None or not src.exists():
        sys.exit("nessun file di database da copiare (serve TURSO_MODE=replica o --src)")
    r = backup(src, Path(a.out), tuple(a.skip))
    print(f"backup {a.out}: {r['raw_bytes'] / 1e6:.0f} MB compattato, {r['gz_bytes'] / 1e6:.0f} MB compresso"
          + (f" · senza {', '.join(r['skipped'])}" if r["skipped"] else ""))
    print("righe: " + ", ".join(f"{t} {n}" for t, n in r["rows"].items()))


def cmd_apif(a) -> None:
    """API-Football: `status` (free), `probe` (1 counted request) or `test` (4 counted requests on one league: finished
    fixtures, their lineups/players in one batch, injuries, next fixtures)."""
    from .providers.apifootball import ApiFootballClient, ApiFootballError
    from .providers.goalapi import shape_summary
    store = SnapshotStore(a.db)
    try:
        c = ApiFootballClient(store=store)
        print(f"stato: {c.status()}")
        if a.apif_cmd == "probe":
            env = c.get(a.path, dict(kv.split("=", 1) for kv in a.param or []))
            print(f"{a.path}: {env.get('results')} risultati")
            for line in shape_summary(env.get("response"))[: a.max_lines]:
                print("  " + line)
        elif a.apif_cmd == "test":
            # the free plan refuses the current season as a parameter, so everything goes through dates and fixture ids
            lg = a.league
            day = c.get("/fixtures", {"date": a.past})["response"]
            past = [f for f in day if f["league"]["id"] == lg][:3]
            print(f"\n1) partite del {a.past}: {len(day)} in tutto, {len(past)} della lega {lg} (stagione {past[0]['league']['season'] if past else '?'})")
            for f in past:
                print(f"   {f['fixture']['id']} {f['fixture']['date'][:16]} {f['teams']['home']['name']}-{f['teams']['away']['name']} {f['goals']}")
            if past:
                try:
                    full = c.get("/fixtures", {"ids": "-".join(str(f["fixture"]["id"]) for f in past)})["response"]
                    print(f"\n2) dettaglio di {len(full)} partite in 1 richiesta (ids=...):")
                    for f in full:
                        lu = f.get("lineups") or []
                        pl = f.get("players") or []
                        print(f"   {f['teams']['home']['name']}-{f['teams']['away']['name']}: formazioni {len(lu)} squadre "
                              f"({[len(x.get('startXI') or []) for x in lu]} titolari), statistiche giocatori {sum(len(t.get('players') or []) for t in pl)}, "
                              f"eventi {len(f.get('events') or [])}, statistiche squadra {len(f.get('statistics') or [])}")
                    if full:
                        for line in shape_summary(full[0].get("lineups"))[:20]:
                            print("     " + line)
                except ApiFootballError as e:
                    print(f"\n2) dettaglio per id: {e}")
            try:
                inj = c.get("/injuries", {"date": a.next})["response"]
                mine = [r for r in inj if r["league"]["id"] == lg]
                print(f"\n3) infortuni e squalifiche del {a.next}: {len(inj)} righe in tutto, {len(mine)} della lega {lg}")
                for r in mine[:10]:
                    print(f"   {r['team']['name']}: {r['player']['name']} ({r['player'].get('type')}: {r['player'].get('reason')})")
            except ApiFootballError as e:
                print(f"\n3) infortuni per data: {e}")
            try:
                nxt = [f for f in c.get("/fixtures", {"date": a.next})["response"] if f["league"]["id"] == lg]
                print(f"\n4) partite della lega {lg} il {a.next}: {len(nxt)}")
                for f in nxt[:10]:
                    print(f"   {f['fixture']['id']} {f['fixture']['date'][:16]} {f['teams']['home']['name']}-{f['teams']['away']['name']}")
            except ApiFootballError as e:
                print(f"\n4) partite per data: {e}")
        print(f"\nrichieste conteggiate in questo run: {c.counted_sent} · limiti dalla risposta: {c.remaining}")
    except ApiFootballError as e:
        sys.exit(f"errore: {e}")


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
    af = sub.add_parser("apif", help="API-Football: status | probe | test (chiave in APIFOOTBALL_KEY)")
    afs = af.add_subparsers(dest="apif_cmd", required=True)
    for name in ("status", "probe", "test"):
        sp = afs.add_parser(name)
        if name == "probe":
            sp.add_argument("path")
            sp.add_argument("--param", action="append")
            sp.add_argument("--max-lines", type=int, default=80)
        if name == "test":
            sp.add_argument("--league", type=int, default=135)  # Serie A
            sp.add_argument("--past", default="2026-09-27", help="giornata già giocata (AAAA-MM-GG)")
            sp.add_argument("--next", default="2026-10-04", help="giornata in arrivo (AAAA-MM-GG)")
        sp.add_argument("--db", default="algowinbet.db")
        sp.set_defaults(fn=cmd_apif)
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
    rm.add_argument("--only-stats", action="store_true", help="aggiunge solo le quote corner e cartellini, non cancella nulla")
    rm.set_defaults(fn=cmd_remap_odds)
    se = sub.add_parser("stat-eval", help="Fase 7: verifica walk-forward dei modelli corner e cartellini (nessuna richiesta)")
    se.add_argument("--db", default="turso")
    se.add_argument("--weeks", type=int, default=26)
    se.add_argument("--stats", nargs="+", default=["corners", "cards"])
    se.set_defaults(fn=cmd_stat_eval)
    sc = sub.add_parser("stat-coverage", help="Fase 7: corner e cartellini salvati per fonte e competizione (nessuna richiesta)")
    sc.add_argument("--db", default="turso")
    sc.add_argument("--all", action="store_true", help="tutte le statistiche, non solo corner e cartellini")
    sc.set_defaults(fn=cmd_stat_coverage)
    dr = sub.add_parser("dataset-report", help="abbinamento dei CSV stagionali football-data alle nostre partite (nessuna richiesta)")
    dr.add_argument("--db", default="turso")
    dr.add_argument("--examples", type=int, default=6)
    dr.set_defaults(fn=cmd_dataset_report)
    mk = sub.add_parser("model-check", help="gol attesi del modello per le prossime partite di una squadra (nessuna richiesta)")
    mk.add_argument("team")
    mk.add_argument("--db", default="turso")
    mk.add_argument("--config")
    mk.set_defaults(fn=cmd_model_check)
    qa = sub.add_parser("quality", help="replay settimanale della qualità del modello (nessuna richiesta API)")
    qa.add_argument("--db", default="turso")
    qa.add_argument("--weeks", type=int, default=60)
    qa.add_argument("--save", action="store_true", help="salva il report per la pagina Qualità del modello")
    qa.add_argument("--config")
    qa.set_defaults(fn=cmd_quality)
    bt = sub.add_parser("boost-train", help="Fase 10: addestra e congela il booster del test in ombra (nessuna richiesta API)")
    bt.add_argument("--db", default="turso")
    bt.add_argument("--force", action="store_true")
    bt.add_argument("--config")
    bt.set_defaults(fn=cmd_boost_train)
    bs = sub.add_parser("boost-shadow", help="Fase 10: booster congelato contro Dixon-Coles sulle coppe nuove (nessuna richiesta API)")
    bs.add_argument("--db", default="turso")
    bs.add_argument("--save", action="store_true")
    bs.add_argument("--config")
    bs.set_defaults(fn=cmd_boost_shadow)
    be = sub.add_parser("boost-eval", help="Fase 10: LightGBM sopra Dixon-Coles, replay walk-forward (nessuna richiesta API)")
    be.add_argument("--db", default="turso")
    be.add_argument("--weeks", type=int, default=60)
    be.add_argument("--history", type=int, default=70, help="settimane prima della prova usate solo per addestrare")
    be.add_argument("--retrain", type=int, default=4, help="riaddestra ogni N settimane")
    be.add_argument("--config")
    be.set_defaults(fn=cmd_boost_eval)
    me = sub.add_parser("model-eval", help="confronto walk-forward delle varianti del modello sui risultati salvati (nessuna richiesta)")
    me.add_argument("--db", default="turso")
    me.add_argument("--weeks", type=int, default=52)
    me.add_argument("--variants", nargs="*", help="es. base senza-elo-nazionali campionati elo-club emivita-180 emivita-540 tutto")
    me.add_argument("--config")
    me.set_defaults(fn=cmd_model_eval)
    mc = sub.add_parser("market-coverage", help="mercati quotati da un bookmaker nelle fotografie salvate (nessuna richiesta API)")
    mc.add_argument("--db", default="turso")
    mc.add_argument("--book", default="sisal")
    mc.add_argument("--snapshots", type=int, default=4)
    mc.set_defaults(fn=cmd_market_coverage)
    rd = sub.add_parser("results-day", help="partite di un giorno e risultati salvati (nessuna richiesta API)")
    rd.add_argument("day", help="AAAA-MM-GG (UTC)")
    rd.add_argument("--comp", default="")
    rd.add_argument("--db", default="algowinbet.db")
    rd.set_defaults(fn=cmd_results_day)
    ins = sub.add_parser("inspect", help="quote grezze OddsPapi vs quote salvate per le partite di una squadra (nessuna richiesta API)")
    ins.add_argument("team")
    ins.add_argument("--db", default="algowinbet.db")
    ins.add_argument("--all-markets", action="store_true")
    ins.set_defaults(fn=cmd_inspect)
    bn = sub.add_parser("db-bench", help="tempi di scrittura su Turso (replica locale contro connessione diretta)")
    bn.add_argument("--replica", default="data/turso-replica.db")
    bn.add_argument("--rows", type=int, default=80)
    bn.set_defaults(fn=cmd_db_bench)
    rl = sub.add_parser("raw-last", help="ultime risposte grezze salvate di un endpoint e loro struttura (nessuna richiesta API)")
    rl.add_argument("endpoint", help="parte del path, es. /lineups")
    rl.add_argument("--source", default="goal")
    rl.add_argument("--n", type=int, default=10)
    rl.add_argument("--max-lines", type=int, default=120)
    rl.add_argument("--dump", type=int, default=3000, help="caratteri del JSON grezzo da stampare (0 = nessuno)")
    rl.add_argument("--db", default="algowinbet.db")
    rl.set_defaults(fn=cmd_raw_last)
    lt = sub.add_parser("lineup-timing", help="quando sono arrivate le formazioni delle ultime partite (nessuna richiesta API)")
    lt.add_argument("--days", type=float, default=7)
    lt.add_argument("--db", default="algowinbet.db")
    lt.set_defaults(fn=cmd_lineup_timing)
    sc = sub.add_parser("scorer-check", help="Fase 9: dati sui marcatori già salvati (nessuna richiesta API)")
    sc.add_argument("--days", type=int, default=14)
    sc.add_argument("--max-raw", type=int, default=400)
    sc.add_argument("--max-lines", type=int, default=60)
    sc.add_argument("--db", default="algowinbet.db")
    sc.set_defaults(fn=cmd_scorer_check)
    ap = sub.add_parser("analysis-preview",help="analisi della prossima settimana senza pubblicarla (nessuna richiesta)")
    ap.add_argument("--db", default="turso")
    ap.add_argument("--days", type=float, default=7.0)
    ap.set_defaults(fn=cmd_analysis_preview)
    pg = sub.add_parser("registry-purge-stats", help="cancella dal registro le giocate corner/cartellini fuori dalle competizioni di club")
    pg.add_argument("--db", default="turso")
    pg.add_argument("--config", default="configs/collect.json")
    pg.add_argument("--apply", action="store_true", help="senza: solo elenco, nessuna cancellazione")
    pg.set_defaults(fn=cmd_registry_purge_stats)
    rb = sub.add_parser("referees-backfill", help="Fase 7: arbitri delle partite passate dai dati già salvati (nessuna richiesta)")
    rb.add_argument("--db", default="turso")
    rb.add_argument("--config", default="configs/collect.json")
    rb.add_argument("--aliases", default="configs/team_aliases.json")
    rb.set_defaults(fn=cmd_referees_backfill)
    sk = sub.add_parser("snapshot-check", help="righe delle ultime fotografie OddsPapi e loro collegamento (nessuna richiesta)")
    sk.add_argument("text", nargs="?", default="", help="id torneo OddsPapi, es. 23755")
    sk.add_argument("--db", default="turso")
    sk.add_argument("--aliases", default="configs/team_aliases.json")
    sk.set_defaults(fn=cmd_snapshot_check)
    qk = sub.add_parser("quotes-check", help="quote Sisal delle prossime partite e collegamento OddsPapi (nessuna richiesta)")
    qk.add_argument("text", nargs="?", default="")
    qk.add_argument("--db", default="turso")
    qk.set_defaults(fn=cmd_quotes_check)
    rc = sub.add_parser("registry-check", help="diagnosi del registro: schedine per giorno, dettagli di una partita (nessuna richiesta API)")
    rc.add_argument("team", nargs="?", default="")
    rc.add_argument("--db", default="turso")
    rc.set_defaults(fn=cmd_registry_check)
    lb = sub.add_parser("lineups-backfill", help="formazioni delle partite finite dei 7 campionati da GOAL (1 richiesta a partita)")
    lb.add_argument("--since", default="2025-07-01")
    lb.add_argument("--max", type=int, default=900)
    lb.add_argument("--max-seconds", type=float, default=840)
    lb.add_argument("--keep", type=int, default=120, help="richieste GOAL di oggi lasciate ai giri normali")
    lb.add_argument("--db", default="turso")
    lb.add_argument("--aliases", default="configs/team_aliases.json")
    lb.set_defaults(fn=cmd_lineups_backfill)
    sr = sub.add_parser("slip-review", help="schedine chiuse: quali selezioni le hanno fatte perdere (nessuna richiesta API)")
    sr.add_argument("--db", default="turso")
    sr.set_defaults(fn=cmd_slip_review)
    er = sub.add_parser("events-remap", help="eventi delle partite rimappati dai grezzi salvati (nessuna richiesta API)")
    er.add_argument("--db", default="turso")
    er.set_defaults(fn=cmd_events_remap)
    xe = sub.add_parser("xi-eval", help="probabili formazioni calcolate da noi contro le ufficiali (replay, nessuna richiesta API)")
    xe.add_argument("--since", default="2026-07-01", help="prima partita giudicata; le formazioni prima servono da base")
    xe.add_argument("--db", default="turso")
    xe.set_defaults(fn=cmd_xi_eval)
    se = sub.add_parser("scorer-eval", help="Fase 9: marcatori rigiocati settimana per settimana (nessuna richiesta API)")
    se.add_argument("--since", default="2025-07-01", help="prima partita giudicata; lo storico prima serve da base")
    se.add_argument("--db", default="turso")
    se.add_argument("--config", default=None)
    se.set_defaults(fn=cmd_scorer_eval)
    le = sub.add_parser("lineup-eval", help="le formazioni ufficiali migliorano le probabilità? (replay, nessuna richiesta API)")
    le.add_argument("--weeks", type=int, default=10)
    le.add_argument("--config", default=None)
    le.add_argument("--db", default="turso")
    le.set_defaults(fn=cmd_lineup_eval)
    lh = sub.add_parser("lineup-history-check", help="GOAL restituisce le formazioni delle partite finite? (2 richieste GOAL)")
    lh.add_argument("--comp", default="Serie A")
    lh.add_argument("--db", default="turso")
    lh.add_argument("--aliases", default="configs/team_aliases.json")
    lh.set_defaults(fn=cmd_lineup_history_check)
    qp = sub.add_parser("quotes-prune", help="sfoltisce le quote delle partite finite da oltre 7 giorni (nessuna richiesta API)")
    qp.add_argument("--dry-run", action="store_true", help="conta soltanto, non cancella")
    qp.add_argument("--max", type=int, default=400)
    qp.add_argument("--max-seconds", type=float, default=600)
    qp.add_argument("--db", default="turso")
    qp.set_defaults(fn=cmd_quotes_prune)
    rl = sub.add_parser("registry-calibration", help="calibrazione del registro contro le chiusure Pinnacle e Sisal (nessuna richiesta API)")
    rl.add_argument("--db", default="turso")
    rl.set_defaults(fn=cmd_registry_calibration)
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
    ca.add_argument("--summary", action="store_true", help="conteggio righe del database e partite in calendario (circa 1 minuto su Turso)")
    ca.add_argument("--health", action="store_true", help="controllo di salute anche fuori dal giro del mattino")
    ca.set_defaults(fn=cmd_collect_auto)
    hc = sub.add_parser("health", help="controllo di salute e spazio del database per tabella (nessuna richiesta API)")
    hc.add_argument("--db", default="turso")
    hc.add_argument("--save", action="store_true")
    hc.set_defaults(fn=cmd_health)
    bk = sub.add_parser("db-backup", help="copia compatta del database (replica locale) in un file .db.gz")
    bk.add_argument("--out", default="backup/algowinbet.db.gz")
    bk.add_argument("--skip", nargs="*", default=[], help="tabelle da non copiare")
    bk.add_argument("--src", default=None, help="file del database (predefinito: la replica locale di Turso)")
    bk.set_defaults(fn=cmd_db_backup)
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
