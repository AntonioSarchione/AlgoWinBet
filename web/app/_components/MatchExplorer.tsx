"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { compShort, dayTime, fairOdds, pct } from "./format";
import { HBar, Ring, Split1X2, TeamBadge } from "./ui";
import { isEstimated } from "@/lib/books";

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
  // headline selections priced by Sisal: playable odds, devigged market probability, final probability used by the slips
  book: Partial<Record<"p_home" | "p_draw" | "p_away" | "p_over25" | "p_btts", { odds: number; book: string; pm: number | null; pf: number }>>;
};

type Side = "p_home" | "p_draw" | "p_away";

// The pure model alone can sit far from the bookmakers (national teams: few matches in its history). Where Sisal prices the
// 1X2, the ring shows the final probability (model shrunk toward the market, the one the slips use) and the Sisal price.
function side(f: ExplorerMatch, k: Side) {
  const b = f.book[k];
  const model = f[k];
  if (!b) return { value: model, sub: `quota equa ${fairOdds(model)}` };
  return { value: b.pf, sub: `${isEstimated(b.book) ? "Sisal stimata" : "Sisal"} ${b.odds.toFixed(2)} · modello ${pct(model)}` };
}

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
  const [h, d, a] = (["p_home", "p_draw", "p_away"] as Side[]).map((k) => side(f, k));
  const priced = Boolean(f.book.p_home || f.book.p_draw || f.book.p_away);
  const apart = (["p_home", "p_draw", "p_away"] as Side[]).some((k) => {
    const pm = f.book[k]?.pm;
    const m = f[k];
    return pm != null && m != null && Math.abs(pm - m) > 0.1;
  });
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
            <h4 className="note" style={{ margin: "0 0 10px" }}>
              {priced ? "Probabilità 1X2 (modello + quote Sisal)" : "Probabilità 1X2 (solo modello: nessuna quota Sisal recente)"}
            </h4>
            <div className="rings">
              <Ring value={h.value} label={f.home} sub={h.sub} top={<TeamBadge name={f.home} size="lg" />} />
              <Ring value={d.value} label="Pareggio" sub={d.sub} top={<span className="ring-x" aria-hidden="true">X</span>} />
              <Ring value={a.value} label={f.away} sub={a.sub} top={<TeamBadge name={f.away} size="lg" />} />
            </div>
            {apart && (
              <p className="note warn-note">
                Il modello da solo si discosta molto dalle quote (dati storici scarsi per queste squadre): le schedine usano la
                probabilità combinata con il mercato mostrata nei cerchi.
              </p>
            )}
            {f.xg_home != null && f.xg_away != null && (
              <p className="note" style={{ textAlign: "center", marginTop: 12 }}>
                Gol attesi: <span className="num">{f.xg_home.toFixed(2)}</span> – <span className="num">{f.xg_away.toFixed(2)}</span>
              </p>
            )}
          </div>
          <div>
            <h4 className="note" style={{ margin: "0 0 10px" }}>Mercati principali (con quota Sisal: probabilità combinata; senza: modello)</h4>
            {f.picks.map(([l, p]) => <HBar key={l} label={l} p={p} />)}
            <div style={{ marginTop: 10 }}><Split1X2 h={h.value} d={d.value} a={a.value} /></div>
          </div>
        </div>
      )}
    </section>
  );
}
