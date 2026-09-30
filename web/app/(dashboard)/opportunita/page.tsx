import Link from "next/link";
import { Percent } from "lucide-react";
import { latestRun, runOpps } from "@/lib/db";
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

export default async function Opportunita({ searchParams }: { searchParams: Promise<{ s?: string; comp?: string }> }) {
  const { s = "", comp = "" } = await searchParams;
  const run = await latestRun();
  const opps = run ? await runOpps(run.id) : [];
  const comps = [...new Set(opps.map((o) => o.competition))].sort();
  const rows = opps.filter((o) => (!s || o.status === s) && (!comp || o.competition === comp));
  const href = (next: { s?: string; comp?: string }) => {
    const q = new URLSearchParams({ ...(s && { s }), ...(comp && { comp }), ...next });
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
          <OppTable rows={rows} />
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
