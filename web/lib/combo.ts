// My Combo (Fase 8): several selections of the same match on one Sisal price. OddsPapi does not carry Sisal's My Combo
// prices, so the user types the price Sisal shows (or asks for one: generateCombo); the model gives the joint probability.
//
// Goals. The score matrix is rebuilt exactly as src/algowinbet/models/dixon_coles.py score_matrix does: Poisson(xg_home) x
// Poisson(xg_away) on 0..10 goals, Dixon-Coles tau on the low scores with the published rho, normalised. The halves split it:
// given the full-time goals each goal falls in the first half with probability h1 (markets.FIRST_HALF_SHARE, the analysis'
// own split), so the full-time grid stays the Dixon-Coles one and the halves follow. The order of the goals: within a half
// every goal is equally likely to be any team's (Poisson processes), which gives the first and the last goal's team. Every
// goal selection is a set of these cells, so the joint probability of goal selections comes out exactly.
//
// Corners, cards, team shots: each statistic its own count grid (models/counts.py CountModel.params: negative binomial per
// team, the shared factor of cards as Gauss-Laguerre nodes), selections on one statistic exact together; shots on target
// with total shots: each shot on target with probability (expected on target / expected shots), so both stay coherent.
// Between goals, corners, cards and shots the model has no link: they multiply (independent).
//
// Players (Sisal's DUO rule: goals, two or more, first goalscorer, assists, goal or assist and shots on target count for the
// player or for whoever replaces him, so the player's slot covers the whole match; total shots, bookings and fouls do not). His share of his team's goals
// is his goals per start (published anytime probability of a start, scorers.py) over the team's expected goals, scaled to
// the whole match (x 90 / his minutes in a start); assists the same from his assist probability (playercard.py). Given the
// team's goals each goal is his with that share, so "1 + he scores", two players of one team scoring and his first goal
// follow the goals exactly. Shots on target (DUO, x 90 / minutes), total shots, booking and fouls (his own minutes only):
// negative binomial counts of his card, independent of the rest. All conditional on his starting.
//
// The model alone is not the final word on each selection: where the published analysis has the final probability of a
// selection (model shrunk toward the market), the joint is rescaled by final / model of every such selection. The
// correlation stays the model's, the level follows the final probabilities.

export const GRID = 11;
export const DISP = 4; // negative binomial shape of the player counts (playercard.py)
export const SISAL_MARGIN = 0.92; // price / fair price of a Sisal single without a price of its own (estimates only)

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

// ---- count models (src/algowinbet/models/counts.py)
export type CountParams = { lh: number; la: number; n: number; size: number | null; g?: [number, number][] };
export type StatName = "corners" | "cards" | "shots" | "shots_on";
export type ComboParams = { h1: number; stats: Partial<Record<StatName, CountParams>> };

/** P(count = k), k = 0..n-1, the tail in the last cell: negative binomial (mean lam, shape size) or Poisson. */
export function countPmf(lam: number, size: number | null, n: number): number[] {
  const p: number[] = [];
  let v = size == null ? Math.exp(-lam) : (size / (size + lam)) ** size;
  let tot = 0;
  for (let k = 0; k < n; k++) {
    if (k > 0) v *= size == null ? lam / k : ((size + k - 1) / k) * (lam / (size + lam));
    p.push(v);
    tot += v;
  }
  p[n - 1] += Math.max(0, 1 - tot);
  return p;
}

export function countMatrix(c: CountParams): Matrix {
  const m: Matrix = Array.from({ length: c.n }, () => new Array(c.n).fill(0));
  let tot = 0;
  for (const [x, w] of c.g ?? [[1, 1]]) {
    const ph = countPmf(c.lh * x, c.size, c.n);
    const pa = countPmf(c.la * x, c.size, c.n);
    for (let i = 0; i < c.n; i++) for (let j = 0; j < c.n; j++) {
      const v = w * ph[i] * pa[j];
      m[i][j] += v;
      tot += v;
    }
  }
  return m.map((r) => r.map((v) => v / tot));
}

// ---- goal cells: full-time score, first-half goals, team of the first and of the last goal (0 none, 1 home, 2 away)
export type Cell = { h: number; a: number; h1: number; a1: number; ft: 0 | 1 | 2; lt: 0 | 1 | 2; w: number };

function binom(n: number, k: number, p: number): number {
  let c = 1;
  for (let i = 0; i < k; i++) c = (c * (n - i)) / (i + 1);
  return c * p ** k * (1 - p) ** (n - k);
}

