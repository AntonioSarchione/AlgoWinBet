// Bankroll (Fase 9-bis): what the recorded paper slips would have done to a real balance under a staking plan.
// Each slip is placed when it was recorded (stake from the balance at that moment) and pays when it was settled, so open
// slips tie up money exactly as real bets do. Paper only: the tool never places bets.
import type { BankSlip } from "./db";

export type Method = "flat" | "pct" | "kelly";
export type Plan = { start: number; method: Method; flat: number; pct: number; kelly: number; cap: number; minStake: number };

export const DEFAULT_PLAN: Plan = { start: 1000, method: "kelly", flat: 10, pct: 0.01, kelly: 0.25, cap: 0.02, minStake: 2 };

export type Bet = { slip: BankSlip; at: string; stake: number; ret: number | null; balanceAfter: number | null };
export type Skip = { slip: BankSlip; reason: string }; // recorded but not staked under this plan (still shown, with its result)
export type Point = { t: number; v: number };
export type BankResult = {
  bets: Bet[]; skipped: Skip[]; skippedLost: number; skippedWon: number; points: Point[]; equity: number; cash: number; openStake: number; staked: number; returned: number;
  profit: number; roi: number | null; won: number; lost: number; refunded: number; open: number; maxDd: number; maxDdPct: number;
  worstRun: number; avgStake: number | null; peak: number;
};

// Sisal multiple bonus on the net winnings: the price the slip actually pays
export const effectiveOdds = (s: BankSlip) => 1 + (s.total_odds - 1) * (1 + (s.bonus ?? 0));

export function stakeFor(s: BankSlip, equity: number, p: Plan): number {
  let x: number;
  if (p.method === "flat") x = p.flat;
  else if (p.method === "pct") x = equity * p.pct;
  else {
    const o = effectiveOdds(s);
    const f = (s.joint * o - 1) / (o - 1); // full Kelly fraction of the balance
    x = f > 0 ? equity * f * p.kelly : 0;
  }
  x = Math.min(x, equity * p.cap);
  x = Math.floor(x * 100) / 100;
  return x >= p.minStake ? x : 0;
}

function skipReason(s: BankSlip, equity: number, p: Plan): string {
  if (p.method !== "kelly") return `puntata sotto il minimo di ${p.minStake} €`;
  const o = effectiveOdds(s);
  const f = (s.joint * o - 1) / (o - 1);
  return f <= 0 ? "nessun vantaggio alla quota registrata" : `Kelly ${(equity * f * p.kelly).toFixed(2)} €, sotto il minimo di ${p.minStake} €`;
}

// gross return per unit staked: won = effective odds, void / non valutabile = stake back, lost = 0
function unitReturn(s: BankSlip): number | null {
  if (s.result === "won") return s.payout ?? effectiveOdds(s);
  if (s.result === "lost") return 0;
  if (s.result) return 1;
  return null;
}

export function simulate(slips: BankSlip[], p: Plan): BankResult {
  type Ev = { t: string; bet: Bet };
  let cash = p.start, open = 0, staked = 0, returned = 0;
  const skipped: Skip[] = [];
  const bets: Bet[] = [];
  const placements = [...slips].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id);
  // placements and settlements interleave in time: walk them together (a settlement at the same instant comes first)
  const queue: Ev[] = placements.map((s) => ({ t: s.created_at, bet: { slip: s, at: s.created_at, stake: 0, ret: null, balanceAfter: null } }));
  const pending: Ev[] = [];
  const points: Point[] = queue.length ? [{ t: new Date(queue[0].t).getTime(), v: p.start }] : [];
  let peak = p.start, maxDd = 0, maxDdPct = 0, run = 0, worstRun = 0, won = 0, lost = 0, refunded = 0;
  const settle = (e: Ev) => {
    const r = unitReturn(e.bet.slip) as number;
    const back = Math.round(e.bet.stake * r * 100) / 100;
    cash += back;
    open -= e.bet.stake;
    returned += back;
    e.bet.ret = back;
    const equity = cash + open;
    e.bet.balanceAfter = equity;
    points.push({ t: new Date(e.t).getTime(), v: equity });
    if (e.bet.slip.result === "won") (won++, (run = 0));
    else if (e.bet.slip.result === "lost") (lost++, run++, (worstRun = Math.max(worstRun, run)));
    else refunded++;
    peak = Math.max(peak, equity);
    maxDd = Math.max(maxDd, peak - equity);
    maxDdPct = Math.max(maxDdPct, peak > 0 ? (peak - equity) / peak : 0);
  };
  for (const e of queue) {
    // first every settlement that happened before this placement
    pending.sort((a, b) => a.t.localeCompare(b.t));
    while (pending.length && pending[0].t <= e.t) settle(pending.shift()!);
    const want = stakeFor(e.bet.slip, cash + open, p);
    const stake = Math.min(want, Math.floor(cash * 100) / 100);
    if (stake < p.minStake) {
      skipped.push({ slip: e.bet.slip, reason: want > 0 ? "saldo libero insufficiente" : skipReason(e.bet.slip, cash + open, p) });
      continue;
    }
    e.bet.stake = stake;
    cash -= stake;
    open += stake;
    staked += stake;
    bets.push(e.bet);
    const r = unitReturn(e.bet.slip);
    if (r != null && e.bet.slip.settled_at) pending.push({ t: e.bet.slip.settled_at, bet: e.bet });
  }
  pending.sort((a, b) => a.t.localeCompare(b.t));
  for (const e of pending.splice(0)) settle(e);
  const settledStake = bets.filter((b) => b.ret != null).reduce((a, b) => a + b.stake, 0);
  const settledReturn = bets.filter((b) => b.ret != null).reduce((a, b) => a + (b.ret as number), 0);
  return {
    bets, skipped, skippedLost: skipped.filter((x) => x.slip.result === "lost").length,
    skippedWon: skipped.filter((x) => x.slip.result === "won").length, points, equity: cash + open, cash, openStake: open, staked, returned, profit: settledReturn - settledStake,
    roi: settledStake > 0 ? (settledReturn - settledStake) / settledStake : null, won, lost, refunded,
    open: bets.filter((b) => b.ret == null).length, maxDd, maxDdPct, worstRun,
    avgStake: bets.length ? staked / bets.length : null, peak,
  };
}
