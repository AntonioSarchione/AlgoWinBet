// Slip profiles shown side by side (Home and manual slips): every profile runs the optimizer with its own weights, and the
// three cards never show the same slip twice.
import type { OppRow } from "./db";
import { optimize, withProfile, type OptimizerCfg, type OptOpp, type OptResult, type OptSettings } from "./optimizer";

export const PROFILE_LABEL: Record<string, string> = { probabilita: "Massima probabilità", equilibrata: "Equilibrata", value: "Value" };
export const PROFILE_HINT: Record<string, string> = {
  probabilita: "la schedina più probabile tra quelle con valore",
  equilibrata: "equilibrio tra probabilità e valore atteso",
  value: "il valore atteso più alto, anche se meno probabile",
};

export type Cand = OptOpp & OppRow;
export type ProfileResult = OptResult<Cand> & { sameAs?: string }; // sameAs: its only slips are those of an earlier profile

// published opportunity rows -> optimizer legs (columns missing on older analyses get neutral values)
export function toLegs(rows: OppRow[]): Cand[] {
  return rows.map((o) => ({
    ...o, sel_key: o.sel_key ?? "", home: o.home ?? "", away: o.away ?? "", p_struct: o.p_struct ?? o.p_final,
    score: o.score ?? 0, disagreement: o.disagreement ?? 0, dq_lineup: o.dq_lineup ?? 0,
  }));
}

const slipKey = (sl: { legs: Cand[] }) => sl.legs.map((l) => `${l.fixture_id}|${l.sel_key}`).sort().join(" + ");

/** Slips of every profile with `overrides` on the published optimizer settings. The published profile picks first; a
 * profile whose best slip is one already shown says so ("sameAs") instead of showing a worse one, and its other slips skip
 * the ones already shown. Returned in the configured profile order. */
export function runProfiles(legs: Cand[], settings: OptSettings, overrides: Partial<OptimizerCfg>): Record<string, ProfileResult> {
  const names = settings.optimizer.profiles ? Object.keys(settings.optimizer.profiles) : ["equilibrata"];
  const main = names.includes(settings.optimizer.profile ?? "") ? (settings.optimizer.profile as string) : names[0];
  const shown = new Map<string, string>(); // slip key -> profile showing it
  const out: Record<string, ProfileResult> = {};
  for (const name of [main, ...names.filter((n) => n !== main)]) {
    const r: ProfileResult = optimize(legs, { ...settings, optimizer: withProfile({ ...settings.optimizer, ...overrides }, name) });
    const top = r.slips[0];
    // its best slip already shown by another profile: say so, never fall back to a worse one under this profile's name
    // ("Massima probabilità" showing a less likely slip than «Equilibrata» because the most likely one is «Equilibrata»'s)
    if (top && shown.has(slipKey(top))) {
      r.sameAs = shown.get(slipKey(top));
      const other = r.sameAs ?? "";
      r.slips = [];
      r.noBet = true;
      r.reasons = [`Con questi filtri la migliore schedina di questo profilo è la stessa di «${PROFILE_LABEL[other] ?? other}».`];
    } else {
      r.slips = r.slips.filter((sl) => !shown.has(slipKey(sl)));
    }
    if (r.slips[0]) shown.set(slipKey(r.slips[0]), name);
    out[name] = r;
  }
  return Object.fromEntries(names.map((n) => [n, out[n]]));
}