export function goalCells(m: Matrix, h1share: number): Cell[] {
  const out: Cell[] = [];
  for (let h = 0; h < GRID; h++) for (let a = 0; a < GRID; a++) {
    if (m[h][a] < 1e-9) continue;
    for (let h1 = 0; h1 <= h; h1++) for (let a1 = 0; a1 <= a; a1++) {
      const w = m[h][a] * binom(h, h1, h1share) * binom(a, a1, h1share);
      if (w < 1e-9) continue;
      const n1 = h1 + a1, h2 = h - h1, a2 = a - a1, n2 = h2 + a2;
      if (n1 + n2 === 0) {
        out.push({ h, a, h1, a1, ft: 0, lt: 0, w });
        continue;
      }
      const q = n1 > 0 ? h1 / n1 : h2 / n2; // first goal: the first half's if any
      const r = n2 > 0 ? h2 / n2 : h1 / n1; // last goal: the second half's if any
      if (n1 + n2 === 1) {
        if (q > 0) out.push({ h, a, h1, a1, ft: 1, lt: 1, w: w * q });
        if (q < 1) out.push({ h, a, h1, a1, ft: 2, lt: 2, w: w * (1 - q) });
        continue;
      }
      for (const [ft, pf] of [[1, q], [2, 1 - q]] as const) for (const [lt, pl] of [[1, r], [2, 1 - r]] as const) {
        if (pf * pl > 0) out.push({ h, a, h1, a1, ft, lt, w: w * pf * pl });
      }
    }
  }
  return out;
}

// ---- selections
export type PlayerKind = "goal" | "goal2" | "assist" | "goal_or_assist" | "first" | "card" | "goal_or_card" | "shots" | "sot" | "fc" | "fd" | "fouls";
export type ComboSel = {
  key: string;
  group: string; // one selection per group in a generated combo
  section: string;
  label: string;
  goal?: (c: Cell) => boolean;
  stat?: StatName;
  count?: (h: number, a: number) => boolean; // the statistic's counts (home, away)
  player?: { pid: string; kind: PlayerKind; k: number };
};

const sign = (x: number) => (x > 0 ? "1" : x < 0 ? "2" : "X");
const g = (key: string, section: string, group: string, label: string, goal: (c: Cell) => boolean): ComboSel => ({ key, section, group, label, goal });
const H2 = (c: Cell) => c.h - c.h1;
const A2 = (c: Cell) => c.a - c.a1;

