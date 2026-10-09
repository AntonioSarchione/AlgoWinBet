import Link from "next/link";
import { unstable_cache } from "next/cache";
import {
  AlertTriangle, ArrowRight, BarChart3, Brain, CalendarClock, CheckCircle2, ChevronRight, CircleSlash, Coins, Database, Filter, Gauge,
  Gem, Layers, ListOrdered, Percent, Scale, Search, ShieldAlert, ShieldCheck, Shapes, Sigma, Target, Ticket, Trophy, TrendingUp, Workflow,
  XCircle, type LucideIcon,
} from "lucide-react";
import { absencesFor, DEPLOY, lastTick, latestRun, oppSummary, parseJSON, runFixtures, slipCandidates, usage, type Absence, type FixtureRow, type ModelMarket } from "@/lib/db";
import { explainSlip, legMinOdds, legReason, type OptSettings } from "@/lib/optimizer";
import { PROFILE_LABEL, profileHint, runProfiles, toLegs, type ProfileResult } from "@/lib/profiles";
import { MARKET_GROUPS, marketGroup } from "@/lib/markets";
import { ago, compShort, dayTime, hour, pct, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, Meter, MatchCell, MiniRing, PBar, ProbGauge, TeamBadge } from "@/app/_components/ui";
import { LegAbsences } from "@/app/_components/Absences";
import { OppTable } from "@/app/_components/OppTable";
import { MatchExplorer, type ExplorerMatch } from "@/app/_components/MatchExplorer";

export const dynamic = "force-dynamic";

const PERIODS = [
  { v: "24", l: "Prossime 24 ore" },
  { v: "48", l: "Prossime 48 ore" },
  { v: "72", l: "Prossimi 3 giorni" },
  { v: "168", l: "Prossimi 7 giorni" },
];

const MAX_EVENTS = [
  { v: "10", l: "Fino a 10" },
  { v: "1", l: "1 (singola)" },
  ...[2, 3, 4, 5, 6, 7, 8, 9].map((k) => ({ v: String(k), l: `Fino a ${k}` })),
];

const MIN_EVENTS = [
  { v: "1", l: "Almeno 1" },
  ...[2, 3, 4, 5, 6, 7, 8, 9, 10].map((k) => ({ v: String(k), l: k === 5 ? "Almeno 5 (bonus Sisal)" : `Almeno ${k}` })),
];

// the slips never take a selection under the published floor (40%, user's rule): "" = that floor
const LEG_PROB = [{ v: "", l: "Regola (almeno 40%)" }, ...[45, 50, 60, 70, 80].map((k) => ({ v: String(k), l: `Almeno ${k}%` }))];
const EV_MIN = [
  { v: "10", l: "Almeno +10%" }, { v: "5", l: "Almeno +5%" }, { v: "2", l: "Almeno +2%" }, { v: "0", l: "Almeno 0% (pari)" },
  { v: "-2", l: "Almeno −2%" }, { v: "-5", l: "Almeno −5%" }, { v: "-10", l: "Almeno −10%" },
];
// "maximum risk" = the lowest chance of winning the slip that is still accepted
const RISK = [
  { v: "", l: "Nessun limite" },
  { v: "50", l: "Basso · vince ≥ 50%" },
  { v: "30", l: "Medio · vince ≥ 30%" },
  { v: "15", l: "Alto · vince ≥ 15%" },
  { v: "5", l: "Molto alto · vince ≥ 5%" },
];
// one icon per slip profile (cards above the slip)
const PROFILE_ICON: Record<string, LucideIcon> = { probabilita: ShieldCheck, equilibrata: Scale, value: Gem };

type Knobs = { maxEvents: number; minEvents: number; qMin: number; qMax: number; legProb: number; evMin: number; riskMin: number; markets: string[] };

