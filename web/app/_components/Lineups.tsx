import { Shirt } from "lucide-react";
import { parseJSON } from "@/lib/db";
import { layout, shortName, type Detail } from "@/lib/pitch";
import { dayTime } from "./format";
import { Empty, TeamBadge } from "./ui";

const ROLE: Record<string, string> = { GK: "P", DEF: "D", MID: "C", FWD: "A" };

export type LineupData = { team: string; status: string; formation: string | null; starters: string; bench: string; observed_at: string; detail: string | null };

export function Lineups({ lineups, players, home, away }: { lineups: LineupData[]; players: Record<string, { name: string; position: string }>; home: string; away: string }) {
  if (!lineups.length) {
    return (
      <Empty icon={Shirt} title="Formazioni non ancora disponibili">
        Le formazioni ufficiali escono circa un&apos;ora prima del calcio d&apos;inizio e vengono raccolte automaticamente.
      </Empty>
    );
  }
  const name = (id: string) => players[id]?.name ?? id;
  const role = (id: string) => ROLE[players[id]?.position ?? ""] ?? "";
  const sides = ([["home", home], ["away", away]] as const).map(([side, team]) => {
    const l = lineups.find((x) => x.team === team);
    const starters = l ? parseJSON<string[]>(l.starters, []) : [];
    const detail = parseJSON<Detail>(l?.detail, {});
    return { side, team, l, starters, detail, spots: l ? layout(starters, l.formation, detail, side) : null };
  });
  const head = (x: (typeof sides)[number]) => (
    <div className={`pitch-head pitch-head-${x.side}`}>
      <h3><span className={`pitch-dot pitch-dot-${x.side}`} aria-hidden="true" /><TeamBadge name={x.team} /> {x.team}</h3>
      {x.l ? (
        <span className={`status ${x.l.status === "confirmed" ? "status-STRONG" : "status-WATCH"}`}>
          {x.l.status === "confirmed" ? "Ufficiale" : "Probabile"}{x.l.formation ? ` · ${x.l.formation}` : ""}
        </span>
      ) : (
        <span className="status status-WATCH">Non ancora pubblicata</span>
      )}
    </div>
  );
  const onPitch = sides.filter((x) => x.spots);
  return (
    <div className="lineups">
      <div className="pitch-heads">
        {head(sides[0])}
        {head(sides[1])}
      </div>
      {onPitch.length > 0 && (
        <div className="pitch" role="group" aria-label={`Formazioni in campo: ${home} in alto, ${away} in basso`}>
          <svg className="pitch-lines pitch-v" viewBox="0 0 68 105" preserveAspectRatio="none" aria-hidden="true">
            <rect x="1" y="1" width="66" height="103" /><line x1="1" y1="52.5" x2="67" y2="52.5" /><circle cx="34" cy="52.5" r="9.15" />
            <rect x="13.85" y="1" width="40.3" height="16.5" /><rect x="24.84" y="1" width="18.32" height="5.5" />
            <rect x="13.85" y="87.5" width="40.3" height="16.5" /><rect x="24.84" y="98.5" width="18.32" height="5.5" />
          </svg>
          <svg className="pitch-lines pitch-h" viewBox="0 0 105 68" preserveAspectRatio="none" aria-hidden="true">
            <rect x="1" y="1" width="103" height="66" /><line x1="52.5" y1="1" x2="52.5" y2="67" /><circle cx="52.5" cy="34" r="9.15" />
            <rect x="1" y="13.85" width="16.5" height="40.3" /><rect x="1" y="24.84" width="5.5" height="18.32" />
            <rect x="87.5" y="13.85" width="16.5" height="40.3" /><rect x="98.5" y="24.84" width="5.5" height="18.32" />
          </svg>
          {onPitch.map((x) => (
            <ul key={x.side} className={`pitch-team pitch-${x.side}`} aria-label={`${x.team}, ${x.l?.formation ?? "modulo n/d"}`}>
              {x.spots!.map((p) => (
                <li key={p.id} style={{ "--x": p.x, "--y": p.y } as React.CSSProperties}>
                  <span className="shirt" aria-hidden="true">{p.number ?? role(p.id)}</span>
                  <span className="pname">{shortName(name(p.id))}</span>
                </li>
              ))}
            </ul>
          ))}
        </div>
      )}
      <div className="pitch-foot">{head(sides[1])}</div>
      <div className="split" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        {sides.map((x) =>
          x.l ? (
            <div key={x.side} className="bench">
              <b className="bench-team"><span className={`pitch-dot pitch-dot-${x.side}`} aria-hidden="true" /> {x.team}</b>
              {!x.spots && (
                <ol className="bench-xi">
                  {x.starters.map((p) => (
                    <li key={p}><span className="muted mono">{role(p)}</span> {name(p)}</li>
                  ))}
                </ol>
              )}
              {parseJSON<string[]>(x.l.bench, []).length > 0 && (
                <p className="note">
                  Panchina:{" "}
                  {parseJSON<string[]>(x.l.bench, [])
                    .map((p) => (x.detail[p]?.n ? `${x.detail[p]!.n} ${shortName(name(p))}` : shortName(name(p))))
                    .join(", ")}
                </p>
              )}
              <p className="note">Rilevata {dayTime(x.l.observed_at)}</p>
            </div>
          ) : null,
        )}
      </div>
    </div>
  );
}

