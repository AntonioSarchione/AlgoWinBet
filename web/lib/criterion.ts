// Pass criterion of the paper trading: the strict version (agreed 2026-10-04, replaces the one of 2026-10-02). Nothing is
// played for real until every required check passes at the same time.
//  1. sample ...... >= 300 settled value bets on >= 100 different matches over >= 8 weeks
//  2. main ........ mean EV at the Pinnacle close >= +1.5% with the 95% lower bound above 0. A bet without the Pinnacle close
//                   counts 0 (never dropped: dropping could hide exactly the bad ones). Failed when, after 300, even the 95%
//                   upper bound is below 0.
//  3. coverage .... Pinnacle close for >= 85% of the value bets
//  4. stability ... mean EV at the Pinnacle close above 0 in the first half, in the second half and in the last 300 bets
//  5. markets ..... no market type with >= 50 bets whose 95% upper bound is below 0 (a losing market is switched off)
//  6. slips ....... mean EV of the settled slips at the Pinnacle close above 0, on >= 50 slips
//  7. calibration . on every recorded selection (>= 1000 decided): no probability band off by more than the family-wise 95%
//                   threshold, mean predicted probability within 2 points of the hit rate, log loss not worse than the Sisal
//                   closing prices without margin
//  8. version ..... >= half of the bets (150 of 300) from the current model version, with mean EV at the close above 0
//  9. Sisal ....... taken odds not worse than the Sisal close on average (mean CLV >= 0)
// 10. ROI ......... shown only; an alarm below -15% after 300 bets, or when even its 95% upper bound is below the EV measured
//                   at the close (results far worse than the measured value: something is wrong)
// Every interval is grouped by match: selections of the same match move together, so they are not counted as independent.
import { parseJSON, type PaperLeg, type PaperSlip } from "./db";

export const CRITERION = {
  n: 300, fixtures: 100, weeks: 8, coverage: 0.85, minEv: 0.015, perMarket: 50, slips: 50, nCal: 1000, maxBias: 0.02, minBand: 20,
  recentShare: 0.5, roiAlarm: -0.15,
} as const;

export type State = "pass" | "fail" | "open" | "alarm";
export type Interval = { n: number; mean: number | null; lo: number | null; hi: number | null };
export type Check = { key: string; label: string; state: State; now: string; detail: string; info?: boolean };

const Z95 = 1.96;
const EPS = 1e-9;

// mean with a 95% interval; with groups, the standard error is the cluster-robust one (each group's residuals summed first)
export function interval(xs: number[], groups?: string[]): Interval {
  const n = xs.length;
  if (!n) return { n, mean: null, lo: null, hi: null };
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  let se: number | null = null;
  if (groups) {
    const r = new Map<string, number>();
    xs.forEach((x, i) => r.set(groups[i], (r.get(groups[i]) ?? 0) + x - mean));
    const g = r.size;
    if (g >= 2) se = Math.sqrt((g / (g - 1)) * [...r.values()].reduce((a, v) => a + v * v, 0)) / n;
  } else if (n >= 2) {
    se = Math.sqrt(xs.reduce((a, x) => a + (x - mean) ** 2, 0) / (n - 1) / n);
  }
  return se == null ? { n, mean, lo: null, hi: null } : { n, mean, lo: mean - Z95 * se, hi: mean + Z95 * se };
}

const settled = (l: PaperLeg) => l.result === "won" || l.result === "lost" || l.result === "void";
const decided = (l: PaperLeg) => l.result === "won" || l.result === "lost";
export const isValue = (l: PaperLeg) => l.status === "STRONG" || l.status === "CANDIDATE";
const chrono = (a: PaperLeg, b: PaperLeg) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id - b.id);
// EV at the Pinnacle close; a bet without the close counts 0
const evClose = (l: PaperLeg) => (l.close_fair == null ? 0 : l.odds * l.close_fair - 1);
const evOf = (legs: PaperLeg[]) => interval(legs.map(evClose), legs.map((l) => l.fixture_id));
export const marketOf = (l: PaperLeg) => l.sel_key.split("|")[0];

export function valueSummary(legs: PaperLeg[]) {
  const s = legs.filter(settled).sort(chrono);
  const withClv = s.filter((l) => l.close_odds);
  return {
    n: s.length,
    covered: s.filter((l) => l.close_fair != null).length,
    ev: evOf(s),
    clv: interval(withClv.map((l) => l.odds / (l.close_odds as number) - 1), withClv.map((l) => l.fixture_id)),
    roi: interval(s.map((l) => (l.result === "won" ? l.odds - 1 : l.result === "lost" ? -1 : 0)), s.map((l) => l.fixture_id)),
    legs: s,
  };
}