// The slips of every profile for one set of filters. Cached per run + filters (switching profile tab, going back, or a second
// visit gets the answer ready), computed once instead of once per profile per request.
const homeSlips = unstable_cache(
  async (runId: number, filter: Parameters<typeof slipCandidates>[1], statuses: string[], settings: OptSettings, k: Knobs) => {
    const picked = new Set(k.markets);
    const legs = toLegs(await slipCandidates(runId, filter, statuses)).filter((o) => !picked.size || picked.has(marketGroup(o.sel_key)));
    return runProfiles(legs, settings, {
      max_legs: k.maxEvents, min_legs: k.minEvents, odds_min: k.qMin || settings.optimizer.odds_min, odds_max: k.qMax || settings.optimizer.odds_max,
      min_leg_probability: Math.max(k.legProb, settings.optimizer.min_leg_probability ?? 0), min_slip_ev: k.evMin, min_probability: k.riskMin,
    });
  },
  ["homeSlips", DEPLOY],
  { revalidate: 6 * 3600 },
);

type SP = {
  min?: string; max?: string; lmin?: string; lmax?: string; h?: string; n?: string; nmin?: string; comp?: string | string[];
  p?: string; pmin?: string; ev?: string; risk?: string; mk?: string | string[];
};

export default async function Home({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
  const run = await latestRun();
  const [use, tick] = await Promise.all([usage(), lastTick()]);
  if (!run) {
    return (
      <div className="card">
        <Empty icon={Database} title="Nessuna analisi pubblicata">
          La prima arriva dal prossimo giro di raccolta automatico.
        </Empty>
      </div>
    );
  }
  // ---- filters: every change rebuilds the slips on the published opportunities (web/lib/optimizer.ts) ----
  const now = Date.now();
  const hours = PERIODS.some((p) => p.v === sp.h) ? Number(sp.h) : 168;
  const maxEvents = MAX_EVENTS.some((m) => m.v === sp.n) ? Number(sp.n) : 10;
  const minEvents = Math.min(MIN_EVENTS.some((m) => m.v === sp.nmin) ? Number(sp.nmin) : 1, maxEvents); // never above the maximum
  const qMin = Number(sp.min) || 0;
  const qMax = Number(sp.max) || 0;
  const lMin = Number(sp.lmin) || 0;
  const lMax = Number(sp.lmax) || 0;
  const legProb = LEG_PROB.some((x) => x.v === sp.pmin) ? Number(sp.pmin || 0) / 100 : 0;
  const riskMin = RISK.some((x) => x.v === sp.risk) ? Number(sp.risk || 0) / 100 : 0;
  const pickedMk = new Set([sp.mk ?? []].flat().filter((m) => (MARKET_GROUPS as readonly string[]).includes(m)));
  const allMk = !pickedMk.size || pickedMk.size === MARKET_GROUPS.length;
  // Competitions: several can be ticked; none in the URL (or all of them) means every competition.
  const picked = new Set([sp.comp ?? []].flat().filter(Boolean));
  const settings = parseJSON<OptSettings | null>(run.optimizer, null);
  const iso = (t: number) => new Date(t).toISOString().slice(0, 16); // minute precision: equal filters share the data cache
  const filter = { from: iso(now), until: iso(now + hours * 3600_000), comps: [...picked], lmin: lMin, lmax: lMax };
  const statuses = ["STRONG", "CANDIDATE", ...(settings?.optimizer.include_watch ? ["WATCH"] : []), ...(settings?.optimizer.include_fair ? ["FAIR"] : [])];
  const evMin = EV_MIN.some((x) => x.v === sp.ev) ? Number(sp.ev) / 100 : (settings?.optimizer.min_slip_ev ?? 0);
  const knobs: Knobs = { maxEvents, minEvents, qMin, qMax, legProb, evMin, riskMin, markets: allMk ? [] : [...pickedMk].sort() };
  const [fixtures, summary, computed] = await Promise.all([
    runFixtures(run.id),
    oppSummary(run.id, filter),
    settings ? homeSlips(run.id, filter, statuses, settings, knobs) : Promise.resolve(null),
  ]);
  const inWindow = (isoTime: string, comp: string) =>
    new Date(isoTime).getTime() <= now + hours * 3600_000 && (!picked.size || picked.has(comp));
  const comps = [...new Set(fixtures.map((f) => f.competition))].sort();
  const fx = fixtures.filter((f) => inWindow(f.kickoff, f.competition) && new Date(f.kickoff).getTime() > now - 2 * 3600_000);
  const results: Record<string, ProfileResult> = computed ?? {
    equilibrata: { slips: [], noBet: true, reasons: ["Analisi pubblicata con una versione precedente: le schedine arrivano dalla prossima pubblicazione."], eligible: 0, evaluated: 0 },
  };
  const profileNames = Object.keys(results);
  const profile = sp.p && results[sp.p] ? sp.p : results[settings?.optimizer.profile ?? ""] ? (settings?.optimizer.profile as string) : profileNames[0];
  const result = results[profile];
  const profileHref = (name: string) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(sp)) for (const x of [v ?? []].flat()) if (k !== "p" && x) q.append(k, x);
    q.set("p", name);
    return `/?${q.toString()}`;
  };
  const sl = result.slips;
  const best = sl[0];
  const statusCounts = parseJSON<Record<string, number>>(run.status_counts, {});
  const nMarkets = Object.values(statusCounts).reduce((a, b) => a + b, 0);
  const reasons = result.reasons;
  const expl = best && settings ? explainSlip(best, settings.z, settings.optimizer.multi_bonus_min_odds ?? 1.25) : null;
  // regular starters reported absent or doubtful for the slip's matches: shown under each leg (uncached, a few rows)
  const absent = best ? await absencesFor(best.legs.map((l) => l.fixture_id)).catch(() => ({}) as Record<string, Absence[]>) : {};
  const op = summary.top;
  const nOpp = summary.count;
  const upcoming = fx.slice(0, 6);
  // strip above the deep analysis: every filtered match not started yet, nearest kickoff first
  const explorer = fx
    .filter((f) => new Date(f.kickoff).getTime() >= now)
    .sort((a, b) => a.kickoff.localeCompare(b.kickoff))
    .map(explorerMatch);
  const filtered = Boolean(picked.size || sp.min || sp.max || sp.lmin || sp.lmax || (sp.h && sp.h !== "168") || (sp.n && sp.n !== "10") || (sp.nmin && sp.nmin !== "1") || sp.pmin || sp.ev || sp.risk || !allMk);
  const mkLabel = allMk ? "Tutti i mercati" : pickedMk.size === 1 ? [...pickedMk][0] : `${pickedMk.size} mercati`;
  const discarded = (statusCounts.AVOID ?? 0) + (statusCounts.NEUTRAL ?? 0) + (statusCounts.INVALID ?? 0);
  const candidates = (statusCounts.STRONG ?? 0) + (statusCounts.CANDIDATE ?? 0) + (statusCounts.FAIR ?? 0) + (statusCounts.WATCH ?? 0);
  const allComps = !picked.size || comps.every((c) => picked.has(c));
  const compLabel = allComps ? "Tutti i campionati" : picked.size === 1 ? [...picked][0] : `${picked.size} campionati`;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Home</h1>
          <p>
            Analisi del {dayTime(run.created_at)} · ultima raccolta dati {ago(tick)} · uso personale, solo paper trading
          </p>
        </div>
      </header>

      {/* key: a new query string remounts the form, so "Azzera" and back/forward reset the uncontrolled fields */}
      <form key={JSON.stringify(sp)} className="card filters" method="get" role="search" aria-label="Filtra l'analisi">
        <div className="field">
          <label htmlFor="min">Quota schedina (min – max)</label>
          <div className="control">
            <Gauge size={17} aria-hidden="true" />
            <input id="min" name="min" type="number" inputMode="decimal" step="0.05" min="1" placeholder="1.50" defaultValue={sp.min} aria-label="Quota minima" />
            <span className="dash">–</span>
            <input name="max" type="number" inputMode="decimal" step="0.05" min="1" placeholder="15.00" defaultValue={sp.max} aria-label="Quota massima" />
          </div>
        </div>
        <div className="field">
          <label htmlFor="lmin">Quota singolo evento (min – max)</label>
          <div className="control">
            <Target size={17} aria-hidden="true" />
            <input id="lmin" name="lmin" type="number" inputMode="decimal" step="0.05" min="1.25" placeholder="1.25" defaultValue={sp.lmin} aria-label="Quota minima del singolo evento" />
            <span className="dash">–</span>
            <input name="lmax" type="number" inputMode="decimal" step="0.05" min="1" placeholder="3.00" defaultValue={sp.lmax} aria-label="Quota massima del singolo evento" />
          </div>
        </div>
        <div className="field">
          <label htmlFor="h">Periodo</label>
          <div className="control">
            <CalendarClock size={17} aria-hidden="true" />
            <select id="h" name="h" defaultValue={String(hours)}>
              {PERIODS.map((p) => (
                <option key={p.v} value={p.v}>{p.l}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="nmin">Numero minimo di eventi</label>
          <div className="control">
            <ListOrdered size={17} aria-hidden="true" />
            <select id="nmin" name="nmin" defaultValue={String(minEvents)}>
              {MIN_EVENTS.map((m) => (
                <option key={m.v} value={m.v}>{m.l}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="n">Numero massimo di eventi</label>
          <div className="control">
            <ListOrdered size={17} aria-hidden="true" />
            <select id="n" name="n" defaultValue={String(maxEvents)}>
              {MAX_EVENTS.map((m) => (
                <option key={m.v} value={m.v}>{m.l}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="pmin">Probabilità minima per evento</label>
          <div className="control">
            <Percent size={17} aria-hidden="true" />
            <select id="pmin" name="pmin" defaultValue={sp.pmin ?? ""}>
              {LEG_PROB.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="ev">EV minimo della schedina</label>
          <div className="control">
            <Sigma size={17} aria-hidden="true" />
            <select id="ev" name="ev" defaultValue={EV_MIN.some((x) => x.v === sp.ev) ? sp.ev : String(Math.round(evMin * 100))}>
              {EV_MIN.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="risk">Rischio massimo</label>
          <div className="control">
            <ShieldAlert size={17} aria-hidden="true" />
            <select id="risk" name="risk" defaultValue={sp.risk ?? ""}>
              {RISK.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <span className="field-label" id="mk-label">Mercati</span>
          <details className="multi">
            <summary className="control" aria-labelledby="mk-label">
              <Shapes size={17} aria-hidden="true" />
              <span className="multi-value">{mkLabel}</span>
            </summary>
            <fieldset className="multi-panel" aria-labelledby="mk-label">
              {MARKET_GROUPS.map((m) => (
                <label key={m} className="check">
                  <input type="checkbox" name="mk" value={m} defaultChecked={allMk || pickedMk.has(m)} />
                  {m}
                </label>
              ))}
              <button type="submit" className="btn btn-primary btn-sm" style={{ marginTop: 6 }}>
                <Filter size={15} aria-hidden="true" /> Applica
              </button>
            </fieldset>
          </details>
        </div>
        {sp.p && <input type="hidden" name="p" value={profile} />}
        <div className="field">
          <span className="field-label" id="comp-label">Campionati</span>
          <details className="multi">
            <summary className="control" aria-labelledby="comp-label">
              <Trophy size={17} aria-hidden="true" />
              <span className="multi-value">{compLabel}</span>
            </summary>
            <fieldset className="multi-panel" aria-labelledby="comp-label">
              {comps.map((c) => (
                <label key={c} className="check">
                  <input type="checkbox" name="comp" value={c} defaultChecked={allComps || picked.has(c)} />
                  {c}
                </label>
              ))}
              <button type="submit" className="btn btn-primary btn-sm" style={{ marginTop: 6 }}>
                <Filter size={15} aria-hidden="true" /> Applica
              </button>
            </fieldset>
          </details>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {filtered && (
            <Link href="/" className="btn btn-ghost" aria-label="Azzera filtri">Azzera</Link>
          )}
          <button type="submit" className="btn btn-primary">
            <Filter size={17} aria-hidden="true" /> Applica
          </button>
        </div>
      </form>

      <div className="layout">
        <div className="col">
          {/* ---------------- hero ---------------- */}
          <section className="card hero" aria-labelledby="hero-title">
            <div className="hero-copy">
              {best ? (
                <span className="pill pill-good"><CheckCircle2 size={13} aria-hidden="true" /> Analisi completata</span>
              ) : (
                <span className="pill pill-warn"><CircleSlash size={13} aria-hidden="true" /> No bet</span>
              )}
              <h2 id="hero-title">{best ? "Ecco la tua schedina ottimizzata" : "Nessuna schedina: niente supera le soglie"}</h2>
              <p>
                Abbiamo analizzato <b>{fx.length}</b> partite
                {nMarkets > 0 && <>, valutato <b>{nMarkets.toLocaleString("it-IT")}</b> mercati</>} e trovato <b>{nOpp}</b> opportunità da osservare.
                {!best && (reasons[0] ? ` ${reasons[0]}` : " Senza quote recenti il motore non propone giocate.")}
              </p>
              {best && (
                <ul className="hero-tags" aria-label="In breve">
                  <li><Ticket size={14} aria-hidden="true" /> {best.legs.length} {best.legs.length === 1 ? "evento" : "eventi"}</li>
                  <li><Workflow size={14} aria-hidden="true" /> profilo {PROFILE_LABEL[profile] ?? profile}</li>
                  <li><Coins size={14} aria-hidden="true" /> puntata paper {best.stake.toFixed(2)} €</li>
                </ul>
              )}
            </div>
            <div className="hero-visual">
              {best ? (
                <>
                  <ProbGauge value={best.joint_probability} label="Probabilità di vincita" />
                  <div className="hero-figs">
                    <div className="hero-fig">
                      <small>Quota totale</small>
                      <b className="num">{best.total_odds.toFixed(2)}</b>
                      <span className={`ev-chip ${best.ev >= 0 ? "pos" : "neg"}`}>EV {signed(best.ev)}</span>
                    </div>
                    <div className="hero-fig">
                      <small>EV prudente</small>
                      <b className={`num ${best.ev_lower >= 0 ? "pos" : "neg"}`}>{signed(best.ev_lower)}</b>
                      <span className="note">{best.bonus ? `bonus multipla +${pct(best.bonus)}` : "limite basso del valore"}</span>
                    </div>
                  </div>
                </>
              ) : (
                <>
                  <ProbGauge value={run.n_fixtures ? run.n_with_quotes / run.n_fixtures : 0} label="Partite con quote" sub={`${run.n_with_quotes} su ${run.n_fixtures}`} />
                  <div className="hero-figs">
                    <div className="hero-fig">
                      <small>Schedine proposte</small>
                      <b className="num">0</b>
                      <span className="note">meglio nessuna giocata che una senza valore</span>
                    </div>
                  </div>
                </>
              )}
            </div>
          </section>

          {/* how the slips were found, with real numbers: published analysis first, then the dashboard search with these filters */}
          <section className="card" aria-labelledby="funnel-title">
            <div className="card-head"><h2 id="funnel-title"><Workflow size={17} aria-hidden="true" /> Riepilogo dell&apos;analisi</h2><span className="count">profilo {PROFILE_LABEL[profile] ?? profile}</span></div>
            <ol className="funnel">
              <Step icon={Layers} label="Partite nel periodo" value={fx.length} />
              <Step icon={BarChart3} label="Mercati valutati" value={nMarkets} />
              <Step icon={Target} label="Candidati" value={candidates}>
                <XCircle size={13} aria-hidden="true" /> {discarded.toLocaleString("it-IT")} scartati
              </Step>
              <Step icon={Filter} label="Idonei con i filtri" value={result.eligible} />
              <Step icon={Sigma} label="Combinazioni provate" value={result.evaluated} />
              <Step icon={ShieldCheck} label="Schedine proposte" value={sl.length} last />
            </ol>
            <p className="note card-pad" style={{ paddingTop: 0 }}>
              Scartati: valore negativo, probabilità sotto il {pct(0.25)} o dati insufficienti. Candidati: selezioni Alta, Media, Equa e Da osservare
              dell&apos;analisi pubblicata. Idonei: quelli che entrano nella ricerca delle schedine con i filtri scelti.
            </p>
          </section>

          {profileNames.length > 1 && (
            <nav className="profiles" aria-label="Profili di schedina">
              {profileNames.map((name) => {
                const r = results[name];
                const s0 = r.slips[0];
                const Icon = PROFILE_ICON[name] ?? Workflow;
                return (
                  <Link key={name} href={profileHref(name)} className={`card profile ${name === profile ? "active" : ""}`} aria-current={name === profile ? "true" : undefined}>
                    <span className="profile-top">
                      <span className="kpi-icon"><Icon size={18} aria-hidden="true" /></span>
                      <span className="profile-title">
                        <span className="profile-name">{PROFILE_LABEL[name] ?? name}</span>
                        <span className="note">{profileHint(name, evMin)}</span>
                      </span>
                      {s0 && <MiniRing value={s0.joint_probability} label="Probabilità di vincita" />}
                    </span>
                    {s0 ? (
                      <span className="profile-stats">
                        <span><small>Quota</small><b className="num">{s0.total_odds.toFixed(2)}</b></span>
                        <span><small>Vince</small><b className="num">{pct(s0.joint_probability, 1)}</b></span>
                        <span><small>EV</small><b className={`num ${s0.ev >= 0 ? "pos" : "neg"}`}>{signed(s0.ev)}</b></span>
                        <span><small>Eventi</small><b className="num">{s0.legs.length}</b></span>
                      </span>
                    ) : r.sameAs ? (
                      <span className="profile-stats"><span><small>Esito</small><b>Stessa di «{PROFILE_LABEL[r.sameAs] ?? r.sameAs}»</b></span></span>
                    ) : (
                      <span className="profile-stats"><span><small>Esito</small><b>No bet</b></span></span>
                    )}
                  </Link>
                );
              })}
            </nav>
          )}

          {/* ---------------- slip + why ---------------- */}
          <div className="split">
            <section className="card ticket" aria-labelledby="slip-title">
              <div className="card-head">
                <h2 id="slip-title">
                  <Ticket size={17} aria-hidden="true" /> Schedina · {PROFILE_LABEL[profile] ?? profile} {best && <span className="count">{best.legs.length} {best.legs.length === 1 ? "evento" : "eventi"}</span>}
                </h2>
                {best && (
                  <span className="muted">
                    Quota totale <b className="num pos">{best.total_odds.toFixed(2)}</b>
                  </span>
                )}
              </div>
              {best ? (
                <>
                  <div className="table-wrap">
                    <table className="compact">
                      <thead>
                        <tr>
                          <th>#</th><th>Evento / Mercato</th><th className="num">Quota</th><th className="num">Probabilità</th>
                          <th className="num">EV · Stato</th>
                          <th className="num" title="Sotto questa quota Sisal l'evento non va più giocato: la schedina scenderebbe sotto l'EV minimo">Gioca se ≥</th>
                        </tr>
                      </thead>
                      <tbody>
                        {best.legs.map((l, i) => {
                          const o = l;
                          const [home, away] = (l.match ?? "").split(" - ");
                          return (
                            <tr key={i}>
                              <td className="muted num">{i + 1}</td>
                              <td className="wrap">
                                <MatchCell home={home} away={away ?? ""} sub={<>{l.market} · {compShort(l.competition)} · {hour(l.kickoff)}</>} href={`/partita/${encodeURIComponent(o.fixture_id)}`} />
                                <div className={`leg-reason leg-reason-${legReason(l).kind}`}>{legReason(l).text}</div>
                                <LegAbsences list={absent[o.fixture_id] ?? []} runAt={run.created_at} />
                              </td>
                              <td className="num"><span className="odds-chip">{l.odds.toFixed(2)}</span></td>
                              <td className="num">
                                {pct(l.p_final, 1)}
                                <PBar p={l.p_final} mark={o.p_market} />
                                {o?.p_low != null && o.p_high != null && <span className="sub">({pct(o.p_low, 1)} – {pct(o.p_high, 1)})</span>}
                              </td>
                              <td className="num">
                                <span className={`ev-chip ${o.ev >= 0 ? "pos" : "neg"}`}>{signed(o.ev)}</span>
                                {<span className="sub"><span className={`status status-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span></span>}
                              </td>
                              <td className="num">{legMinOdds(best, l.odds, evMin).toFixed(2)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <div className="ticket-foot">
                    <Mini icon={Gauge} label="Quota totale" value={best.total_odds.toFixed(2)} />
                    <Mini icon={Percent} label="Probabilità complessiva" value={pct(best.joint_probability, 1)} />
                    <Mini icon={TrendingUp} label="EV stimato" value={signed(best.ev)} tone={best.ev >= 0 ? "pos" : "neg"} />
                    <Mini icon={Coins} label="Puntata paper" value={`${best.stake.toFixed(2)} €`} />
                  </div>
                </>
              ) : (
                <Empty icon={CircleSlash} title="NO BET">
                  {reasons.length ? reasons.join(" ") : "Nessuna combinazione con valore atteso positivo e probabilità sopra soglia."}
                </Empty>
              )}
            </section>

            <section className="card" aria-labelledby="why-title">
              <div className="card-head">
                <h2 id="why-title"><Brain size={17} aria-hidden="true" /> {best ? "Perché questa schedina?" : "Perché nessuna schedina?"}</h2>
              </div>
              <div className="card-pad">
                <ul className="checklist">
                  {best ? (
                    <>
                      {(expl?.positive_factors ?? []).map((x, i) => (
                        <li key={`p${i}`}><CheckCircle2 size={17} className="ok" aria-label="a favore" /> <span>{x}</span></li>
                      ))}
                      {(expl?.negative_factors ?? []).map((x, i) => (
                        <li key={`n${i}`}><AlertTriangle size={17} className="ko" aria-label="attenzione" /> <span>{x}</span></li>
                      ))}
                    </>
                  ) : (
                    (reasons.length ? reasons : ["Nessuna quota recente da confrontare con il modello."]).map((x, i) => (
                      <li key={i}><AlertTriangle size={17} className="ko" aria-label="motivo" /> <span>{x}</span></li>
                    ))
                  )}
                </ul>
                {best && (expl?.what_would_change_it ?? []).length > 0 && (
                  <>
                    <h3 className="note" style={{ margin: "16px 0 6px", textTransform: "uppercase", letterSpacing: ".06em" }}>Cosa la cambierebbe</h3>
                    <ul className="checklist">
                      {(expl?.what_would_change_it ?? []).map((x, i) => (
                        <li key={i}><ArrowRight size={17} className="muted" aria-hidden="true" /> <span>{x}</span></li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </section>
          </div>

          {/* ---------------- opportunities ---------------- */}
          <section className="card" aria-labelledby="opp-title">
            <div className="card-head">
              <h2 id="opp-title"><TrendingUp size={17} aria-hidden="true" /> Migliori opportunità <span className="count">{nOpp}</span></h2>
              <Link href="/opportunita" className="btn btn-ghost btn-sm">Vedi tutte <ChevronRight size={15} aria-hidden="true" /></Link>
            </div>
            {op.length ? <OppTable rows={op.slice(0, 8)} /> : (
              <Empty icon={Percent} title="Nessun mercato sopra le soglie">
                Le quote arrivano da OddsPapi nelle 24 ore prima delle partite: senza prezzi recenti non si calcola il valore atteso.
              </Empty>
            )}
          </section>

          {explorer.length > 0 && <MatchExplorer matches={explorer} initial={explorer.find((m) => m.p_home != null)?.id} />}
        </div>

        {/* ---------------- right rail ---------------- */}
        <aside className="col rail" aria-label="Pannelli rapidi">
          <section className="card">
            <div className="card-head"><h2><Search size={17} aria-hidden="true" /> Analisi rapida di una partita</h2></div>
            <form action="/palinsesto" method="get" role="search" style={{ padding: "12px 16px 4px" }}>
              <label htmlFor="q" className="sr-only">Cerca squadra o campionato</label>
              <div className="control">
                <Search size={17} aria-hidden="true" />
                <input id="q" name="q" type="search" placeholder="Cerca squadra o campionato…" />
              </div>
            </form>
            {upcoming.length ? (
              <ul className="quick">
                {upcoming.map((f) => (
                  <li key={f.fixture_id}>
                    <Link href={`/partita/${encodeURIComponent(f.fixture_id)}`}>
                      <span className="badges" style={{ display: "flex", gap: 2 }}>
                        <TeamBadge name={f.home} />
                        <TeamBadge name={f.away} />
                      </span>
                      <span style={{ minWidth: 0 }}>
                        <b>{f.home} - {f.away}</b>
                        <small>{compShort(f.competition)} · {dayTime(f.kickoff)}</small>
                      </span>
                      <ChevronRight size={17} aria-hidden="true" />
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty icon={CalendarClock} title="Nessuna partita nel periodo" />
            )}
          </section>

          <section className="card">
            <div className="card-head"><h2><Shapes size={17} aria-hidden="true" /> Mercati analizzati</h2></div>
            <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 14 }}>
              <div>
                <span className="note">Con quote dei bookmaker (valore atteso)</span>
                <div className="text-2">Tutti i mercati gol quotati da Sisal: 1X2, doppia chance, draw no bet, Under/Over e gol squadra, Gol/NoGol, handicap, risultato e gol esatti, margine, primo/ultimo gol, 1° e 2° tempo, Parziale/Finale. Corner (1X2, totale, squadra) dal modello dei corner. Cartellini: solo 1X2 (squadra con più cartellini)</div>
              </div>
              <div>
                <span className="note">Solo probabilità del modello (quota equa)</span>
                <div className="text-2">Multigol e Combo. Cartellini e marcatori arrivano con i loro modelli</div>
              </div>
            </div>
          </section>

          <section className="card">
            <div className="card-head"><h2><Database size={17} aria-hidden="true" /> Budget richieste API</h2></div>
            <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <Meter label="GOAL API · oggi" used={use.goalDay} limit={1000} hint="Calendario, risultati, formazioni, statistiche" />
              <Meter label="API-Football · oggi" used={use.apifDay} limit={100} hint="Formazioni con posizioni, infortuni e squalifiche, rose. Prima i 7 campionati" />
              <Meter label="OddsPapi · mese" used={use.oddsMonth} limit={250} hint="Richieste conteggiate: solo fotografie Sisal. Storico Sisal e Pinnacle con richieste libere" />
              <div className="kv" style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
                <span>Ultima raccolta</span>
                <span>{ago(tick)}</span>
              </div>
            </div>
          </section>
        </aside>
      </div>
    </>
  );
}

// one step of the analysis funnel: matches -> markets -> candidates -> eligible -> combinations -> slips
function Step({ icon: Icon, label, value, last, children }: { icon: LucideIcon; label: string; value: number; last?: boolean; children?: React.ReactNode }) {
  return (
    <li className={`funnel-step${last ? " last" : ""}`}>
      <span className="funnel-icon"><Icon size={16} aria-hidden="true" /></span>
      <b className="num">{value.toLocaleString("it-IT")}</b>
      <small>{label}</small>
      {children && <span className="funnel-sub">{children}</span>}
    </li>
  );
}

function Mini({ icon: Icon, label, value, tone }: { icon: LucideIcon; label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div className="ticket-stat">
      <span className="note"><Icon size={13} aria-hidden="true" /> {label}</span>
      <b className={`num ${tone ?? ""}`}>{value}</b>
    </div>
  );
}

function explorerMatch(f: FixtureRow): ExplorerMatch {
  const mk = parseJSON<ModelMarket[]>(f.markets, []);
  const get = (g: string, l: string) => mk.find((m) => m.g === g && m.l === l)?.p;
  const book = parseJSON<ExplorerMatch["book"]>(f.book ?? null, {});
  // where Sisal prices the selection, the bar shows the probability the slips use (model shrunk toward the market)
  const picks: [string, number | undefined, ("p_over25" | "p_btts")?][] = [
    ["Over 2.5", book.p_over25?.pf ?? get("Under/Over", "Over 2.5") ?? f.p_over25 ?? undefined, "p_over25"],
    ["Gol", book.p_btts?.pf ?? get("Gol/NoGol", "Gol") ?? f.p_btts ?? undefined, "p_btts"],
    ["Multigol 2-4", get("Multigol", "Multigol 2-4")],
    ["1X", get("Doppia chance", "1X")],
    ["1 + Over 2.5", get("Combo", "1 + Over 2.5")],
  ];
  return {
    id: f.fixture_id, kickoff: f.kickoff, competition: f.competition, home: f.home, away: f.away,
    p_home: f.p_home, p_draw: f.p_draw, p_away: f.p_away, xg_home: f.xg_home, xg_away: f.xg_away, book,
    picks: picks
      .filter((x) => x[1] != null)
      .map(([l, p, k]) => {
        const b = k ? book[k] : undefined;
        return [b ? `${l} · Sisal ${b.odds.toFixed(2)}` : l, p as number] as [string, number];
      }),
  };
}