function goalSelections(): ComboSel[] {
  const S = "Esito e goal", T1 = "1° tempo", T2 = "2° tempo", TM = "Goal squadra";
  const out: ComboSel[] = [
    g("MATCH_1X2|HOME|", S, "Esito finale", "1", (c) => c.h > c.a),
    g("MATCH_1X2|DRAW|", S, "Esito finale", "X", (c) => c.h === c.a),
    g("MATCH_1X2|AWAY|", S, "Esito finale", "2", (c) => c.h < c.a),
    g("DOUBLE_CHANCE|1X|", S, "Doppia chance", "1X", (c) => c.h >= c.a),
    g("DOUBLE_CHANCE|X2|", S, "Doppia chance", "X2", (c) => c.h <= c.a),
    g("DOUBLE_CHANCE|12|", S, "Doppia chance", "12", (c) => c.h !== c.a),
    g("BTTS|YES|", S, "Goal/NoGoal", "Goal", (c) => c.h > 0 && c.a > 0),
    g("BTTS|NO|", S, "Goal/NoGoal", "NoGoal", (c) => c.h === 0 || c.a === 0),
    g("BOTH_2PLUS|YES|", S, "Entrambe almeno 2 goal", "Sì", (c) => c.h >= 2 && c.a >= 2),
    g("BOTH_2PLUS|NO|", S, "Entrambe almeno 2 goal", "No", (c) => c.h < 2 || c.a < 2),
  ];
  for (const l of [0.5, 1.5, 2.5, 3.5, 4.5]) {
    out.push(g(`TOTAL_GOALS|OVER|${l}`, S, "Under/Over", `Over ${l}`, (c) => c.h + c.a > l));
    out.push(g(`TOTAL_GOALS|UNDER|${l}`, S, "Under/Over", `Under ${l}`, (c) => c.h + c.a < l));
  }
  for (const [lo, hi] of [[1, 2], [1, 3], [2, 3], [2, 4], [2, 5], [2, 6], [3, 5], [4, 5], [4, 6]] as const) {
    out.push(g(`MULTIGOL|${lo}-${hi}|`, S, "Multigoal", `${lo}-${hi}`, (c) => c.h + c.a >= lo && c.h + c.a <= hi));
  }
  out.push(
    g("ODD_EVEN|ODD|", S, "Pari/Dispari", "Dispari", (c) => (c.h + c.a) % 2 === 1),
    g("ODD_EVEN|EVEN|", S, "Pari/Dispari", "Pari", (c) => (c.h + c.a) % 2 === 0),
    g("FIRST_GOAL|HOME|", S, "Prima squadra a segnare", "Casa", (c) => c.ft === 1),
    g("FIRST_GOAL|NONE|", S, "Prima squadra a segnare", "Nessuna", (c) => c.ft === 0),
    g("FIRST_GOAL|AWAY|", S, "Prima squadra a segnare", "Ospite", (c) => c.ft === 2),
    g("LAST_GOAL|HOME|", S, "Ultima squadra a segnare", "Casa", (c) => c.lt === 1),
    g("LAST_GOAL|AWAY|", S, "Ultima squadra a segnare", "Ospite", (c) => c.lt === 2),
  );
  for (const ht of "1X2") for (const ft of "1X2") {
    out.push(g(`HT_FT|${ht}/${ft}|`, S, "Parziale/Finale", `${ht}/${ft}`, (c) => sign(c.h1 - c.a1) === ht && sign(c.h - c.a) === ft));
  }
  // halves: the analysis' codes with @H1 / @H2, so its final probabilities are found
  for (const [P, sec, gh, ga] of [["@H1", T1, (c: Cell) => c.h1, (c: Cell) => c.a1], ["@H2", T2, H2, A2]] as const) {
    out.push(
      g(`MATCH_1X2${P}|HOME|`, sec, `Esito ${sec}`, "1", (c) => gh(c) > ga(c)),
      g(`MATCH_1X2${P}|DRAW|`, sec, `Esito ${sec}`, "X", (c) => gh(c) === ga(c)),
      g(`MATCH_1X2${P}|AWAY|`, sec, `Esito ${sec}`, "2", (c) => gh(c) < ga(c)),
      g(`DOUBLE_CHANCE${P}|1X|`, sec, `Doppia chance ${sec}`, "1X", (c) => gh(c) >= ga(c)),
      g(`DOUBLE_CHANCE${P}|X2|`, sec, `Doppia chance ${sec}`, "X2", (c) => gh(c) <= ga(c)),
      g(`DOUBLE_CHANCE${P}|12|`, sec, `Doppia chance ${sec}`, "12", (c) => gh(c) !== ga(c)),
      g(`BTTS${P}|YES|`, sec, `Goal/NoGoal ${sec}`, "Goal", (c) => gh(c) > 0 && ga(c) > 0),
      g(`BTTS${P}|NO|`, sec, `Goal/NoGoal ${sec}`, "NoGoal", (c) => gh(c) === 0 || ga(c) === 0),
    );
    for (const l of [0.5, 1.5, 2.5]) {
      out.push(g(`TOTAL_GOALS${P}|OVER|${l}`, sec, `Under/Over ${sec}`, `Over ${l}`, (c) => gh(c) + ga(c) > l));
      out.push(g(`TOTAL_GOALS${P}|UNDER|${l}`, sec, `Under/Over ${sec}`, `Under ${l}`, (c) => gh(c) + ga(c) < l));
    }
    for (const [lo, hi] of [[0, 1], [1, 2], [1, 3], [2, 3]] as const) {
      out.push(g(`MULTIGOL${P}|${lo}-${hi}|`, sec, `Multigoal ${sec}`, `${lo}-${hi}`, (c) => gh(c) + ga(c) >= lo && gh(c) + ga(c) <= hi));
    }
    for (const l of [0.5, 1.5]) {
      out.push(g(`TEAM_TOTAL_HOME${P}|OVER|${l}`, sec, `Goal casa ${sec}`, `Over ${l}`, (c) => gh(c) > l));
      out.push(g(`TEAM_TOTAL_HOME${P}|UNDER|${l}`, sec, `Goal casa ${sec}`, `Under ${l}`, (c) => gh(c) < l));
      out.push(g(`TEAM_TOTAL_AWAY${P}|OVER|${l}`, sec, `Goal ospite ${sec}`, `Over ${l}`, (c) => ga(c) > l));
      out.push(g(`TEAM_TOTAL_AWAY${P}|UNDER|${l}`, sec, `Goal ospite ${sec}`, `Under ${l}`, (c) => ga(c) < l));
    }
  }
  for (const l of [0.5, 1.5, 2.5]) {
    out.push(g(`TEAM_TOTAL_HOME|OVER|${l}`, TM, "Goal casa", `Over ${l}`, (c) => c.h > l));
    out.push(g(`TEAM_TOTAL_HOME|UNDER|${l}`, TM, "Goal casa", `Under ${l}`, (c) => c.h < l));
    out.push(g(`TEAM_TOTAL_AWAY|OVER|${l}`, TM, "Goal ospite", `Over ${l}`, (c) => c.a > l));
    out.push(g(`TEAM_TOTAL_AWAY|UNDER|${l}`, TM, "Goal ospite", `Under ${l}`, (c) => c.a < l));
  }
  for (const [lo, hi] of [[1, 2], [1, 3], [2, 3]] as const) {
    out.push(g(`MULTIGOL_HOME|${lo}-${hi}|`, TM, "Multigoal casa", `${lo}-${hi}`, (c) => c.h >= lo && c.h <= hi));
    out.push(g(`MULTIGOL_AWAY|${lo}-${hi}|`, TM, "Multigoal ospite", `${lo}-${hi}`, (c) => c.a >= lo && c.a <= hi));
  }
  out.push(
    g("WIN_TO_NIL_HOME|YES|", TM, "Vince a zero", "Casa", (c) => c.h > c.a && c.a === 0),
    g("WIN_TO_NIL_AWAY|YES|", TM, "Vince a zero", "Ospite", (c) => c.a > c.h && c.h === 0),
    g("SCORE_BOTH_HALVES_HOME|YES|", TM, "Segna in entrambi i tempi", "Casa", (c) => c.h1 > 0 && H2(c) > 0),
    g("SCORE_BOTH_HALVES_AWAY|YES|", TM, "Segna in entrambi i tempi", "Ospite", (c) => c.a1 > 0 && A2(c) > 0),
  );
  return out;
}

