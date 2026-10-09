import type { ReactNode } from "react";
import { Coins, Gauge, Layers, Percent, PiggyBank, Scale, TrendingDown, Wallet } from "lucide-react";
import { bankrollSlips, parseJSON } from "@/lib/db";
import { DEFAULT_PLAN, effectiveOdds, simulate, type Method, type Plan } from "@/lib/bankroll";
import { PROFILE_LABEL } from "@/lib/profiles";
import { independentSlips } from "@/lib/criterion";
import { dayTime, pct, signed } from "@/app/_components/format";
import { Empty } from "@/app/_components/ui";
import { OddsChart } from "@/app/_components/OddsChart";

export const dynamic = "force-dynamic";
export const metadata = { title: "Bankroll" };

const METHOD: Record<Method, { label: string; hint: string }> = {
  kelly: { label: "Kelly frazionato", hint: "puntata proporzionale al vantaggio stimato: più valore, più puntata; nessun valore, nessuna puntata" },
  pct: { label: "Percentuale del saldo", hint: "sempre la stessa quota del saldo: la puntata cresce e cala con il saldo" },
  flat: { label: "Puntata fissa", hint: "sempre la stessa cifra, qualunque sia il saldo" },
};
const RESULT_LABEL: Record<string, string> = { won: "Vinta", lost: "Persa", void: "Rimborsata", "non valutabile": "Rimborsata" };
const RESULT_CLASS: Record<string, string> = { won: "status-STRONG", lost: "status-AVOID", void: "status-WATCH", "non valutabile": "status-WATCH" };

type SP = { start?: string; m?: string; flat?: string; pct?: string; kelly?: string; cap?: string; p?: string; o?: string };

