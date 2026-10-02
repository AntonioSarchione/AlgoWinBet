// Where each starter stands on a vertical pitch (home in the top half attacking down, away in the bottom half attacking up).
// API-Football gives every starter a grid "row:col" (row 1 = goalkeeper, columns left to right as the team attacks). Without
// it the rows come from the formation ("4-3-3" -> 1 + 4 + 3 + 3), starters taken in the published order.

export type Spot = { id: string; x: number; y: number; number: number | null };
export type Detail = Record<string, { n?: number | null; g?: string | null }>;

const NEAR = 6; // % from the goal line to the goalkeeper
const FAR = 44; // % from the goal line to the most advanced row (just short of halfway)

function rowsFromFormation(formation: string | null, n: number): number[] | null {
  const parts = (formation ?? "").split("-").map(Number).filter((x) => Number.isInteger(x) && x > 0);
  const rows = [1, ...parts];
  return parts.length && rows.reduce((a, b) => a + b, 0) === n ? rows : null;
}

export function layout(starters: string[], formation: string | null, detail: Detail, side: "home" | "away"): Spot[] | null {
  // grid positions when every starter has one, else the formation
  let cells: { id: string; row: number; col: number }[] | null = null;
  const grid = starters.map((id) => detail[id]?.g?.split(":").map(Number));
  if (starters.length === 11 && grid.every((g) => g && g.length === 2 && g.every(Number.isFinite))) {
    cells = starters.map((id, i) => ({ id, row: grid[i]![0], col: grid[i]![1] }));
  } else {
    const rows = rowsFromFormation(formation, starters.length);
    if (!rows) return null;
    cells = [];
    let k = 0;
    rows.forEach((count, r) => {
      for (let c = 1; c <= count; c++) cells!.push({ id: starters[k++], row: r + 1, col: c });
    });
  }
  const nRows = Math.max(...cells.map((c) => c.row));
  const perRow = new Map<number, number>();
  for (const c of cells) perRow.set(c.row, Math.max(perRow.get(c.row) ?? 0, c.col));
  return cells.map((c) => {
    const depth = nRows > 1 ? NEAR + ((c.row - 1) / (nRows - 1)) * (FAR - NEAR) : NEAR;
    const across = ((c.col - 0.5) / (perRow.get(c.row) ?? 1)) * 100;
    // columns as most lineup graphics draw them (checked on Cyprus-Armenia, 2026-10-02): the top team in grid order, the bottom
    // team mirrored, so the two teams face each other; the wide-screen pitch is the same picture turned a quarter
    return { id: c.id, x: side === "home" ? across : 100 - across, y: side === "home" ? depth : 100 - depth, number: detail[c.id]?.n ?? null };
  });
}

// "L. Konomis" -> "Konomis"; names without an initial stay whole
export function shortName(name: string): string {
  const m = name.match(/^[A-Z]\.\s+(.+)$/);
  return m ? m[1] : name;
}