const STAT_LINES: Record<StatName, { title: string; total: number[]; team: number[]; both: number[] }> = {
  corners: { title: "Corner", total: [7.5, 8.5, 9.5, 10.5, 11.5], team: [2.5, 3.5, 4.5, 5.5, 6.5], both: [2, 3, 4] },
  cards: { title: "Cartellini", total: [2.5, 3.5, 4.5, 5.5, 6.5], team: [0.5, 1.5, 2.5, 3.5], both: [1, 2] },
  shots: { title: "Tiri totali", total: [20.5, 22.5, 24.5, 26.5, 28.5], team: [8.5, 10.5, 12.5, 14.5, 16.5], both: [8, 10] },
  shots_on: { title: "Tiri in porta", total: [6.5, 7.5, 8.5, 9.5, 10.5], team: [2.5, 3.5, 4.5, 5.5], both: [2, 3, 4] },
};
const STAT_CODE: Record<StatName, string> = { corners: "CORNERS", cards: "CARDS", shots: "SHOTS", shots_on: "SHOTS_ON" };

function statSelections(stat: StatName): ComboSel[] {
  const { title, total, team, both } = STAT_LINES[stat];
  const C = STAT_CODE[stat];
  const s = (key: string, group: string, label: string, count: (h: number, a: number) => boolean): ComboSel => ({ key, section: title, group, label, stat, count });
  const out: ComboSel[] = [
    s(`${C}_1X2|HOME|`, `1X2 ${title.toLowerCase()}`, "1", (h, a) => h > a),
    s(`${C}_1X2|DRAW|`, `1X2 ${title.toLowerCase()}`, "X", (h, a) => h === a),
    s(`${C}_1X2|AWAY|`, `1X2 ${title.toLowerCase()}`, "2", (h, a) => h < a),
  ];
  for (const l of total) {
    out.push(s(`${C}_TOTAL|OVER|${l}`, `U/O ${title.toLowerCase()}`, `Over ${l}`, (h, a) => h + a > l));
    out.push(s(`${C}_TOTAL|UNDER|${l}`, `U/O ${title.toLowerCase()}`, `Under ${l}`, (h, a) => h + a < l));
  }
  for (const l of team) {
    out.push(s(`${C}_TEAM_HOME|OVER|${l}`, `${title} casa`, `Over ${l}`, (h) => h > l));
    out.push(s(`${C}_TEAM_HOME|UNDER|${l}`, `${title} casa`, `Under ${l}`, (h) => h < l));
    out.push(s(`${C}_TEAM_AWAY|OVER|${l}`, `${title} ospite`, `Over ${l}`, (_h, a) => a > l));
    out.push(s(`${C}_TEAM_AWAY|UNDER|${l}`, `${title} ospite`, `Under ${l}`, (_h, a) => a < l));
  }
  for (const k of both) out.push(s(`${C}_BOTH|${k}|`, `Entrambe almeno X ${title.toLowerCase()}`, `Almeno ${k}`, (h, a) => h >= k && a >= k));
  return out;
}

// ---- players
export type ComboPlayer = {
  pid: string;
  name: string;
  team: 1 | 2; // home, away
  start: number; // probability that he starts (1 with the official XI)
  share: number; // his slot's share of his team's goals (DUO: whole match)
  assist: number; // his slot's share of his team's goals that he assists (DUO)
  sot: number; sh: number; card: number; // expected counts: shots on target DUO (whole match), shots and bookings his own
  fc: number; fd: number; // his own minutes only
};

export const PLAYER_MARKETS: { kind: PlayerKind; label: string; ks: number[]; duo: boolean }[] = [
  { kind: "goal", label: "Segna", ks: [1], duo: true },
  { kind: "goal2", label: "Doppietta o più", ks: [2], duo: true },
  { kind: "first", label: "Primo marcatore", ks: [1], duo: true },
  { kind: "assist", label: "Assist", ks: [1], duo: true },
  { kind: "goal_or_assist", label: "Segna o fa assist", ks: [1], duo: true },
  { kind: "card", label: "Cartellino", ks: [1], duo: false },
  { kind: "goal_or_card", label: "Segna o cartellino", ks: [1], duo: false },
  { kind: "sot", label: "Tiri in porta", ks: [1, 2, 3], duo: true },
  { kind: "shots", label: "Tiri totali", ks: [1, 2, 3, 4], duo: false },
  { kind: "fc", label: "Falli commessi", ks: [1, 2, 3], duo: false },
  { kind: "fd", label: "Falli subiti", ks: [1, 2, 3], duo: false },
  { kind: "fouls", label: "Falli commessi + subiti", ks: [2, 3, 4], duo: false },
];

export function playerSelections(players: ComboPlayer[], teamName: (t: 1 | 2) => string): ComboSel[] {
  const out: ComboSel[] = [];
  for (const p of players) for (const m of PLAYER_MARKETS) for (const k of m.ks) {
    const many = m.ks.length > 1 || k > 1;
    out.push({
      key: `PLAYER|${p.pid}|${m.kind}|${k}`, section: `Giocatori ${teamName(p.team)}`, group: `${p.pid}|${m.kind}`,
      label: `${p.name}: ${m.label}${many && m.kind !== "goal2" ? ` ${k}+` : ""}`, player: { pid: p.pid, kind: m.kind, k },
    });
  }
  return out;
}