const eur = (x: number) => x.toLocaleString("it-IT", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const eurSigned = (x: number) => `${x >= 0 ? "+" : "−"}${eur(Math.abs(x))}`;
const tone = (x: number | null) => (x == null ? "" : x >= 0 ? "pos" : "neg");

function num(x: string | undefined, lo: number, hi: number, dflt: number): number {
  const v = Number((x ?? "").replace(",", "."));
  return x && Number.isFinite(v) && v >= lo && v <= hi ? v : dflt;
}

export default async function Bankroll({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
  const plan: Plan = {
    ...DEFAULT_PLAN,
    start: num(sp.start, 10, 1e7, DEFAULT_PLAN.start),
    method: sp.m === "flat" || sp.m === "pct" || sp.m === "kelly" ? sp.m : DEFAULT_PLAN.method,
    flat: num(sp.flat, 1, 1e6, DEFAULT_PLAN.flat),
    pct: num(sp.pct, 0.1, 20, DEFAULT_PLAN.pct * 100) / 100,
    kelly: num(sp.kelly, 5, 100, DEFAULT_PLAN.kelly * 100) / 100,
    cap: num(sp.cap, 0.5, 20, DEFAULT_PLAN.cap * 100) / 100,
  };
  const all = await bankrollSlips();
  const profiles = [...new Set((all ?? []).map((s) => s.profile ?? ""))].filter(Boolean).sort();
  const profile = sp.p && profiles.includes(sp.p) ? sp.p : "";
  const inProfile = (all ?? []).filter((s) => !profile || s.profile === profile);
  // default: only the slips a bettor would hold together (no selection shared with a slip still open), see criterion.ts
  const overlap = sp.o === "tutte";
  const slips = overlap ? inProfile : independentSlips(inProfile);
  const kept = new Set(slips.map((s) => s.id));
  const r = simulate(slips, plan);
  r.skipped.push(...inProfile.filter((s) => !kept.has(s.id)).map((slip) => ({ slip, reason: "condivide un evento con una schedina ancora aperta" })));
  r.skippedWon = r.skipped.filter((x) => x.slip.result === "won").length;
  r.skippedLost = r.skipped.filter((x) => x.slip.result === "lost").length;
  const growth = r.equity / plan.start - 1;
  // every recorded slip, newest first: the staked ones and those this plan leaves out (with the reason)
  const rows = [
    ...r.bets.map((b) => ({ ...b, skip: null as string | null })),
    ...r.skipped.map((x) => ({ slip: x.slip, at: x.slip.created_at, stake: 0, ret: null, balanceAfter: null, skip: x.reason })),
  ].sort((a, b) => b.at.localeCompare(a.at) || b.slip.id - a.slip.id);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Bankroll</h1>
          <p className="reg-intro">
            Cosa avrebbero fatto al saldo le schedine del Registro con un piano di puntate: ognuna puntata quando è stata registrata, con la quota
            Sisal di quel momento, e pagata quando si è chiusa. Solo carta: nessuna scommessa reale finché il criterio di passaggio non è superato.
          </p>
        </div>
      </header>

      {/* the fields of the chosen method only: shown by CSS from the selected option (no client code), see .bank-form */}
      <form key={JSON.stringify(sp)} className="card filters bank-form" method="get" aria-label="Piano di puntate">
        <div className="field">
          <label htmlFor="m">Metodo</label>
          <div className="control">
            <Scale size={17} aria-hidden="true" />
            <select id="m" name="m" defaultValue={plan.method}>
              {(Object.keys(METHOD) as Method[]).map((k) => <option key={k} value={k}>{METHOD[k].label}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="start">Capitale iniziale (€)</label>
          <div className="control">
            <PiggyBank size={17} aria-hidden="true" />
            <input id="start" name="start" type="number" inputMode="decimal" min="10" step="10" defaultValue={sp.start ?? plan.start} />
          </div>
        </div>
        <div className="field f-kelly">
          <label htmlFor="kelly">Frazione di Kelly (%)</label>
          <div className="control">
            <Gauge size={17} aria-hidden="true" />
            <input id="kelly" name="kelly" type="number" inputMode="decimal" min="5" max="100" step="5" defaultValue={sp.kelly ?? plan.kelly * 100} />
          </div>
        </div>
        <div className="field f-pct">
          <label htmlFor="pct">Percentuale del saldo (%)</label>
          <div className="control">
            <Percent size={17} aria-hidden="true" />
            <input id="pct" name="pct" type="number" inputMode="decimal" min="0.1" max="20" step="0.1" defaultValue={sp.pct ?? plan.pct * 100} />
          </div>
        </div>
        <div className="field f-flat">
          <label htmlFor="flat">Puntata fissa (€)</label>
          <div className="control">
            <Coins size={17} aria-hidden="true" />
            <input id="flat" name="flat" type="number" inputMode="decimal" min="1" step="1" defaultValue={sp.flat ?? plan.flat} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="cap">Tetto per schedina (% del saldo)</label>
          <div className="control">
            <Wallet size={17} aria-hidden="true" />
            <input id="cap" name="cap" type="number" inputMode="decimal" min="0.5" max="20" step="0.5" defaultValue={sp.cap ?? plan.cap * 100} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="p">Schedine del profilo</label>
          <div className="control">
            <Layers size={17} aria-hidden="true" />
            <select id="p" name="p" defaultValue={profile}>
              <option value="">Tutti i profili</option>
              {profiles.map((p) => <option key={p} value={p}>{PROFILE_LABEL[p] ?? p}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="o">Schedine sovrapposte</label>
          <div className="control">
            <Layers size={17} aria-hidden="true" />
            <select id="o" name="o" defaultValue={overlap ? "tutte" : ""}>
              <option value="">Salta quelle con un evento già in gioco</option>
              <option value="tutte">Punta tutte</option>
            </select>
          </div>
        </div>
        <div className="field" style={{ alignSelf: "end" }}>
          <button type="submit" className="btn btn-primary">Simula</button>
        </div>
        <p className="note bank-hint">
          {(Object.keys(METHOD) as Method[]).map((k) => <span key={k} className={`h-${k}`}>{METHOD[k].label}: {METHOD[k].hint}. </span>)}
          Puntata minima Sisal 2 €: sotto, la schedina non si punta (resta in tabella
          con il motivo). Ogni analisi registra tre schedine, spesso sugli stessi eventi: di norma si salta quella che condivide un evento con una
          schedina ancora aperta, come farebbe chi scommette davvero. Il Registro conta 1 unità su ogni schedina; qui conta solo quello che il
          piano punta davvero.
        </p>
      </form>

      {!all ? (
        <div className="card"><Empty icon={Wallet} title="Registro vuoto">Le schedine arrivano con le prossime analisi pubblicate.</Empty></div>
      ) : (
        <>
          <div className="reg-kpis">
            <Kpi icon={Wallet} label="Saldo" value={eur(r.equity)} cls={tone(growth)}>
              <b className={tone(growth)}>{signed(growth)}</b> dal capitale iniziale{r.openStake > 0 && <> · <b>{eur(r.openStake)}</b> in gioco</>}
            </Kpi>
            <Kpi icon={Coins} label="Utile sulle chiuse" value={eurSigned(r.profit)} cls={tone(r.profit)}>
              rendimento <b className={tone(r.roi)}>{signed(r.roi)}</b> su {eur(r.staked - r.openStake)} puntati
            </Kpi>
            <Kpi icon={Layers} label="Schedine giocate" value={String(r.bets.length)}>
              <b>{r.won}</b> vinte, <b>{r.lost}</b> perse{r.refunded > 0 && <>, {r.refunded} rimborsate</>} · {r.open} in attesa
              {r.skipped.length > 0 && (
                <> · <b>{r.skipped.length}</b> non puntate ({r.skippedWon} vinte, {r.skippedLost} perse)</>
              )}
            </Kpi>
            <Kpi icon={TrendingDown} label="Calo massimo" value={r.maxDd > 0 ? `−${eur(r.maxDd)}` : "–"}>
              <b>{pct(r.maxDdPct, 1)}</b> dal punto più alto · peggior serie <b>{r.worstRun}</b> perse di fila
            </Kpi>
          </div>

          <section className="card" aria-labelledby="bank-curve">
            <div className="card-head">
              <h2 id="bank-curve">Andamento del saldo</h2>
              {r.avgStake != null && <span className="muted">puntata media <b className="num">{eur(r.avgStake)}</b></span>}
            </div>
            <div className="card-pad">
              {r.points.length > 1 ? (
                <OddsChart
                  title="Saldo dopo ogni schedina chiusa"
                  series={[{ key: "saldo", label: "Saldo (€)", short: "€", color: "var(--accent)", points: r.points }]}
                />
              ) : (
                <p className="note">Il grafico compare con la prima schedina chiusa.</p>
              )}
            </div>
          </section>

          <section className="card" aria-labelledby="bank-bets">
            <div className="card-head">
              <h2 id="bank-bets">Schedine del registro</h2>
              <span className="count">{rows.length}</span>
              {r.skipped.length > 0 && <span className="muted">comprese le {r.skipped.length} non puntate con questo piano</span>}
            </div>
            {rows.length ? (
              <div className="table-wrap">
                <table className="compact">
                  <thead>
                    <tr>
                      <th>Registrata</th><th>Schedina</th><th className="num">Quota</th><th className="num">Vince</th>
                      <th className="num">Puntata</th><th>Esito</th><th className="num">Saldo dopo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 60).map((b) => {
                      const legs = parseJSON<{ match: string; market: string }[]>(b.slip.legs, []);
                      return (
                        <tr key={b.slip.id} className={b.skip ? "muted" : undefined}>
                          <td className="num">{dayTime(b.at)}</td>
                          <td className="wrap">
                            {legs.length} eventi
                            <span className="sub">{legs.map((l) => `${l.match}: ${l.market}`).join(" · ")}</span>
                          </td>
                          <td className="num">{effectiveOdds(b.slip).toFixed(2)}</td>
                          <td className="num">{pct(b.slip.joint, 1)}</td>
                          <td className="num">{b.skip ? <span title={b.skip}>non puntata</span> : eur(b.stake)}</td>
                          <td>
                            {b.slip.result ? (
                              <span className={`status ${RESULT_CLASS[b.slip.result] ?? "status-WATCH"}`}>{RESULT_LABEL[b.slip.result] ?? b.slip.result}</span>
                            ) : (
                              <span className="muted">in attesa</span>
                            )}
                          </td>
                          <td className="num">{b.skip ? <span className="note">{b.skip}</span> : b.balanceAfter == null ? "–" : eur(b.balanceAfter)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty icon={Layers} title="Nessuna schedina puntata">
                Il registro non ha ancora schedine.
              </Empty>
            )}
          </section>

          <p className="note">
            Quota con il bonus multipla Sisal. La puntata si calcola sul saldo al momento della registrazione, soldi in gioco compresi; Kelly usa la
            probabilità stimata dal modello, che può sbagliare: per questo si usa una frazione e un tetto per schedina.
          </p>
        </>
      )}
    </>
  );
}

function Kpi({ icon: Icon, label, value, cls = "", children }: { icon: typeof Wallet; label: string; value: string; cls?: string; children: ReactNode }) {
  return (
    <div className="card reg-kpi">
      <div className="reg-kpi-top">
        <span className="kpi-icon"><Icon size={16} aria-hidden="true" /></span>
        <span>{label}</span>
      </div>
      <b className={`reg-kpi-value num ${cls}`}>{value}</b>
      <p className="reg-kpi-sub">{children}</p>
    </div>
  );
}
