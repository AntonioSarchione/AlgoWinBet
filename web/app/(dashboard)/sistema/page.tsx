import { Activity, Database } from "lucide-react";
import { lastTick, parseJSON, systemStatus, usage } from "@/lib/db";
import { ago, dayTime, shortDate } from "@/app/_components/format";
import { Empty, Meter } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Stato del sistema" };

const LABELS: Record<string, string> = {
  raw_requests: "Risposte API salvate",
  fixtures: "Rilevazioni calendario",
  results: "Risultati storici",
  quotes: "Quote rilevate",
  lineups: "Formazioni",
  match_stats: "Statistiche partita",
};

export default async function Sistema() {
  const [st, use, tick] = await Promise.all([systemStatus(), usage(), lastTick()]);
  const backfill = st.jobs.filter((j) => j.name.startsWith("backfill:"));
  type Report = { season: string; competition: string; rows: number; linked: number; unmatched: string[] };
  const datasets = st.jobs
    .filter((j) => j.name.startsWith("dataset-report:"))
    .map((j) => ({ at: j.done_at, ...parseJSON<Report>(j.detail, { season: "", competition: "", rows: 0, linked: 0, unmatched: [] }) }))
    .sort((a, b) => a.competition.localeCompare(b.competition) || b.season.localeCompare(a.season));
  const rows = datasets.reduce((n, d) => n + d.rows, 0);
  const linked = datasets.reduce((n, d) => n + d.linked, 0);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Stato del sistema</h1>
          <p>Raccolta automatica su GitHub Actions, database Turso, analisi pubblicate. Ultima raccolta {ago(tick)}.</p>
        </div>
      </header>

      <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
        {Object.entries(st.counts).map(([k, v]) => (
          <div key={k} className="card kpi">
            <span className="kpi-icon"><Database size={18} aria-hidden="true" /></span>
            <span>
              <small>{LABELS[k] ?? k}</small>
              <b className="num">{v.toLocaleString("it-IT")}</b>
            </span>
          </div>
        ))}
      </div>

      <div className="split">
        <section className="card">
          <div className="card-head"><h2>Storico partite per competizione</h2><span className="count">{backfill.length} storici completati</span></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Competizione</th><th className="num">Partite</th><th>Dal</th><th>Al</th></tr></thead>
              <tbody>
                {st.byComp.map((c) => (
                  <tr key={c.competition}>
                    <td>{c.competition}</td>
                    <td className="num">{c.n.toLocaleString("it-IT")}</td>
                    <td className="muted">{shortDate(c.first)}</td>
                    <td className="muted">{shortDate(c.last)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="card">
          <div className="card-head"><h2>Budget richieste API</h2></div>
          <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Meter label="GOAL API · oggi" used={use.goalDay} limit={1000} hint="Riserva di 50 richieste mai usata dai giri automatici" />
            <Meter label="OddsPapi · mese" used={use.oddsMonth} limit={250} hint={`Piano automatico 200 + aggiornamenti manuali (${use.manualMonth}/5 usati); riserva di 20 mai toccata`} />
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-head">
          <h2>Dati stagionali (football-data.co.uk)</h2>
          <span className="count">{rows ? `${((100 * linked) / rows).toFixed(1)}% abbinate` : "in attesa del primo download"}</span>
        </div>
        {datasets.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Competizione</th><th>Stagione</th><th className="num">Partite</th><th className="num">Abbinate</th><th>Aggiornato</th><th>Non abbinate</th></tr>
              </thead>
              <tbody>
                {datasets.map((d) => (
                  <tr key={`${d.competition}-${d.season}`}>
                    <td>{d.competition}</td>
                    <td className="muted">20{d.season.slice(0, 2)}/{d.season.slice(2)}</td>
                    <td className="num">{d.rows}</td>
                    <td className="num">{d.rows ? `${((100 * d.linked) / d.rows).toFixed(0)}%` : "—"}</td>
                    <td className="muted">{ago(d.at)}</td>
                    <td className="muted" style={{ whiteSpace: "normal", fontSize: 12, minWidth: 200 }}>{d.unmatched.slice(0, 3).join(" · ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty icon={Database} title="Nessun file stagionale ancora">Statistiche, xG e quote di apertura/chiusura arrivano dopo lo storico GOAL.</Empty>
        )}
      </section>

      <section className="card">
        <div className="card-head"><h2><Activity size={17} color="var(--accent)" aria-hidden="true" /> Analisi pubblicate</h2></div>
        {st.runs.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>#</th><th>Quando</th><th className="num">Partite</th><th className="num">Con quote</th><th>Esito</th><th>Note</th></tr>
              </thead>
              <tbody>
                {st.runs.map((r) => {
                  const notes = parseJSON<string[]>(r.notes, []);
                  return (
                    <tr key={r.id}>
                      <td className="num muted">{r.id}</td>
                      <td>{dayTime(r.created_at)}</td>
                      <td className="num">{r.n_fixtures}</td>
                      <td className="num">{r.n_with_quotes}</td>
                      <td>{r.no_bet ? <span className="status status-WATCH">No bet</span> : <span className="status status-STRONG">Schedine</span>}</td>
                      <td className="muted" style={{ whiteSpace: "normal", fontSize: 12, minWidth: 240 }}>{notes.slice(0, 2).join(" · ")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty icon={Activity} title="Nessuna analisi pubblicata" />
        )}
      </section>
    </>
  );
}
