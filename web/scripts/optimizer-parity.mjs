// Parity check: web/lib/optimizer.ts must reproduce the Python optimizer on every golden case
// (scripts/optimizer_golden.py). Run from web/: node scripts/optimizer-parity.mjs  (Node >= 22.18 runs the .ts directly)
import { readFileSync } from "node:fs";
import { optimize, withProfile } from "../lib/optimizer.ts";

const golden = JSON.parse(readFileSync(new URL("./optimizer.golden.json", import.meta.url), "utf8"));
const close = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b));
let failures = 0;
for (const c of golden.cases) {
  const s = structuredClone(golden.settings);
  if (c.profile) s.optimizer = withProfile(s.optimizer, c.profile);
  if (c.edge_shrink != null) s.optimizer.edge_shrink = c.edge_shrink;
  s.optimizer.max_legs = c.max_legs;
  if (c.odds_min) s.optimizer.odds_min = c.odds_min;
  if (c.odds_max) s.optimizer.odds_max = c.odds_max;
  if (c.min_slip_ev != null) s.optimizer.min_slip_ev = c.min_slip_ev;
  if (c.min_legs != null) s.optimizer.min_legs = c.min_legs;
  if (c.national_competitions) s.optimizer.national_competitions = c.national_competitions;
  if (c.multi_bonus_min_odds != null) s.optimizer.multi_bonus_min_odds = c.multi_bonus_min_odds;
  const r = optimize(golden.opportunities, s);
  const problems = [];
  if (r.noBet !== c.no_bet) problems.push(`no_bet ${r.noBet} != ${c.no_bet}`);
  if (r.slips.length !== c.slips.length) problems.push(`${r.slips.length} schedine invece di ${c.slips.length}`);
  c.slips.forEach((e, k) => {
    const g = r.slips[k];
    if (!g) return;
    const keys = (legs) => legs.map((l) => l.join("|")).sort().join(" + ");
    if (keys(g.legs.map((l) => [l.fixture_id, l.sel_key])) !== keys(e.legs)) problems.push(`schedina ${k + 1}: eventi diversi`);
    for (const f of ["total_odds", "joint_probability", "ev", "ev_lower", "objective", "bonus"]) {
      if (!close(g[f], e[f])) problems.push(`schedina ${k + 1}: ${f} ${g[f]} != ${e[f]}`);
    }
    if (Math.abs(g.stake - e.stake) > 0.011) problems.push(`schedina ${k + 1}: stake ${g.stake} != ${e.stake}`);
  });
  console.log(`${problems.length ? "FAIL" : "ok  "} ${c.name}: ${r.slips.length} schedine${problems.length ? "\n  " + problems.join("\n  ") : ""}`);
  failures += problems.length;
}
process.exit(failures ? 1 : 0);
