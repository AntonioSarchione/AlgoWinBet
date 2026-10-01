// Italian names of the market codes stored in `quotes` (see src/algowinbet/markets.py), in the order the odds tab lists them.
// Half-time markets carry a period suffix ("TOTAL_GOALS@H1"); markets over both halves have their own codes.
export const MARKET_NAMES: Record<string, string> = {
  MATCH_1X2: "1X2",
  DOUBLE_CHANCE: "Doppia chance",
  DRAW_NO_BET: "Draw no bet",
  TOTAL_GOALS: "Under/Over",
  BTTS: "Gol/NoGol",
  TEAM_TOTAL_HOME: "Under/Over casa",
  TEAM_TOTAL_AWAY: "Under/Over ospite",
  EURO_HANDICAP: "Handicap europeo",
  ASIAN_HANDICAP: "Handicap asiatico",
  ODD_EVEN: "Pari/Dispari",
  TEAM_ODD_EVEN_HOME: "Pari/Dispari casa",
  TEAM_ODD_EVEN_AWAY: "Pari/Dispari ospite",
  TOTAL_EXACT: "Gol totali esatti",
  TEAM_EXACT_HOME: "Gol esatti casa",
  TEAM_EXACT_AWAY: "Gol esatti ospite",
  CORRECT_SCORE: "Risultato esatto",
  WINNING_MARGIN: "Margine di vittoria",
  WIN_TO_NIL_HOME: "Casa vince a zero",
  WIN_TO_NIL_AWAY: "Ospite vince a zero",
  FIRST_GOAL: "Primo gol",
  LAST_GOAL: "Ultimo gol",
  HT_FT: "Parziale/Finale",
  HIGHEST_HALF: "Tempo con più gol",
  HIGHEST_HALF_HOME: "Tempo con più gol casa",
  HIGHEST_HALF_AWAY: "Tempo con più gol ospite",
  SCORE_BOTH_HALVES_HOME: "Casa segna in entrambi i tempi",
  SCORE_BOTH_HALVES_AWAY: "Ospite segna in entrambi i tempi",
  WIN_BOTH_HALVES_HOME: "Casa vince entrambi i tempi",
  WIN_BOTH_HALVES_AWAY: "Ospite vince entrambi i tempi",
  WIN_EITHER_HALF_HOME: "Casa vince almeno un tempo",
  WIN_EITHER_HALF_AWAY: "Ospite vince almeno un tempo",
};

const BASE_ORDER = Object.keys(MARKET_NAMES);
const BOTH_HALVES = new Set(BASE_ORDER.filter((c) => c === "HT_FT" || c.includes("HALF") || c.includes("HALVES")));

export const MARKET_GROUPS = [
  { key: "ft", label: "Partita" },
  { key: "h1", label: "1° tempo" },
  { key: "h2", label: "2° tempo" },
  { key: "both", label: "Due tempi" },
] as const;
export type MarketGroup = (typeof MARKET_GROUPS)[number]["key"];

export const baseCode = (code: string) => code.split("@")[0];

export function groupOf(code: string): MarketGroup {
  const period = code.split("@")[1];
  if (period === "H1") return "h1";
  if (period === "H2") return "h2";
  return BOTH_HALVES.has(code) ? "both" : "ft";
}

/** Known codes in display order (unknown ones are left out: the odds tab only lists markets it can name). */
export function orderMarkets(codes: string[]): string[] {
  return codes
    .filter((c) => baseCode(c) in MARKET_NAMES)
    .sort((a, b) => BASE_ORDER.indexOf(baseCode(a)) - BASE_ORDER.indexOf(baseCode(b)));
}

export const marketName = (code: string) => MARKET_NAMES[baseCode(code)] ?? code;

const FIXED: Record<string, string> = {
  HOME: "1", DRAW: "X", AWAY: "2", OVER: "Over", UNDER: "Under", YES: "Sì", NO: "No", ODD: "Dispari", EVEN: "Pari",
  NONE: "Nessun gol", "1ST": "1° tempo", "2ND": "2° tempo", EQUAL: "Uguale",
};

export function selectionName(market: string, sel: string): string {
  const base = baseCode(market);
  if (base === "BTTS") return sel === "YES" ? "Gol" : "NoGol";
  if (base === "WINNING_MARGIN") {
    if (sel === "D") return "Pareggio con gol";
    if (sel === "NG") return "0-0";
    if (sel === "DI") return "Pareggio";
    return `${sel[0] === "H" ? "Casa" : "Ospite"} di ${sel.slice(1).replace("+", " o più")}`;
  }
  if (base === "TOTAL_EXACT" || base.startsWith("TEAM_EXACT")) return sel.replace("+", " o più");
  if (base === "FIRST_GOAL" || base === "LAST_GOAL") return { HOME: "Casa", AWAY: "Ospite", NONE: "Nessun gol" }[sel] ?? sel;
  return FIXED[sel] ?? sel;
}

// Fixed order for the selections of a market; anything else (scores, margins, half-time/full-time) keeps a natural sort.
const SEL_ORDER = ["HOME", "1X", "DRAW", "12", "X2", "AWAY", "NONE", "OVER", "UNDER", "YES", "NO", "ODD", "EVEN", "1ST", "EQUAL", "2ND"];
export function sortSelections(a: string, b: string): number {
  const ia = SEL_ORDER.indexOf(a);
  const ib = SEL_ORDER.indexOf(b);
  if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  return a.localeCompare(b, "it", { numeric: true });
}

export function lineName(market: string, lineKey: string): string {
  const v = Number(lineKey);
  if (market.includes("HANDICAP")) return `${v > 0 ? "+" : ""}${v} casa`;
  return String(v);
}

const PERIOD_LABEL: Record<string, string> = { H1: " · 1° tempo", H2: " · 2° tempo" };

// "Over 2.5", "1 (+1 casa)", "Under/Over: Over 0.5 · 1° tempo"... used as chart and page titles.
export function quoteLabel(market: string, sel: string, lineKey: string): string {
  const s = selectionName(market, sel);
  const period = PERIOD_LABEL[market.split("@")[1] ?? ""] ?? "";
  const name = marketName(market);
  if (!lineKey) return `${name}: ${s}${period}`;
  if (market.includes("HANDICAP")) return `${name} ${lineName(market, lineKey)}: ${s}${period}`;
  return `${name}: ${s} ${lineName(market, lineKey)}${period}`;
}
