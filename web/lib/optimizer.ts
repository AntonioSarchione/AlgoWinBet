// Slip optimizer for the dashboard: a line-by-line port of src/algowinbet/optimizer.py (optimize, make_slip, violations),
// correlation.py (joint_of, pair_dependence) and risk.py (assign_stakes), so every filter change can rebuild slips on the
// published opportunities in milliseconds. Parity with Python: web/scripts/optimizer-parity.ts on scripts/optimizer_golden.py.
//
// Scope: one leg per fixture (optimizer max_legs_per_fixture = 1, the published setting). Same-fixture combos need the
// model's score matrices, which are not published; with more than one leg per fixture this port refuses to run.

export type OptOpp = {
  fixture_id: string;
  sel_key: string;
  competition: string;
  home: string;
  away: string;
  odds: number;
  p_final: number;
  p_struct: number;
  p_market: number | null;
  uncertainty: number;
  disagreement: number;
  score: number;
  status: string;
  dq_lineup: number;
};

export type OptimizerCfg = {
  odds_min: number;
  odds_max: number;
  min_probability: number;
  max_legs: number;
  max_legs_per_fixture: number;
  max_legs_per_competition: number;
  min_leg_probability: number;
  beam_width: number;
  output_count: number;
  max_overlap: number;
  correlation_limit: number;
  min_slip_ev: number;
  cross_match_rho: number;
  same_competition_rho: number;
  include_watch: boolean;
  include_fair: boolean;
  multi_bonus: number[];
  multi_bonus_min_odds: number;
  w_ev: number;
  w_prob: number;
  w_div: number;
  w_unc: number;
  w_corr: number;
  w_disagree: number;
};

export type RiskCfg = {
  bankroll: number;
  stake_method: string;
  flat_stake: number;
  pct: number;
  kelly_fraction: number;
  max_stake_pct_per_slip: number;
  max_exposure_total_pct: number;
  max_exposure_per_fixture_pct: number;
  max_exposure_per_team_pct: number;
};

export type OptSettings = { optimizer: OptimizerCfg; z: number; risk: RiskCfg };

export type Slip<T extends OptOpp = OptOpp> = {
  legs: T[];
  total_odds: number;
  joint_probability: number;
  fair_odds: number;
  ev: number;
  ev_lower: number;
  uncertainty: number;
  correlation_penalty: number;
  model_disagreement: number;
  objective: number;
  stake: number;
  bonus: number; // Sisal multiple bonus on the net winnings (0.04 = +4%), already inside ev / ev_lower
  riskNotes: string[];
};

export type OptResult<T extends OptOpp = OptOpp> = { slips: Slip<T>[]; noBet: boolean; reasons: string[]; eligible: number };

