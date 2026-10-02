// Pass criterion of the paper trading (agreed 2026-10-02). Nothing is played for real until it is passed.
//  1. main ........ >= 300 settled value bets, Pinnacle close for >= 80% of them, mean EV at the Pinnacle close >= +1% with
//                   the 95% lower bound above 0. Failed when, after 300, even the 95% upper bound is below 0.
//  2. secondary ... mean CLV on the Sisal close with the 95% lower bound above 0.
//  3. calibration . on every recorded selection (>= 1000 settled): no probability band off by more than 2 standard errors
//                   (corrected for the number of bands, so a calibrated model passes 95% of the time), and log loss not
//                   worse than the Sisal closing prices without margin.
//  4. ROI ......... shown only; an alarm below -15% after 300 bets.
// The intervals treat bets as independent: selections of the same match are not, so the bounds are a little optimistic.
import type { PaperLeg } from "./db";

export const CRITERION = { n: 300, coverage: 0.8, minEv: 0.01, nCal: 1000, roiAlarm: -0.15, minBand: 20 } as const;

export type State = "pass" | "fail" | "open" | "alarm";
export type Interval = { n: number; mean: number | null; lo: number | null; hi: number | null };
export type Check = { key: string; label: string; state: State; now: string; detail: string };

const Z95 = 1.96;

export function interval(xs: number[]): Interval {
  const n = xs.length;
  if (!n) return { n, mean: null, lo: null, hi: null };
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  if (n < 2) return { n, mean, lo: null, hi: null };
  const sd = Math.sqrt(xs.reduce((a, x) => a + (x - mean) ** 2, 0) / (n - 1));
  const half = (Z95 * sd) / Math.sqrt(n);
  return { n, mean, lo: mean - half, hi: mean + half };
}

const settled = (l: PaperLeg) => l.result === "won" || l.result === "lost" || l.result === "void";
const decided = (l: PaperLeg) => l.result === "won" || l.result === "lost";
export const isValue = (l: PaperLeg) => l.status === "STRONG" || l.status === "CANDIDATE";

