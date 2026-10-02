import Link from "next/link";
import { Activity, Gauge, LineChart, Scale, Target } from "lucide-react";
import { latestQuality, type QualityFamily } from "@/lib/db";
import { ago, pct, shortDate, signed } from "@/app/_components/format";
import { Empty } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Qualità del modello" };

const GROUPS: Record<string, string> = { campionati: "Campionati", coppe: "Coppe europee", nazionali: "Nazionali", tutte: "Tutte" };
const FAMILIES = ["1X2", "U/O 2.5", "Gol/NoGol"];
// one colour and one marker shape per series, so identity never relies on colour alone
const SERIES = [
  { key: "model", label: "Modello", color: "var(--s1)", shape: "circle" },
  { key: "ens", label: "Modello + mercato (peso fisso)", color: "var(--s3)", shape: "square" },
  { key: "meta", label: "Meta-modello", color: "var(--s4)", shape: "diamond" },
  { key: "close", label: "Quota di chiusura", color: "var(--s2)", shape: "triangle" },
] as const;

const CALIB_LABEL = { platt: "Platt", beta: "Beta", isotonic: "Isotonica" } as const;
const f4 = (x: number | undefined | null) => (x == null ? "–" : x.toFixed(4));
const gap = (a?: number, b?: number) => (a == null || b == null ? null : a - b);

function Marker({ shape, x, y, color }: { shape: string; x: number; y: number; color: string }) {
  if (shape === "square") return <rect x={x - 4} y={y - 4} width={8} height={8} fill={color} stroke="var(--card)" strokeWidth={2} />;
  if (shape === "diamond") return <path d={`M${x},${y - 5.5} L${x + 5.5},${y} L${x},${y + 5.5} L${x - 5.5},${y} Z`} fill={color} stroke="var(--card)" strokeWidth={2} />;
  if (shape === "triangle") return <path d={`M${x},${y - 5} L${x + 5},${y + 4} L${x - 5},${y + 4} Z`} fill={color} stroke="var(--card)" strokeWidth={2} />;
  return <circle cx={x} cy={y} r={4.5} fill={color} stroke="var(--card)" strokeWidth={2} />;
}

