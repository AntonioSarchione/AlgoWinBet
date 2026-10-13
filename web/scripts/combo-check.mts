// Parity of the My Combo engine (lib/combo.ts) with the Python model on one match: node --experimental-strip-types
// scripts/combo-check.mts <reference json> [<structural reference json>] (written by the Python side: count-model params and
// reference probabilities; the structural one: shots and corners given the goals, brute force).
import { readFileSync } from "node:fs";
import { buildModel, comboSelections, evaluateCombo, generateCombo, jointModel, type ComboPlayer } from "../lib/combo.ts";

const ref = JSON.parse(readFileSync(process.argv[2], "utf8"));
const players: ComboPlayer[] = [
  { pid: "p1", name: "Punta casa", team: 1, start: 0.95, share: 0.3, assist: 0.12, sot: 1.4, sh: 3.2, card: 0.12, fc: 1.1, fd: 1.2 },
  { pid: "p2", name: "Mezzala casa", team: 1, start: 0.9, share: 0.12, assist: 0.15, sot: 0.6, sh: 1.5, card: 0.2, fc: 1.3, fd: 1.0 },
  { pid: "p3", name: "Punta ospite", team: 2, start: 0.9, share: 0.35, assist: 0.1, sot: 0.9, sh: 2.1, card: 0.15, fc: 1.0, fd: 1.4 },
];
const model = buildModel(ref.xg[0], ref.xg[1], ref.rho, { h1: 0.45, stats: ref.stats }, players);
const sels = comboSelections(model, (t) => (t === 1 ? "casa" : "ospite"));
const by = new Map(sels.map((s) => [s.key, s]));
let bad = 0;
const struct: Record<string, number> = process.argv[3] ? JSON.parse(readFileSync(process.argv[3], "utf8")) : {};
for (const [k, p] of Object.entries(ref.ref) as [string, number][]) {
  if (k in struct) continue; // given the goals: checked against the structural reference below
  const s = by.get(k);
  if (!s) { console.log(`manca ${k}`); bad++; continue; }
  const q = jointModel(model, [s]);
  const tol = k.includes("@H1") || k.startsWith("HT_FT") || k.startsWith("FIRST_GOAL") ? 0.03 : 0.002; // halves: binomial split of the DC grid vs Python's Poisson halves
  const ok = Math.abs(q - p) <= tol;
  if (!ok) bad++;
  console.log(`${ok ? "ok " : "NO "} ${k.padEnd(28)} python ${p.toFixed(4)}  ts ${q.toFixed(4)}`);
}
for (const [k, p] of Object.entries(struct)) {
  const q = jointModel(model, k.split(" + ").map((x) => by.get(x)!));
  const ok = Math.abs(q - p) <= 0.003;
  if (!ok) bad++;
  console.log(`${ok ? "ok " : "NO "} ${k.padEnd(60)} python ${p.toFixed(4)}  ts ${q.toFixed(4)}`);
}
const tot = model.cells.reduce((x, c) => x + c.w, 0);
console.log(`celle ${model.cells.length}, somma pesi ${tot.toFixed(6)}`);
if (Math.abs(tot - 1) > 1e-6) bad++;
// players: the share of each player's goals is coherent with the goals (no goal for the team, no goal for him)
const pick = (...ks: string[]) => ks.map((k) => by.get(k)!);
const pGoal = jointModel(model, pick("PLAYER|p1|goal|1"));
const pGoal00 = jointModel(model, pick("PLAYER|p1|goal|1", "TEAM_TOTAL_HOME|UNDER|0.5"));
const both = jointModel(model, pick("PLAYER|p1|goal|1", "PLAYER|p2|goal|1"));
const one = jointModel(model, pick("PLAYER|p1|goal|1", "PLAYER|p2|goal|1", "TEAM_TOTAL_HOME|UNDER|1.5"));
const first = jointModel(model, pick("PLAYER|p1|first|1")) + jointModel(model, pick("PLAYER|p2|first|1"));
console.log(`p1 segna ${pGoal.toFixed(4)}; con casa a zero ${pGoal00.toExponential(2)}; p1 e p2 ${both.toFixed(4)}; p1 e p2 con casa under 1.5 ${one.toExponential(2)}; primo marcatore p1+p2 ${first.toFixed(4)} (casa segna per prima ${ref.ref["FIRST_GOAL|HOME|"].toFixed(4)})`);
if (pGoal00 > 1e-12 || one > 1e-12 || first >= ref.ref["FIRST_GOAL|HOME|"]) bad++;
const r = evaluateCombo(model, pick("MATCH_1X2|HOME|", "CORNERS_TOTAL|OVER|9.5", "PLAYER|p1|goal_or_assist|1", "SHOTS_ON_TOTAL|OVER|8.5", "SHOTS_TOTAL|OVER|24.5"), {}, {}, 4.0);
console.log(`esempio: p ${r.p.toFixed(4)}, quota equa ${r.fair?.toFixed(2)}, quota stimata ${r.estOdds?.toFixed(2)}, EV a 4.00 ${r.ev?.toFixed(3)}`);
for (const [lo, hi] of [[1.8, 2.5], [3, 4], [6, 9]]) {
  const t0 = performance.now();
  const gen = generateCombo(model, sels, {}, {}, lo, hi);
  console.log(`genera ${lo}-${hi}: ${(performance.now() - t0).toFixed(0)} ms -> ${gen ? gen.sels.map((s) => `${s.group}: ${s.label}`).join(" + ") + ` p ${gen.r.p.toFixed(3)} quota stimata ${gen.r.estOdds?.toFixed(2)}` : "nessuna"}`);
}
process.exit(bad ? 1 : 0);
