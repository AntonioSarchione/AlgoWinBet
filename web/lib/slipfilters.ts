// Filters shared by the home page and the manual slip (/schedina).
import { FREE_MAX_LEGS } from "./freeslip";

// the slips never take a selection under the published floor (40%, user's rule): "" = that floor
export const LEG_PROB = [{ v: "", l: "Regola (almeno 40%)" }, ...[45, 50, 60, 70, 80].map((k) => ({ v: String(k), l: `Almeno ${k}%` }))];

// "maximum risk" = the lowest chance of winning the slip that is still accepted
export const RISK = [
  { v: "", l: "Nessun limite" },
  { v: "50", l: "Basso · vince ≥ 50%" },
  { v: "30", l: "Medio · vince ≥ 30%" },
  { v: "15", l: "Alto · vince ≥ 15%" },
  { v: "5", l: "Molto alto · vince ≥ 5%" },
];

// number of legs from the URL: 1 to 20, else the default
export const legCount = (v: string | undefined, d: number) => {
  const n = Math.round(Number(v));
  return Number.isFinite(n) && n >= 1 ? Math.min(n, FREE_MAX_LEGS) : d;
};