// Reliability diagram: predicted probability (x) against how often it happened (y). On the diagonal = calibrated.
function Calibration({ data, title }: { data: Record<string, [number, number, number][]>; title: string }) {
  const W = 420, H = 300, P = { l: 44, r: 12, t: 12, b: 36 };
  const x = (p: number) => P.l + p * (W - P.l - P.r);
  const y = (p: number) => H - P.b - p * (H - P.t - P.b);
  const ticks = [0, 0.2, 0.4, 0.6, 0.8, 1];
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)} stroke="var(--line)" />
            <text x={P.l - 8} y={y(t) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{pct(t)}</text>
            <text x={x(t)} y={H - P.b + 16} textAnchor="middle" fontSize="11" fill="var(--muted)">{pct(t)}</text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="var(--line-strong)" strokeDasharray="4 4" />
        <text x={W - P.r} y={H - 4} textAnchor="end" fontSize="11" fill="var(--muted)">probabilità prevista</text>
        {SERIES.map((s) => {
          const pts = (data[s.key] ?? []).filter((b) => b[2] >= 15); // bins with too few events are noise
          return (
            <g key={s.key}>
              <path d={pts.map((b, i) => `${i ? "L" : "M"}${x(b[0]).toFixed(1)},${y(b[1]).toFixed(1)}`).join(" ")} fill="none" stroke={s.color} strokeWidth={2} opacity={0.85} />
              {pts.map((b, i) => <Marker key={i} shape={s.shape} x={x(b[0])} y={y(b[1])} color={s.color} />)}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// Monthly 1X2 log loss of the model and of the closing price (leagues): the gap is what the model still misses.
function Monthly({ rows }: { rows: { month: string; n: number; ll_model: number; ll_close: number }[] }) {
  const W = 640, H = 220, P = { l: 48, r: 16, t: 14, b: 28 };
  const vs = rows.flatMap((r) => [r.ll_model, r.ll_close]);
  const lo = Math.floor(Math.min(...vs) * 20) / 20, hi = Math.ceil(Math.max(...vs) * 20) / 20 || lo + 0.05;
  const x = (i: number) => P.l + (rows.length < 2 ? 0.5 : i / (rows.length - 1)) * (W - P.l - P.r);
  const y = (v: number) => P.t + (1 - (v - lo) / (hi - lo || 1)) * (H - P.t - P.b);
  const ticks = [0, 1, 2, 3, 4].map((k) => lo + ((hi - lo) * k) / 4);
  const line = (k: "ll_model" | "ll_close") => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[k]).toFixed(1)}`).join(" ");
  const month = (m: string) => new Date(`${m}-01T12:00:00Z`).toLocaleDateString("it-IT", { month: "short", year: "2-digit" });
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Log loss 1X2 mese per mese: modello e quota di chiusura">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)} stroke="var(--line)" />
            <text x={P.l - 8} y={y(t) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{t.toFixed(2)}</text>
          </g>
        ))}
        {rows.map((r, i) => (i % Math.max(1, Math.ceil(rows.length / 8)) === 0 || i === rows.length - 1) && (
          <text key={r.month} x={x(i)} y={H - 8} textAnchor="middle" fontSize="11" fill="var(--muted)">{month(r.month)}</text>
        ))}
        <path d={line("ll_model")} fill="none" stroke="var(--s1)" strokeWidth={2} />
        <path d={line("ll_close")} fill="none" stroke="var(--s2)" strokeWidth={2} strokeDasharray="5 4" />
        {rows.map((r, i) => (
          <g key={r.month}>
            <Marker shape="circle" x={x(i)} y={y(r.ll_model)} color="var(--s1)" />
            <Marker shape="triangle" x={x(i)} y={y(r.ll_close)} color="var(--s2)" />
            <title>{`${month(r.month)}: modello ${r.ll_model.toFixed(3)}, chiusura ${r.ll_close.toFixed(3)} (${r.n} partite)`}</title>
          </g>
        ))}
      </svg>
    </div>
  );
}

export default async function Qualita({ searchParams }: { searchParams: Promise<{ f?: string }> }) {
  const { f = "1X2" } = await searchParams;
  const fam = FAMILIES.includes(f) ? f : "1X2";
  const run = await latestQuality();
  if (!run) {
    return (
      <>
        <header className="page-head"><div><h1>Qualità del modello</h1></div></header>
        <section className="card">
          <Empty icon={Gauge} title="Nessun report ancora">Il primo report arriva con il giro settimanale del lunedì (workflow quality).</Empty>
        </section>
      </>
    );
  }
  const rep = run.report;
  const league = rep.groups.campionati?.["1X2"];
  const v1x2 = rep.value["1X2"];
  const dModel = gap(league?.ll_model_same, league?.ll_close_same);
  const dEns = gap(league?.ll_ens_same, league?.ll_close_same);
  const dMeta = gap(league?.ll_meta_same, league?.ll_close_same);
  const metas = Object.entries(rep.meta ?? {});
  const GROUP_OF_KEY = (k: string) => GROUPS[k.split("|")[0]] ?? k.split("|")[0];
  const rows: { g: string; fam: string; m: QualityFamily }[] = [];
  for (const g of Object.keys(GROUPS)) for (const fm of FAMILIES) if (rep.groups[g]?.[fm]) rows.push({ g, fam: fm, m: rep.groups[g][fm] });

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Qualità del modello</h1>
          <p>
            Ogni partita finita tra il {shortDate(run.window_start)} e il {shortDate(run.window_end)} rigiocata come se fosse futura: il modello vede solo i
            risultati noti a mezzanotte del giorno della partita, il mercato solo i prezzi di 2 ore prima. Aggiornato {ago(run.created_at)} · modello {run.model_version}.
          </p>
        </div>
      </header>

      <div className="kpis">
        <div className="card kpi">
          <span className="kpi-icon"><Activity size={18} aria-hidden="true" /></span>
          <span><small>Partite rigiocate</small><b className="num">{rep.n_matches.toLocaleString("it-IT")}</b></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><Scale size={18} aria-hidden="true" /></span>
          <span><small>Modello vs chiusura · campionati 1X2</small><b className="num">{dModel == null ? "–" : `${dModel >= 0 ? "+" : ""}${dModel.toFixed(3)}`}</b></span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><LineChart size={18} aria-hidden="true" /></span>
          <span>
            <small>{dMeta == null ? "Modello + mercato vs chiusura" : "Meta-modello vs chiusura"}</small>
            <b className="num">{(dMeta ?? dEns) == null ? "–" : `${(dMeta ?? dEns)! >= 0 ? "+" : ""}${(dMeta ?? dEns)!.toFixed(3)}`}</b>
            {dMeta != null && dEns != null && <span className="note">peso fisso {dEns >= 0 ? "+" : ""}{dEns.toFixed(3)}</span>}
          </span>
        </div>
        <div className="card kpi">
          <span className="kpi-icon"><Target size={18} aria-hidden="true" /></span>
          <span><small>Test del valore 1X2 · CLV</small><b className={`num ${v1x2?.mean_clv != null && v1x2.mean_clv >= 0 ? "pos" : "neg"}`}>{signed(v1x2?.mean_clv)}</b></span>
        </div>
      </div>
      <p className="note" style={{ margin: "-6px 2px 0" }}>
        Differenze di log loss sulle stesse partite: 0 = bravo quanto la quota di chiusura, valori positivi = ancora meno preciso del mercato.
      </p>

      <section className="card">
        <div className="card-head"><h2>Qualità delle probabilità</h2><span className="count">più basso = meglio</span></div>
        <div className="table-wrap">
          <table className="compact">
            <thead>
              <tr>
                <th>Gruppo</th><th>Mercato</th><th className="num">Partite</th><th className="num">LL modello</th><th className="num">LL prima della Fase 2</th>
                <th className="num">LL modello*</th><th className="num">LL peso fisso*</th><th className="num">LL meta-modello*</th><th className="num">LL chiusura*</th>
                <th className="num">Senza quote: modello → calibrato</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ g, fam: fm, m }) => (
                <tr key={`${g}-${fm}`}>
                  <td>{GROUPS[g]}</td><td>{fm}</td><td className="num">{m.n}</td>
                  <td className="num">{f4(m.ll_model)}</td><td className="num muted">{f4(m.ll_v1)}</td>
                  <td className="num">{f4(m.ll_model_same)}</td><td className="num">{f4(m.ll_ens_same)}</td><td className="num">{f4(m.ll_meta_same)}</td>
                  <td className="num">{f4(m.ll_close_same)}</td>
                  <td className="num">{m.n_alone ? `${f4(m.ll_model_alone)} → ${f4(m.ll_meta_alone)}` : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="note card-pad">
          * stesse partite per le quattro colonne: quelle con il prezzo di 2 ore prima, la quota di chiusura e un meta-modello già adattato. Il meta-modello di ogni
          settimana è adattato solo sulle settimane precedenti, quindi il confronto è onesto. Ultima colonna: partite senza quote di riferimento, dove la
          calibrazione corregge il modello da solo.
        </p>
      </section>

      {rep.calib_methods && Object.keys(rep.calib_methods).length > 0 && (
        <section className="card">
          <div className="card-head"><h2>Calibrazione senza quote</h2><span className="count">log loss fuori campione · più basso = meglio</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead>
                <tr><th>Mercato</th><th className="num">Partite</th><th className="num">Platt</th><th className="num">Beta</th><th className="num">Isotonica</th><th>In uso</th></tr>
              </thead>
              <tbody>
                {Object.entries(rep.calib_methods).map(([fm, c]) => (
                  <tr key={fm}>
                    <td>{fm}</td><td className="num">{c.n}</td>
                    {(["platt", "beta", "isotonic"] as const).map((m) => (
                      <td key={m} className={`num ${c.chosen === m ? "pos" : ""}`}>{f4(c[m])}</td>
                    ))}
                    <td>{CALIB_LABEL[c.chosen]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">
            Partite senza quota Pinnacle (coppe, Gol/NoGol, nazionali): il modello va corretto da solo. Ogni settimana i tre metodi sono adattati solo sulle
            settimane precedenti; l&apos;analisi live usa quello che sbaglia meno.
          </p>
        </section>
      )}

      {metas.length > 0 && (
        <section className="card">
          <div className="card-head"><h2>Meta-modello in uso</h2><span className="count">adattato su tutta la finestra · usato dall&apos;analisi live</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead>
                <tr><th>Gruppo</th><th>Mercato</th><th>Con</th><th className="num">Peso modello</th><th className="num">Peso mercato</th><th className="num">Partite</th><th>Dettaglio</th></tr>
              </thead>
              <tbody>
                {metas.map(([k, m]) => (
                  <tr key={k}>
                    <td>{GROUP_OF_KEY(k)}</td><td>{k.split("|")[1]}</td><td>{m.kind === "pool" ? "quote di riferimento" : "solo modello"}</td>
                    <td className="num">{m.a.toFixed(2)}</td><td className="num">{m.kind === "pool" ? m.b.toFixed(2) : "–"}</td><td className="num">{m.n}</td>
                    <td className="muted">{m.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">
            Le probabilità finali sono una media pesata (in scala logaritmica) di modello e prezzo Pinnacle senza margine, più una piccola correzione per esito.
            Pesi che sommano più di 1 rendono le previsioni più decise, meno di 1 più prudenti: è anche la calibrazione. Se un gruppo ha meno di 300 partite usa i pesi di tutti i gruppi.
          </p>
        </section>
      )}

      <div className="split">
        <section className="card">
          <div className="card-head">
            <h2>Calibrazione · {fam}</h2>
            <nav className="chips" aria-label="Mercato">
              {FAMILIES.map((x) => (
                <Link key={x} href={x === "1X2" ? "/qualita" : `/qualita?f=${encodeURIComponent(x)}`} aria-current={x === fam ? "true" : undefined}>{x}</Link>
              ))}
            </nav>
          </div>
          <div className="card-pad">
            {rep.calibration[fam] ? <Calibration data={rep.calibration[fam]} title={`Calibrazione ${fam}: probabilità prevista contro frequenza osservata`} /> : <p className="note">Nessun dato.</p>}
            <div className="legend" style={{ marginTop: 8 }}>
              {SERIES.map((s) => (
                <span key={s.key}>
                  <svg width="12" height="12" aria-hidden="true"><Marker shape={s.shape} x={6} y={6} color={s.color} /></svg>
                  {s.label}
                </span>
              ))}
              <span>– – diagonale = calibrato</span>
            </div>
            <p className="note">Punti sopra la diagonale: l&apos;esito accade più spesso di quanto previsto (probabilità sottostimata); sotto: sovrastimata.</p>
          </div>
        </section>
        <section className="card">
          <div className="card-head"><h2>Test del valore</h2><span className="count">1 unità a giocata</span></div>
          <div className="table-wrap">
            <table className="compact">
              <thead>
                <tr><th>Mercato</th><th className="num">Giocate</th><th className="num">Vinte</th><th className="num">ROI</th><th className="num">CLV</th></tr>
              </thead>
              <tbody>
                {FAMILIES.filter((x) => rep.value[x]).map((x) => {
                  const v = rep.value[x];
                  return (
                    <tr key={x}>
                      <td>{x}</td><td className="num">{v.n}</td><td className="num">{v.n ? pct(v.hits / v.n) : "–"}</td>
                      <td className={`num ${v.roi >= 0 ? "pos" : "neg"}`}>{signed(v.roi)}</td>
                      <td className={`num ${v.mean_clv != null && v.mean_clv >= 0 ? "pos" : "neg"}`}>{signed(v.mean_clv)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="note card-pad">
            Selezioni che avrebbero superato le soglie live (EV ≥ {pct(rep.thresholds.min_ev)}, probabilità ≥ {pct(rep.thresholds.min_probability)}) alla quota di quel momento:
            Sisal quando l&apos;abbiamo, altrimenti la media del mercato. Il CLV (quota presa contro chiusura) misura il valore vero; il ROI su pochi casi è soprattutto fortuna.
          </p>
        </section>
      </div>

      {rep.monthly.length > 1 && (
        <section className="card">
          <div className="card-head"><h2>Mese per mese · campionati 1X2</h2></div>
          <div className="card-pad">
            <Monthly rows={rep.monthly} />
            <div className="legend" style={{ marginTop: 8 }}>
              <span><svg width="12" height="12" aria-hidden="true"><Marker shape="circle" x={6} y={6} color="var(--s1)" /></svg>Modello</span>
              <span><svg width="12" height="12" aria-hidden="true"><Marker shape="triangle" x={6} y={6} color="var(--s2)" /></svg>Quota di chiusura (tratteggio)</span>
            </div>
          </div>
        </section>
      )}
    </>
  );
}
