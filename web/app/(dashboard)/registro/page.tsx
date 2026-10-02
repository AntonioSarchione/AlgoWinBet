import { BookOpenCheck, Scale, Target, TrendingDown, Wallet } from "lucide-react";
import { paperRegistry, parseJSON, type PaperLeg, type PaperSlip } from "@/lib/db";
import { dayTime, pct, signed } from "@/app/_components/format";
import { Empty } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Registro" };

const RESULT_LABEL: Record<string, string> = { won: "Vinta", lost: "Persa", void: "Rimborsata", "non valutabile": "Non valutabile" };
const RESULT_CLASS: Record<string, string> = { won: "status-STRONG", lost: "status-AVOID", void: "status-WATCH", "non valutabile": "status-WATCH" };
// pass criterion proposed for paper trading: to be confirmed together before anything is played for real
const CRITERION = { n: 300, ece: 0.03 };

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

// predicted probability against how often it happened, in 10-point bins (settled selections with a winner or a loser)
function calibration(legs: PaperLeg[]) {
  const bins = Array.from({ length: 10 }, () => ({ p: 0, y: 0, n: 0 }));
  for (const l of legs) {
    if (l.result !== "won" && l.result !== "lost") continue;
    const b = bins[Math.min(9, Math.floor(l.p * 10))];
    b.p += l.p;
    b.y += l.result === "won" ? 1 : 0;
    b.n += 1;
  }
  const rows = bins.map((b, i) => ({ lo: i / 10, n: b.n, p: b.n ? b.p / b.n : null, y: b.n ? b.y / b.n : null })).filter((r) => r.n > 0);
  const tot = rows.reduce((s, r) => s + r.n, 0);
  const ece = tot ? rows.reduce((s, r) => s + Math.abs((r.p ?? 0) - (r.y ?? 0)) * r.n, 0) / tot : null;
  return { rows, ece };
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
  const cal = calibration(reg.legs);
  const settledSlips = reg.slips.filter((s) => s.result && s.result !== "non valutabile");
  const slipPnl = settledSlips.reduce((a, s) => a + (s.payout ?? 0) - 1, 0);
  const checks = [
    { label: `Almeno ${CRITERION.n} giocate di valore chiuse`, ok: sv.n >= CRITERION.n, now: `${sv.n}/${CRITERION.n}` },
    { label: "CLV medio sulla chiusura Sisal positivo", ok: sv.clv != null && sv.clv > 0, now: signed(sv.clv) },
    { label: "EV medio alla chiusura Pinnacle positivo", ok: sv.evClose != null && sv.evClose > 0, now: signed(sv.evClose) },
    { label: `Scarto di calibrazione medio ≤ ${pct(CRITERION.ece)}`, ok: cal.ece != null && cal.ece <= CRITERION.ece, now: cal.ece == null ? "–" : pct(cal.ece, 1) },
  ];

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

      <div className="split">
        <section className="card">
          <div className="card-head"><h2>Criterio di passaggio</h2><span className="count">proposta, da confermare</span></div>
          <div className="table-wrap">
            <table className="compact">
              <tbody>
                {checks.map((c) => (
                  <tr key={c.label}>
                    <td>{c.label}</td>
                    <td className="num">{c.now}</td>
                    <td><span className={`status ${c.ok ? "status-STRONG" : "status-WATCH"}`}>{c.ok ? "Raggiunto" : "Non ancora"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">Finché i quattro punti non sono raggiunti insieme, il tool resta in prova.</p>
        </section>
        <section className="card">
          <div className="card-head"><h2>Calibrazione reale</h2><span className="count">probabilità prevista contro esito</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead><tr><th>Fascia</th><th className="num">Selezioni</th><th className="num">Prevista</th><th className="num">Accaduta</th></tr></thead>
              <tbody>
                {cal.rows.map((r) => (
                  <tr key={r.lo}>
                    <td>{pct(r.lo)}–{pct(r.lo + 0.1)}</td><td className="num">{r.n}</td><td className="num">{pct(r.p, 1)}</td><td className="num">{pct(r.y, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">Tutte le selezioni registrate (valore ed eque). Con poche selezioni per fascia le differenze sono soprattutto caso.</p>
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