export function valueSummary(legs: PaperLeg[]) {
  const s = legs.filter(settled);
  const pnl = s.map((l) => (l.result === "won" ? l.odds - 1 : l.result === "lost" ? -1 : 0));
  return {
    n: s.length,
    ev: interval(s.filter((l) => l.close_fair != null).map((l) => l.odds * (l.close_fair as number) - 1)),
    clv: interval(s.filter((l) => l.close_odds).map((l) => l.odds / (l.close_odds as number) - 1)),
    roi: interval(pnl),
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
  const bins = Array.from({ length: 10 }, () => ({ p: 0, y: 0, n: 0 }));
  for (const l of legs) {
    if (!decided(l)) continue;
    const b = bins[Math.min(9, Math.floor(l.p * 10))];
    b.p += l.p;
    b.y += l.result === "won" ? 1 : 0;
    b.n += 1;
  }
  const rows = bins
    .map((b, i) => {
      const p = b.n ? b.p / b.n : 0;
      const y = b.n ? b.y / b.n : 0;
      const se = b.n ? Math.sqrt(Math.max(p * (1 - p), 1e-6) / b.n) : 0;
      return { lo: i / 10, n: b.n, p, y, z: se ? (y - p) / se : 0 };
    })
    .filter((r) => r.n > 0);
  const tested = rows.filter((r) => r.n >= CRITERION.minBand);
  const zMax = zFor(tested.length);
  return { rows, tested: tested.length, zMax, worst: tested.reduce((m, r) => Math.max(m, Math.abs(r.z)), 0), n: rows.reduce((s, r) => s + r.n, 0) };
}

// log loss of our probability minus that of the Sisal closing price without margin, per decided selection (negative = we are better)
export function logLossGap(legs: PaperLeg[]): Interval {
  const ll = (p: number, won: boolean) => -Math.log(Math.min(1 - 1e-6, Math.max(1e-6, won ? p : 1 - p)));
  return interval(legs.filter((l) => decided(l) && l.close_sisal_fair != null).map((l) => ll(l.p, l.result === "won") - ll(l.close_sisal_fair as number, l.result === "won")));
}

const sg = (x: number | null, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
const ci = (i: Interval) => (i.lo == null || i.hi == null ? "intervallo non ancora calcolabile" : `intervallo 95% da ${sg(i.lo)} a ${sg(i.hi)}`);

export function evaluate(all: PaperLeg[]) {
  const value = all.filter(isValue);
  const v = valueSummary(value);
  const full = v.n >= CRITERION.n;
  const coverage = v.n ? v.ev.n / v.n : 0;
  const missing = Math.max(0, CRITERION.n - v.n);

  const main: Check = {
    key: "main",
    label: `EV alla chiusura Pinnacle ≥ ${sg(CRITERION.minEv, 0)} su almeno ${CRITERION.n} giocate di valore`,
    now: `${sg(v.ev.mean)} su ${v.ev.n}`,
    detail: `${ci(v.ev)} · chiusura Pinnacle per ${Math.round(coverage * 100)}% (serve ≥ ${CRITERION.coverage * 100}%)${missing ? ` · mancano ${missing} giocate` : ""}`,
    state:
      full && v.ev.hi != null && v.ev.hi < -1e-9 ? "fail"
      : full && coverage >= CRITERION.coverage && (v.ev.mean ?? -1) >= CRITERION.minEv && (v.ev.lo ?? -1) > 0 ? "pass"
      : "open",
  };
  const clv: Check = {
    key: "clv",
    label: "CLV sulla chiusura Sisal positivo oltre il caso",
    now: `${sg(v.clv.mean)} su ${v.clv.n}`,
    detail: `${ci(v.clv)} · serve il limite basso sopra 0`,
    state: full && v.clv.hi != null && v.clv.hi < -1e-9 ? "fail" : full && (v.clv.lo ?? -1) > 0 ? "pass" : "open",
  };

  const bands = calibrationBands(all);
  const gap = logLossGap(all);
  const calFull = bands.n >= CRITERION.nCal;
  const bandsOk = bands.tested > 0 && bands.worst <= bands.zMax;
  const llOk = gap.lo != null && gap.lo <= 0; // not significantly worse than Sisal without margin
  const cal: Check = {
    key: "cal",
    label: `Calibrazione su almeno ${CRITERION.nCal} selezioni e log loss non peggiore di Sisal senza margine`,
    now: `${bands.n}/${CRITERION.nCal}`,
    detail:
      `fascia peggiore a ${bands.worst.toFixed(1)} errori standard (limite ${bands.zMax.toFixed(1)} per ${bands.tested} fasce) · ` +
      `log loss rispetto a Sisal ${gap.mean == null ? "–" : `${gap.mean >= 0 ? "+" : "−"}${Math.abs(gap.mean).toFixed(4)}`} su ${gap.n}` +
      (gap.lo != null && gap.hi != null ? ` (da ${gap.lo.toFixed(4)} a ${gap.hi.toFixed(4)})` : ""),
    state: calFull && gap.lo != null && gap.lo > 0 ? "fail" : calFull && bandsOk && llOk ? "pass" : "open",
  };
  const roi: Check = {
    key: "roi",
    label: "Rendimento (solo indicativo)",
    now: `${sg(v.roi.mean)} su ${v.roi.n}`,
    detail: `${ci(v.roi)} · allarme sotto ${sg(CRITERION.roiAlarm, 0)} dopo ${CRITERION.n} giocate`,
    state: full && (v.roi.mean ?? 0) < CRITERION.roiAlarm ? "alarm" : "open",
  };

  const checks = [main, clv, cal, roi];
  const verdict: State =
    main.state === "fail" ? "fail" : [main, clv, cal].every((c) => c.state === "pass") && roi.state !== "alarm" ? "pass" : "open";

  // pace: settled value bets per week since the first one was recorded
  const first = value.reduce<string | null>((m, l) => (m == null || l.created_at < m ? l.created_at : m), null);
  const weeks = first ? Math.max(1 / 7, (Date.now() - Date.parse(first)) / (7 * 864e5)) : null;
  const perWeek = weeks ? v.n / weeks : null;
  const weeksLeft = missing && perWeek ? Math.ceil(missing / perWeek) : null;

  return { checks, verdict, value: v, bands, gap, perWeek, weeksLeft, missing };
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
