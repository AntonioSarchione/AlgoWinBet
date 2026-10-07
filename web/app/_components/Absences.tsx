import { UserX } from "lucide-react";
import { REGULAR_SHEETS, type Absence } from "@/lib/db";
import { dayTime } from "./format";

export const ABSENCE_LABEL: Record<string, string> = { SUSPENDED: "Squalificato", OUT: "Out", DOUBTFUL: "In dubbio" };
const ABSENCE_CLASS: Record<string, string> = { SUSPENDED: "status-AVOID", OUT: "status-AVOID", DOUBTFUL: "status-WATCH" };
const KIND: Record<string, string> = { injury: "infortunio", suspension: "squalifica", internationalduty: "impegno con la nazionale" };
const SOURCE: Record<string, string> = { fotmob: "FotMob", "api-football": "API-Football" };

/** "injury: Late October" (FotMob: type and expected return) -> "infortunio · rientro: Late October"; other sources as they are. */
export function absenceReason(a: Absence): string {
  if (!a.reason) return "";
  const m = /^([A-Za-z]+): (.*)$/.exec(a.reason);
  if (!m || a.source !== "fotmob") return a.reason;
  const back = m[2].trim();
  const kind = KIND[m[1].toLowerCase()] ?? m[1];
  return back && back !== "-" && !/doubt/i.test(back) ? `${kind} · rientro: ${back}` : kind;
}

/** A starter of the team's recent official XI (the absences that move the model and the slips). */
export const isRegular = (a: Absence) => (a.starts ?? 0) > 0;

export function startsLabel(a: Absence): string {
  if (a.starts == null) return "";
  return a.starts > 0 ? `titolare in ${a.starts} delle ultime ${REGULAR_SHEETS}` : `non titolare nelle ultime ${REGULAR_SHEETS}`;
}

export function Absences({ absences, home, away }: { absences: Absence[]; home: string; away: string }) {
  if (!absences.length) return null;
  return (
    <div>
      <h2 className="section"><UserX size={17} aria-hidden="true" /> Assenti e in dubbio</h2>
      <p className="note" style={{ margin: "4px 0 10px" }}>
        Infortuni, squalifiche e dubbi letti prima della partita. In alto i titolari delle ultime {REGULAR_SHEETS} formazioni ufficiali.
      </p>
      <div className="split" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        {(
          [
            ["home", home],
            ["away", away],
          ] as const
        ).map(([side, team]) => {
          const list = absences.filter((a) => a.team === team);
          return (
            <div key={side} className="bench">
              <b className="bench-team"><span className={`pitch-dot pitch-dot-${side}`} aria-hidden="true" /> {team}</b>
              {list.length ? (
                <ul className="absences">
                  {list.map((a) => (
                    <li key={a.player_id} className={isRegular(a) ? "absence-regular" : undefined}>
                      <span className={`status ${ABSENCE_CLASS[a.status] ?? "status-NEUTRAL"}`}>{ABSENCE_LABEL[a.status] ?? a.status}</span>
                      <span>
                        <b>{a.name}</b>
                        <span className="sub">
                          {[absenceReason(a), startsLabel(a), `${SOURCE[a.source] ?? a.source} ${dayTime(a.observed_at)}`].filter(Boolean).join(" · ")}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted" style={{ margin: 0 }}>Nessun assente segnalato.</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
