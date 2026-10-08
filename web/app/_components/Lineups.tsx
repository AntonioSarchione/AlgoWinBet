import { Shirt } from "lucide-react";
import { parseJSON } from "@/lib/db";
import type { Absence } from "@/lib/db";
import { layout, type Detail } from "@/lib/pitch";
import { dayTime, pct } from "./format";
import { Empty, TeamBadge } from "./ui";
import { ABSENCE_LABEL, Absences } from "./Absences";
import { PitchBoard, type PitchPlayer, type PitchSide, type PlayerCard } from "./PitchBoard";

const ROLE: Record<string, string> = { GK: "P", DEF: "D", MID: "C", FWD: "A" };

export type LineupData = { team: string; status: string; formation: string | null; starters: string; bench: string; observed_at: string; detail: string | null };
// our probable XI (pub_fixtures.probable, published with the analysis): [id, name, role, probability of starting, status]
type ProbRow = [string, string, string, number, string | null];
export type ProbableData = Record<string, { formation: string; xi: ProbRow[]; alt: ProbRow[] }>;

type Side = {
  side: "home" | "away";
  team: string;
  kind: "official" | "listed" | "ours" | "none"; // official XI, a source's probable XI, our probable XI, nothing yet
  starters: string[];
  formation: string | null;
  detail: Detail;
  bench: string[];
  at: string | null;
  probs: Record<string, number>; // ours only
  statuses: Record<string, string>; // ours only
  alt: ProbRow[];
};

