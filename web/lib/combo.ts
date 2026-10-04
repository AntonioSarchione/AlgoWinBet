// My Combo (Fase 8): several selections of the same match on one Sisal price. OddsPapi does not carry Sisal's My Combo
// prices, so the user types the price Sisal shows; the model gives the joint probability.
//
// The score matrix is rebuilt exactly as src/algowinbet/models/dixon_coles.py score_matrix does: Poisson(xg_home) x
// Poisson(xg_away) on 0..10 goals, Dixon-Coles tau on the low scores with the published rho, normalised. Every selection is
// a set of scores, so the joint probability of a combo is the sum over the scores where all its selections win: the
// dependence between them (1 + Over 2.5, Gol + Over 2.5...) comes out exactly.
// The model alone is not the final word on each selection: where the published analysis has the final probability of a
// selection (model shrunk toward the market), the joint is rescaled by final / model of every such selection. The
// correlation stays the model's, the level follows the final probabilities.

export const GRID = 11;

export type Matrix = number[][];

function poisson(k: number, lam: number): number {
  let p = Math.exp(-lam);
  for (let i = 1; i <= k; i++) p *= lam / i;
  return p;
}

export function scoreMatrix(lh: number, la: number, rho: number): Matrix {
  const m: Matrix = [];
  let tot = 0;
  for (let i = 0; i < GRID; i++) {
    m.push([]);
    for (let j = 0; j < GRID; j++) {
      let t = 1;
      if (i === 0 && j === 0) t = 1 - lh * la * rho;
      else if (i === 0 && j === 1) t = 1 + lh * rho;
      else if (i === 1 && j === 0) t = 1 + la * rho;
      else if (i === 1 && j === 1) t = 1 - rho;
      const v = Math.max(0, poisson(i, lh) * poisson(j, la) * t);
      m[i].push(v);
      tot += v;
    }
  }
  return m.map((r) => r.map((v) => v / tot));
}

export type ComboSel = { key: string; group: string; label: string; win: (h: number, a: number) => boolean };

const sel = (key: string, group: string, label: string, win: ComboSel["win"]): ComboSel => ({ key, group, label, win });

// keys are the analysis' selection keys (market|selection|line), so the final probability of the same selection is found
export const COMBO_SELECTIONS: ComboSel[] = [
  sel("MATCH_1X2|HOME|", "Esito finale", "1", (h, a) => h > a),
  sel("MATCH_1X2|DRAW|", "Esito finale", "X", (h, a) => h === a),
  sel("MATCH_1X2|AWAY|", "Esito finale", "2", (h, a) => h < a),
  sel("DOUBLE_CHANCE|1X|", "Doppia chance", "1X", (h, a) => h >= a),
  sel("DOUBLE_CHANCE|X2|", "Doppia chance", "X2", (h, a) => h <= a),
  sel("DOUBLE_CHANCE|12|", "Doppia chance", "12", (h, a) => h !== a),
  sel("BTTS|YES|", "Gol/NoGol", "Gol", (h, a) => h > 0 && a > 0),
  sel("BTTS|NO|", "Gol/NoGol", "NoGol", (h, a) => h === 0 || a === 0),
  ...[0.5, 1.5, 2.5, 3.5, 4.5].flatMap((l) => [
    sel(`TOTAL_GOALS|OVER|${l}`, "Under/Over", `Over ${l}`, (h, a) => h + a > l),
    sel(`TOTAL_GOALS|UNDER|${l}`, "Under/Over", `Under ${l}`, (h, a) => h + a < l),
  ]),
  ...[0.5, 1.5, 2.5].flatMap((l) => [
    sel(`TEAM_TOTAL_HOME|OVER|${l}`, "Gol squadra casa", `Over ${l}`, (h) => h > l),
    sel(`TEAM_TOTAL_HOME|UNDER|${l}`, "Gol squadra casa", `Under ${l}`, (h) => h < l),
    sel(`TEAM_TOTAL_AWAY|OVER|${l}`, "Gol squadra ospite", `Over ${l}`, (_h, a) => a > l),
    sel(`TEAM_TOTAL_AWAY|UNDER|${l}`, "Gol squadra ospite", `Under ${l}`, (_h, a) => a < l),
  ]),
  ...([[1, 2], [1, 3], [2, 3], [2, 4], [3, 5], [4, 6]] as const).map(([lo, hi]) =>
    sel(`MULTIGOL|${lo}-${hi}|`, "Multigol", `${lo}-${hi}`, (h, a) => h + a >= lo && h + a <= hi)),
  sel("WIN_TO_NIL_HOME|YES|", "Vince a zero", "Casa", (h, a) => h > a && a === 0),
  sel("WIN_TO_NIL_AWAY|YES|", "Vince a zero", "Ospite", (h, a) => a > h && h === 0),
  sel("ODD_EVEN|ODD|", "Pari/Dispari", "Dispari", (h, a) => (h + a) % 2 === 1),
  sel("ODD_EVEN|EVEN|", "Pari/Dispari", "Pari", (h, a) => (h + a) % 2 === 0),
];

export const COMBO_GROUPS = [...new Set(COMBO_SELECTIONS.map((s) => s.group))];

export function probabilityOf(m: Matrix, win: (h: number, a: number) => boolean): number {
  let p = 0;
  for (let i = 0; i < GRID; i++) for (let j = 0; j < GRID; j++) if (win(i, j)) p += m[i][j];
  return p;
}

export type ComboResult = {
  pModel: number; // joint, model only
  p: number; // joint rescaled to the final probabilities of the selections
  fair: number | null; // 1 / p
  ev: number | null; // at the typed Sisal price
  rescaled: number; // selections whose final probability was used
  impossible: boolean; // no score satisfies them all
};

export function evaluateCombo(m: Matrix, sels: ComboSel[], finals: Record<string, number>, odds: number | null): ComboResult {
  const pModel = sels.length ? probabilityOf(m, (h, a) => sels.every((s) => s.win(h, a))) : 0;
  let ratio = 1;
  let rescaled = 0;
  for (const s of sels) {
    const f = finals[s.key];
    const pm = probabilityOf(m, s.win);
    if (f != null && pm > 1e-9) {
      ratio *= f / pm;
      rescaled++;
    }
  }
  // never above the least likely selection (rescaling cannot make the combo likelier than its hardest part)
  const cap = Math.min(...sels.map((s) => finals[s.key] ?? probabilityOf(m, s.win)), 1);
  const p = sels.length ? Math.min(pModel * ratio, cap) : 0;
  return {
    pModel, p, fair: p > 1e-9 ? 1 / p : null, ev: odds && odds > 1 && p > 0 ? p * odds - 1 : null, rescaled, impossible: sels.length > 0 && pModel < 1e-12,
  };
}
