// The players of the My Combo (lib/combo.ts): who can play (official XI, else our probable lineup with his chance of starting,
// lib/playermarkets.ts playerPool) with his published card (playercard.py) and his goals per start (scorers.py), turned into
// what Sisal's DUO rule pays on: his slot over the whole match, the player or whoever replaces him.
import type { ComboPlayer } from "./combo";
import { nbMean } from "./combo";
import { duoSlot, type PmData } from "./playermarkets";

type ScorerRow = { n: string; s: number; a: number; as?: number };
type Scorers = Record<string, { players: ScorerRow[] }>;

const key = (n: string) => n.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
export const MAX_COMBO_PLAYERS = 16; // per team, most likely starters first
const MIN_START = 0.3;

/** p: the published anytime probability in a start; the goals per start behind it (Poisson). */
const goalsPerStart = (p: number) => -Math.log(1 - Math.min(p, 0.95));

export function comboPlayers(home: string, away: string, xgHome: number, xgAway: number, pm: PmData, scorers: Scorers): ComboPlayer[] {
  const out: ComboPlayer[] = [];
  for (const [team, t, xg] of [[home, 1, xgHome], [away, 2, xgAway]] as const) {
    const sc = new Map((scorers[team]?.players ?? []).map((p) => [key(p.n), p] as const));
    const players = [...(pm[team]?.players ?? [])]
      .filter((p) => !p.c.gk && p.s >= MIN_START)
      .sort((a, b) => b.s - a.s)
      .slice(0, MAX_COMBO_PLAYERS);
    for (const p of players) {
      const c = p.c;
      const whole = duoSlot(c.min); // DUO: his slot over the whole match
      const row = sc.get(key(p.n));
      // goals in a start: the scorers table's anytime-if-he-starts (as), else his card's expected goals
      const g = row ? goalsPerStart(row.as ?? (row.s >= 1 ? row.a : row.a / Math.max(row.s, 0.01))) : (c.xg ?? 0);
      const assistsPerStart = nbMean(c.as ?? 0);
      out.push({
        pid: p.id, name: p.n, team: t, start: p.s,
        share: xg > 0 ? Math.min(0.9, (g * whole) / xg) : 0,
        assist: xg > 0 ? Math.min(0.9, (assistsPerStart * whole) / xg) : 0,
        sot: (c.sot ?? 0) * whole, sh: c.sh ?? 0, card: nbMean(c.cg ?? 0), // total shots and bookings: not DUO, his own minutes
        fc: c.fc ?? 0, fd: c.fd ?? 0,
      });
    }
  }
  return out;
}