/** Mean of a negative binomial count (shape DISP) with P(at least one) = p. */
export function nbMean(p: number): number {
  if (p <= 0) return 0;
  return DISP * ((1 - Math.min(p, 0.999)) ** (-1 / DISP) - 1);
}

function nbTail(mu: number, cap: number): number[] { // P(count = k) for k < cap, P(count >= cap) in the last cell
  return countPmf(mu, mu > 0 ? DISP : null, cap + 1);
}

// distribution of a player's goal-free dimensions his conditions read: card (0/1+), shots, shots on target, fouls
type Dims = { card: number; shots: number; sot: number; fc: number; fd: number };
function dimStates(p: ComboPlayer, conds: { kind: PlayerKind; k: number }[]): { d: Dims; w: number }[] {
  const need = (kinds: PlayerKind[]) => Math.max(0, ...conds.filter((c) => kinds.includes(c.kind)).map((c) => c.k));
  const cap: Dims = {
    card: conds.some((c) => c.kind === "card" || c.kind === "goal_or_card") ? 1 : 0,
    shots: need(["shots"]), sot: need(["sot"]), fc: Math.max(need(["fc"]), need(["fouls"])), fd: Math.max(need(["fd"]), need(["fouls"])),
  };
  const dist = (mu: number, c: number) => (c ? nbTail(mu, c) : [1]);
  const parts: [keyof Dims, number[]][] = [
    ["card", dist(p.card, cap.card)], ["shots", dist(p.sh, cap.shots)], ["sot", dist(p.sot, cap.sot)], ["fc", dist(p.fc, cap.fc)], ["fd", dist(p.fd, cap.fd)],
  ];
  let states: { d: Dims; w: number }[] = [{ d: { card: 0, shots: 0, sot: 0, fc: 0, fd: 0 }, w: 1 }];
  for (const [name, pr] of parts) {
    if (pr.length === 1) continue;
    states = states.flatMap((s) => pr.map((w, v) => ({ d: { ...s.d, [name]: v }, w: s.w * w })));
  }
  return states;
}

function playerOk(c: { kind: PlayerKind; k: number }, goals: number, assists: number, first: boolean, d: Dims): boolean {
  switch (c.kind) {
    case "goal": return goals >= c.k;
    case "goal2": return goals >= 2;
    case "first": return first;
    case "assist": return assists >= c.k;
    case "goal_or_assist": return goals + assists >= 1;
    case "card": return d.card >= 1;
    case "goal_or_card": return goals >= 1 || d.card >= 1;
    case "shots": return d.shots >= c.k;
    case "sot": return d.sot >= c.k;
    case "fc": return d.fc >= c.k;
    case "fd": return d.fd >= c.k;
    case "fouls": return d.fc + d.fd >= c.k;
  }
}

/** For one team's players with conditions: f[n][b] = P(every condition holds | the team scores n goals, b = 1 when the
 * match's first goal is the team's). Each goal is player i's with his share; its assist player j's (j not the scorer). */
function teamFactor(ps: ComboPlayer[], conds: Map<string, { kind: PlayerKind; k: number }[]>): number[][] {
  const m = ps.length;
  const scale = Math.min(1, 0.95 / Math.max(1e-9, ps.reduce((x, p) => x + p.share, 0)));
  const s = ps.map((p) => p.share * scale);
  const aScale = Math.min(1, 0.9 / Math.max(1e-9, ps.reduce((x, p) => x + p.assist, 0)));
  const as = ps.map((p) => p.assist * aScale);
  // state: goals (capped at 2) and assists (capped at 1) per player, then the first goal's scorer (-1 someone else, -2 none yet)
  type St = { g: number[]; a: number[]; f: number };
  const keyOf = (x: St) => `${x.g.join("")}|${x.a.join("")}|${x.f}`;
  let cur = new Map<string, { s: St; w: number }>([[keyOf({ g: Array(m).fill(0), a: Array(m).fill(0), f: -2 }), { s: { g: Array(m).fill(0), a: Array(m).fill(0), f: -2 }, w: 1 }]]);
  const byN: Map<string, { s: St; w: number }>[] = [cur];
  for (let n = 1; n < GRID; n++) {
    const next = new Map<string, { s: St; w: number }>();
    const add = (x: St, w: number) => {
      if (w < 1e-14) return;
      const k = keyOf(x);
      const e = next.get(k);
      if (e) e.w += w;
      else next.set(k, { s: x, w });
    };
    for (const { s: st, w } of cur.values()) {
      for (let i = -1; i < m; i++) { // scorer: -1 someone else
        const ps_ = i < 0 ? 1 - s.reduce((x, y) => x + y, 0) : s[i];
        if (ps_ <= 0) continue;
        const restA = as.reduce((x, y, j) => x + (j === i ? 0 : y), 0);
        for (let j = -1; j < m; j++) { // assist: -1 someone else or none
          if (j === i && j >= 0) continue;
          const pa = j < 0 ? 1 - restA : as[j];
          if (pa <= 0) continue;
          const g2 = [...st.g], a2 = [...st.a];
          if (i >= 0) g2[i] = Math.min(2, g2[i] + 1);
          if (j >= 0) a2[j] = 1;
          add({ g: g2, a: a2, f: st.f === -2 ? i : st.f }, w * ps_ * pa);
        }
      }
    }
    cur = next;
    byN.push(cur);
  }
  const dims = ps.map((p) => dimStates(p, conds.get(p.pid) ?? []));
  // per player and (goals, assists, first): P(his conditions hold) over his goal-free dimensions
  const ok = ps.map((p, i) => {
    const cs = conds.get(p.pid) ?? [];
    const memo = new Map<string, number>();
    return (goals: number, assists: number, first: boolean) => {
      const k = `${goals}${assists}${first ? 1 : 0}`;
      let v = memo.get(k);
      if (v == null) {
        v = dims[i].reduce((x, st) => x + (cs.every((c) => playerOk(c, goals, assists, first, st.d)) ? st.w : 0), 0);
        memo.set(k, v);
      }
      return v;
    };
  });
  return byN.map((states) => [0, 1].map((b) => {
    let tot = 0;
    for (const { s: st, w } of states.values()) {
      let v = w;
      for (let i = 0; i < m && v > 0; i++) v *= ok[i](st.g[i], st.a[i], b === 1 && st.f === i);
      tot += v;
    }
    return tot;
  }));
}

