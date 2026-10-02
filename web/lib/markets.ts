// Market groups for the dashboard filter "Mercati", from the market code of a selection key ("CODE[@H1]|SEL|LINE").
// Mirrors src/algowinbet/markets.py REGISTRY: a code with a period (@H1, @H2) belongs to "1° e 2° tempo".

export const MARKET_GROUPS = [
  "1X2",
  "Doppia chance",
  "Draw no bet",
  "Under/Over",
  "Gol/NoGol",
  "Gol squadra",
  "Handicap",
  "Risultato e gol esatti",
  "1° e 2° tempo",
  "Primo/ultimo gol",
] as const;
export type MarketGroup = (typeof MARKET_GROUPS)[number];

const BY_CODE: Record<string, MarketGroup> = {
  MATCH_1X2: "1X2",
  DOUBLE_CHANCE: "Doppia chance",
  DRAW_NO_BET: "Draw no bet",
  TOTAL_GOALS: "Under/Over",
  BTTS: "Gol/NoGol",
  EURO_HANDICAP: "Handicap",
  ASIAN_HANDICAP: "Handicap",
  CORRECT_SCORE: "Risultato e gol esatti",
  TOTAL_EXACT: "Risultato e gol esatti",
  WINNING_MARGIN: "Risultato e gol esatti",
  ODD_EVEN: "Risultato e gol esatti",
  WIN_TO_NIL_HOME: "Risultato e gol esatti",
  WIN_TO_NIL_AWAY: "Risultato e gol esatti",
  FIRST_GOAL: "Primo/ultimo gol",
  LAST_GOAL: "Primo/ultimo gol",
};

export function marketGroup(selKey: string): MarketGroup {
  const code = selKey.split("|")[0] ?? "";
  const [base, period] = code.split("@");
  if (period || base.startsWith("HT_FT") || base.includes("HALF")) return "1° e 2° tempo";
  if (base.startsWith("TEAM_")) return "Gol squadra";
  return BY_CODE[base] ?? "Risultato e gol esatti";
}