// two-sided normal quantile for a family-wise 95% over k bands (Bonferroni): 10 bands -> about 2.8
function zFor(k: number): number {
  const a = 0.05 / Math.max(1, k) / 2;
  // Abramowitz-Stegun 26.2.23 for the upper tail quantile, error below 5e-4 (enough for a threshold)
  const t = Math.sqrt(-2 * Math.log(a));
  return t - (2.515517 + 0.802853 * t + 0.010328 * t * t) / (1 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t ** 3);
}

export function calibrationBands(legs: PaperLeg[]) {
  const bins = Array.from({ length: 10 }, () => ({ p: 0, y: 0, n: 0, legs: [] as PaperLeg[] }));
  for (const l of legs) {
    if (!decided(l)) continue;
    const b = bins[Math.min(9, Math.floor(l.p * 10))];
    b.p += l.p;
    b.y += l.result === "won" ? 1 : 0;
    b.n += 1;
    b.legs.push(l);
  }
  const rows = bins
    .map((b, i) => {
      const p = b.n ? b.p / b.n : 0;
      const y = b.n ? b.y / b.n : 0;
      // standard error grouped by match: selections of the same match win and lose together (a low-scoring weekend
      // wins every under at once), so a band of many selections from few matches must not look significant
      const r = interval(b.legs.map((l) => (l.result === "won" ? 1 : 0) - l.p), b.legs.map((l) => l.fixture_id));
      const binom = b.n ? Math.sqrt(Math.max(p * (1 - p), 1e-6) / b.n) : 0;
      const se = r.hi != null && r.mean != null ? Math.max((r.hi - r.mean) / Z95, binom) : binom;
      return { lo: i / 10, n: b.n, p, y, z: se ? (y - p) / se : 0 };
    })
    .filter((r) => r.n > 0);
  const tested = rows.filter((r) => r.n >= CRITERION.minBand);
  const zMax = zFor(tested.length);
  const n = rows.reduce((s, r) => s + r.n, 0);
  // mean predicted probability minus hit rate over every decided selection (positive: the model promises too much)
  const bias = n ? bins.reduce((s, b) => s + b.p - b.y, 0) / n : null;
  return { rows, tested: tested.length, zMax, worst: tested.reduce((m, r) => Math.max(m, Math.abs(r.z)), 0), n, bias };
}

// log loss of our probability minus that of the Sisal closing price without margin, per decided selection (negative = we are better)
export function logLossGap(legs: PaperLeg[]): Interval {
  const ll = (p: number, won: boolean) => -Math.log(Math.min(1 - 1e-6, Math.max(1e-6, won ? p : 1 - p)));
  const d = legs.filter((l) => decided(l) && l.close_sisal_fair != null);
  return interval(d.map((l) => ll(l.p, l.result === "won") - ll(l.close_sisal_fair as number, l.result === "won")), d.map((l) => l.fixture_id));
}

// EV of the settled slips at the Pinnacle close: the model's joint probability (which keeps the correlation of selections of
// the same match) rescaled leg by leg from the model probability to the Pinnacle fair one, times the payout with the bonus.
// Only slips whose selections all have the Pinnacle close.
export function slipCloseEv(slips: PaperSlip[], legs: PaperLeg[]): Interval {
  const byKey = new Map(legs.map((l) => [`${l.fixture_id}|${l.sel_key}`, l]));
  const xs: number[] = [];
  for (const s of slips) {
    if (s.result !== "won" && s.result !== "lost") continue;
    const ls = parseJSON<{ fixture_id: string; sel_key: string }[]>(s.legs, []).map((l) => byKey.get(`${l.fixture_id}|${l.sel_key}`));
    if (!ls.length || ls.some((l) => !l || l.close_fair == null || !(l.p > 0))) continue;
    const ratio = ls.reduce((a, l) => a * ((l as PaperLeg).close_fair as number) / (l as PaperLeg).p, 1);
    xs.push(Math.min(1, s.joint * ratio) * (1 + (s.total_odds - 1) * (1 + (s.bonus ?? 0))) - 1);
  }
  return interval(xs);
}