const legKey = (l: OptOpp) => `${l.fixture_id}\u0000${l.sel_key}`;
const pct = (x: number, d = 0) => `${(x * 100).toFixed(d)}%`;
const signedPct = (x: number, d = 1) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(d)}%`;

function pairDependence(a: OptOpp, b: OptOpp, o: OptimizerCfg): number {
  if (a === b) return 0;
  if (a.fixture_id === b.fixture_id) return 0; // never combined: max_legs_per_fixture = 1 (see header)
  if (a.competition === b.competition) return o.same_competition_rho;
  return o.cross_match_rho;
}

// Sisal multiple bonus for these legs: share added to the net winnings (0 below 5 legs or with a leg under the minimum odds)
export function multiBonus(legs: { odds: number }[], o: OptimizerCfg): number {
  const n = legs.length;
  const table = o.multi_bonus ?? [];
  if (n < 5 || !table.length || legs.some((l) => l.odds < o.multi_bonus_min_odds)) return 0;
  return table[Math.min(n, 4 + table.length) - 5];
}

function makeSlip<T extends OptOpp>(legs: T[], s: OptSettings, C: Map<string, number>, idx: number[]): Slip<T> {
  const o = s.optimizer;
  let odds = 1;
  for (const l of legs) odds *= l.odds;
  // joint_of: one leg per fixture, so every group is a single calibrated marginal; cross-fixture pairs get exp(-C)
  let joint = 1;
  for (const l of legs) joint *= l.p_final;
  let tot = 0;
  let pairs = 0;
  for (let i = 0; i < legs.length; i++) {
    for (let j = i + 1; j < legs.length; j++) {
      pairs += 1;
      const c = C.get(`${Math.min(idx[i], idx[j])},${Math.max(idx[i], idx[j])}`) ?? 0;
      tot += c;
      if (legs[i].fixture_id !== legs[j].fixture_id) joint *= Math.exp(-c);
    }
  }
  const pen = pairs ? tot / pairs : 0;
  let rel2 = 0;
  for (const l of legs) rel2 += (l.uncertainty / Math.max(l.p_final, 1e-9)) ** 2;
  const rel = Math.sqrt(rel2);
  const pLo = joint * Math.exp(-s.z * rel);
  const bonus = multiBonus(legs, o);
  const payout = 1 + (odds - 1) * (1 + bonus); // what a winning unit returns, bonus on the net winnings included
  const ev = joint * payout - 1;
  const evLo = pLo * payout - 1;
  let dis = 0;
  for (const l of legs) dis += l.disagreement;
  dis /= legs.length;
  const div = new Set(legs.map((l) => l.competition)).size / legs.length;
  const obj = o.w_ev * ev + o.w_prob * joint + o.w_div * div - o.w_unc * Math.min(1, rel) - o.w_corr * pen - o.w_disagree * dis;
  return {
    legs: [...legs], total_odds: odds, joint_probability: joint, fair_odds: joint > 0 ? 1 / joint : Infinity, ev, ev_lower: evLo,
    uncertainty: joint * rel, correlation_penalty: pen, model_disagreement: dis, objective: obj, stake: 0, bonus, riskNotes: [],
  };
}

function violations(sl: Slip, o: OptimizerCfg): string[] {
  const v: string[] = [];
  if (sl.total_odds < o.odds_min) v.push(`quota ${sl.total_odds.toFixed(2)} < minima ${o.odds_min}`);
  if (sl.total_odds > o.odds_max) v.push(`quota ${sl.total_odds.toFixed(2)} > massima ${o.odds_max}`);
  if (sl.joint_probability < o.min_probability) v.push(`probabilità ${pct(sl.joint_probability, 1)} < minima ${pct(o.min_probability, 1)}`);
  if (sl.correlation_penalty > o.correlation_limit) v.push(`correlazione ${sl.correlation_penalty.toFixed(2)} > limite ${o.correlation_limit}`);
  if (sl.ev < o.min_slip_ev) v.push(`EV schedina ${signedPct(sl.ev)} < minimo ${signedPct(o.min_slip_ev)}`);
  return v;
}

function overlap(a: Slip, b: Slip): number {
  const ka = new Set(a.legs.map(legKey));
  const kb = new Set(b.legs.map(legKey));
  let inter = 0;
  for (const k of ka) if (kb.has(k)) inter += 1;
  return inter / new Set([...ka, ...kb]).size;
}

export function optimize<T extends OptOpp>(opps: T[], s: OptSettings): OptResult<T> {
  const o = s.optimizer;
  if (o.max_legs_per_fixture !== 1) throw new Error("optimizer.ts supporta solo una leg per partita (max_legs_per_fixture = 1)");
  const ok = new Set(["STRONG", "CANDIDATE", ...(o.include_watch ? ["WATCH"] : []), ...(o.include_fair ? ["FAIR"] : [])]);
  let elig = opps.filter((x) => ok.has(x.status));
  const reasons: string[] = [];
  if (!elig.length) {
    reasons.push(`Nessuna selezione idonea (con valore${o.include_fair ? " o a quota equa" : ""}) su ${opps.length} con i filtri scelti.`);
    return { slips: [], noBet: true, reasons, eligible: 0 };
  }
  elig = elig.filter((x) => x.p_final >= o.min_leg_probability);
  if (!elig.length) {
    reasons.push(`Tutte le opportunità hanno probabilità < soglia per leg ${pct(o.min_leg_probability)}.`);
    return { slips: [], noBet: true, reasons, eligible: 0 };
  }
  // keep the best few candidates per fixture to bound the search space (insertion order = first appearance by score)
  const perFix = new Map<string, T[]>();
  for (const x of [...elig].sort((a, b) => b.score - a.score)) {
    const lst = perFix.get(x.fixture_id) ?? [];
    lst.push(x);
    perFix.set(x.fixture_id, lst);
  }
  let cands = [...perFix.values()].flatMap((lst) => lst.slice(0, Math.max(o.max_legs_per_fixture, 3)));
  cands.sort((a, b) => b.score - a.score);
  cands = cands.slice(0, 80);
  const n = cands.length;
  const C = new Map<string, number>();
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) C.set(`${i},${j}`, pairDependence(cands[i], cands[j], o));

  const allowed = (idx: number[], nw: number) => {
    const nl = cands[nw];
    let sameFix = 0;
    let sameComp = 0;
    for (const i of idx) {
      const l = cands[i];
      if (l.fixture_id === nl.fixture_id) {
        sameFix += 1;
        if (l.sel_key === nl.sel_key) return false;
      }
      if (l.competition === nl.competition) sameComp += 1;
    }
    return sameFix < o.max_legs_per_fixture && sameComp < o.max_legs_per_competition;
  };

  const pool = new Map<string, Slip<T>>();
  let beam: number[][] = cands.map((_, i) => [i]);
  const seen = new Set<string>();
  let nearest: { slip: Slip<T>; v: string[] } | null = null;
  for (let depth = 1; depth <= o.max_legs; depth++) {
    const scored: [number, number[]][] = [];
    for (const idx of beam) {
      const key = idx.join(",");
      if (seen.has(key)) continue;
      seen.add(key);
      const sl = makeSlip(idx.map((i) => cands[i]), s, C, idx);
      if (sl.total_odds > o.odds_max) continue; // odds only grow with more legs: prune branch
      const v = violations(sl, o);
      if (!v.length) pool.set(sl.legs.map(legKey).sort().join("\u0001"), sl);
      else if (!nearest || v.length < nearest.v.length || (v.length === nearest.v.length && sl.objective > nearest.slip.objective)) nearest = { slip: sl, v };
      scored.push([sl.objective, idx]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    const next: number[][] = [];
    for (const [, idx] of scored.slice(0, o.beam_width)) {
      for (let nw = 0; nw < n; nw++) {
        if (!idx.includes(nw) && allowed(idx, nw)) next.push([...idx, nw].sort((a, b) => a - b));
      }
    }
    if (!next.length) break;
    beam = next;
  }

  if (!pool.size) {
    reasons.push(
      `${elig.length} opportunità idonee ma nessuna combinazione rispetta insieme quota ${o.odds_min}–${o.odds_max}, ` +
        `correlazione ≤ ${o.correlation_limit}, EV schedina ≥ ${signedPct(o.min_slip_ev, 0)} e massimo ${o.max_legs} eventi.`,
    );
    if (nearest) reasons.push(`La più vicina (quota ${nearest.slip.total_odds.toFixed(2)}) viola: ${nearest.v.join("; ")}.`);
    return { slips: [], noBet: true, reasons, eligible: elig.length };
  }
  const ranked = [...pool.values()].sort((a, b) => b.objective - a.objective);
  const chosen: Slip<T>[] = [];
  for (const sl of ranked) {
    if (chosen.every((c) => overlap(sl, c) <= o.max_overlap)) chosen.push(sl);
    if (chosen.length >= o.output_count) break;
  }
  assignStakes(chosen, s.risk);
  return { slips: chosen, noBet: false, reasons, eligible: elig.length };
}

function rawStake(sl: Slip, r: RiskCfg): number {
  if (r.stake_method === "flat") return r.flat_stake;
  if (r.stake_method === "pct") return r.bankroll * r.pct;
  const b = (sl.total_odds - 1) * (1 + sl.bonus); // net winnings per unit, Sisal multiple bonus included
  const f = b > 0 ? sl.ev_lower / b : 0; // Kelly at the conservative joint probability
  return Math.max(0, f) * r.kelly_fraction * r.bankroll;
}

// Python round(x, 2) (half to even on the binary value) matches Math.round on every stake that is not an exact .xx5 tie.
const round2 = (x: number) => Math.round(x * 100) / 100;

export function assignStakes(slips: Slip[], r: RiskCfg): void {
  const totalCap = r.bankroll * r.max_exposure_total_pct;
  const fixCap = r.bankroll * r.max_exposure_per_fixture_pct;
  const teamCap = r.bankroll * r.max_exposure_per_team_pct;
  let usedTotal = 0;
  const usedFix = new Map<string, number>();
  const usedTeam = new Map<string, number>();
  for (const sl of slips) {
    let stake = Math.min(rawStake(sl, r), r.bankroll * r.max_stake_pct_per_slip, totalCap - usedTotal);
    for (const l of sl.legs) {
      stake = Math.min(stake, fixCap - (usedFix.get(l.fixture_id) ?? 0));
      for (const team of [l.home, l.away]) stake = Math.min(stake, teamCap - (usedTeam.get(team) ?? 0));
    }
    stake = Math.max(0, round2(stake));
    sl.stake = stake;
    sl.riskNotes = stake === 0 ? ["stake 0: nessun vantaggio conservativo o limite di esposizione raggiunto"] : [];
    usedTotal += stake;
    for (const l of sl.legs) {
      usedFix.set(l.fixture_id, (usedFix.get(l.fixture_id) ?? 0) + stake);
      for (const team of [l.home, l.away]) usedTeam.set(team, (usedTeam.get(team) ?? 0) + stake);
    }
  }
}

// explain.py explain_slip (the parts the dashboard shows)
export function explainSlip(sl: Slip, z: number, minBonusOdds = 1.25) {
  const pos: string[] = [];
  const neg: string[] = [];
  const strong = sl.legs.filter((l) => l.status === "STRONG").length;
  if (sl.ev > 0) pos.push(`EV schedina ${signedPct(sl.ev)} con probabilità congiunta ${pct(sl.joint_probability, 1)}`);
  if (sl.bonus) pos.push(`bonus multipla Sisal +${pct(sl.bonus)} sulla vincita netta (${sl.legs.length} eventi a quota ≥ ${minBonusOdds.toFixed(2)})`);
  const fair = sl.legs.filter((l) => l.status === "FAIR").length;
  if (fair) pos.push(`${fair}/${sl.legs.length} eventi a quota equa: Sisal non trattiene margine su di loro`);
  if (sl.ev_lower > 0) pos.push(`EV positivo anche stimando la probabilità al limite inferiore (${signedPct(sl.ev_lower)})`);
  if (strong) pos.push(`${strong}/${sl.legs.length} eventi con valore alto`);
  if (sl.correlation_penalty < 0.05) pos.push("dipendenza tra gli eventi bassa");
  if (new Set(sl.legs.map((l) => l.competition)).size > 1) pos.push("eventi distribuiti su più competizioni");
  if (sl.joint_probability < 0.15) neg.push(`probabilità congiunta bassa (${pct(sl.joint_probability, 1)}): la schedina perde molto più spesso di quanto vince`);
  if (sl.ev_lower <= 0) neg.push("EV non robusto: con probabilità al limite inferiore l'EV è ≤ 0");
  const unconf = sl.legs.filter((l) => l.dq_lineup <= 0.5).length;
  if (unconf) neg.push(`${unconf} ${unconf === 1 ? "evento" : "eventi"} senza formazione confermata`);
  if (sl.legs.some((l) => l.p_market == null)) neg.push("almeno un evento senza prezzo di mercato di confronto");
  if (sl.legs.length >= 5) neg.push(`${sl.legs.length} eventi: l'errore di stima si accumula`);
  const pLow = sl.joint_probability * Math.exp(-z * (sl.uncertainty / Math.max(sl.joint_probability, 1e-9)));
  return {
    positive_factors: pos,
    negative_factors: neg,
    what_would_change_it: [
      `quota totale di pareggio (EV=0): ${(1 + (sl.fair_odds - 1) / (1 + sl.bonus)).toFixed(2)} (quota attuale ${sl.total_odds.toFixed(2)}${sl.bonus ? `, bonus +${pct(sl.bonus)} incluso` : ""})`,
      "formazione ufficiale diversa dall'attesa / nuovi infortuni su giocatori chiave (ricalcolo automatico)",
      "movimento di quota > 3% su uno qualsiasi degli eventi",
    ],
    p_low: pLow,
  };
}
