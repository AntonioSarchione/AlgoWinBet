// The selections of a recorded slip, closed until tapped (Registro, Bankroll): they must be readable on a phone, where there is
// no hover. Each one links to its match and shows its own result.
import Link from "next/link";
import type { PaperLeg } from "@/lib/db";

export type SlipLeg = { fixture_id: string; sel_key: string; match: string; market: string; odds: number };

const LABEL: Record<string, string> = { won: "Vinta", lost: "Persa", void: "Rimborsata", "non valutabile": "Non valutabile" };
const CLASS: Record<string, string> = { won: "status-STRONG", lost: "status-AVOID", void: "status-WATCH", "non valutabile": "status-WATCH" };

export const legResults = (legs: PaperLeg[]) => new Map(legs.map((l) => [`${l.fixture_id}|${l.sel_key}`, l]));

export function SlipLegs({ legs, results }: { legs: SlipLeg[]; results: Map<string, PaperLeg> }) {
  return (
    <details className="slip-legs">
      <summary>
        {legs.length} {legs.length === 1 ? "evento" : "eventi"} · {legs.slice(0, 2).map((l) => l.match).join(", ")}{legs.length > 2 ? "…" : ""}
      </summary>
      <ul>
        {legs.map((l) => {
          const res = results.get(`${l.fixture_id}|${l.sel_key}`);
          return (
            <li key={`${l.fixture_id}|${l.sel_key}`}>
              <Link href={`/partita/${encodeURIComponent(l.fixture_id)}`}>{l.match}</Link>
              <span className="muted"> · {l.market} @{l.odds.toFixed(2)}</span>{" "}
              {res?.result ? (
                <span className={`status ${CLASS[res.result] ?? ""}`}>{LABEL[res.result] ?? res.result}{res.score ? ` ${res.score}` : ""}</span>
              ) : (
                <span className="muted">in attesa</span>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
