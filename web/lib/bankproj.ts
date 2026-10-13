// Bankroll projection (Fase 9-bis): where the balance could be after 1-3 months with a staking plan, starting today from the
// plan's capital. Monte Carlo: every day a number of slips at the pace of the slips a bettor would hold (Poisson around their
// slips per day), each one drawn from those same slips (its price with the bonus, the model's probability and how many days
// its money stayed in play, from recording to its last match), staked with the plan
// (lib/bankroll.ts stakeFor) on the balance, money in play included, and paid back the day its last match is over. A share
// of slips is refunded, as many as in the Registro so far. Three ways of deciding whether a slip wins:
//   modello   with the model's probability: what the plan expects if the model is right;
//   storico   the model's probability times the Registro's wins / expected wins so far, pulled toward 1 while the settled
//             slips are few (PRIOR expected wins of weight);
//   pareggio  with 1 / price: no edge at all, the balance only moves by luck and the plan's stakes.
// Paper only: a projection, not a promise. The same seed every time, so a page reload does not move the numbers.
import type { BankSlip } from "./db";
import { effectiveOdds, stakeFor, type Plan } from "./bankroll";

export type Scenario = "modello" | "storico" | "pareggio";
export const SCENARIOS: Scenario[] = ["modello", "storico", "pareggio"];
export const SCENARIO: Record<Scenario, { label: string; hint: string }> = {
  modello: { label: "Se il modello ha ragione", hint: "ogni schedina vince con la probabilità del modello" },
  storico: { label: "Come finora", hint: "probabilità del modello corretta da quante schedine hanno vinto finora" },
  pareggio: { label: "Nessun vantaggio", hint: "ogni schedina vince con 1 / quota: guadagno atteso zero" },
};
export const HORIZONS = [30, 60, 90];
export const PATHS = 2000;
export const QS = [0.05, 0.25, 0.5, 0.75, 0.95];
const PRIOR = 200; // expected wins of weight pulling the "storico" correction toward 1: a few weeks of luck must not move it

export type ProjInput = {
  pool: BankSlip[]; delays: number[]; perDay: number; settled: number; wins: number; expected: number; calib: number; refundRate: number;
};
const DAY = 86_400_000;
const SETTLE_AFTER = 2.5 * 3600_000; // from the last kickoff: the match is over

/** pool: the recorded slips the plan would hold (lib/criterion.ts independentSlips): what a slip looks like and how many a day. */
export function projectionInput(pool: BankSlip[]): ProjInput | null {
  if (pool.length < 10) return null;
  const ts = pool.map((s) => new Date(s.created_at).getTime());
  const days = Math.max(1, (Math.max(...ts) - Math.min(...ts)) / DAY + 1);
  const done = pool.filter((s) => s.result === "won" || s.result === "lost");
  const refunded = pool.filter((s) => s.result === "void" || s.result === "non valutabile").length;
  const wins = done.filter((s) => s.result === "won").length;
  const expected = done.reduce((a, s) => a + s.joint, 0);
  // whole days between recording and the day the last match is over (0: paid back the same day)
  const delays = pool.map((s) => {
    const placed = new Date(s.created_at).getTime();
    const over = new Date(s.last_kickoff).getTime() + SETTLE_AFTER;
    return Math.max(0, Math.floor(over / DAY) - Math.floor(placed / DAY));
  });
  return {
    pool, delays, perDay: pool.length / days, settled: done.length, wins, expected, calib: (wins + PRIOR) / (expected + PRIOR),
    refundRate: done.length + refunded ? refunded / (done.length + refunded) : 0,
  };
}

export type Projection = {
  scenario: Scenario;
  days: number;
  final: number[]; // QS quantiles of the final balance
  mean: number;
  pLoss: number; // final balance below the capital
  pHalf: number; // at some point half the capital or less
  ddMedian: number; // median of the largest fall from a peak, share of the peak
  betsPerPath: number;
  avgWinProb: number; // mean win probability of the pool under this scenario
  bands: number[][]; // per day 0..days: QS quantiles of the balance
};

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function poisson(lambda: number, r: () => number): number {
  const l = Math.exp(-lambda);
  let k = 0;
  let p = r();
  while (p > l) {
    k++;
    p *= r();
  }
  return k;
}

function quantiles(xs: Float64Array): number[] {
  const s = Float64Array.from(xs).sort();
  return QS.map((q) => s[Math.min(s.length - 1, Math.floor(q * s.length))]);
}

export function project(inp: ProjInput, plan: Plan, days: number, scenario: Scenario, paths = PATHS): Projection {
  const n = inp.pool.length;
  const odds = inp.pool.map(effectiveOdds);
  const prob = inp.pool.map((s, i) =>
    scenario === "modello" ? s.joint : scenario === "storico" ? Math.min(0.99, s.joint * inp.calib) : 1 / odds[i]);
  const r = mulberry32(20261013);
  const traj = Array.from({ length: days + 1 }, () => new Float64Array(paths));
  const maxDelay = Math.max(0, ...inp.delays);
  const due = new Float64Array(days + maxDelay + 2); // paid back on that day, reset every path
  const inPlay = new Float64Array(days + maxDelay + 2); // stakes whose slip is over on that day
  const finals = new Float64Array(paths);
  const dds = new Float64Array(paths);
  let half = 0;
  let bets = 0;
  for (let k = 0; k < paths; k++) {
    due.fill(0);
    inPlay.fill(0);
    let cash = plan.start, open = 0, peak = plan.start, dd = 0, low = plan.start;
    let eq = plan.start;
    traj[0][k] = eq;
    for (let d = 1; d <= days; d++) {
      const m = poisson(inp.perDay, r);
      for (let j = 0; j < m; j++) {
        const i = Math.floor(r() * n);
        const stake = Math.min(stakeFor(inp.pool[i], cash + open, plan), Math.floor(cash * 100) / 100);
        const u = r();
        if (stake < plan.minStake) continue;
        cash -= stake;
        open += stake;
        bets++;
        const at = d + inp.delays[i];
        inPlay[at] += stake;
        if (u < inp.refundRate) due[at] += stake;
        else if ((u - inp.refundRate) / (1 - inp.refundRate) < prob[i]) due[at] += Math.round(stake * odds[i] * 100) / 100;
      }
      cash += due[d]; // the day's slips are over: paid back, their stakes leave the money in play
      open -= inPlay[d];
      eq = cash + open;
      peak = Math.max(peak, eq);
      dd = Math.max(dd, peak > 0 ? (peak - eq) / peak : 0);
      low = Math.min(low, eq);
      traj[d][k] = eq;
    }
    finals[k] = eq;
    dds[k] = dd;
    if (low <= plan.start / 2) half++;
  }
  let loss = 0, sum = 0;
  for (const x of finals) {
    sum += x;
    if (x < plan.start) loss++;
  }
  return {
    scenario, days, final: quantiles(finals), mean: sum / paths, pLoss: loss / paths, pHalf: half / paths,
    ddMedian: quantiles(dds)[2], betsPerPath: bets / paths, avgWinProb: prob.reduce((a, p) => a + p, 0) / n,
    bands: traj.map(quantiles),
  };
}
