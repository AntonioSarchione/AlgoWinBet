// Italian names of the market codes stored in `quotes` (see src/algowinbet/markets.py), in the order the odds tab lists them.
export const MARKET_NAMES: Record<string, string> = {
  MATCH_1X2: "1X2",
  DOUBLE_CHANCE: "Doppia chance",
  TOTAL_GOALS: "Under/Over",
  BTTS: "Gol/NoGol",
  TEAM_TOTAL_HOME: "Under/Over casa",
  TEAM_TOTAL_AWAY: "Under/Over ospite",
  EURO_HANDICAP: "Handicap europeo",
  ASIAN_HANDICAP: "Handicap asiatico",
  ODD_EVEN: "Pari/Dispari",
  TOTAL_EXACT: "Gol totali esatti",
  TEAM_EXACT_HOME: "Gol esatti casa",
  TEAM_EXACT_AWAY: "Gol esatti ospite",
  CORRECT_SCORE: "Risultato esatto",
  WINNING_MARGIN: "Margine di vittoria",
  WIN_TO_NIL_HOME: "Casa vince a zero",
  WIN_TO_NIL_AWAY: "Ospite vince a zero",
};

export const MARKET_ORDER = Object.keys(MARKET_NAMES);

const FIXED: Record<string, string> = {
  HOME: "1", DRAW: "X", AWAY: "2", OVER: "Over", UNDER: "Under", YES: "Sì", NO: "No", ODD: "Dispari", EVEN: "Pari",
};

export function selectionName(market: string, sel: string): string {
  if (market === "BTTS") return sel === "YES" ? "Gol" : "NoGol";
  if (market === "WINNING_MARGIN") {
    if (sel === "D") return "Pareggio con gol";
    if (sel === "NG") return "0-0";
    if (sel === "DI") return "Pareggio";
    return `${sel[0] === "H" ? "Casa" : "Ospite"} di ${sel.slice(1).replace("+", " o più")}`;
  }
  if (market === "TOTAL_EXACT" || market.startsWith("TEAM_EXACT")) return sel.replace("+", " o più");
  return FIXED[sel] ?? sel;
}

// Fixed order for the selections of a market; anything else (scores, margins) keeps a natural sort.
const SEL_ORDER = ["HOME", "1X", "DRAW", "12", "X2", "AWAY", "OVER", "UNDER", "YES", "NO", "ODD", "EVEN"];
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

// "Over 2.5", "1 (+1 casa)"... used as chart and page titles.
export function quoteLabel(market: string, sel: string, lineKey: string): string {
  const s = selectionName(market, sel);
  if (!lineKey) return `${MARKET_NAMES[market] ?? market}: ${s}`;
  if (market.includes("HANDICAP")) return `${MARKET_NAMES[market]} ${lineName(market, lineKey)}: ${s}`;
  return `${MARKET_NAMES[market] ?? market}: ${s} ${lineName(market, lineKey)}`;
}
