// "Senza regole" slip (home page filter): the model's rules are off (status, EV, leg probability floor, risk, markets,
// competitions). Only the user's limits count: total odds range, leg odds range, period, number of legs; plus the two rules
// the user keeps everywhere: leg odds >= 1.25 and never national teams together with clubs.
//
// Every playable Sisal selection of every match in the period (pub_book) is a candidate, one leg per match. A leg is worth
// ln p + BETA * ln odds: the probability weighs most, the odds a little ("equilibrio con più peso alla probabilità"). The
// slip maximising the sum of the leg scores is exact: a dynamic programme over matches x number of legs x ln(total odds)
// in steps of STEP. Adding a leg always costs score (p * odds^BETA < 1 for every price on offer), so with no minimum on
// the legs the search keeps the slip short and likely, as the user chose ("massima probabilità della schedina").
import { MIN_LEG_ODDS, NATIONAL_COMPETITIONS } from "./optimizer";

export type FreeOpt = { fixture_id: string; competition: string; kickoff: string; match: string; market: string; odds: number; p: number; p_market: number | null; bookmaker: string };
export type FreeLimits = { qMin: number; qMax: number; lMin: number; lMax: number; nMin: number; nMax: number };
export type FreeSlip = { legs: FreeOpt[]; odds: number; p: number; ev: number };
export type FreeResult = { slip: FreeSlip | null; matches: number; options: number; reason: string | null };

export const FREE_MAX_LEGS = 20;
const BETA = 0.3;
const STEP = 0.005; // ln-odds bucket: the bounds are checked again on the exact product
const PER_MATCH_SCORE = 4; // best options per match by score...
const PER_MATCH_ODDS = 2; // ...plus the highest prices, for a high minimum total odds

const score = (x: FreeOpt) => Math.log(x.p) + BETA * Math.log(x.odds);
const isNational = (comp: string) => NATIONAL_COMPETITIONS.some((k) => comp.toLowerCase().includes(k.toLowerCase()));

export function freeSlip(all: FreeOpt[], k: FreeLimits): FreeResult {
  const lMin = Math.max(k.lMin || 0, MIN_LEG_ODDS);
  const lMax = k.lMax || Infinity;
  const ok = all.filter((x) => x.p > 0 && x.p < 1 && x.odds >= lMin && x.odds <= lMax);
  const byMatch = new Map<string, FreeOpt[]>();
  for (const x of ok) byMatch.set(x.fixture_id, [...(byMatch.get(x.fixture_id) ?? []), x]);
  const groups = [...byMatch.values()].map((lst) => {
    const keep = new Set([...lst].sort((a, b) => score(b) - score(a)).slice(0, PER_MATCH_SCORE));
    for (const x of [...lst].sort((a, b) => b.odds - a.odds).slice(0, PER_MATCH_ODDS)) keep.add(x);
    return [...keep];
  });
  const base = { matches: groups.length, options: ok.length };
  if (!groups.length) return { slip: null, ...base, reason: "Nessuna selezione Sisal nel range di quota per evento e nel periodo." };
  // national teams and clubs never in the same slip: the best of the two searches
  const best = [false, true]
    .map((nat) => search(groups.filter((g) => isNational(g[0].competition) === nat), k))
    .filter((s): s is { slip: FreeSlip; score: number } => s != null)
    .sort((a, b) => b.score - a.score)[0];
  if (!best) {
    return { slip: null, ...base, reason: `Nessuna combinazione da ${k.nMin} a ${k.nMax} eventi con quota totale ${bounds(k)}: allarga i range o il periodo.` };
  }
  return { slip: best.slip, ...base, reason: null };
}

const bounds = (k: FreeLimits) => (k.qMax ? `tra ${(k.qMin || 1).toFixed(2)} e ${k.qMax.toFixed(2)}` : `di almeno ${(k.qMin || 1).toFixed(2)}`);

function search(groups: FreeOpt[][], k: FreeLimits): { slip: FreeSlip; score: number } | null {
  const nMax = Math.min(k.nMax, groups.length);
  if (nMax < k.nMin) return null;
  const lo = Math.log(Math.max(k.qMin || 1, 1));
  const hi = k.qMax ? Math.log(k.qMax) : Infinity;
  if (hi < lo) return null;
  // buckets: floor(ln odds / STEP). With an upper bound, a sum past it is dropped; without, sums saturate at the lower
  // bound's bucket (past it only "enough" matters). Two paths in one cell keep the better score: near a bound this can
  // miss a feasible slip by less than one bucket (0.5% of the total odds).
  const cap = Number.isFinite(hi) ? Math.floor(hi / STEP) : Math.ceil(lo / STEP);
  const B = cap + 1;
  const W = (nMax + 1) * B;
  let cur = new Float64Array(W).fill(-Infinity);
  let curSum = new Float64Array(W); // exact ln(total odds) of the best path in the cell
  cur[0] = 0;
  const choice: Int8Array[] = []; // per match: option taken into the cell (-1 = match skipped)
  const prev: Int32Array[] = []; // per match: cell it came from
  for (const g of groups) {
    const next = Float64Array.from(cur);
    const nextSum = Float64Array.from(curSum);
    const ch = new Int8Array(W).fill(-1);
    const pv = new Int32Array(W);
    const lnOdds = g.map((x) => Math.log(x.odds));
    const sc = g.map(score);
    for (let c = 0; c < nMax; c++) {
      for (let b = 0; b < B; b++) {
        const from = c * B + b;
        const s0 = cur[from];
        if (s0 === -Infinity) continue;
        for (let i = 0; i < g.length; i++) {
          const ex = curSum[from] + lnOdds[i];
          if (ex > hi + 1e-12) continue;
          const to = (c + 1) * B + Math.min(Math.floor(ex / STEP), cap);
          const s = s0 + sc[i];
          if (s > next[to]) {
            next[to] = s;
            nextSum[to] = ex;
            ch[to] = i;
            pv[to] = from;
          }
        }
      }
    }
    choice.push(ch);
    prev.push(pv);
    cur = next;
    curSum = nextSum;
  }
  let bestCell = -1;
  for (let c = Math.max(k.nMin, 1); c <= nMax; c++) {
    for (let b = 0; b < B; b++) {
      const cell = c * B + b;
      if (cur[cell] === -Infinity || curSum[cell] < lo - 1e-12 || curSum[cell] > hi + 1e-12) continue;
      if (bestCell < 0 || cur[cell] > cur[bestCell]) bestCell = cell;
    }
  }
  if (bestCell < 0) return null;
  // walk back: a match whose choice in this cell is -1 was skipped (the cell kept its previous value)
  const legs: FreeOpt[] = [];
  let cell = bestCell;
  for (let m = groups.length - 1; m >= 0; m--) {
    const i = choice[m][cell];
    if (i < 0) continue;
    legs.push(groups[m][i]);
    cell = prev[m][cell];
  }
  legs.sort((a, b) => a.kickoff.localeCompare(b.kickoff));
  const odds = legs.reduce((a, x) => a * x.odds, 1);
  const p = legs.reduce((a, x) => a * x.p, 1);
  return { slip: { legs, odds, p, ev: p * odds - 1 }, score: cur[bestCell] };
}
