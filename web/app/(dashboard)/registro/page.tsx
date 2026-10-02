import { AlertTriangle, BookOpenCheck, CheckCircle2, Clock, Scale, Target, TrendingDown, Wallet, XCircle } from "lucide-react";
import { paperRegistry, parseJSON, type PaperLeg, type PaperSlip } from "@/lib/db";
import { byVersion, CRITERION, evaluate, type State } from "@/lib/criterion";
import { dayTime, pct, signed } from "@/app/_components/format";
import { Empty } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Registro" };

const RESULT_LABEL: Record<string, string> = { won: "Vinta", lost: "Persa", void: "Rimborsata", "non valutabile": "Non valutabile" };
const RESULT_CLASS: Record<string, string> = { won: "status-STRONG", lost: "status-AVOID", void: "status-WATCH", "non valutabile": "status-WATCH" };
const STATE: Record<State, { label: string; cls: string; icon: typeof CheckCircle2 }> = {
  pass: { label: "Superato", cls: "status-STRONG", icon: CheckCircle2 },
  fail: { label: "Fallito", cls: "status-AVOID", icon: XCircle },
  alarm: { label: "Allarme", cls: "status-AVOID", icon: AlertTriangle },
  open: { label: "In corso", cls: "status-WATCH", icon: Clock },
};
const VERDICT: Record<State, string> = {
  pass: "Criterio superato: il metodo ha mostrato un vantaggio misurabile sui prezzi di chiusura.",
  fail: "Criterio fallito: dopo 300 giocate il valore alla chiusura è negativo oltre il caso. Il vantaggio non c\u2019è: si cambia strategia e si riparte da zero.",
  alarm: "Allarme sul rendimento.",
  open: "In prova: nessuna giocata reale finché il criterio non è superato.",
};

type Stats = { n: number; open: number; hits: number; roi: number | null; clv: number | null; nClv: number; evClose: number | null; nClose: number; maxDd: number };

function stats(legs: PaperLeg[]): Stats {
  const settled = legs.filter((l) => l.result === "won" || l.result === "lost" || l.result === "void");
  let cum = 0, peak = 0, maxDd = 0;
  for (const l of settled) {
    cum += l.result === "won" ? l.odds - 1 : l.result === "lost" ? -1 : 0;
    peak = Math.max(peak, cum);
    maxDd = Math.max(maxDd, peak - cum);
  }
  const clv = settled.filter((l) => l.close_odds).map((l) => l.odds / (l.close_odds as number) - 1);
  const evc = settled.filter((l) => l.close_fair).map((l) => l.odds * (l.close_fair as number) - 1);
  const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  return {
    n: settled.length,
    open: legs.filter((l) => l.result == null).length,
    hits: settled.filter((l) => l.result === "won").length,
    roi: settled.length ? cum / settled.length : null,
    clv: mean(clv),
    nClv: clv.length,
    evClose: mean(evc),
    nClose: evc.length,
    maxDd,
  };
}

function StatRow({ label, s }: { label: string; s: Stats }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="num">{s.n}</td>
      <td className="num muted">{s.open}</td>
      <td className="num">{s.n ? pct(s.hits / s.n) : "–"}</td>
      <td className={`num ${s.roi != null && s.roi >= 0 ? "pos" : "neg"}`}>{signed(s.roi)}</td>
      <td className={`num ${s.clv != null && s.clv >= 0 ? "pos" : "neg"}`}>{signed(s.clv)}<span className="muted"> ({s.nClv})</span></td>
      <td className={`num ${s.evClose != null && s.evClose >= 0 ? "pos" : "neg"}`}>{signed(s.evClose)}<span className="muted"> ({s.nClose})</span></td>
      <td className="num">{s.maxDd.toFixed(1)} u</td>
    </tr>
  );
}