// ---- evaluation
export type ComboModel = {
  cells: Cell[];
  stats: Partial<Record<StatName, Matrix>>;
  players: Map<string, ComboPlayer>;
};

export function buildModel(xgHome: number, xgAway: number, rho: number, params: ComboParams | null, players: ComboPlayer[]): ComboModel {
  const stats: Partial<Record<StatName, Matrix>> = {};
  for (const [k, v] of Object.entries(params?.stats ?? {}) as [StatName, CountParams][]) stats[k] = countMatrix(v);
  return { cells: goalCells(scoreMatrix(xgHome, xgAway, rho), params?.h1 ?? 0.45), stats, players: new Map(players.map((p) => [p.pid, p])) };
}

/** Every selection the match offers: goals always, a statistic when its model has the match, the players given. */
export function comboSelections(model: ComboModel, teamName: (t: 1 | 2) => string): ComboSel[] {
  const out = goalSelections();
  for (const st of ["corners", "cards", "shots", "shots_on"] as StatName[]) if (model.stats[st]) out.push(...statSelections(st));
  out.push(...playerSelections([...model.players.values()], teamName));
  return out;
}

function statBlock(model: ComboModel, stat: StatName, sels: ComboSel[]): number {
  if (!sels.length) return 1;
  const m = model.stats[stat];
  if (!m) return 0;
  let p = 0;
  for (let i = 0; i < m.length; i++) for (let j = 0; j < m.length; j++) if (m[i][j] > 0 && sels.every((s) => s.count!(i, j))) p += m[i][j];
  return p;
}

// shots on target together with total shots: each shot on target with probability r (expected on target / expected shots)
function shotsBlock(model: ComboModel, shots: ComboSel[], sot: ComboSel[]): number {
  const S = model.stats.shots, O = model.stats.shots_on;
  if (!shots.length) return statBlock(model, "shots_on", sot);
  if (!sot.length) return statBlock(model, "shots", shots);
  if (!S || !O) return 0;
  const n = S.length, cap = O.length;
  const rh = Math.min(1, sumRow(O, 0) / Math.max(1e-9, sumRow(S, 0)));
  const ra = Math.min(1, sumRow(O, 1) / Math.max(1e-9, sumRow(S, 1)));
  const bh = binomRows(n, cap, rh), ba = binomRows(n, cap, ra);
  // P(the shots on target pass | i, j shots) = (bh A ba')[i][j], A[x][y] = the shots on target pass with x, y on target:
  // two matrix products instead of every split of every cell
  const A = Array.from({ length: cap }, (_, x) => Array.from({ length: cap }, (_, y) => (sot.every((s) => s.count!(x, y)) ? 1 : 0)));
  const T = bh.map((row) => Array.from({ length: cap }, (_, y) => {
    let t = 0;
    for (let x = 0; x < cap; x++) if (row[x] && A[x][y]) t += row[x];
    return t;
  }));
  let p = 0;
  for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
    if (S[i][j] < 1e-9 || !shots.every((s) => s.count!(i, j))) continue;
    let q = 0;
    for (let y = 0; y < cap; y++) q += T[i][y] * ba[j][y];
    p += S[i][j] * q;
  }
  return p;
}
function sumRow(m: Matrix, side: 0 | 1): number { // expected count of one side
  let e = 0;
  for (let i = 0; i < m.length; i++) for (let j = 0; j < m.length; j++) e += m[i][j] * (side === 0 ? i : j);
  return e;
}
function binomRows(n: number, cap: number, r: number): number[][] {
  return Array.from({ length: n }, (_, i) => Array.from({ length: cap }, (_, x) => (x <= i ? binom(i, x, r) : 0)));
}

