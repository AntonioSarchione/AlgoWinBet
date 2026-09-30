import Link from "next/link";
import { Filter, Percent, Target } from "lucide-react";
import { latestRun, oppSummary, runCompetitions } from "@/lib/db";
import { dayTime } from "@/app/_components/format";
import { OppTable } from "@/app/_components/OppTable";
import { Empty } from "@/app/_components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Opportunità" };

const STATUSES = [
  { v: "", l: "Tutte" },
  { v: "STRONG", l: "Alta" },
  { v: "CANDIDATE", l: "Media" },
  { v: "WATCH", l: "Da osservare" },
];

type SP = { s?: string; comp?: string; lmin?: string; lmax?: string };

const MAX_ROWS = 200; // the database filters and returns only the first rows by EV: the page stays light

export default async function Opportunita({ searchParams }: { searchParams: Promise<SP> }) {
  const { s = "", comp = "", lmin = "", lmax = "" } = await searchParams;
  const lo = Number(lmin) || 0;
  const hi = Number(lmax) || 0;
  const run = await latestRun();
  const iso = (t: number) => new Date(t).toISOString().slice(0, 16); // minute precision: equal filters share the data cache
  const [comps, res] = run
    ? await Promise.all([
        runCompetitions(run.id),
        oppSummary(run.id, { from: iso(Date.now()), until: "9999", comps: comp ? [comp] : [], lmin: lo, lmax: hi, status: s || undefined }, MAX_ROWS),
      ])
    : [[], { count: 0, top: [] }];
  const rows = res.top;
  const total = res.count;
  const href = (next: { s?: string; comp?: string }) => {
    const q = new URLSearchParams({ ...(s && { s }), ...(comp && { comp }), ...(lmin && { lmin }), ...(lmax && { lmax }), ...next });
    for (const [k, v] of [...q]) if (!v) q.delete(k);
    return q.size ? `/opportunita?${q}` : "/opportunita";
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Opportunità</h1>
          <p>
            Mercati con valore atteso da osservare, ordinati per EV{run && ` · analisi del ${dayTime(run.created_at)}`}. Il valore esiste solo
            se la quota del bookmaker supera la quota equa del modello.
          </p>
        </div>
      </header>
      <div className="card card-pad" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <form key={`${s}|${comp}|${lmin}|${lmax}`} method="get" style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "end" }} aria-label="Filtra per quota">
          {s && <input type="hidden" name="s" value={s} />}
          {comp && <input type="hidden" name="comp" value={comp} />}
          <div className="field" style={{ minWidth: 260 }}>
            <label htmlFor="lmin">Quota singolo evento (min – max)</label>
            <div className="control">
              <Target size={17} aria-hidden="true" />
              <input id="lmin" name="lmin" type="number" inputMode="decimal" step="0.05" min="1" placeholder="1.20" defaultValue={lmin} aria-label="Quota minima" />
              <span className="dash">–</span>
              <input name="lmax" type="number" inputMode="decimal" step="0.05" min="1" placeholder="3.00" defaultValue={lmax} aria-label="Quota massima" />
            </div>
          </div>
          <button type="submit" className="btn btn-primary"><Filter size={17} aria-hidden="true" /> Applica</button>
        </form>
        <nav className="chips" aria-label="Filtra per stato">
          {STATUSES.map((x) => (
            <Link key={x.v} href={href({ s: x.v })} aria-current={s === x.v ? "true" : undefined}>{x.l}</Link>
          ))}
        </nav>
        {comps.length > 1 && (
          <nav className="chips" aria-label="Filtra per competizione">
            <Link href={href({ comp: "" })} aria-current={!comp ? "true" : undefined}>Tutte le competizioni</Link>
            {comps.map((c) => (
              <Link key={c} href={href({ comp: c })} aria-current={comp === c ? "true" : undefined}>{c}</Link>
            ))}
          </nav>
        )}
      </div>
      <section className="card">
        {rows.length ? (
          <>
            <OppTable rows={rows} />
            {total > rows.length && (
              <p className="note card-pad">
                Mostrate le prime {rows.length} su {total} per EV. Usa i filtri per stato, competizione o quota per vedere le altre.
              </p>
            )}
          </>
        ) : (
          <Empty icon={Percent} title="Nessuna opportunità">
            Le quote vengono raccolte nelle 24 ore prima delle partite. Senza prezzi recenti il motore non calcola il valore atteso e non propone
            giocate.
          </Empty>
        )}
      </section>
    </>
  );
}
