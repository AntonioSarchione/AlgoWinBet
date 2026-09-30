import Link from "next/link";
import {
  AlertTriangle, ArrowRight, BarChart3, Brain, CalendarClock, CheckCircle2, ChevronRight, CircleSlash, Database, Filter, Gauge,
  Layers, ListOrdered, Percent, Search, ShieldCheck, Target, Trophy, TrendingUp,
} from "lucide-react";
import { lastTick, latestRun, parseJSON, runFixtures, runOpps, runSlips, usage, type FixtureRow, type Leg, type ModelMarket } from "@/lib/db";
import { ago, compShort, dayTime, fairOdds, hour, pct, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, HBar, Meter, MatchCell, Ring, Split1X2, TeamBadge } from "@/app/_components/ui";
import { OppTable } from "@/app/_components/OppTable";

export const dynamic = "force-dynamic";

const PERIODS = [
  { v: "24", l: "Prossime 24 ore" },
  { v: "48", l: "Prossime 48 ore" },
  { v: "72", l: "Prossimi 3 giorni" },
  { v: "168", l: "Prossimi 7 giorni" },
];

// Slips are published per period and per maximum number of events (see publish.py SLIP_MAX_LEGS)
const MAX_EVENTS = [
  { v: "8", l: "Qualsiasi" },
  { v: "1", l: "1 (singola)" },
  { v: "2", l: "Fino a 2" },
  { v: "3", l: "Fino a 3" },
  { v: "4", l: "Fino a 4" },
  { v: "5", l: "Fino a 5" },
  { v: "6", l: "Fino a 6" },
];

