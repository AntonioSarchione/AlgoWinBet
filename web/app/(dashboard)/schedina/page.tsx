import Link from "next/link";
import { unstable_cache } from "next/cache";
import { ArrowLeft, CircleSlash, Filter, Gauge, ListChecks, ListOrdered, Percent, Shapes, ShieldAlert, Sigma, Target, Unlock } from "lucide-react";
import { absencesFor, DEPLOY, fixtureBook, fixtureCandidates, latestRun, parseJSON, runFixtures, type Absence, type BookSel, type OppRow } from "@/lib/db";
import { legMinOdds, type OptSettings } from "@/lib/optimizer";
import { PROFILE_LABEL, profileHint, runProfiles, toLegs, type Cand, type ProfileResult } from "@/lib/profiles";
import { compShort, dayTime, hour, pct, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, Fold, MatchCell } from "@/app/_components/ui";
import { FreeCard } from "@/app/_components/FreeSlipCard";
import { freeSlip, type FreeOpt } from "@/lib/freeslip";
import { LEG_PROB, legCount, RISK } from "@/lib/slipfilters";
import { MARKET_GROUPS, marketGroup } from "@/lib/markets";
import { MAX_PICK } from "@/lib/pick";
import { MyCombo } from "@/app/_components/MyCombo";
import { isEstimated } from "@/lib/books";
import { LegAbsences } from "@/app/_components/Absences";

export const dynamic = "force-dynamic";
export const metadata = { title: "Schedina manuale" };

const EV_MIN = [
  { v: "", l: "Nessun limite" }, { v: "10", l: "Almeno +10%" }, { v: "5", l: "Almeno +5%" }, { v: "2", l: "Almeno +2%" },
  { v: "0", l: "Almeno 0% (pari)" }, { v: "-2", l: "Almeno −2%" }, { v: "-5", l: "Almeno −5%" }, { v: "-10", l: "Almeno −10%" },
];

const ALL_STATUSES = ["STRONG", "CANDIDATE", "FAIR", "WATCH", "NEUTRAL", "AVOID"];
const PER_FIXTURE = 6; // kept per match and per criterion (value score, probability, EV) before the search

type Limits = {
  evMin: number; legProb: number; qMin: number; qMax: number; lMin: number; lMax: number; nMin: number; nMax: number; riskMin: number;
  markets: string[];
};

// The candidates of the chosen matches: every playable Sisal selection (pub_book, any status) where the run has it, else
// the published opportunities. Per match the best few by value score, by probability and by EV, so every profile finds
// its kind of selection.
async function candidates(runId: number, ids: string[], now: string, k: Limits): Promise<Cand[]> {
  const [fixtures, book] = await Promise.all([runFixtures(runId), fixtureBook(runId, ids)]);
  const fx = new Map(fixtures.map((f) => [f.fixture_id, f]));
  const rows: OppRow[] = [];
  for (const b of book) {
    const f = fx.get(b.fixture_id);
    if (!f) continue;
    for (const x of parseJSON<BookSel[]>(b.sels, [])) {
      rows.push({
        fixture_id: f.fixture_id, kickoff: f.kickoff, competition: f.competition, match: `${f.home} - ${f.away}`, home: f.home, away: f.away,
        market: x.m, bookmaker: x.b, odds: x.o, fair_odds: 1 / x.p, p_final: x.p, p_market: x.pm, ev: x.e, ev_lower: x.e, uncertainty: x.u,
        data_quality: 1, status: x.st, odds_stale: 0, lineup_state: f.lineup_state, p_struct: x.s, sel_key: x.k, score: x.sc,
        disagreement: x.d, dq_lineup: x.l,
      } as OppRow);
    }
  }
  const inBook = new Set(book.map((b) => b.fixture_id));
  const rest = ids.filter((id) => !inBook.has(id));
  if (rest.length) rows.push(...(await fixtureCandidates(runId, rest)));
  const picked = new Set(k.markets);
  const legs = toLegs(rows).filter(
    (o) => o.kickoff > now && o.p_final >= k.legProb && (!k.lMin || o.odds >= k.lMin) && (!k.lMax || o.odds <= k.lMax)
      && (!picked.size || picked.has(marketGroup(o.sel_key))),
  );
  const out: Cand[] = [];
  for (const id of ids) {
    const mine = legs.filter((l) => l.fixture_id === id);
    const pick = new Set<Cand>();
    for (const by of [(l: Cand) => l.score, (l: Cand) => l.p_final, (l: Cand) => l.ev]) {
      for (const l of [...mine].sort((a, b) => by(b) - by(a)).slice(0, PER_FIXTURE)) pick.add(l);
    }
    out.push(...pick);
  }
  return out;
}