/** Joint probability of the selections, model only (no rescaling). */
export function jointModel(model: ComboModel, sels: ComboSel[]): number {
  if (!sels.length) return 0;
  const goals = sels.filter((s) => s.goal);
  const pl = sels.filter((s) => s.player);
  let gp = 0;
  if (goals.length || pl.length) {
    let fac: ((c: Cell) => number) | null = null;
    if (pl.length) {
      const conds = new Map<string, { kind: PlayerKind; k: number }[]>();
      for (const s of pl) conds.set(s.player!.pid, [...(conds.get(s.player!.pid) ?? []), { kind: s.player!.kind, k: s.player!.k }]);
      const byTeam = ([1, 2] as const).map((t) => [...conds.keys()].map((id) => model.players.get(id)!).filter((p) => p && p.team === t));
      const f = byTeam.map((ps) => (ps.length ? teamFactor(ps, conds) : null));
      fac = (c) => (f[0] ? f[0][c.h][c.ft === 1 ? 1 : 0] : 1) * (f[1] ? f[1][c.a][c.ft === 2 ? 1 : 0] : 1);
    }
    for (const c of model.cells) {
      if (!goals.every((s) => s.goal!(c))) continue;
      gp += c.w * (fac ? fac(c) : 1);
    }
  } else gp = 1;
  const by = (st: StatName) => sels.filter((s) => s.stat === st);
  return gp * statBlock(model, "corners", by("corners")) * statBlock(model, "cards", by("cards")) * shotsBlock(model, by("shots"), by("shots_on"));
}

export type ComboResult = {
  pModel: number; // joint, model only
  p: number; // joint rescaled to the final probabilities of the selections
  fair: number | null; // 1 / p
  ev: number | null; // at the typed Sisal price
  rescaled: number; // selections whose final probability was used
  impossible: boolean; // no outcome satisfies them all
  estOdds: number | null; // estimated Sisal My Combo price: singles' prices x their probabilities / joint
};

export function selectionProb(model: ComboModel, s: ComboSel, finals: Record<string, number>, cache?: Map<string, number>): number {
  const f = finals[s.key];
  if (f != null) return f;
  const hit = cache?.get(s.key);
  if (hit != null) return hit;
  const p = jointModel(model, [s]);
  cache?.set(s.key, p);
  return p;
}

export function evaluateCombo(model: ComboModel, sels: ComboSel[], finals: Record<string, number>, prices: Record<string, number>,
  odds: number | null, cache?: Map<string, number>): ComboResult {
  const pModel = jointModel(model, sels);
  let ratio = 1;
  let rescaled = 0;
  let est = 1;
  for (const s of sels) {
    const pm = cache?.get(`m:${s.key}`) ?? jointModel(model, [s]);
    cache?.set(`m:${s.key}`, pm);
    const f = finals[s.key];
    if (f != null && pm > 1e-9) {
      ratio *= f / pm;
      rescaled++;
    }
    const pi = f ?? pm;
    est *= (prices[s.key] ?? SISAL_MARGIN / Math.max(pi, 1e-9)) * pi;
  }
  // never above the least likely selection (rescaling cannot make the combo likelier than its hardest part)
  const cap = Math.min(...sels.map((s) => finals[s.key] ?? cache?.get(`m:${s.key}`) ?? 1), 1);
  const p = sels.length ? Math.min(pModel * ratio, cap) : 0;
  return {
    pModel, p, fair: p > 1e-9 ? 1 / p : null, ev: odds && odds > 1 && p > 0 ? p * odds - 1 : null, rescaled,
    impossible: sels.length > 0 && pModel < 1e-12, estOdds: sels.length && p > 1e-9 ? Math.max(1.01, est / p) : null,
  };
}

// ---- "Genera My Combo": the likeliest combination whose estimated price falls in [qMin, qMax]
export const GEN = { minProb: 0.4, minOdds: 1.25, maxLegs: 6, beam: 30, minStart: 0.8 };

/** Beam search over the selections that pass the user's rules (probability at least 40%, single price, Sisal's or estimated,
 * at least 1.25), one per group: each step keeps the likeliest combinations still under qMax and remembers the likeliest
 * one inside [qMin, qMax]. Goal selections are masks over the goal cells, so a step costs one dot product per candidate. */
