"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { X } from "lucide-react";

// What a player does in a match he starts (pub_players, src/algowinbet/playercard.py). Goalkeepers: gk = 1.
export type PlayerCard = {
  xg?: number | null; sot?: number; sh?: number; fc?: number; fd?: number; cg: number; as?: number; n: number; min?: number;
  gk?: 1; ts?: number | null; gc?: number | null; sv?: number | null; cs?: number | null;
};
export type PitchPlayer = { id: string; x: number; y: number; shirt: string; name: string; prob: string | null; flag: string | null; role: string };
export type PitchSide = { side: "home" | "away"; team: string; label: string; probable: boolean; players: PitchPlayer[] };

const ROLE_NAME: Record<string, string> = { P: "Portiere", D: "Difensore", C: "Centrocampista", A: "Attaccante" };
const num = (v: number | null | undefined, d = 2) => (v == null ? "–" : v.toFixed(d).replace(/\.?0+$/, "") || "0");
const pc = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);

type Pos = { left: number; top: number; above: boolean; arrow: number };

/** The pitch of the lineups tab. A tap or click on a player highlights him and opens his card next to him; a tap on another
 * player moves the card there, a tap anywhere else (or Esc, or the close button) closes it. The card lives outside the pitch
 * (which clips its content) and is placed from the player's on-screen box, kept inside the board, flipped above the player
 * when there is no room below; it follows the board when its size changes (rotation, sidebar). */
export function PitchBoard({ sides, cards, label, children }: {
  sides: PitchSide[]; cards: Record<string, PlayerCard>; label: string; children: React.ReactNode;
}) {
  const [sel, setSel] = useState<string | null>(null);
  const [pos, setPos] = useState<Pos | null>(null);
  const board = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());

  const key = (side: string, id: string) => `${side}|${id}`;
  const close = useCallback((focus = false) => {
    if (focus && sel) buttons.current.get(sel)?.focus();
    setSel(null);
    setPos(null);
  }, [sel]);

  const place = useCallback(() => {
    if (!sel || !board.current || !card.current) return;
    const btn = buttons.current.get(sel);
    if (!btn) return;
    const b = board.current.getBoundingClientRect();
    const r = btn.getBoundingClientRect();
    const w = card.current.offsetWidth;
    const h = card.current.offsetHeight;
    const gap = 8;
    const cx = r.left + r.width / 2 - b.left;
    const left = Math.max(4, Math.min(cx - w / 2, b.width - w - 4));
    const below = r.bottom - b.top + gap;
    const above = below + h > b.height && r.top - b.top - gap - h >= 0;
    setPos({ left, top: above ? r.top - b.top - gap - h : below, above, arrow: Math.max(14, Math.min(cx - left, w - 14)) });
  }, [sel]);

  useLayoutEffect(place, [place]);

  useEffect(() => {
    if (!sel) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (card.current?.contains(t)) return;
      if ((t as Element).closest?.("[data-player]")) return; // the player's own click opens or switches the card
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close(true);
    };
    const ro = new ResizeObserver(() => place());
    if (board.current) ro.observe(board.current);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      ro.disconnect();
    };
  }, [sel, close, place]);

  const player = sel ? sides.flatMap((s) => s.players.map((p) => ({ s, p }))).find(({ s, p }) => key(s.side, p.id) === sel) : undefined;
  const c = player ? cards[player.p.id] : undefined;

  return (
    <div className="pitch-board" ref={board}>
      <div className={`pitch${sel ? " pitch-has-sel" : ""}`} role="group" aria-label={label}>
        {children}
        {sides.map((s) => (
          <ul key={s.side} className={`pitch-team pitch-${s.side}${s.probable ? " pitch-probable" : ""}`} aria-label={`${s.team}, ${s.label}`}>
            {s.players.map((p) => {
              const k = key(s.side, p.id);
              return (
                <li key={p.id} className={`${p.flag ? "pitch-flag " : ""}${sel === k ? "is-sel" : ""}`} style={{ "--x": p.x, "--y": p.y } as React.CSSProperties}>
                  <button
                    type="button"
                    data-player=""
                    ref={(el) => {
                      if (el) buttons.current.set(k, el);
                      else buttons.current.delete(k);
                    }}
                    aria-expanded={sel === k}
                    aria-controls={sel === k ? "player-card" : undefined}
                    onClick={() => {
                      if (sel === k) close();
                      else { setPos(null); setSel(k); }
                    }}
                  >
                    <span className="shirt" aria-hidden="true">
                      <span className="shirt-role">{p.shirt}</span>
                      {p.prob && <span className="shirt-p">{p.prob.replace("%", "")}</span>}
                    </span>
                    <span className="pname">{p.name}</span>
                    {(p.prob || p.flag) && <span className="pprob">{[p.prob, p.flag].filter(Boolean).join(" · ")}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        ))}
      </div>
      {player && (
        <div
          id="player-card"
          ref={card}
          className={`player-card${pos?.above ? " is-above" : ""}`}
          role="dialog"
          aria-label={`Statistiche di ${player.p.name}`}
          style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden", "--arrow": `${pos?.arrow ?? 0}px` } as React.CSSProperties}
        >
          <div className="player-card-head">
            <span className={`pitch-dot pitch-dot-${player.s.side}`} aria-hidden="true" />
            <div>
              <b>{player.p.name}</b>
              <span className="sub">{[ROLE_NAME[player.p.role], player.s.team, player.p.prob ? `titolare ${player.p.prob}` : null, player.p.flag].filter(Boolean).join(" · ")}</span>
            </div>
            <button type="button" className="player-card-x" onClick={() => close(true)} aria-label="Chiudi">
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          {!c ? (
            <p className="muted">Statistiche non disponibili: troppo pochi minuti giocati nelle partite che abbiamo (FotMob, ultimi 13 mesi).</p>
          ) : c.gk ? (
            <>
              <dl className="player-card-stats">
                <div><dt>TS</dt><dd>{num(c.ts)}</dd></div>
                <div><dt>GS</dt><dd>{num(c.gc)}</dd></div>
                <div><dt>PP</dt><dd>{num(c.sv)}</dd></div>
                <div><dt>CG</dt><dd>{pc(c.cg)}</dd></div>
              </dl>
              <p className="note">
                In questa partita, da {c.n} presenze e dall&apos;attacco avversario{c.cs != null ? `; porta inviolata: ${pc(c.cs)}` : ""}. TS tiri in porta subiti ·
                GS gol subiti · PP parate · CG probabilità di cartellino.
              </p>
            </>
          ) : (
            <>
              <dl className="player-card-stats">
                <div><dt>xG</dt><dd>{num(c.xg)}</dd></div>
                <div><dt>TP/TT</dt><dd>{num(c.sot)}/{num(c.sh, 1)}</dd></div>
                <div><dt>FF/FS</dt><dd>{num(c.fc, 1)}/{num(c.fd, 1)}</dd></div>
                <div><dt>CG</dt><dd>{pc(c.cg)}</dd></div>
                <div><dt>AS</dt><dd>{pc(c.as)}</dd></div>
                <div><dt>MIN</dt><dd>{c.min != null ? `${c.min}'` : "–"}</dd></div>
              </dl>
              <p className="note">
                A partita da titolare, ultime {c.n} presenze. TP tiri in porta · TT tiri totali · FF falli fatti · FS falli subiti · CG probabilità di cartellino ·
                AS probabilità di assist · MIN minuti giocati da titolare.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
