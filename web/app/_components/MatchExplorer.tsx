"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { compShort, dayTime, fairOdds } from "./format";
import { HBar, Ring, Split1X2, TeamBadge } from "./ui";

export type ExplorerMatch = {
  id: string;
  kickoff: string;
  competition: string;
  home: string;
  away: string;
  p_home: number | null;
  p_draw: number | null;
  p_away: number | null;
  xg_home: number | null;
  xg_away: number | null;
  picks: [string, number][];
};

// Home: every match passing the filters in a scrollable strip (nearest kickoff first); the selected one drives the rings
// and the market bars below. Selection is client state, so switching match costs no server round trip.
export function MatchExplorer({ matches, initial }: { matches: ExplorerMatch[]; initial?: string }) {
  const [sel, setSel] = useState<string | undefined>(() => (matches.some((m) => m.id === initial) ? initial! : matches[0]?.id));
  const strip = useRef<HTMLDivElement>(null);
  const f = matches.find((m) => m.id === sel) ?? matches[0];

  useEffect(() => {
    if (!matches.some((m) => m.id === sel)) setSel(matches.some((m) => m.id === initial) ? initial : matches[0]?.id);
  }, [matches, initial, sel]);

  useEffect(() => {
    // keep the selected card visible inside the strip only (scrollIntoView would also scroll the page on first render)
    const el = strip.current?.querySelector<HTMLElement>('[aria-pressed="true"]');
    const box = strip.current;
    if (!el || !box) return;
    if (el.offsetLeft < box.scrollLeft || el.offsetLeft + el.offsetWidth > box.scrollLeft + box.clientWidth) {
      box.scrollTo({ left: el.offsetLeft - 18, behavior: "smooth" });
    }
  }, [sel]);

  if (!f) return null;
  const scroll = (dir: number) => strip.current?.scrollBy({ left: dir * strip.current.clientWidth * 0.8, behavior: "smooth" });
  const move = (e: React.KeyboardEvent, i: number) => {
    const j = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : -1;
    if (j < 0 || j >= matches.length) return;
    e.preventDefault();
    setSel(matches[j].id);
    strip.current?.querySelectorAll<HTMLButtonElement>("button.strip-item")[j]?.focus();
  };

  return (
    <section className="card" aria-labelledby="deep-title">
      <div className="card-head">
        <h2 id="deep-title">Partite nel filtro <span className="count">{matches.length}</span></h2>
        <div style={{ display: "flex", gap: 6 }}>
          <button type="button" className="btn btn-ghost btn-sm strip-nav" onClick={() => scroll(-1)} aria-label="Scorri a sinistra">
            <ChevronLeft size={16} aria-hidden="true" />
          </button>
          <button type="button" className="btn btn-ghost btn-sm strip-nav" onClick={() => scroll(1)} aria-label="Scorri a destra">
            <ChevronRight size={16} aria-hidden="true" />
          </button>
        </div>
      </div>
      <div className="strip" ref={strip} role="group" aria-label="Partite ordinate dalla più vicina">
        {matches.map((m, i) => (
          <button key={m.id} type="button" className="strip-item" aria-pressed={m.id === f.id} onClick={() => setSel(m.id)} onKeyDown={(e) => move(e, i)}>
            <span className="strip-teams" aria-hidden="true">
              <TeamBadge name={m.home} />
              <TeamBadge name={m.away} />
            </span>
            <span className="strip-text">
              <b>{m.home}</b>
              <b>{m.away}</b>
              <small>{dayTime(m.kickoff)} · {compShort(m.competition)}</small>
            </span>
          </button>
        ))}
      </div>

      <div className="card-head" style={{ borderTop: "1px solid var(--line)" }}>
        <h3>Analisi approfondita · {f.home} vs {f.away}</h3>
        <Link href={`/partita/${encodeURIComponent(f.id)}`} className="btn btn-ghost btn-sm">
          Apri analisi completa <ChevronRight size={15} aria-hidden="true" />
        </Link>
      </div>
      {f.p_home == null ? (
        <p className="card-pad note">Probabilità del modello non disponibili per questa partita (storico insufficiente).</p>
      ) : (
        <div className="split card-pad" style={{ gap: 24 }}>
          <div>
            <h4 className="note" style={{ margin: "0 0 10px" }}>Probabilità 1X2 (modello)</h4>
            <div className="rings">
              <Ring value={f.p_home} label={f.home} sub={`quota equa ${fairOdds(f.p_home)}`} color="var(--s1)" top={<TeamBadge name={f.home} size="lg" />} />
              <Ring value={f.p_draw} label="Pareggio" sub={`quota equa ${fairOdds(f.p_draw)}`} color="var(--s2)" top={<span className="ring-x" aria-hidden="true">X</span>} />
              <Ring value={f.p_away} label={f.away} sub={`quota equa ${fairOdds(f.p_away)}`} color="var(--s3)" top={<TeamBadge name={f.away} size="lg" />} />
            </div>
            {f.xg_home != null && f.xg_away != null && (
              <p className="note" style={{ textAlign: "center", marginTop: 12 }}>
                Gol attesi: <span className="num">{f.xg_home.toFixed(2)}</span> – <span className="num">{f.xg_away.toFixed(2)}</span>
              </p>
            )}
          </div>
          <div>
            <h4 className="note" style={{ margin: "0 0 10px" }}>Mercati principali (probabilità del modello)</h4>
            {f.picks.map(([l, p]) => <HBar key={l} label={l} p={p} />)}
            <div style={{ marginTop: 10 }}><Split1X2 h={f.p_home} d={f.p_draw} a={f.p_away} /></div>
          </div>
        </div>
      )}
    </section>
  );
}
