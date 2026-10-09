"use client";
// "Analisi rapida" on the home page: the next matches, filtered while typing by team (or competition) name. Enter opens the
// full schedule search (/palinsesto?q=), so the box also works before the script loads.
import { useMemo, useState } from "react";
import Link from "next/link";
import { CalendarClock, ChevronRight, Search } from "lucide-react";
import { compShort, dayTime } from "./format";
import { Empty, TeamBadge } from "./ui";

export type QuickMatch = { id: string; home: string; away: string; competition: string; kickoff: string };

const SHOWN = 6;
const MAX_FOUND = 10;
// accents and punctuation do not count: "koln" finds Köln
const norm = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

export function QuickSearch({ matches }: { matches: QuickMatch[] }) {
  const [q, setQ] = useState("");
  const index = useMemo(() => matches.map((m) => ({ m, teams: [norm(m.home), norm(m.away)], all: norm(`${m.home} ${m.away} ${m.competition}`) })), [matches]);
  const needle = norm(q);
  const list = useMemo(() => {
    if (!needle) return matches.slice(0, SHOWN);
    const words = needle.split(" ");
    return index
      .filter((x) => words.every((w) => x.all.includes(w)))
      // a team name (or one of its words) starting with the text first, then by kickoff
      .map((x) => ({ m: x.m, rank: x.teams.some((t) => t.startsWith(needle) || t.split(" ").some((p) => p.startsWith(needle))) ? 0 : 1 }))
      .sort((a, b) => a.rank - b.rank || a.m.kickoff.localeCompare(b.m.kickoff))
      .slice(0, MAX_FOUND)
      .map((x) => x.m);
  }, [index, matches, needle]);

  return (
    <>
      <form action="/palinsesto" method="get" role="search" style={{ padding: "12px 16px 4px" }}>
        <label htmlFor="q" className="sr-only">Cerca squadra o campionato</label>
        <div className="control">
          <Search size={17} aria-hidden="true" />
          <input id="q" name="q" type="search" placeholder="Cerca squadra o campionato…" autoComplete="off" value={q} onChange={(e) => setQ(e.target.value)} aria-controls="quick-list" />
        </div>
      </form>
      <p className="sr-only" aria-live="polite">{needle ? `${list.length} partite trovate` : ""}</p>
      {list.length ? (
        <ul className="quick" id="quick-list">
          {list.map((f) => (
            <li key={f.id}>
              <Link href={`/partita/${encodeURIComponent(f.id)}`}>
                <span className="badges" style={{ display: "flex", gap: 2 }}>
                  <TeamBadge name={f.home} />
                  <TeamBadge name={f.away} />
                </span>
                <span style={{ minWidth: 0 }}>
                  <b>{f.home} - {f.away}</b>
                  <small>{compShort(f.competition)} · {dayTime(f.kickoff)}</small>
                </span>
                <ChevronRight size={17} aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
      ) : needle ? (
        <Empty icon={Search} title="Nessuna partita trovata">Nessuna partita in programma con «{q.trim()}».</Empty>
      ) : (
        <Empty icon={CalendarClock} title="Nessuna partita in programma" />
      )}
    </>
  );
}
