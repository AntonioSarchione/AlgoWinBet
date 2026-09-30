import Link from "next/link";
import { CalendarDays, Search } from "lucide-react";
import { latestRun, runFixtures, type FixtureRow } from "@/lib/db";
import { compShort, dayKey, dayLong, fairOdds, hour, pct } from "@/app/_components/format";
import { Empty, MatchCell, Split1X2 } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Palinsesto" };

export default async function Palinsesto({ searchParams }: { searchParams: Promise<{ q?: string; comp?: string }> }) {
  const { q = "", comp = "" } = await searchParams;
  const run = await latestRun();
  const all = run ? await runFixtures(run.id) : [];
  const comps = [...new Set(all.map((f) => f.competition))].sort();
  const needle = q.trim().toLowerCase();
  const rows = all.filter(
    (f) =>
      (!comp || f.competition === comp) &&
      (!needle || [f.home, f.away, f.competition].some((x) => x.toLowerCase().includes(needle))),
  );
  const days = new Map<string, FixtureRow[]>();
  for (const f of rows) days.set(dayKey(f.kickoff), [...(days.get(dayKey(f.kickoff)) ?? []), f]);
  const href = (c: string) => {
    const p = new URLSearchParams({ ...(q && { q }), ...(c && { comp: c }) });
    return p.size ? `/palinsesto?${p}` : "/palinsesto";
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Palinsesto completo</h1>
          <p>Probabilità del modello per ogni partita dei prossimi 7 giorni. Passa sopra una percentuale per la quota equa.</p>
        </div>
      </header>

      <div className="card card-pad" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <form method="get" role="search" style={{ display: "flex", gap: 8 }}>
          {comp && <input type="hidden" name="comp" value={comp} />}
          <label htmlFor="q" className="sr-only">Cerca squadra o campionato</label>
          <div className="control" style={{ flex: 1 }}>
            <Search size={17} aria-hidden="true" />
            <input id="q" name="q" type="search" defaultValue={q} placeholder="Cerca squadra o campionato…" />
          </div>
          <button type="submit" className="btn btn-primary">Cerca</button>
        </form>
        <nav className="chips" aria-label="Filtra per competizione">
          <Link href={href("")} aria-current={!comp ? "true" : undefined}>Tutte ({all.length})</Link>
          {comps.map((c) => (
            <Link key={c} href={href(c)} aria-current={comp === c ? "true" : undefined}>
              {compShort(c)} ({all.filter((f) => f.competition === c).length})
            </Link>
          ))}
        </nav>
      </div>

      {rows.length === 0 ? (
        <div className="card">
          <Empty icon={CalendarDays} title="Nessuna partita trovata">
            {needle ? `Nessun risultato per "${q}".` : "Il calendario si aggiorna ogni mattina."}
          </Empty>
        </div>
      ) : (
        [...days.entries()].map(([day, list]) => (
          <section key={day} className="card" aria-label={dayLong(list[0].kickoff)}>
            <div className="card-head">
              <h2 style={{ textTransform: "capitalize" }}>{dayLong(list[0].kickoff)}</h2>
              <span className="count">{list.length} partite</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ora</th><th>Partita</th><th className="num">1</th><th className="num">X</th><th className="num">2</th>
                    <th>Esito</th><th className="num">Over 2.5</th><th className="num">Gol</th><th className="num">Gol attesi</th><th>Info</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((f) => (
                    <tr key={f.fixture_id}>
                      <td className="num">{hour(f.kickoff)}</td>
                      <td className="wrap">
                        <MatchCell home={f.home} away={f.away} sub={compShort(f.competition)} href={`/partita/${encodeURIComponent(f.fixture_id)}`} />
                      </td>
                      {[f.p_home, f.p_draw, f.p_away].map((p, i) => (
                        <td key={i} className="num" title={p == null ? undefined : `quota equa ${fairOdds(p)}`}>{pct(p)}</td>
                      ))}
                      <td><Split1X2 h={f.p_home} d={f.p_draw} a={f.p_away} /></td>
                      <td className="num" title={f.p_over25 == null ? undefined : `quota equa ${fairOdds(f.p_over25)}`}>{pct(f.p_over25)}</td>
                      <td className="num" title={f.p_btts == null ? undefined : `quota equa ${fairOdds(f.p_btts)}`}>{pct(f.p_btts)}</td>
                      <td className="num muted">{f.xg_home != null && f.xg_away != null ? `${f.xg_home.toFixed(1)} – ${f.xg_away.toFixed(1)}` : "–"}</td>
                      <td className="muted" style={{ fontSize: 12 }}>
                        {[
                          f.lineup_state === "confirmed" ? "XI ufficiali" : f.lineup_state === "probable" ? "XI probabili" : null,
                          f.n_quotes ? `${f.n_quotes} mercati quotati` : null,
                          f.p_home == null ? "storico insufficiente" : null,
                        ].filter(Boolean).join(" · ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}
      <p className="note">
        Legenda barra esito: <span style={{ color: "var(--s1)" }}>■</span> 1 casa · <span style={{ color: "var(--s2)" }}>■</span> X pareggio ·{" "}
        <span style={{ color: "var(--s3)" }}>■</span> 2 ospite.
      </p>
    </>
  );
}