// One selection per chosen match; by default every chosen match enters the slip. The only limits are the ones chosen on this page.
const manualSlips = unstable_cache(
  async (runId: number, ids: string[], settings: OptSettings, k: Limits, now: string) => {
    const legs = await candidates(runId, ids, now, k);
    const n = new Set(legs.map((l) => l.fixture_id)).size;
    if (!n) return { n, results: null };
    const results = runProfiles(legs, settings, {
      min_legs: Math.min(k.nMin, n), max_legs: Math.min(k.nMax, n), max_legs_per_competition: n, odds_min: k.qMin || 1, odds_max: k.qMax || 1e9,
      min_probability: k.riskMin,
      min_leg_probability: Math.max(k.legProb, settings.optimizer.min_leg_probability ?? 0), min_slip_ev: k.evMin, statuses: ALL_STATUSES,
      candidates_per_fixture: 3 * PER_FIXTURE, max_candidates: 3 * PER_FIXTURE * MAX_PICK,
    });
    return { n, results };
  },
  ["manualSlips", DEPLOY],
  { revalidate: 6 * 3600 },
);

type SP = {
  fx?: string | string[]; p?: string; ev?: string; pmin?: string; min?: string; max?: string; lmin?: string; lmax?: string; combo?: string;
  n?: string; nmin?: string; risk?: string; mk?: string | string[]; free?: string;
};
const KEPT = ["ev", "pmin", "min", "max", "lmin", "lmax", "n", "nmin", "risk", "free"] as const; // carried by the profile links

// "Senza regole": every playable Sisal selection of the chosen matches
async function freeOptions(runId: number, ids: string[]): Promise<FreeOpt[]> {
  const [fixtures, book] = await Promise.all([runFixtures(runId), fixtureBook(runId, ids)]);
  const now = Date.now();
  const fx = new Map(fixtures.filter((f) => new Date(f.kickoff).getTime() > now).map((f) => [f.fixture_id, f]));
  return book.flatMap((b) => {
    const f = fx.get(b.fixture_id);
    if (!f) return [];
    return parseJSON<BookSel[]>(b.sels, []).map((x) => ({
      fixture_id: f.fixture_id, competition: f.competition, kickoff: f.kickoff, match: `${f.home} - ${f.away}`, market: x.m, odds: x.o, p: x.p,
      p_market: x.pm, bookmaker: x.b,
    }));
  });
}