export function Lineups({ lineups, players, home, away, absences = [], probable = {}, analysedAt, cards = {} }: {
  lineups: LineupData[]; players: Record<string, { name: string; position: string }>; home: string; away: string;
  absences?: Absence[]; probable?: ProbableData; analysedAt?: string; cards?: Record<string, PlayerCard>;
}) {
  const names: Record<string, string> = Object.fromEntries(Object.entries(players).map(([k, v]) => [k, v.name]));
  const roles: Record<string, string> = Object.fromEntries(Object.entries(players).map(([k, v]) => [k, v.position]));
  for (const t of Object.values(probable)) for (const [id, n, r] of [...t.xi, ...t.alt]) { names[id] ??= n; roles[id] ??= r; }
  const name = (id: string) => names[id] ?? id;
  const role = (id: string) => ROLE[roles[id] ?? ""] ?? "";

  const sides: Side[] = ([["home", home], ["away", away]] as const).map(([side, team]): Side => {
    const l = lineups.find((x) => x.team === team);
    const ours = probable[team];
    if (l && (l.status === "confirmed" || !ours)) {
      return {
        side, team, kind: l.status === "confirmed" ? "official" : "listed", starters: parseJSON<string[]>(l.starters, []), formation: l.formation,
        detail: parseJSON<Detail>(l.detail, {}), bench: parseJSON<string[]>(l.bench, []), at: l.observed_at, probs: {}, statuses: {}, alt: [],
      };
    }
    if (ours) {
      const rows = [...ours.xi, ...ours.alt];
      return {
        side, team, kind: "ours", starters: ours.xi.map((r) => r[0]), formation: ours.formation, detail: {}, bench: [], at: analysedAt ?? null,
        probs: Object.fromEntries(rows.map((r) => [r[0], r[3]])),
        statuses: Object.fromEntries(rows.filter((r) => r[4]).map((r) => [r[0], r[4]!])), alt: ours.alt,
      };
    }
    return { side, team, kind: "none", starters: [], formation: null, detail: {}, bench: [], at: null, probs: {}, statuses: {}, alt: [] };
  });

  if (sides.every((x) => x.kind === "none")) {
    return (
      <div className="col">
        <Empty icon={Shirt} title="Formazioni non ancora disponibili">
          La nostra formazione probabile arriva con la prossima analisi pubblicata; quella ufficiale circa un&apos;ora prima del calcio d&apos;inizio.
        </Empty>
        <Absences absences={absences} home={home} away={away} />
      </div>
    );
  }

  const spots = (x: Side) => (x.starters.length ? layout(x.starters, x.formation, x.detail, x.side) : null);
  const label = (x: Side) =>
    x.kind === "official" ? "Ufficiale" : x.kind === "listed" ? "Probabile (fonte esterna)" : x.kind === "ours" ? "Probabile (nostro modello)" : "Non ancora pubblicata";
  const head = (x: Side) => (
    <div className={`pitch-head pitch-head-${x.side}`}>
      <h3><span className={`pitch-dot pitch-dot-${x.side}`} aria-hidden="true" /><TeamBadge name={x.team} /> {x.team}</h3>
      <span className={`status ${x.kind === "official" ? "status-STRONG" : "status-WATCH"}`}>
        {label(x)}{x.formation ? ` · ${x.formation}` : ""}
      </span>
    </div>
  );
  const flag = (x: Side, id: string) => (x.statuses[id] ? ` (${(ABSENCE_LABEL[x.statuses[id]] ?? x.statuses[id]).toLowerCase()})` : "");
  const onPitch = sides.map((x) => ({ x, s: spots(x) })).filter((v) => v.s);
  const offPitch = (x: Side, id: string): PitchPlayer => ({
    id, x: 0, y: 0, shirt: role(id), name: name(id), role: role(id), prob: x.probs[id] != null ? pct(x.probs[id], 0) : null,
    flag: x.statuses[id] ? (ABSENCE_LABEL[x.statuses[id]] ?? x.statuses[id]).toLowerCase() : null,
  });
  const anyOurs = sides.some((x) => x.kind === "ours");

  return (
    <div className="lineups">
      <div className="pitch-heads">
        {head(sides[0])}
        {head(sides[1])}
      </div>
      {onPitch.length > 0 && (
        <PitchBoard
          label={`Formazioni in campo: ${home} in alto, ${away} in basso. Tocca un giocatore, anche tra alternative e panchina, per le sue statistiche`}
          cards={cards}
          sides={onPitch.map(({ x, s }): PitchSide => ({
            side: x.side, team: x.team, label: `${label(x)}, ${x.formation ?? "modulo n/d"}`, probable: x.kind === "ours",
            players: s!.map((p) => ({
              id: p.id, x: p.x, y: p.y, shirt: String(p.number ?? role(p.id)), name: name(p.id), role: role(p.id),
              prob: x.probs[p.id] != null ? pct(x.probs[p.id], 0) : null,
              flag: x.statuses[p.id] ? (ABSENCE_LABEL[x.statuses[p.id]] ?? x.statuses[p.id]).toLowerCase() : null,
            })),
            extras: [
              { label: "Alternative", players: x.alt.map((r) => offPitch(x, r[0])) },
              { label: "Panchina", players: x.bench.map((p) => ({ ...offPitch(x, p), shirt: String(x.detail[p]?.n ?? role(p)) })) },
            ],
          }))}
        >
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
        </PitchBoard>
      )}
      <div className="pitch-foot">{head(sides[1])}</div>
      <div className="split" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        {sides.map((x) =>
          x.kind !== "none" ? (
            <div key={x.side} className="bench">
              <b className="bench-team"><span className={`pitch-dot pitch-dot-${x.side}`} aria-hidden="true" /> {x.team}</b>
              {!spots(x) && (
                <ol className="bench-xi">
                  {x.starters.map((p) => (
                    <li key={p}>
                      <span className="muted mono">{role(p)}</span> {name(p)}
                      {x.probs[p] != null && <span className="muted"> · {pct(x.probs[p], 0)}</span>}
                      {flag(x, p) && <span className="neg">{flag(x, p)}</span>}
                    </li>
                  ))}
                </ol>
              )}
              {x.kind === "ours" && x.starters.some((p) => x.statuses[p]) && (
                <p className="note">
                  Nella probabile ma segnalati: {x.starters.filter((p) => x.statuses[p]).map((p) => `${name(p)}${flag(x, p)}`).join(", ")}
                </p>
              )}
              {x.alt.length > 0 && !spots(x) && (
                <p className="note">Alternative: {x.alt.map((r) => `${r[1]} ${pct(r[3], 0)}${flag(x, r[0])}`).join(", ")}</p>
              )}
              {x.bench.length > 0 && !spots(x) && (
                <p className="note">
                  Panchina: {x.bench.map((p) => (x.detail[p]?.n ? `${x.detail[p]!.n} ${name(p)}` : name(p))).join(", ")}
                </p>
              )}
              {x.at && <p className="note">{x.kind === "ours" ? "Calcolata con l'analisi del" : "Rilevata"} {dayTime(x.at)}</p>}
            </div>
          ) : null,
        )}
      </div>
      {anyOurs && (
        <p className="note">
          Probabile del nostro modello: per ogni giocatore la probabilità di partire titolare (sul telefono è il numero nel pallino), da
          presenze recenti, turnover, squalifiche, infortuni e dubbi segnalati. Tocca un giocatore per le sue statistiche. Lascia il posto alla formazione ufficiale appena esce (circa un&apos;ora prima del calcio d&apos;inizio).
        </p>
      )}
      <Absences absences={absences} home={home} away={away} />
    </div>
  );
}