export function generateCombo(model: ComboModel, all: ComboSel[], finals: Record<string, number>, prices: Record<string, number>,
  qMin: number, qMax: number): { sels: ComboSel[]; r: ComboResult } | null {
  const cache = new Map<string, number>();
  const cells = model.cells;
  const nc = cells.length;
  const cands = all.filter((s) => {
    if (s.player && (model.players.get(s.player.pid)?.start ?? 0) < GEN.minStart) return false;
    const p = selectionProb(model, s, finals, cache);
    const price = prices[s.key] ?? SISAL_MARGIN / Math.max(p, 1e-9);
    return p >= GEN.minProb && price >= GEN.minOdds;
  });
  const pm = cands.map((s) => jointModel(model, [s])); // model alone, for the rescaling
  const pf = cands.map((s, i) => finals[s.key] ?? pm[i]);
  const ratio = cands.map((s, i) => (finals[s.key] != null && pm[i] > 1e-9 ? finals[s.key] / pm[i] : 1));
  const factor = cands.map((s, i) => (prices[s.key] ?? SISAL_MARGIN / Math.max(pf[i], 1e-9)) * pf[i]); // price x probability
  const masks = cands.map((s) => (s.goal ? Uint8Array.from(cells, (c) => (s.goal!(c) ? 1 : 0)) : null));
  const hits = masks.map((m) => (m ? Uint32Array.from([...m.keys()].filter((c) => m[c])) : null)); // the cells a goal selection keeps
  // the players' factor per cell for a set of player selections (cached by the set)
  const playerVec = new Map<string, Float64Array>();
  const pv = (idx: number[]): Float64Array | null => {
    const ps = idx.filter((i) => cands[i].player);
    if (!ps.length) return null;
    const key = ps.join(",");
    let v = playerVec.get(key);
    if (!v) {
      const conds = new Map<string, { kind: PlayerKind; k: number }[]>();
      for (const i of ps) {
        const p = cands[i].player!;
        conds.set(p.pid, [...(conds.get(p.pid) ?? []), { kind: p.kind, k: p.k }]);
      }
      const f = ([1, 2] as const).map((t) => {
        const team = [...conds.keys()].map((id) => model.players.get(id)!).filter((p) => p && p.team === t);
        return team.length ? teamFactor(team, conds) : null;
      });
      v = Float64Array.from(cells, (c) => (f[0] ? f[0][c.h][c.ft === 1 ? 1 : 0] : 1) * (f[1] ? f[1][c.a][c.ft === 2 ? 1 : 0] : 1));
      playerVec.set(key, v);
    }
    return v;
  };
  const statCache = new Map<string, number>();
  const statsP = (idx: number[]): number => {
    let p = 1;
    for (const st of ["corners", "cards"] as StatName[]) {
      const ss = idx.filter((i) => cands[i].stat === st);
      if (!ss.length) continue;
      const k = `${st}:${ss.join(",")}`;
      if (!statCache.has(k)) statCache.set(k, statBlock(model, st, ss.map((i) => cands[i])));
      p *= statCache.get(k)!;
    }
    const sh = idx.filter((i) => cands[i].stat === "shots"), so = idx.filter((i) => cands[i].stat === "shots_on");
    if (sh.length || so.length) {
      const k = `sh:${sh.join(",")}|${so.join(",")}`;
      if (!statCache.has(k)) statCache.set(k, shotsBlock(model, sh.map((i) => cands[i]), so.map((i) => cands[i])));
      p *= statCache.get(k)!;
    }
    return p;
  };
  type Node = { idx: number[]; groups: Set<string>; goal: Float64Array | null; parent: Node | null; gp: number; p: number; est: number };
  // goals and players of a node plus candidate i: a statistic leaves them as they were, a goal selection sums its cells
  const goalPart = (node: Node, i: number, idx: number[]): number => {
    const s = cands[i];
    if (s.stat) return node.gp;
    const v = pv(idx);
    const goal = node.goal!;
    let gp = 0;
    const h = hits[i];
    if (h) for (let k = 0; k < h.length; k++) gp += v ? goal[h[k]] * v[h[k]] : goal[h[k]];
    else for (let c = 0; c < nc; c++) gp += v ? goal[c] * v[c] : goal[c];
    return gp;
  };
  const result = (idx: number[], gp: number): { p: number; est: number } => {
    const pModel = gp * statsP(idx);
    const capped = Math.min(pModel * idx.reduce((x, i) => x * ratio[i], 1), ...idx.map((i) => pf[i]), 1);
    return { p: capped, est: capped > 1e-9 ? Math.max(1.01, idx.reduce((x, i) => x * factor[i], 1) / capped) : Infinity };
  };
  let beam: Node[] = [{ idx: [], groups: new Set(), goal: Float64Array.from(cells, (c) => c.w), parent: null, gp: 1, p: 1, est: 1 }];
  let best: Node | null = null;
  for (let depth = 0; depth < GEN.maxLegs && beam.length; depth++) {
    const next: Node[] = [];
    for (const node of beam) {
      const from = node.idx.length ? node.idx[node.idx.length - 1] + 1 : 0;
      for (let i = from; i < cands.length; i++) {
        const s = cands[i];
        if (node.groups.has(s.group)) continue;
        const idx = [...node.idx, i];
        const gp = goalPart(node, i, idx);
        const r = result(idx, gp);
        if (r.p < 0.02 || !Number.isFinite(r.est)) continue;
        if (node.idx.length && r.p > node.p * 0.97) continue; // hardly changes the joint: nothing Sisal would pay for
        const n: Node = { idx, groups: new Set([...node.groups, s.group]), goal: null, parent: node, gp, p: r.p, est: r.est };
        if (r.est >= qMin && r.est <= qMax && idx.length >= 2 && (!best || r.p > best.p)) best = n;
        if (r.est <= qMax) next.push(n);
      }
    }
    beam = next.sort((a, b) => b.p - a.p).slice(0, GEN.beam);
    for (const n of beam) { // the goal weights only for the combinations kept
      const m = masks[n.idx[n.idx.length - 1]];
      n.goal = m ? n.parent!.goal!.map((w, c) => (m[c] ? w : 0)) : n.parent!.goal;
    }
  }
  if (!best) return null;
  const sels = best.idx.map((k) => cands[k]);
  return { sels, r: evaluateCombo(model, sels, finals, prices, null, cache) };
}
