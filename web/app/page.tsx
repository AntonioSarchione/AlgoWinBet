import Link from "next/link";
import { loadDashboard, type FixtureRow, type Leg } from "@/lib/db";

export const dynamic = "force-dynamic";

const TZ = "Europe/Rome";
const dt = (iso: string) =>
  new Date(iso).toLocaleString("it-IT", { timeZone: TZ, weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const pct = (p: number | null | undefined) => (p == null ? "–" : `${Math.round(p * 100)}%`);
const fair = (p: number | null | undefined) => (p == null || p <= 0 ? "" : (1 / p).toFixed(2));
const signed = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;

export default async function Page({ searchParams }: { searchParams: Promise<{ comp?: string }> }) {
  const { comp } = await searchParams;
  const d = await loadDashboard();

  if (!d.run) {
    return (
      <main>
        <h1>AlgoWinBet</h1>
        <p className="muted">Nessuna analisi pubblicata ancora: la prima arriva dal prossimo giro di raccolta.</p>
      </main>
    );
  }

  const reasons: string[] = JSON.parse(d.run.reasons || "[]");
  const notes: string[] = JSON.parse(d.run.notes || "[]");
  const comps = Array.from(new Set(d.fixtures.map((f) => f.competition))).sort();
  const shown: FixtureRow[] = comp ? d.fixtures.filter((f) => f.competition === comp) : d.fixtures;
  const use = (src: string, kind: "D" | "M") => d.usage.find((u) => u.source === src && u.period.startsWith(kind))?.used ?? 0;

  return (
    <main>
      <h1>AlgoWinBet</h1>
      <p className="muted">
        Analisi del {dt(d.run.created_at)} · prossimi {d.run.horizon_days} giorni · uso personale, solo paper trading
      </p>

      <div className="grid">
        <div className="panel stat"><span className="muted">Partite</span><b>{d.run.n_fixtures}</b></div>
        <div className="panel stat"><span className="muted">Con quote (ultime 24h)</span><b>{d.run.n_with_quotes}</b></div>
        <div className="panel stat"><span className="muted">Schedine</span><b>{d.slips.length}</b></div>
        <div className="panel stat">
          <span className="muted">Richieste API</span>
          <b style={{ fontSize: 14 }}>GOAL {use("goal-api", "D")}/1000 oggi · OddsPapi {use("oddspapi", "M")}/250 mese</b>
        </div>
      </div>

      <h2>Schedine</h2>
      {d.slips.length === 0 ? (
        <div className="panel nobet">
          <b>NO BET</b>
          <ul>{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      ) : (
        d.slips.map((s) => {
          const legs: Leg[] = JSON.parse(s.legs);
          return (
            <div className="panel slip" key={s.rank}>
              <b>#{s.rank} · quota {s.total_odds.toFixed(2)} · probabilità {pct(s.joint_probability)}</b>{" "}
              <span className={s.ev >= 0 ? "pos" : "neg"}>EV {signed(s.ev)}</span>{" "}
              <span className="muted">(prudente {signed(s.ev_lower)}) · puntata paper {s.stake.toFixed(2)}</span>
              <ul>
                {legs.map((l, i) => (
                  <li key={i}>
                    {dt(l.kickoff)} · <b>{l.match}</b> <span className="muted">[{l.competition}]</span> — {l.market} @ {l.odds.toFixed(2)}{" "}
                    <span className="muted">({l.bookmaker}, p {pct(l.p)})</span>
                  </li>
                ))}
              </ul>
            </div>
          );
        })
      )}

      <h2>Opportunità</h2>
      {d.opps.length === 0 ? (
        <p className="muted">Nessun mercato sopra le soglie di osservazione.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Stato</th><th>Partita</th><th>Mercato</th><th>Book</th>
                <th className="num">Quota</th><th className="num">Equa</th><th className="num">p modello</th>
                <th className="num">p mercato</th><th className="num">EV</th><th>Note</th>
              </tr>
            </thead>
            <tbody>
              {d.opps.map((o, i) => (
                <tr key={i}>
                  <td className={o.status}>{o.status}</td>
                  <td>{dt(o.kickoff)} · {o.match}</td>
                  <td>{o.market}</td>
                  <td>{o.bookmaker}</td>
                  <td className="num">{o.odds.toFixed(2)}</td>
                  <td className="num">{o.fair_odds.toFixed(2)}</td>
                  <td className="num">{pct(o.p_final)}</td>
                  <td className="num">{pct(o.p_market)}</td>
                  <td className={`num ${o.ev >= 0 ? "pos" : "neg"}`}>{signed(o.ev)}</td>
                  <td className="muted">{o.odds_stale ? "quota da ricontrollare" : o.lineup_state === "confirmed" ? "XI ufficiali" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>Probabilità del modello</h2>
      <nav className="comps">
        <Link href="/" className={!comp ? "on" : ""}><span className="chip">Tutte</span></Link>
        {comps.map((c) => (
          <Link key={c} href={`/?comp=${encodeURIComponent(c)}`} className={comp === c ? "on" : ""}>
            <span className="chip">{c}</span>
          </Link>
        ))}
      </nav>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Quando</th><th>Partita</th><th>Competizione</th>
              <th className="num">1</th><th className="num">X</th><th className="num">2</th>
              <th className="num">Over 2.5</th><th className="num">Goal</th><th>Formazioni</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.fixture_id}>
                <td>{dt(f.kickoff)}</td>
                <td>{f.home} - {f.away}</td>
                <td className="muted">{f.competition}</td>
                {[f.p_home, f.p_draw, f.p_away, f.p_over25, f.p_btts].map((p, i) => (
                  <td className="num" key={i} title={p == null ? "" : `quota equa ${fair(p)}`}>{pct(p)}</td>
                ))}
                <td className="muted">{f.lineup_state === "confirmed" ? "ufficiali" : f.lineup_state === "probable" ? "probabili" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note muted">
        Passa il mouse su una probabilità per la quota equa. Probabilità alte non significano valore: conta il confronto con la quota
        (EV). Ultima raccolta dati: {d.lastTick ? dt(d.lastTick) : "–"}.
      </p>
      {notes.length > 0 && <p className="note muted">{notes.join(" · ")}</p>}
    </main>
  );
}