export default async function Schedina({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
  if (sp.combo) return <ComboPage id={sp.combo} />;
  const ids = [...new Set([sp.fx ?? []].flat().filter(Boolean))].slice(0, MAX_PICK).sort();
  const back = `/palinsesto?${new URLSearchParams(ids.map((id) => ["fx", id]))}`;
  const run = await latestRun();
  if (!ids.length || !run) {
    return (
      <div className="card">
        <Empty icon={ListChecks} title="Nessuna partita scelta">
          Apri il <Link href="/palinsesto">Palinsesto</Link>, spunta le partite che vuoi giocare e premi «Crea schedina con queste».
        </Empty>
      </div>
    );
  }
  const settings = parseJSON<OptSettings | null>(run.optimizer, null);
  const evMin = EV_MIN.some((x) => x.v === sp.ev) && sp.ev ? Number(sp.ev) / 100 : -1; // -100% = no limit
  const legProb = LEG_PROB.some((x) => x.v === sp.pmin) ? Number(sp.pmin || 0) / 100 : 0;
  const num = (x?: string) => (Number(x) > 1 ? Number(x) : 0);
  // number of legs: by default every chosen match
  const nMax = legCount(sp.n, ids.length);
  const nMin = Math.min(legCount(sp.nmin, ids.length), nMax);
  const riskMin = RISK.some((x) => x.v === sp.risk) ? Number(sp.risk || 0) / 100 : 0;
  const pickedMk = [...new Set([sp.mk ?? []].flat().filter((m) => (MARKET_GROUPS as readonly string[]).includes(m)))].sort();
  const allMk = !pickedMk.length || pickedMk.length === MARKET_GROUPS.length;
  const free = sp.free === "1";
  const limits: Limits = {
    evMin, legProb, qMin: num(sp.min), qMax: num(sp.max), lMin: num(sp.lmin), lMax: num(sp.lmax), nMin, nMax, riskMin, markets: allMk ? [] : pickedMk,
  };
  const filtered = Boolean(sp.ev || sp.pmin || sp.min || sp.max || sp.lmin || sp.lmax || sp.n || sp.nmin || sp.risk || !allMk || free);
  const freeRes = free
    ? freeSlip(await freeOptions(run.id, ids), { qMin: limits.qMin, qMax: limits.qMax, lMin: limits.lMin, lMax: limits.lMax, nMin, nMax })
    : null;
  const nowIso = new Date().toISOString().slice(0, 16); // minute precision: equal choices share the cache
  const [fixtures, computed] = await Promise.all([
    runFixtures(run.id),
    settings ? manualSlips(run.id, ids, settings, limits, nowIso) : Promise.resolve({ n: 0, results: null }),
  ]);
  const byId = new Map(fixtures.map((f) => [f.fixture_id, f]));
  const results: Record<string, ProfileResult> = computed.results ?? {};
  const names = Object.keys(results);
  const profile = sp.p && results[sp.p] ? sp.p : names[0];
  const result = profile ? results[profile] : null;
  const best = result?.slips[0];
  const inSlip = new Set((freeRes ? freeRes.slip?.legs : best?.legs)?.map((l) => l.fixture_id) ?? []);
  const left = ids.filter((id) => !inSlip.has(id));
  const absent = best ? await absencesFor(best.legs.map((l) => l.fixture_id)).catch(() => ({}) as Record<string, Absence[]>) : {};
  const href = (p: string) => {
    const q = new URLSearchParams(ids.map((id) => ["fx", id]));
    for (const key of KEPT) if (sp[key]) q.set(key, sp[key]!);
    for (const m of pickedMk) q.append("mk", m);
    q.set("p", p);
    return `/schedina?${q}`;
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Schedina manuale</h1>
          <p>
            {ids.length} {ids.length === 1 ? "partita scelta" : "partite scelte"} da te: per ognuna il modello sceglie, tra tutte le quote Sisal con
            probabilità almeno del 25%, l&apos;esito che rende migliore la schedina. Uso personale, solo paper trading: queste schedine non entrano nel Registro.
          </p>
        </div>
        <Link href={back} className="btn"><ArrowLeft size={17} aria-hidden="true" /> Modifica la scelta</Link>
      </header>

      <Fold title="Filtri" icon={Filter} side={filtered && <span className="count">attivi</span>}>
      <form key={JSON.stringify(sp)} className="filters" method="get" aria-label="Limiti della schedina manuale">
        {ids.map((id) => <input key={id} type="hidden" name="fx" value={id} />)}
        {sp.p && <input type="hidden" name="p" value={sp.p} />}
        <div className="field">
          <label htmlFor="min">Quota schedina (min – max)</label>
          <div className="control">
            <Gauge size={17} aria-hidden="true" />
            <input id="min" name="min" type="number" inputMode="decimal" step="0.05" min="1" placeholder="nessuna" defaultValue={sp.min} aria-label="Quota minima della schedina" />
            <span className="dash">–</span>
            <input name="max" type="number" inputMode="decimal" step="0.05" min="1" placeholder="nessuna" defaultValue={sp.max} aria-label="Quota massima della schedina" />
          </div>
        </div>
        <div className="field">
          <label htmlFor="lmin">Quota singolo evento (min – max)</label>
          <div className="control">
            <Target size={17} aria-hidden="true" />
            <input id="lmin" name="lmin" type="number" inputMode="decimal" step="0.05" min="1.25" placeholder="1.25" defaultValue={sp.lmin} aria-label="Quota minima del singolo evento" />
            <span className="dash">–</span>
            <input name="lmax" type="number" inputMode="decimal" step="0.05" min="1" placeholder="nessuna" defaultValue={sp.lmax} aria-label="Quota massima del singolo evento" />
          </div>
        </div>
        <div className="field">
          <label htmlFor="nmin">Numero di eventi (min – max)</label>
          <div className="control">
            <ListOrdered size={17} aria-hidden="true" />
            <input id="nmin" name="nmin" type="number" inputMode="numeric" step="1" min="1" max={ids.length} placeholder={String(ids.length)} defaultValue={sp.nmin} aria-label="Numero minimo di eventi" />
            <span className="dash">–</span>
            <input name="n" type="number" inputMode="numeric" step="1" min="1" max={ids.length} placeholder={String(ids.length)} defaultValue={sp.n} aria-label="Numero massimo di eventi" />
          </div>
        </div>
        <div className="field rule-field">
          <label htmlFor="pmin">Probabilità minima per evento</label>
          <div className="control">
            <Percent size={17} aria-hidden="true" />
            <select id="pmin" name="pmin" defaultValue={sp.pmin ?? ""}>
              {LEG_PROB.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field rule-field">
          <label htmlFor="ev">EV minimo della schedina</label>
          <div className="control">
            <Sigma size={17} aria-hidden="true" />
            <select id="ev" name="ev" defaultValue={sp.ev ?? ""}>
              {EV_MIN.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field rule-field">
          <label htmlFor="risk">Rischio massimo</label>
          <div className="control">
            <ShieldAlert size={17} aria-hidden="true" />
            <select id="risk" name="risk" defaultValue={sp.risk ?? ""}>
              {RISK.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </div>
        </div>
        <div className="field rule-field">
          <span className="field-label" id="mk-label">Mercati</span>
          <details className="multi">
            <summary className="control" aria-labelledby="mk-label">
              <Shapes size={17} aria-hidden="true" />
              <span className="multi-value">{allMk ? "Tutti i mercati" : pickedMk.length === 1 ? pickedMk[0] : `${pickedMk.length} mercati`}</span>
            </summary>
            <fieldset className="multi-panel" aria-labelledby="mk-label">
              {MARKET_GROUPS.map((m) => (
                <label key={m} className="check">
                  <input type="checkbox" name="mk" value={m} defaultChecked={allMk || pickedMk.includes(m)} />
                  {m}
                </label>
              ))}
              <button type="submit" className="btn btn-primary btn-sm" style={{ marginTop: 6 }}>
                <Filter size={15} aria-hidden="true" /> Applica
              </button>
            </fieldset>
          </details>
        </div>
        <label className="check free-check" title="Spegne le regole del modello: restano quota schedina, quota evento e numero di eventi">
          <input type="checkbox" id="free" name="free" value="1" defaultChecked={free} />
          <Unlock size={15} aria-hidden="true" /> Senza regole
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          {filtered && <Link href={`/schedina?${new URLSearchParams(ids.map((id) => ["fx", id]))}`} className="btn btn-ghost">Azzera</Link>}
          <button type="submit" className="btn btn-primary"><Filter size={17} aria-hidden="true" /> Applica</button>
        </div>
      </form>
      </Fold>

      {freeRes ? <FreeCard r={freeRes} scope="tra le partite scelte" /> : (<>
      {names.length > 1 && (
        <nav className="profiles" aria-label="Profili di schedina">
          {names.map((name) => {
            const r = results[name];
            const s0 = r.slips[0];
            return (
              <Link key={name} href={href(name)} className={`card profile ${name === profile ? "active" : ""}`} aria-current={name === profile ? "true" : undefined}>
                <span className="profile-name">{PROFILE_LABEL[name] ?? name}</span>
                <span className="note">{profileHint(name, evMin)}</span>
                {s0 ? (
                  <span className="profile-stats">
                    <span><small>Quota</small><b className="num">{s0.total_odds.toFixed(2)}</b></span>
                    <span><small>Vince</small><b className="num">{pct(s0.joint_probability, 1)}</b></span>
                    <span><small>EV</small><b className={`num ${s0.ev >= 0 ? "pos" : "neg"}`}>{signed(s0.ev)}</b></span>
                    <span><small>Eventi</small><b className="num">{s0.legs.length}</b></span>
                  </span>
                ) : (
                  <span className="profile-stats">
                    <span><small>Esito</small><b>{r.sameAs ? `Stessa di «${PROFILE_LABEL[r.sameAs] ?? r.sameAs}»` : "Nessuna"}</b></span>
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      )}

      <section className="card" aria-labelledby="slip-title">
        <div className="card-head">
          <h2 id="slip-title">
            Schedina · {PROFILE_LABEL[profile ?? ""] ?? "manuale"}{" "}
            {best && <span className="count">{best.legs.length} {best.legs.length === 1 ? "evento" : "eventi"}</span>}
          </h2>
          {best && <span className="muted">Quota totale <b className="num pos">{best.total_odds.toFixed(2)}</b></span>}
        </div>
        {best ? (
          <>
            <div className="table-wrap">
              <table className="compact">
                <thead>
                  <tr>
                    <th>#</th><th>Evento / Esito scelto</th><th className="num">Quota</th><th className="num">Probabilità</th><th className="num">EV · Stato</th>
                    {evMin > -1 && (
                      <th className="num" title="Sotto questa quota Sisal la schedina scende sotto l'EV minimo scelto">Gioca se ≥</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {best.legs.map((l, i) => {
                    const [home, away] = (l.match ?? "").split(" - ");
                    return (
                      <tr key={l.fixture_id}>
                        <td className="muted num">{i + 1}</td>
                        <td className="wrap">
                          <MatchCell home={home} away={away ?? ""} sub={<>{l.market} · {compShort(l.competition)} · {dayTime(l.kickoff)}</>} href={`/partita/${encodeURIComponent(l.fixture_id)}`} />
                          {isEstimated(l.bookmaker) && (
                            <span className="sub crit-warn">Quota Sisal stimata da Pinnacle: gioca solo se su Sisal è almeno {(1 / l.p_final).toFixed(2)}</span>
                          )}
                          <LegAbsences list={absent[l.fixture_id] ?? []} runAt={run.created_at} />
                        </td>
                        <td className="num">{l.odds.toFixed(2)}</td>
                        <td className="num">{pct(l.p_final, 1)}</td>
                        <td className="num">
                          <span className={l.ev >= 0 ? "pos" : "neg"}>{signed(l.ev)}</span>
                          <span className="sub"><span className={`status status-${l.status}`}>{STATUS_LABEL[l.status] ?? l.status}</span></span>
                        </td>
                        {evMin > -1 && <td className="num">{legMinOdds(best, l.odds, evMin).toFixed(2)}</td>}
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
              <Mini label="Bonus multipla Sisal" value={best.bonus ? `+${(best.bonus * 100).toFixed(0)}%` : "—"} />
            </div>
            {best.legs.some((l) => isEstimated(l.bookmaker)) && (
              <p className="note card-pad" style={{ paddingTop: 0 }}>
                Alcune partite non sono quotate da Sisal sul nostro fornitore: la quota è stimata dal prezzo equo di Pinnacle per il margine
                abituale di Sisal. Controlla la quota vera su Sisal prima di giocare; queste giocate non entrano nel Registro.
              </p>
            )}
            {best.ev < 0 && (
              <p className="note card-pad" style={{ paddingTop: 0 }}>
                EV negativo: con queste partite anche la combinazione migliore paga meno di quanto vale. Il modello non la giocherebbe.
              </p>
            )}
          </>
        ) : (
          <Empty icon={CircleSlash} title="Nessuna schedina">
            {(result?.reasons ?? []).join(" ") ||
              (settings ? (allMk ? "Nessuna delle partite scelte ha giocate pubblicate." : "Nessuna quota Sisal delle partite scelte nei mercati scelti.") : "Analisi pubblicata con una versione precedente: riprova dopo la prossima pubblicazione.")}
          </Empty>
        )}
      </section>
      </>)}

      {left.length > 0 && (
        <section className="card" aria-labelledby="left-title">
          <div className="card-head"><h2 id="left-title">Partite scelte rimaste fuori</h2></div>
          <ul className="checklist card-pad">
            {left.map((id) => {
              const f = byId.get(id);
              return (
                <li key={id}>
                  <CircleSlash size={17} className="ko" aria-hidden="true" />
                  <span>
                    {f ? <><b>{f.home} – {f.away}</b> ({compShort(f.competition)}, {hour(f.kickoff)}): </> : <><b>Partita non più in analisi</b>: </>}
                    {!f
                      ? "non è nell'ultima analisi pubblicata (già iniziata o fuori dai 7 giorni)."
                      : new Date(f.kickoff).getTime() <= Date.now()
                        ? "già iniziata."
                        : "fuori dalla schedina migliore con i filtri scelti (quota, probabilità, numero di eventi, mercati) o nessuna quota Sisal per questa partita."}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </>
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

// My Combo: several selections of one match on the single price Sisal shows for them together (typed by hand: no feed
// carries Sisal's My Combo prices). The page gives the expected goals; the browser does the rest.
async function ComboPage({ id }: { id: string }) {
  const run = await latestRun();
  const f = run ? (await runFixtures(run.id)).find((x) => x.fixture_id === id) : undefined;
  const back = `/partita/${encodeURIComponent(id)}`;
  const head = (
    <header className="page-head">
      <div>
        <h1>My Combo{f ? ` · ${f.home} – ${f.away}` : ""}</h1>
        <p>
          {f ? <>{compShort(f.competition)} · {dayTime(f.kickoff)}. </> : null}
          Scegli più esiti della stessa partita e scrivi la quota My Combo che vedi su Sisal: il modello calcola la probabilità che vincano
          insieme, la quota equa e l&apos;EV. Uso personale, solo paper trading: non entra nel Registro.
        </p>
      </div>
      <Link href={f ? back : "/palinsesto"} className="btn"><ArrowLeft size={17} aria-hidden="true" /> {f ? "Torna alla partita" : "Palinsesto"}</Link>
    </header>
  );
  if (!run || !f) {
    return <>{head}<div className="card"><Empty icon={CircleSlash} title="Partita non in analisi">Non è nell&apos;ultima analisi pubblicata (già iniziata o fuori dai 7 giorni).</Empty></div></>;
  }
  if (f.xg_home == null || f.xg_away == null || f.rho == null) {
    return <>{head}<div className="card"><Empty icon={CircleSlash} title="Matrice dei risultati non disponibile">Analisi pubblicata con una versione precedente: riprova dopo la prossima pubblicazione.</Empty></div></>;
  }
  const sels = parseJSON<BookSel[]>((await fixtureBook(run.id, [id]))[0]?.sels ?? "[]", []);
  const finals: Record<string, number> = {};
  const prices: Record<string, number> = {};
  for (const x of sels) {
    finals[x.k] = x.p;
    if (!isEstimated(x.b)) prices[x.k] = x.o;
  }
  return (
    <>
      {head}
      <MyCombo home={f.home} away={f.away} xgHome={f.xg_home} xgAway={f.xg_away} rho={f.rho} finals={finals} prices={prices} />
    </>
  );
}
