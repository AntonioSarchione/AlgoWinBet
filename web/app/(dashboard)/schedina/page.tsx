import Link from "next/link";
import { unstable_cache } from "next/cache";
import { ArrowLeft, CircleSlash, ListChecks, Percent, Sigma } from "lucide-react";
import { fixtureCandidates, latestRun, parseJSON, runFixtures } from "@/lib/db";
import { legMinOdds, type OptSettings } from "@/lib/optimizer";
import { PROFILE_HINT, PROFILE_LABEL, runProfiles, toLegs, type ProfileResult } from "@/lib/profiles";
import { compShort, dayTime, hour, pct, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, MatchCell } from "@/app/_components/ui";
import { MAX_PICK } from "@/lib/pick";

export const dynamic = "force-dynamic";
export const metadata = { title: "Schedina manuale" };

const EV_MIN = [
  { v: "", l: "Nessun limite" }, { v: "10", l: "Almeno +10%" }, { v: "5", l: "Almeno +5%" }, { v: "2", l: "Almeno +2%" },
  { v: "0", l: "Almeno 0% (pari)" }, { v: "-2", l: "Almeno −2%" }, { v: "-5", l: "Almeno −5%" }, { v: "-10", l: "Almeno −10%" },
];
const LEG_PROB = [{ v: "", l: "Qualsiasi" }, ...[30, 40, 50, 60, 70, 80].map((k) => ({ v: String(k), l: `Almeno ${k}%` }))];

// Every chosen match enters the slip with exactly one selection: the search runs over all their published selections
// (Alta, Media, Equa, Da osservare), with no slip-odds window, so the only limits are the ones chosen on this page.
const manualSlips = unstable_cache(
  async (runId: number, ids: string[], settings: OptSettings, evMin: number, legProb: number, now: string) => {
    const legs = toLegs(await fixtureCandidates(runId, ids)).filter((o) => o.kickoff > now && o.p_final >= legProb);
    const n = new Set(legs.map((l) => l.fixture_id)).size;
    if (!n) return { n, results: null };
    const results = runProfiles(legs, settings, {
      min_legs: n, max_legs: n, max_legs_per_competition: n, odds_min: 1, odds_max: 1e9, min_probability: 0,
      min_leg_probability: legProb, min_slip_ev: evMin, include_watch: true, include_fair: true,
      candidates_per_fixture: 8, max_candidates: 8 * MAX_PICK,
    });
    return { n, results };
  },
  ["manualSlips"],
  { revalidate: 6 * 3600 },
);

type SP = { fx?: string | string[]; p?: string; ev?: string; pmin?: string };

export default async function Schedina({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
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
  const nowIso = new Date().toISOString().slice(0, 16); // minute precision: equal choices share the cache
  const [fixtures, computed] = await Promise.all([
    runFixtures(run.id),
    settings ? manualSlips(run.id, ids, settings, evMin, legProb, nowIso) : Promise.resolve({ n: 0, results: null }),
  ]);
  const byId = new Map(fixtures.map((f) => [f.fixture_id, f]));
  const results: Record<string, ProfileResult> = computed.results ?? {};
  const names = Object.keys(results);
  const profile = sp.p && results[sp.p] ? sp.p : names[0];
  const result = profile ? results[profile] : null;
  const best = result?.slips[0];
  const inSlip = new Set(best?.legs.map((l) => l.fixture_id) ?? []);
  const left = ids.filter((id) => !inSlip.has(id));
  const href = (p: string) => {
    const q = new URLSearchParams(ids.map((id) => ["fx", id]));
    if (sp.ev) q.set("ev", sp.ev);
    if (sp.pmin) q.set("pmin", sp.pmin);
    q.set("p", p);
    return `/schedina?${q}`;
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Schedina manuale</h1>
          <p>
            {ids.length} {ids.length === 1 ? "partita scelta" : "partite scelte"} da te: per ognuna il modello sceglie l&apos;esito che rende migliore la
            schedina. Uso personale, solo paper trading: queste schedine non entrano nel Registro.
          </p>
        </div>
        <Link href={back} className="btn"><ArrowLeft size={17} aria-hidden="true" /> Modifica la scelta</Link>
      </header>

      <form className="card filters" method="get" aria-label="Limiti della schedina manuale">
        {ids.map((id) => <input key={id} type="hidden" name="fx" value={id} />)}
        {sp.p && <input type="hidden" name="p" value={sp.p} />}
        <div className="field">
          <label htmlFor="ev">EV minimo della schedina</label>
          <div className="control">
            <Sigma size={17} aria-hidden="true" />
            <select id="ev" name="ev" defaultValue={sp.ev ?? ""}>
              {EV_MIN.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}
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
        <div className="field" style={{ alignSelf: "end" }}>
          <button type="submit" className="btn btn-primary">Applica</button>
        </div>
      </form>

      {names.length > 1 && (
        <nav className="profiles" aria-label="Profili di schedina">
          {names.map((name) => {
            const r = results[name];
            const s0 = r.slips[0];
            return (
              <Link key={name} href={href(name)} className={`card profile ${name === profile ? "active" : ""}`} aria-current={name === profile ? "true" : undefined}>
                <span className="profile-name">{PROFILE_LABEL[name] ?? name}</span>
                <span className="note">{PROFILE_HINT[name] ?? ""}</span>
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
            {best.ev < 0 && (
              <p className="note card-pad" style={{ paddingTop: 0 }}>
                EV negativo: con queste partite anche la combinazione migliore paga meno di quanto vale. Il modello non la giocherebbe.
              </p>
            )}
          </>
        ) : (
          <Empty icon={CircleSlash} title="Nessuna schedina">
            {(result?.reasons ?? []).join(" ") ||
              (settings ? "Nessuna delle partite scelte ha giocate pubblicate." : "Analisi pubblicata con una versione precedente: riprova dopo la prossima pubblicazione.")}
          </Empty>
        )}
      </section>

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
                        : "nessun esito Sisal con valore o a quota equa, oppure sotto la probabilità minima scelta. Il modello non la metterebbe in schedina."}
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