const sg = (x: number | null, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
const ci = (i: Interval) => (i.lo == null || i.hi == null ? "intervallo non ancora calcolabile" : `intervallo 95% da ${sg(i.lo)} a ${sg(i.hi)}`);
const pos = (i: Interval) => i.mean != null && i.mean > EPS;

export function evaluate(all: PaperLeg[], slips: PaperSlip[] = []) {
  const value = all.filter(isValue);
  const v = valueSummary(value);
  const full = v.n >= CRITERION.n;
  const missing = Math.max(0, CRITERION.n - v.n);

  // 1. sample
  const fixtures = new Set(v.legs.map((l) => l.fixture_id)).size;
  const kos = v.legs.map((l) => Date.parse(l.kickoff)).filter(Number.isFinite);
  const spanWeeks = kos.length ? (Math.max(...kos) - Math.min(...kos)) / (7 * 864e5) : 0;
  const sample: Check = {
    key: "sample",
    label: `Campione: almeno ${CRITERION.n} giocate di valore, ${CRITERION.fixtures} partite diverse, ${CRITERION.weeks} settimane`,
    now: `${v.n} · ${fixtures} · ${spanWeeks.toFixed(1)} sett.`,
    detail: "poche giornate fortunate non devono decidere tutto",
    state: full && fixtures >= CRITERION.fixtures && spanWeeks >= CRITERION.weeks ? "pass" : "open",
  };

  // 2. main
  const main: Check = {
    key: "main",
    label: `Valore sul prezzo finale Pinnacle ≥ ${sg(CRITERION.minEv)}, oltre il caso`,
    now: `${sg(v.ev.mean)} su ${v.ev.n}`,
    detail: `${ci(v.ev)} (raggruppato per partita) · serve il limite basso sopra 0 · le giocate senza chiusura Pinnacle contano 0`,
    state:
      full && v.ev.hi != null && v.ev.hi < -EPS ? "fail"
      : full && (v.ev.mean ?? -1) >= CRITERION.minEv && (v.ev.lo ?? -1) > 0 ? "pass"
      : "open",
  };

  // 3. coverage
  const coverage = v.n ? v.covered / v.n : 0;
  const cover: Check = {
    key: "cover",
    label: `Chiusura Pinnacle per almeno l'${CRITERION.coverage * 100}% delle giocate`,
    now: `${Math.round(coverage * 100)}%`,
    detail: `${v.covered} su ${v.n} con il prezzo finale Pinnacle`,
    state: full && coverage >= CRITERION.coverage ? "pass" : "open",
  };

  // 4. stability over time
  const half = Math.floor(v.n / 2);
  const first = evOf(v.legs.slice(0, half));
  const second = evOf(v.legs.slice(half));
  const recent = evOf(v.legs.slice(-CRITERION.n));
  const stable: Check = {
    key: "stable",
    label: "Tenuta nel tempo: valore positivo nella prima metà, nella seconda e nelle ultime 300",
    now: `${sg(first.mean)} · ${sg(second.mean)} · ${sg(recent.mean)}`,
    detail: "il vantaggio deve reggere, non solo all'inizio; dopo il passaggio le ultime 300 restano sotto controllo",
    state: full && pos(first) && pos(second) && pos(recent) ? "pass" : "open",
  };

  // 5. market types
  const byMarket = new Map<string, PaperLeg[]>();
  for (const l of v.legs) byMarket.set(marketOf(l), [...(byMarket.get(marketOf(l)) ?? []), l]);
  const markets = [...byMarket.entries()]
    .filter(([, ls]) => ls.length >= CRITERION.perMarket)
    .map(([market, ls]) => ({ market, ev: evOf(ls) }));
  const losing = markets.filter((m) => m.ev.hi != null && m.ev.hi < -EPS);
  const mkt: Check = {
    key: "markets",
    label: `Nessun tipo di mercato chiaramente in perdita (da ${CRITERION.perMarket} giocate)`,
    now: `${markets.length} controllat${markets.length === 1 ? "o" : "i"}`,
    detail: losing.length
      ? `da spegnere: ${losing.map((m) => `${m.market} ${sg(m.ev.mean)} su ${m.ev.n}`).join(", ")}`
      : markets.length
        ? markets.map((m) => `${m.market} ${sg(m.ev.mean)} su ${m.ev.n}`).join(" · ")
        : `nessun tipo con almeno ${CRITERION.perMarket} giocate`,
    state: losing.length ? "alarm" : full && markets.length > 0 ? "pass" : "open",
  };

  // 6. slips
  const sl = slipCloseEv(slips, all);
  const slip: Check = {
    key: "slips",
    label: `Schedine: valore medio sul prezzo Pinnacle sopra 0, su almeno ${CRITERION.slips}`,
    now: `${sg(sl.mean)} su ${sl.n}`,
    detail: `${ci(sl)} · schedine chiuse con la chiusura Pinnacle di ogni evento; è quello che si gioca davvero`,
    state: sl.n >= CRITERION.slips && pos(sl) ? "pass" : "open",
  };

  // 7. calibration
  const bands = calibrationBands(all);
  const gap = logLossGap(all);
  const calFull = bands.n >= CRITERION.nCal;
  const bandsOk = bands.tested > 0 && bands.worst <= bands.zMax;
  const biasOk = bands.bias != null && Math.abs(bands.bias) <= CRITERION.maxBias;
  const llOk = gap.mean != null && gap.mean <= 0;
  const cal: Check = {
    key: "cal",
    label: `Calibrazione su almeno ${CRITERION.nCal} selezioni, scarto medio entro ${CRITERION.maxBias * 100} punti, log loss non peggiore di Sisal senza margine`,
    now: `${bands.n}/${CRITERION.nCal}`,
    detail:
      `scarto medio ${bands.bias == null ? "–" : `${bands.bias >= 0 ? "+" : "−"}${Math.abs(bands.bias * 100).toFixed(1)} punti`} · ` +
      `fascia peggiore a ${bands.worst.toFixed(1)} errori standard (limite ${bands.zMax.toFixed(1)} per ${bands.tested} fasce) · ` +
      `log loss rispetto a Sisal ${gap.mean == null ? "–" : `${gap.mean >= 0 ? "+" : "−"}${Math.abs(gap.mean).toFixed(4)}`} su ${gap.n}` +
      (gap.lo != null && gap.hi != null ? ` (da ${gap.lo.toFixed(4)} a ${gap.hi.toFixed(4)})` : ""),
    state: calFull && gap.lo != null && gap.lo > 0 ? "fail" : calFull && bandsOk && biasOk && llOk ? "pass" : "open",
  };

  // 8. current model version
  const current = [...all].sort(chrono).at(-1)?.model_version ?? null;
  const mine = v.legs.filter((l) => l.model_version === current);
  const mineEv = evOf(mine);
  const need = Math.ceil(CRITERION.n * CRITERION.recentShare);
  const ver: Check = {
    key: "version",
    label: `Almeno ${need} giocate con la versione attuale del modello, con valore positivo`,
    now: `${mine.length}/${need} · ${sg(mineEv.mean)}`,
    detail: `versione ${current ?? "–"}: un modello cambiato non eredita il merito del precedente`,
    state: mine.length >= need && pos(mineEv) ? "pass" : "open",
  };

  // 9. Sisal close
  const sisal: Check = {
    key: "clv",
    label: "Quota presa non peggiore della chiusura Sisal, in media",
    now: `${sg(v.clv.mean)} su ${v.clv.n}`,
    detail: `${ci(v.clv)} · conferma che giocare quando il sistema propone è praticabile`,
    state: full && v.clv.mean != null && v.clv.mean >= 0 ? "pass" : "open",
  };

  // 10. ROI: never a reason to pass, only an alarm
  const roiLow = full && (v.roi.mean ?? 0) < CRITERION.roiAlarm;
  const roiOff = full && v.roi.hi != null && v.ev.mean != null && v.roi.hi < v.ev.mean;
  const roi: Check = {
    key: "roi",
    label: "Rendimento (solo indicativo)",
    now: `${sg(v.roi.mean)} su ${v.roi.n}`,
    detail: `${ci(v.roi)} · allarme sotto ${sg(CRITERION.roiAlarm, 0)} dopo ${CRITERION.n} giocate, o se resta molto sotto il valore misurato alla chiusura`,
    state: roiLow || roiOff ? "alarm" : "open",
    info: true,
  };

  const required = [sample, main, cover, stable, mkt, slip, cal, ver, sisal];
  const checks = [...required, roi];
  const verdict: State =
    main.state === "fail" || cal.state === "fail" ? "fail"
    : roi.state === "alarm" || mkt.state === "alarm" ? "alarm"
    : required.every((c) => c.state === "pass") ? "pass"
    : "open";

  // pace: settled value bets per week since the first one was recorded
  const firstAt = value.reduce<string | null>((m, l) => (m == null || l.created_at < m ? l.created_at : m), null);
  const weeks = firstAt ? Math.max(1 / 7, (Date.now() - Date.parse(firstAt)) / (7 * 864e5)) : null;
  const perWeek = weeks ? v.n / weeks : null;
  const weeksLeft = missing && perWeek ? Math.ceil(missing / perWeek) : null;
  const passed = required.filter((c) => c.state === "pass").length;

  return { checks, verdict, value: v, bands, gap, perWeek, weeksLeft, missing, passed, required: required.length };
}

// main metric per model version: improvements change the model, so the results are kept apart
export function byVersion(all: PaperLeg[]) {
  const groups = new Map<string, PaperLeg[]>();
  for (const l of all.filter(isValue)) {
    const k = l.model_version ?? "–";
    groups.set(k, [...(groups.get(k) ?? []), l]);
  }
  return [...groups.entries()].map(([version, legs]) => ({ version, ...valueSummary(legs) })).sort((a, b) => b.n - a.n);
}