export default async function Registro() {
  const reg = await paperRegistry();
  if (!reg || (!reg.legs.length && !reg.slips.length)) {
    return (
      <>
        <header className="page-head"><div><h1>Registro</h1></div></header>
        <section className="card">
          <Empty icon={BookOpenCheck} title="Registro vuoto">La prima analisi pubblicata registra le sue proposte; si chiudono da sole dopo le partite.</Empty>
        </section>
      </>
    );
  }
  const value = reg.legs.filter((l) => l.status === "STRONG" || l.status === "CANDIDATE");
  const fair = reg.legs.filter((l) => l.status === "FAIR");
  const sv = stats(value);
  const crit = evaluate(reg.legs);
  const cal = crit.bands;
  const versions = byVersion(reg.legs);
  const V = STATE[crit.verdict];
  const settledSlips = reg.slips.filter((s) => s.result && s.result !== "non valutabile");
  const slipPnl = settledSlips.reduce((a, s) => a + (s.payout ?? 0) - 1, 0);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Registro</h1>
          <p>
            Ogni proposta è scritta la prima volta che un&apos;analisi la mostra, con la quota Sisal di quel momento, e non viene più modificata. Dopo la partita
            si chiude da sola con il risultato e le quote di chiusura. Una unità a giocata, solo carta: nessuna scommessa viene piazzata.
          </p>
        </div>
      </header>

      <div className="kpis">
        <div className="card kpi">
          <span className="kpi-icon"><BookOpenCheck size={18} aria-hidden="true" /></span>
          <span><small>Giocate di valore chiuse</small><b className="num">{sv.n}</b><span className="note">{sv.open} in attesa</span></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><Wallet size={18} aria-hidden="true" /></span>
          <span><small>Rendimento (1 u a giocata)</small><b className={`num ${sv.roi != null && sv.roi >= 0 ? "pos" : "neg"}`}>{signed(sv.roi)}</b></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><Target size={18} aria-hidden="true" /></span>
          <span><small>CLV sulla chiusura Sisal</small><b className={`num ${sv.clv != null && sv.clv >= 0 ? "pos" : "neg"}`}>{signed(sv.clv)}</b></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><Scale size={18} aria-hidden="true" /></span>
          <span><small>EV alla chiusura Pinnacle</small><b className={`num ${sv.evClose != null && sv.evClose >= 0 ? "pos" : "neg"}`}>{signed(sv.evClose)}</b></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><TrendingDown size={18} aria-hidden="true" /></span>
          <span><small>Calo massimo</small><b className="num">{sv.maxDd.toFixed(1)} u</b></span>
        </div>
      </div>

      <section className="card" aria-labelledby="crit-title">
        <div className="card-head">
          <h2 id="crit-title">Criterio di passaggio</h2>
          <span className={`status ${V.cls}`}><V.icon size={14} aria-hidden="true" /> {V.label}</span>
        </div>
        <p className="card-pad" style={{ paddingBottom: 0 }}>
          {VERDICT[crit.verdict]}
          {crit.missing > 0 && (
            <span className="muted">
              {" "}Mancano {crit.missing} giocate di valore chiuse
              {crit.weeksLeft != null ? `: al ritmo attuale (${crit.perWeek?.toFixed(1)} a settimana) circa ${crit.weeksLeft} settimane.` : "."}
            </span>
          )}
        </p>
        <ul className="crit-list">
          {crit.checks.map((c) => {
            const S = STATE[c.state];
            return (
              <li key={c.key}>
                <S.icon size={18} aria-hidden="true" className={`crit-${c.state}`} />
                <div>
                  <div className="crit-head"><b>{c.label}</b><span className="num">{c.now}</span></div>
                  <div className="note">{c.detail}</div>
                </div>
                <span className={`status ${S.cls}`}>{S.label}</span>
              </li>
            );
          })}
        </ul>
        <p className="note card-pad">
          Superato solo quando i primi tre punti sono superati insieme e il rendimento non è in allarme. Contano solo le proposte registrate alla prima
          comparsa: nessuna scelta a posteriori. Gli intervalli trattano le giocate come indipendenti; selezioni della stessa partita non lo sono, quindi
          sono un po&apos; ottimisti.
        </p>
      </section>

      <div className="split">
        <section className="card">
          <div className="card-head"><h2>Per versione del modello</h2><span className="count">giocate di valore</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead><tr><th>Versione</th><th className="num">Chiuse</th><th className="num">EV chiusura Pinnacle</th><th className="num">CLV Sisal</th><th className="num">Rendimento</th></tr></thead>
              <tbody>
                {versions.map((r) => (
                  <tr key={r.version}>
                    <td>{r.version}</td>
                    <td className="num">{r.n}</td>
                    <td className="num">{signed(r.ev.mean)}{r.ev.lo != null && <span className="muted"> ({signed(r.ev.lo)} / {signed(r.ev.hi)})</span>}</td>
                    <td className="num">{signed(r.clv.mean)}</td>
                    <td className="num">{signed(r.roi.mean)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">Tra parentesi l&apos;intervallo al 95%. Ogni miglioramento del modello cambia la versione: i risultati restano separati.</p>
        </section>
        <section className="card">
          <div className="card-head"><h2>Calibrazione reale</h2><span className="count">probabilità prevista contro esito</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead><tr><th>Fascia</th><th className="num">Selezioni</th><th className="num">Prevista</th><th className="num">Accaduta</th><th className="num">Errori standard</th></tr></thead>
              <tbody>
                {cal.rows.map((r) => (
                  <tr key={r.lo}>
                    <td>{pct(r.lo)}–{pct(r.lo + 0.1)}</td><td className="num">{r.n}</td><td className="num">{pct(r.p, 1)}</td><td className="num">{pct(r.y, 1)}</td>
                    <td className={`num ${r.n < CRITERION.minBand ? "muted" : Math.abs(r.z) > cal.zMax ? "neg" : ""}`}>
                      {r.n < CRITERION.minBand ? "poche" : `${r.z >= 0 ? "+" : "−"}${Math.abs(r.z).toFixed(1)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">
            Tutte le selezioni registrate (valore ed eque). Una fascia è fuori quando dista dalla previsione più di {cal.zMax.toFixed(1)} errori standard:
            con {cal.tested} fasce controllate, un modello ben calibrato resta dentro 95 volte su 100. Fasce con meno di {CRITERION.minBand} selezioni non contano.
          </p>
        </section>
      </div>

      <section className="card">
        <div className="card-head"><h2>Per tipo di selezione</h2><span className="count">1 unità a giocata</span></div>
        <div className="table-wrap">
          <table className="compact">
            <thead>
              <tr>
                <th>Tipo</th><th className="num">Chiuse</th><th className="num">In attesa</th><th className="num">Vinte</th><th className="num">Rendimento</th>
                <th className="num">CLV Sisal (n)</th><th className="num">EV chiusura Pinnacle (n)</th><th className="num">Calo massimo</th>
              </tr>
            </thead>
            <tbody>
              <StatRow label="Valore (Alta + Media)" s={sv} />
              <StatRow label="Alta" s={stats(reg.legs.filter((l) => l.status === "STRONG"))} />
              <StatRow label="Media" s={stats(reg.legs.filter((l) => l.status === "CANDIDATE"))} />
              <StatRow label="Equa" s={stats(fair)} />
            </tbody>
          </table>
        </div>
        <p className="note card-pad">
          CLV Sisal: quota presa contro quota Sisal di chiusura. EV alla chiusura Pinnacle: quota presa per la probabilità equa di Pinnacle in chiusura, la misura più
          vicina al valore vero. Le selezioni eque servono nelle schedine: il loro rendimento atteso è circa zero.
        </p>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Schedine registrate</h2>
          <span className="count">{settledSlips.length} chiuse · saldo {signed(settledSlips.length ? slipPnl / settledSlips.length : null)} a schedina</span>
        </div>
        <div className="table-wrap">
          <table className="compact">
            <thead>
              <tr>
                <th>Registrata</th><th>Eventi</th><th className="num">Quota</th><th className="num">Bonus</th><th className="num">Probabilità</th>
                <th className="num">EV</th><th>Esito</th><th className="num">Incasso</th><th className="num">CLV</th>
              </tr>
            </thead>
            <tbody>
              {reg.slips.slice(0, 50).map((s: PaperSlip) => {
                const legs = parseJSON<{ match: string; market: string; odds: number }[]>(s.legs, []);
                return (
                  <tr key={s.id}>
                    <td className="muted">{dayTime(s.created_at)}</td>
                    <td title={legs.map((l) => `${l.match} · ${l.market} @${l.odds.toFixed(2)}`).join("\n")}>
                      {legs.length} · {legs.slice(0, 2).map((l) => l.match).join(", ")}{legs.length > 2 ? "…" : ""}
                    </td>
                    <td className="num">{s.total_odds.toFixed(2)}</td>
                    <td className="num">{s.bonus ? `+${pct(s.bonus)}` : "–"}</td>
                    <td className="num">{pct(s.joint, 1)}</td>
                    <td className={`num ${s.ev >= 0 ? "pos" : "neg"}`}>{signed(s.ev)}</td>
                    <td>{s.result ? <span className={`status ${RESULT_CLASS[s.result] ?? ""}`}>{RESULT_LABEL[s.result] ?? s.result}</span> : <span className="muted">in attesa</span>}</td>
                    <td className="num">{s.payout == null ? "–" : s.payout.toFixed(2)}</td>
                    <td className="num">{signed(s.clv)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
