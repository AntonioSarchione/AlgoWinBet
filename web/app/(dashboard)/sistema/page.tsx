import { Activity, AlertTriangle, CheckCircle2, Database, HeartPulse, XCircle } from "lucide-react";
import { lastTick, latestHealth, parseJSON, systemStatus, usage, type HealthCheck } from "@/lib/db";
import { ago, dayTime, shortDate } from "@/app/_components/format";
import { Empty, Meter } from "@/app/_components/ui";
import { tursoUsage } from "@/lib/tursoUsage";

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

const LEVEL = {
  ok: { label: "Ok", cls: "status-STRONG", icon: CheckCircle2, state: "pass" },
  warn: { label: "Attenzione", cls: "status-WATCH", icon: AlertTriangle, state: "warn" },
  error: { label: "Errore", cls: "status-AVOID", icon: XCircle, state: "fail" },
} as const;
const TURSO_FREE_MB = 5000;
// Turso free plan, per billing cycle (the 1st of the month): synced to the embedded replicas, rows read and written
const TURSO_SYNC_MB = 3000;
const TURSO_READS_M = 500;
const TURSO_WRITES_M = 10;

export default async function Sistema() {
  const [st, use, tick, health, turso] = await Promise.all([systemStatus(), usage(), lastTick(), latestHealth(), tursoUsage()]);
  // live check of the Turso quotas (Platform API), first in the list: the morning's checks come from the collector
  const fmt = (n: number) => n.toLocaleString("it-IT", { maximumFractionDigits: 1 });
  const quotas = turso.ok
    ? [
        { l: "sincronizzati", used: turso.usage.bytesSynced / 1e6, limit: TURSO_SYNC_MB, u: "MB" },
        { l: "righe lette", used: turso.usage.rowsRead / 1e6, limit: TURSO_READS_M, u: "milioni" },
        { l: "righe scritte", used: turso.usage.rowsWritten / 1e6, limit: TURSO_WRITES_M, u: "milioni" },
      ]
    : [];
  const over = quotas.filter((q) => q.used >= q.limit);
  const reset = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth() + 1, 1)).toLocaleDateString("it-IT", { day: "numeric", month: "long", timeZone: "UTC" });
  const share = (q: (typeof quotas)[number]) => `${q.l} ${fmt(q.used)} su ${fmt(q.limit)} ${q.u} (${Math.round((100 * q.used) / q.limit)}%)`;
  const dbCheck: HealthCheck = {
    key: "turso-account",
    label: "Account database Turso",
    level: !turso.ok || quotas.some((q) => q.used >= 0.7 * q.limit) ? "warn" : "ok",
    detail: !turso.ok
      ? `consumi non disponibili: ${turso.reason}`
      : over.length
        ? `quota mensile del piano gratuito superata (${over.map(share).join(", ")}): l'account può essere bloccato in qualsiasi momento. `
          + `Raccolta e workflow sospesi fino al ${reset}, quando le quote si azzerano. Altre quote: ${quotas.filter((q) => !over.includes(q)).map(share).join(" · ")}`
        : quotas.map(share).join(" · "),
  };
  // the morning checks saved before the rename keep the old label
  const RENAMED: Record<string, string> = { "Spazio del database": "Spazio database Turso" };
  const checks: HealthCheck[] = [dbCheck, ...(health?.checks ?? []).map((c) => ({ ...c, label: RENAMED[c.label] ?? c.label }))];
  const worst = checks.some((c) => c.level === "error") ? "error" : checks.some((c) => c.level === "warn") ? "warn" : "ok";
  const W = LEVEL[worst];
  const backfill = st.jobs.filter((j) => j.name.startsWith("backfill:"));
  type Report = { season: string; competition: string; rows: number; linked: number; unmatched: string[] };
  const datasets = st.jobs
    .filter((j) => j.name.startsWith("dataset-report:"))
    .map((j) => ({ at: j.done_at, ...parseJSON<Report>(j.detail, { season: "", competition: "", rows: 0, linked: 0, unmatched: [] }) }))
    .sort((a, b) => a.competition.localeCompare(b.competition) || b.season.localeCompare(a.season));
  const rows = datasets.reduce((n, d) => n + d.rows, 0);
  const linked = datasets.reduce((n, d) => n + d.linked, 0);

  // Turso's count (Platform API): shown with or without the morning's size measure
  // Turso's own counts (Platform API), with or without the morning's size measure: hitting any limit blocks the account
  const synced = turso.ok ? (
    <>
      <Meter label="Turso · byte sincronizzati nel mese (MB)" used={Math.round(turso.usage.bytesSynced / 1e6)} limit={TURSO_SYNC_MB}
        hint="Copie locali del database nelle run di GitHub Actions" />
      <Meter label="Turso · righe lette nel mese (milioni)" used={Math.round(turso.usage.rowsRead / 1e6)} limit={TURSO_READS_M}
        hint="Sito, avvio delle raccolte e run senza copia locale" />
      <Meter label="Turso · righe scritte nel mese (milioni)" used={Math.round(turso.usage.rowsWritten / 1e5) / 10} limit={TURSO_WRITES_M}
        hint="Raccolta, pubblicazione delle analisi, registro" />
      <p className="note">Piano gratuito Turso: superare uno qualsiasi dei limiti blocca tutto l&apos;account fino all&apos;inizio del mese seguente.</p>
    </>
  ) : (
    <p className="note">Consumi Turso non disponibili: {turso.reason}.</p>
  );
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Stato del sistema</h1>
          <p>Raccolta automatica su GitHub Actions, database Turso, analisi pubblicate. Ultima raccolta {ago(tick)}.</p>
        </div>
      </header>

      <div className="split">
        <section className="card" aria-labelledby="health-title">
          <div className="card-head">
            <h2 id="health-title">Controllo di salute</h2>
            {health && <span className="count">{dayTime(health.at)}</span>}
            <span className={`status ${W.cls}`}><W.icon size={14} aria-hidden="true" /> {W.label}</span>
          </div>
          <ul className="crit-list">
            {checks.map((c) => {
              const L = LEVEL[c.level];
              return (
                <li key={c.key}>
                  <L.icon size={18} aria-hidden="true" className={`crit-${L.state}`} />
                  <div>
                    <div className="crit-head"><b>{c.label}</b></div>
                    <div className="note">{c.detail}</div>
                  </div>
                  <span className={`status ${L.cls}`}>{L.label}</span>
                </li>
              );
            })}
          </ul>
          {health ? (
            <>
              <p className="note card-pad">
                Ogni mattina il giro delle 06:05 controlla tutta la catena. Un errore fa fallire quel giro dopo aver finito il lavoro: arriva una sola
                email da GitHub. Le attenzioni restano solo qui. Le quote Turso sono lette dal vivo (al massimo ogni 30 minuti).
              </p>
            </>
          ) : (
            <Empty icon={HeartPulse} title="Nessun controllo del mattino ancora">Il primo arriva con il giro del mattino.</Empty>
          )}
        </section>
        <section className="card" aria-labelledby="space-title">
          <div className="card-head"><h2 id="space-title">Database</h2></div>
          {health?.dbBytes != null ? (
            <div className="card-pad" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <Meter
                label="Turso · totale (MB)"
                used={Math.round(health.dbBytes / 1e6)}
                limit={TURSO_FREE_MB}
                hint={health.weekGrowth ? `Prezzi: circa +${Math.round(health.weekGrowth / 1e6)} MB a settimana` : undefined}
              />
              {synced}
              <table className="compact">
                <thead><tr><th>Tabella</th><th className="num">MB</th><th className="num">Quota</th></tr></thead>
                <tbody>
                  {Object.entries(health.tables).slice(0, 6).map(([t, b]) => (
                    <tr key={t}>
                      <td>{LABELS[t] ?? t}</td>
                      <td className="num">{(b / 1e6).toFixed(1)}</td>
                      <td className="num muted">{health.dbBytes ? `${((100 * b) / health.dbBytes).toFixed(0)}%` : "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="note">Copia compatta di tutto il database ogni lunedì, tenuta 3 settimane tra gli artifact del workflow quality.</p>
            </div>
          ) : (
            <>
              <Empty icon={Database} title="Misura in arrivo">Il giro del mattino misura lo spazio sulla copia locale del database.</Empty>
              <div className="card-pad" style={{ paddingTop: 0 }}>{synced}</div>
            </>
          )}
        </section>
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
            <Meter label="API-Football · oggi" used={use.apifDay} limit={100} hint="Si azzera a mezzanotte UTC. Riserva di 3 richieste; le formazioni dei 7 campionati hanno la precedenza su coppe e nazionali" />
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
