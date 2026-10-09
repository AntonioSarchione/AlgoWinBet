// Player markets of the Giocatori tab beyond goals: who can play (official XI, else our probable lineup with each player's
// chance of starting) with his published card (src/algowinbet/playercard.py: expected shots, shots on target, fouls committed
// and won per start, booking and assist probability), and Sisal's player prices (player_quotes) matched to our names.
import type { PlayerCardData, PlayerQuoteRow } from "./db";

export type PmPlayer = { id: string; n: string; r: string; s: number; c: PlayerCardData };
export type PmTeam = { state: "ufficiale" | "probabile"; players: PmPlayer[] };
export type PmData = Record<string, PmTeam>;
// our player name -> market key ("SCORER", "FIRST_SCORER", "TWO_PLUS", "SHOTS@1.5", "SHOTS_ON@0.5") -> Sisal price
export type SisalPrices = Record<string, Record<string, number>>;

type Lineup = { team: string; status: string; starters: string };
type ProbRow = [string, string, string, number, string | null];
type Probable = Record<string, { xi: ProbRow[]; alt: ProbRow[] }>;

const parse = <T,>(s: string | null | undefined, d: T): T => {
  try {
    return s ? (JSON.parse(s) as T) : d;
  } catch {
    return d;
  }
};

export function playerPool(teams: string[], lineups: Lineup[], probable: Probable, names: Record<string, { name: string; position: string }>,
  cards: Record<string, PlayerCardData>): PmData {
  const out: PmData = {};
  for (const team of teams) {
    const l = lineups.find((x) => x.team === team && x.status === "confirmed");
    let players: PmPlayer[] = [];
    let state: PmTeam["state"] = "probabile";
    if (l) {
      state = "ufficiale";
      players = parse<string[]>(l.starters, []).map((id) => ({ id, n: names[id]?.name ?? id.split(":").pop()!, r: names[id]?.position ?? "", s: 1, c: cards[id] }));
    } else if (probable[team]) {
      players = [...probable[team].xi, ...probable[team].alt].map(([id, n, r, p]) => ({ id, n, r, s: p, c: cards[id] }));
    }
    players = players.filter((p) => p.c && p.s > 0);
    if (players.length) out[team] = { state, players };
  }
  return out;
}

// ---- Sisal names ("Surname, Name") to ours, within the two teams of the match
const norm = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const words = (s: string) => norm(s).split(" ").filter(Boolean);

function score(ours: string[], sisal: string): number {
  const [sur, given = ""] = sisal.split(",");
  const s = words(sur);
  const g = words(given);
  if (!s.length || !s.every((t) => ours.includes(t))) return 0;
  if (g.length && !ours.some((t) => t[0] === g[0][0])) return 0;
  return g.length && g.every((t) => ours.includes(t)) ? 3 : g.length ? 2 : 1;
}

export function matchSisal(ourNames: string[], quotes: PlayerQuoteRow[]): SisalPrices {
  const ours = [...new Set(ourNames)].map((n) => ({ n, w: words(n) }));
  const byKey = new Map<string, PlayerQuoteRow[]>();
  for (const q of quotes) byKey.set(q.player_key, [...(byKey.get(q.player_key) ?? []), q]);
  const out: SisalPrices = {};
  for (const rows of byKey.values()) {
    const scored = ours.map((o) => ({ n: o.n, v: score(o.w, rows[0].player_name) })).filter((x) => x.v > 0).sort((a, b) => b.v - a.v);
    if (!scored.length || (scored[1] && scored[1].v === scored[0].v)) continue; // nobody, or two of ours equally likely: no price
    const mine = (out[scored[0].n] ??= {});
    for (const q of rows) mine[q.line_key ? `${q.market}@${q.line_key}` : q.market] = q.odds;
  }
  return out;
}

// P(count >= k) for a negative binomial count with mean mu and shape DISP (playercard.py)
export const DISP = 4;
export function pAtLeast(mu: number | undefined, k: number): number {
  if (!mu || mu <= 0) return 0;
  const q = mu / (DISP + mu);
  let pj = (DISP / (DISP + mu)) ** DISP; // P(0)
  let below = pj;
  for (let j = 1; j < k; j++) {
    pj *= ((DISP + j - 1) / j) * q;
    below += pj;
  }
  return Math.max(0, 1 - below);
}