type SP = { min?: string; max?: string; lmin?: string; lmax?: string; h?: string; n?: string; comp?: string };

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
  const [fixtures, opps, slips] = await Promise.all([runFixtures(run.id), runOpps(run.id), runSlips(run.id)]);

  // ---- filters (applied to the latest published analysis) ----
  const now = Date.now();
  const hours = PERIODS.some((p) => p.v === sp.h) ? Number(sp.h) : 168;
  const maxEvents = MAX_EVENTS.some((m) => m.v === sp.n) ? Number(sp.n) : 8;
  const qMin = Number(sp.min) || 0;
  const qMax = Number(sp.max) || 0;
  const lMin = Number(sp.lmin) || 0;
  const lMax = Number(sp.lmax) || 0;
  const legOk = (odds: number) => (!lMin || odds >= lMin) && (!lMax || odds <= lMax);
  const inWindow = (iso: string, comp: string) =>
    new Date(iso).getTime() <= now + hours * 3600_000 && (!sp.comp || comp === sp.comp);
  const comps = [...new Set(fixtures.map((f) => f.competition))].sort();
  const fx = fixtures.filter((f) => inWindow(f.kickoff, f.competition) && new Date(f.kickoff).getTime() > now - 2 * 3600_000);
  const op = opps.filter((o) => inWindow(o.kickoff, o.competition) && legOk(o.odds));
  const built = slips.some((s) => s.horizon_h != null);
  const sl = slips
    .filter((s) => !built || (s.horizon_h === hours && s.max_legs === maxEvents))
    .map((s) => ({ ...s, legList: parseJSON<Leg[]>(s.legs, []) }))
    .filter((s) => s.legList.length <= maxEvents)
    .filter((s) => s.legList.every((l) => inWindow(l.kickoff, l.competition) && legOk(l.odds)))
    .filter((s) => (!qMin || s.total_odds >= qMin) && (!qMax || s.total_odds <= qMax));
  const best = sl[0];
  const statusCounts = parseJSON<Record<string, number>>(run.status_counts, {});
  const nMarkets = Object.values(statusCounts).reduce((a, b) => a + b, 0);
  const reasons = parseJSON<string[]>(run.reasons, []);
  const oppOf = (l: Leg) => op.find((o) => o.match === l.match && o.market === l.market && o.bookmaker === l.bookmaker);
  const expl = parseJSON<{ positive_factors?: string[]; negative_factors?: string[]; what_would_change_it?: string[] }>(best?.explanation, {});
  const featured = pickFeatured(fx, best);
  const upcoming = fx.slice(0, 6);
  const filtered = Boolean(sp.comp || sp.min || sp.max || sp.lmin || sp.lmax || (sp.h && sp.h !== "168") || (sp.n && sp.n !== "8"));

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

      <form className="card filters" method="get" role="search" aria-label="Filtra l'analisi">
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
            <input id="lmin" name="lmin" type="number" inputMode="decimal" step="0.05" min="1" placeholder="1.20" defaultValue={sp.lmin} aria-label="Quota minima del singolo evento" />
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
          <label htmlFor="comp">Campionati</label>
          <div className="control">
            <Trophy size={17} aria-hidden="true" />
            <select id="comp" name="comp" defaultValue={sp.comp ?? ""}>
              <option value="">Tutti i campionati</option>
              {comps.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
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
            <div>
              {best ? (
                <span className="pill pill-good"><CheckCircle2 size={13} aria-hidden="true" /> Analisi completata</span>
              ) : (
                <span className="pill pill-warn"><CircleSlash size={13} aria-hidden="true" /> No bet</span>
              )}
              <h2 id="hero-title">{best ? "Ecco la tua schedina ottimizzata" : "Nessuna schedina: niente supera le soglie"}</h2>
              <p>
                Abbiamo analizzato <b>{fx.length}</b> partite
                {nMarkets > 0 && <>, valutato <b>{nMarkets.toLocaleString("it-IT")}</b> mercati</>} e trovato <b>{op.length}</b>{" "}
                {op.length === 1 ? "opportunità" : "opportunità"} da osservare.
                {!best && (reasons[0] ? ` ${reasons[0]}` : " Senza quote recenti il motore non propone giocate.")}
              </p>
            </div>
            <div className="hero-stats">
              {best ? (
                <>
                  <div className="hero-stat">
                    <small>Quota totale</small>
                    <b className="num">{best.total_odds.toFixed(2)}</b>
                    <span className={`chip-ev ${best.ev >= 0 ? "pos" : "neg"}`} style={{ background: best.ev >= 0 ? "var(--good-soft)" : "var(--bad-soft)" }}>
                      EV {signed(best.ev)}
                    </span>
                  </div>
                  <div className="hero-stat">
                    <small>Probabilità complessiva</small>
                    <b className="num">{pct(best.joint_probability, 1)}</b>
                    <span className="note">EV prudente {signed(best.ev_lower)}</span>
                  </div>
                </>
              ) : (
                <>
                  <div className="hero-stat">
                    <small>Partite con quote (24h)</small>
                    <b className="num">{run.n_with_quotes}</b>
                    <span className="note">su {run.n_fixtures} in calendario</span>
                  </div>
                  <div className="hero-stat">
                    <small>Schedine proposte</small>
                    <b className="num">0</b>
                    <span className="note">meglio nessuna giocata che una senza valore</span>
                  </div>
                </>
              )}
            </div>
          </section>

          <div className="kpis">
            <Kpi icon={Layers} label="Partite analizzate" value={fx.length} />
            <Kpi icon={BarChart3} label="Mercati valutati" value={nMarkets} />
            <Kpi icon={Target} label="Opportunità" value={op.length} />
            <Kpi icon={ShieldCheck} label="Schedine" value={sl.length} />
          </div>

          {/* ---------------- slip + why ---------------- */}
          <div className="split">
            <section className="card" aria-labelledby="slip-title">
              <div className="card-head">
                <h2 id="slip-title">
                  La tua schedina consigliata {best && <span className="count">{best.legList.length} eventi</span>}
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
                        </tr>
                      </thead>
                      <tbody>
                        {best.legList.map((l, i) => {
                          const o = oppOf(l);
                          const [home, away] = (l.match ?? "").split(" - ");
                          return (
                            <tr key={i}>
                              <td className="muted num">{i + 1}</td>
                              <td className="wrap">
                                <MatchCell home={home} away={away ?? ""} sub={<>{l.market} · {compShort(l.competition)} · {hour(l.kickoff)}</>} href={o ? `/partita/${encodeURIComponent(o.fixture_id)}` : undefined} />
                              </td>
                              <td className="num">{l.odds.toFixed(2)}</td>
                              <td className="num">
                                {pct(l.p, 1)}
                                {o?.p_low != null && o.p_high != null && <span className="sub">({pct(o.p_low, 1)} – {pct(o.p_high, 1)})</span>}
                              </td>
                              <td className="num">
                                <span className={o && o.ev >= 0 ? "pos" : "neg"}>{o ? signed(o.ev) : "–"}</span>
                                {o && <span className="sub"><span className={`status status-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span></span>}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <div className="kpis" style={{ padding: 12, borderTop: "1px solid var(--line)" }}>
                    <Mini label="Quota totale" value={best.total_odds.toFixed(2)} />
                    <Mini label="Probabilità complessiva" value={pct(best.joint_probability, 1)} />
                    <Mini label="EV stimato" value={signed(best.ev)} tone={best.ev >= 0 ? "pos" : "neg"} />
                    <Mini label="Puntata paper" value={`${best.stake.toFixed(2)} €`} />
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
                <h2 id="why-title"><Brain size={17} color="var(--accent)" aria-hidden="true" /> {best ? "Perché questa schedina?" : "Perché nessuna schedina?"}</h2>
              </div>
              <div className="card-pad">
                <ul className="checklist">
                  {best ? (
                    <>
                      {(expl.positive_factors ?? []).map((x, i) => (
                        <li key={`p${i}`}><CheckCircle2 size={17} className="ok" aria-label="a favore" /> <span>{x}</span></li>
                      ))}
                      {(expl.negative_factors ?? []).map((x, i) => (
                        <li key={`n${i}`}><AlertTriangle size={17} className="ko" aria-label="attenzione" /> <span>{x}</span></li>
                      ))}
                    </>
                  ) : (
                    (reasons.length ? reasons : ["Nessuna quota recente da confrontare con il modello."]).map((x, i) => (
                      <li key={i}><AlertTriangle size={17} className="ko" aria-label="motivo" /> <span>{x}</span></li>
                    ))
                  )}
                </ul>
                {best && (expl.what_would_change_it ?? []).length > 0 && (
                  <>
                    <h3 className="note" style={{ margin: "16px 0 6px", textTransform: "uppercase", letterSpacing: ".06em" }}>Cosa la cambierebbe</h3>
                    <ul className="checklist">
                      {(expl.what_would_change_it ?? []).map((x, i) => (
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
              <h2 id="opp-title"><TrendingUp size={17} color="var(--accent)" aria-hidden="true" /> Migliori opportunità <span className="count">{op.length}</span></h2>
              <Link href="/opportunita" className="btn btn-ghost btn-sm">Vedi tutte <ChevronRight size={15} aria-hidden="true" /></Link>
            </div>
            {op.length ? <OppTable rows={op.slice(0, 8)} /> : (
              <Empty icon={Percent} title="Nessun mercato sopra le soglie">
                Le quote arrivano da OddsPapi nelle 24 ore prima delle partite: senza prezzi recenti non si calcola il valore atteso.
              </Empty>
            )}
          </section>

          {featured && <DeepPreview f={featured} />}
        </div>

        {/* ---------------- right rail ---------------- */}
        <aside className="col rail" aria-label="Pannelli rapidi">
          <section className="card">
            <div className="card-head"><h2>Analisi rapida di una partita</h2></div>
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
            <div className="card-head"><h2>Mercati analizzati</h2></div>
            <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 14 }}>
              <div>
                <span className="note">Con quote dei bookmaker (valore atteso)</span>
                <div className="text-2">1X2, Doppia chance, Under/Over, Gol/NoGol</div>
              </div>
              <div>
                <span className="note">Solo probabilità del modello (quota equa)</span>
                <div className="text-2">Multigol, Combo, Gol squadra, Risultato esatto</div>
              </div>
            </div>
          </section>

          <section className="card">
            <div className="card-head"><h2>Budget richieste API</h2></div>
            <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <Meter label="GOAL API · oggi" used={use.goalDay} limit={1000} hint="Calendario, risultati, formazioni, statistiche" />
              <Meter label="OddsPapi · mese" used={use.oddsMonth} limit={250} hint="Quote Sisal e Pinnacle, solo nelle 24h prima" />
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

function Kpi({ icon: Icon, label, value }: { icon: typeof Layers; label: string; value: number }) {
  return (
    <div className="card kpi">
      <span className="kpi-icon"><Icon size={18} aria-hidden="true" /></span>
      <span>
        <small>{label}</small>
        <b className="num">{value.toLocaleString("it-IT")}</b>
      </span>
    </div>
  );
}

function Mini({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div>
      <span className="note">{label}</span>
      <b className={`num ${tone ?? ""}`} style={{ display: "block", fontSize: 18 }}>{value}</b>
    </div>
  );
}

function pickFeatured(fx: FixtureRow[], best?: { legList: Leg[] }) {
  if (best) {
    const [h, a] = (best.legList[0]?.match ?? "").split(" - ");
    const f = fx.find((x) => x.home === h && x.away === a);
    if (f) return f;
  }
  return fx.find((f) => f.p_home != null) ?? null;
}

function DeepPreview({ f }: { f: FixtureRow }) {
  const mk = parseJSON<ModelMarket[]>(f.markets, []);
  const get = (g: string, l: string) => mk.find((m) => m.g === g && m.l === l)?.p;
  const picks: [string, number | undefined][] = [
    ["Over 2.5", get("Under/Over", "Over 2.5") ?? f.p_over25 ?? undefined],
    ["Gol", get("Gol/NoGol", "Gol") ?? f.p_btts ?? undefined],
    ["Multigol 2-4", get("Multigol", "Multigol 2-4")],
    ["1X", get("Doppia chance", "1X")],
    ["1 + Over 2.5", get("Combo", "1 + Over 2.5")],
  ];
  return (
    <section className="card" aria-labelledby="deep-title">
      <div className="card-head">
        <h2 id="deep-title">Analisi approfondita · {f.home} vs {f.away}</h2>
        <Link href={`/partita/${encodeURIComponent(f.fixture_id)}`} className="btn btn-ghost btn-sm">
          Apri analisi completa <ChevronRight size={15} aria-hidden="true" />
        </Link>
      </div>
      <div className="split card-pad" style={{ gap: 24 }}>
        <div>
          <h3 className="note" style={{ margin: "0 0 10px" }}>Probabilità 1X2 (modello)</h3>
          <div className="rings">
            <Ring value={f.p_home} label={f.home} sub={`quota equa ${fairOdds(f.p_home)}`} color="var(--s1)" />
            <Ring value={f.p_draw} label="Pareggio" sub={`quota equa ${fairOdds(f.p_draw)}`} color="var(--s2)" />
            <Ring value={f.p_away} label={f.away} sub={`quota equa ${fairOdds(f.p_away)}`} color="var(--s3)" />
          </div>
          {f.xg_home != null && f.xg_away != null && (
            <p className="note" style={{ textAlign: "center", marginTop: 12 }}>
              Gol attesi: <span className="num">{f.xg_home.toFixed(2)}</span> – <span className="num">{f.xg_away.toFixed(2)}</span>
            </p>
          )}
        </div>
        <div>
          <h3 className="note" style={{ margin: "0 0 10px" }}>Mercati principali (probabilità del modello)</h3>
          {picks.filter(([, p]) => p != null).map(([l, p]) => <HBar key={l} label={l} p={p!} />)}
          <div style={{ marginTop: 10 }}><Split1X2 h={f.p_home} d={f.p_draw} a={f.p_away} /></div>
        </div>
      </div>
    </section>
  );
}
