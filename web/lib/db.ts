import "server-only";
import { cache } from "react";
import { unstable_cache } from "next/cache";
import { createClient, type Client, type InValue } from "@libsql/client";

let client: Client | null = null;

// Server-side only: the Turso token never reaches the browser. Use a READ-ONLY token for this app.
export function db(): Client {
  if (!client) {
    const url = process.env.TURSO_DATABASE_URL;
    if (!url) throw new Error("TURSO_DATABASE_URL non impostata nelle variabili del progetto Vercel");
    client = createClient({ url, authToken: process.env.TURSO_AUTH_TOKEN });
  }
  return client;
}

export type Run = {
  id: number;
  created_at: string;
  cutoff: string;
  horizon_days: number;
  n_fixtures: number;
  n_with_quotes: number;
  no_bet: number;
  reasons: string;
  status_counts: string;
  notes: string;
  optimizer: string | null; // settings for web/lib/optimizer.ts (null on runs published before on-demand slips)
};

export type FixtureRow = {
  fixture_id: string;
  kickoff: string;
  competition: string;
  home: string;
  away: string;
  p_home: number | null;
  p_draw: number | null;
  p_away: number | null;
  p_over25: number | null;
  p_btts: number | null;
  lineup_state: string;
  n_quotes: number;
  xg_home: number | null;
  xg_away: number | null;
  markets: string | null;
  book?: string | null; // JSON: Sisal price, market and final probability of the headline selections (absent on old runs)
};

export type OppRow = {
  fixture_id: string;
  kickoff: string;
  competition: string;
  match: string;
  market: string;
  bookmaker: string;
  odds: number;
  fair_odds: number;
  p_final: number;
  p_market: number | null;
  ev: number;
  ev_lower: number;
  uncertainty: number;
  data_quality: number;
  status: string;
  odds_stale: number;
  lineup_state: string;
  p_struct: number | null;
  p_low: number | null;
  p_high: number | null;
  n_books: number | null;
  edge: number | null;
  factors: string | null;
  sel_key?: string | null;
  home?: string | null;
  away?: string | null;
  score?: number | null;
  disagreement?: number | null;
  dq_lineup?: number | null;
};

export type SlipRow = {
  rank: number;
  total_odds: number;
  joint_probability: number;
  ev: number;
  ev_lower: number;
  stake: number;
  legs: string;
  explanation: string;
  horizon_h: number | null; // period the slip was built for (null: runs published before the per-filter pool)
  max_legs: number | null; // maximum number of events allowed when it was built
};

export type Leg = { match: string; competition: string; kickoff: string; market: string; odds: number; bookmaker: string; p: number };
export type Usage = { source: string; period: string; used: number };
export type ModelMarket = { g: string; l: string; p: number };
export type QuotePoint = { selection: string; bookmaker: string; odds: number; observed_at: string };
export type LineupRow = { team: string; status: string; formation: string | null; starters: string; bench: string; observed_at: string };
export type ResultRow = { fixture_id: string; kickoff: string; competition: string; home: string; away: string; home_goals: number; away_goals: number };

// Rows become plain objects keyed by the column names the server reports (never the driver's Row objects, whose named
// properties were missing in production for some columns).
async function all<T>(sql: string, args: InValue[] = []): Promise<T[]> {
  const r = await db().execute({ sql, args });
  return r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]]))) as T[];
}

// Server-side data cache (shared by every request and page). A published run never changes, so reads keyed by run id can be
// kept for long; only "which run is the latest" and the counters are refreshed every minute. Moving between pages then
// costs no database round trip for data already read.
const RUN_TTL = 6 * 3600;
const LIVE_TTL = 60;
function persist<A extends unknown[], R>(fn: (...args: A) => Promise<R>, name: string, seconds: number) {
  return cache(unstable_cache(fn, [name], { revalidate: seconds }));
}

export const latestRun = persist(async (): Promise<Run | null> => {
  try {
    return (await all<Run>("SELECT * FROM pub_runs ORDER BY id DESC LIMIT 1"))[0] ?? null;
  } catch {
    return null; // tables not created yet: nothing published
  }
}, "latestRun", LIVE_TTL);

export const runFixtures = persist(
  (runId: number) => all<FixtureRow>("SELECT * FROM pub_fixtures WHERE run_id = ? ORDER BY kickoff, competition, home", [runId]),
  "runFixtures",
  RUN_TTL,
);

// Lists never need the per-market explanation (factors): the match page reads it on its own.
// `match` is an SQL keyword: selected under an alias (the remote driver dropped the bare column name) and mapped back.
const OPP_COLS =
  "fixture_id, kickoff, competition, pub_opportunities.\"match\" AS match_label, market, bookmaker, odds, fair_odds, p_final, p_market, ev, ev_lower, uncertainty, " +
  "data_quality, status, odds_stale, lineup_state, p_struct, p_low, p_high, n_books, edge, NULL AS factors";

const withMatch = (rows: (OppRow & { match_label?: string })[]) => rows.map(({ match_label, ...o }) => ({ ...o, match: match_label ?? o.match ?? "" }));

export const runOpps = cache(async (runId: number) =>
  withMatch(await all<OppRow>(`SELECT ${OPP_COLS} FROM pub_opportunities WHERE run_id = ? ORDER BY ev DESC`, [runId])),
);

// ---- filtered reads for the home page: the database does the filtering, only what is shown travels ----
export type OppFilter = { from: string; until: string; comps: string[]; lmin: number; lmax: number; status?: string };

function oppWhere(runId: number, f: OppFilter) {
  const parts = ["run_id = ?", "kickoff > ?", "kickoff <= ?"];
  const args: InValue[] = [runId, f.from, f.until];
  if (f.comps.length) {
    parts.push(`competition IN (${f.comps.map(() => "?").join(",")})`);
    args.push(...f.comps);
  }
  if (f.lmin) {
    parts.push("odds >= ?");
    args.push(f.lmin);
  }
  if (f.lmax) {
    parts.push("odds <= ?");
    args.push(f.lmax);
  }
  if (f.status) {
    parts.push("status = ?");
    args.push(f.status);
  }
  return { where: parts.join(" AND "), args };
}

export const oppSummary = persist(async (runId: number, f: OppFilter, limit = 8) => {
  const w = oppWhere(runId, f);
  const [n, top] = await Promise.all([
    all<{ n: number }>(`SELECT COUNT(*) AS n FROM pub_opportunities WHERE ${w.where}`, w.args),
    // best first: status (Alta, Media, Da osservare), then the prudent EV (the EV at the low end of the probability range)
    all<OppRow>(
      `SELECT ${OPP_COLS} FROM pub_opportunities WHERE ${w.where} ORDER BY CASE status WHEN 'STRONG' THEN 0 WHEN 'CANDIDATE' THEN 1 ELSE 2 END, ev_lower DESC LIMIT ${limit}`,
      w.args,
    ).then(withMatch),
  ]);
  return { count: Number(n[0]?.n ?? 0), top };
}, "oppSummary", RUN_TTL);

export const runCompetitions = persist(
  async (runId: number) =>
    (await all<{ competition: string }>("SELECT DISTINCT competition FROM pub_fixtures WHERE run_id = ? ORDER BY competition", [runId])).map(
      (r) => r.competition,
    ),
  "runCompetitions",
  RUN_TTL,
);

// Candidates for the on-demand slip optimizer: only the statuses it may use, with its inputs.
export const slipCandidates = persist(async (runId: number, f: OppFilter, statuses: string[]) => {
  const w = oppWhere(runId, f);
  return all<OppRow>(
    `SELECT ${OPP_COLS}, sel_key, home, away, score, disagreement, dq_lineup FROM pub_opportunities ` +
      `WHERE ${w.where} AND status IN (${statuses.map(() => "?").join(",")}) AND sel_key IS NOT NULL`,
    [...w.args, ...statuses],
  ).then(withMatch);
}, "slipCandidates", RUN_TTL);

export const runSlips = cache((runId: number) => all<SlipRow>("SELECT * FROM pub_slips WHERE run_id = ? ORDER BY rank", [runId]));

export const usage = persist(async () => {
  const now = new Date();
  const day = `D${now.toISOString().slice(0, 10)}`;
  const month = `M${now.toISOString().slice(0, 7)}`;
  const rows = await all<Usage>("SELECT source, period, used FROM api_usage WHERE period IN (?, ?)", [day, month]);
  const get = (src: string, kind: "D" | "M") => rows.find((u) => u.source === src && u.period.startsWith(kind))?.used ?? 0;
  return { goalDay: get("goal-api", "D"), oddsMonth: get("oddspapi", "M"), oddsDay: get("oddspapi", "D"), manualMonth: get("manual-refresh", "M") };
}, "usage", LIVE_TTL);

/** Uncached: the manual refresh button checks the monthly cap right before starting a run. */
export async function manualRefreshesThisMonth(): Promise<number> {
  const rows = await all<{ used: number }>("SELECT used FROM api_usage WHERE source = 'manual-refresh' AND period = ?", [
    `M${new Date().toISOString().slice(0, 7)}`,
  ]);
  return rows[0]?.used ?? 0;
}

export const lastTick = persist(async () => {
  const r = await all<{ t: string | null }>("SELECT MAX(fetched_at) AS t FROM raw_requests");
  return r[0]?.t ?? null;
}, "lastTick", LIVE_TTL);

export const fixtureDetail = persist(fixtureDetailUncached, "fixtureDetail", 300);

async function fixtureDetailUncached(id: string) {
  const run = await latestRun();
  if (!run) return null;
  const fx = (await all<FixtureRow>("SELECT * FROM pub_fixtures WHERE run_id = ? AND fixture_id = ?", [run.id, id]))[0];
  if (!fx) return null;
  const [opps, nq, lineups, formHome, formAway, h2h] = await Promise.all([
    all<OppRow>(`SELECT ${OPP_COLS.replace("NULL AS factors", "factors")} FROM pub_opportunities WHERE run_id = ? AND fixture_id = ? ORDER BY ev DESC`, [
      run.id,
      id,
    ]).then(withMatch),
    all<{ n: number }>("SELECT COUNT(*) AS n FROM quotes WHERE fixture_id = ? AND bookmaker LIKE 'sisal%'", [id]),
    all<LineupRow>(
      "SELECT team, status, formation, starters, bench, observed_at FROM lineups WHERE fixture_id = ? ORDER BY observed_at DESC",
      [id],
    ),
    teamForm(fx.home, fx.kickoff),
    teamForm(fx.away, fx.kickoff),
    all<ResultRow>(
      "SELECT fixture_id, kickoff, competition, home, away, home_goals, away_goals FROM results " +
        "WHERE ((home = ? AND away = ?) OR (home = ? AND away = ?)) AND kickoff < ? ORDER BY kickoff DESC LIMIT 5",
      [fx.home, fx.away, fx.away, fx.home, fx.kickoff],
    ),
  ]);
  const latest = new Map<string, LineupRow>();
  for (const l of lineups) if (!latest.has(l.team)) latest.set(l.team, l);
  // lineups store player ids: resolve names and roles from the players table
  const ids = [...latest.values()].flatMap((l) => [...parseJSON<string[]>(l.starters, []), ...parseJSON<string[]>(l.bench, [])]);
  const players: Record<string, { name: string; position: string }> = {}; // plain object: the data cache stores JSON
  for (let k = 0; k < ids.length; k += 200) {
    const chunk = ids.slice(k, k + 200);
    const rows = await all<{ id: string; name: string; position: string }>(
      `SELECT id, name, position FROM players WHERE id IN (${chunk.map(() => "?").join(",")})`,
      chunk,
    );
    for (const r of rows) players[r.id] = { name: r.name, position: r.position };
  }
  return { run, fx, opps, nQuotes: Number(nq[0]?.n ?? 0), lineups: [...latest.values()], players, formHome, formAway, h2h };
}

// Only Sisal is playable: the odds tab lists and draws Sisal prices (Pinnacle stays an internal reference of the model).
const PLAYABLE = "bookmaker LIKE 'sisal%'";

// Every priced selection of a fixture (market, selection, line): the menu of the odds-trend tab.
export type QuoteKey = { market_code: string; selection: string; line_key: string; n: number };
export const quoteMenu = persist(quoteMenuUncached, "quoteMenu", 300);
function quoteMenuUncached(id: string) {
  return all<QuoteKey>(
    `SELECT market_code, selection, COALESCE(line_key, '') AS line_key, COUNT(*) AS n FROM quotes WHERE fixture_id = ? AND ${PLAYABLE} ` +
      "GROUP BY market_code, selection, COALESCE(line_key, '')",
    [id],
  );
}

// Price path of one market line (all its selections, all bookmakers), oldest first.
export const quotePath = persist(quotePathUncached, "quotePath", 300);
function quotePathUncached(id: string, market: string, lineKey: string) {
  return all<QuotePoint>(
    `SELECT selection, bookmaker, odds, observed_at FROM quotes WHERE fixture_id = ? AND market_code = ? AND COALESCE(line_key, '') = ? AND ${PLAYABLE} ` +
      "ORDER BY observed_at",
    [id, market, lineKey],
  );
}

function teamForm(team: string, before: string) {
  return all<ResultRow>(
    "SELECT fixture_id, kickoff, competition, home, away, home_goals, away_goals FROM results " +
      "WHERE (home = ? OR away = ?) AND kickoff < ? ORDER BY kickoff DESC LIMIT 6",
    [team, team, before],
  );
}

export async function systemStatus() {
  const tables = ["raw_requests", "fixtures", "results", "quotes", "lineups", "match_stats"] as const;
  const counts = await Promise.all(tables.map((t) => all<{ n: number }>(`SELECT COUNT(*) AS n FROM ${t}`)));
  const [runs, jobs, byComp] = await Promise.all([
    all<Run>("SELECT * FROM pub_runs ORDER BY id DESC LIMIT 12"),
    all<{ name: string; done_at: string; detail: string | null }>("SELECT name, done_at, detail FROM jobs ORDER BY done_at DESC"),
    all<{ competition: string; n: number; first: string; last: string }>(
      "SELECT competition, COUNT(*) AS n, MIN(kickoff) AS first, MAX(kickoff) AS last FROM results GROUP BY competition ORDER BY n DESC",
    ),
  ]);
  return {
    counts: Object.fromEntries(tables.map((t, i) => [t, Number(counts[i][0]?.n ?? 0)])) as Record<(typeof tables)[number], number>,
    runs,
    jobs,
    byComp,
  };
}

export function parseJSON<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

// ---- model quality (weekly replay written by the `quality` workflow) ----
export type QualityFamily = {
  n: number;
  n_model?: number; ll_model?: number; brier_model?: number;
  ll_v1?: number; n_ens?: number; ll_ens?: number; brier_ens?: number;
  n_close?: number; ll_close?: number; brier_close?: number;
  n_same?: number; ll_model_same?: number; ll_ens_same?: number; ll_close_same?: number;
};
export type QualityValue = { n: number; hits: number; roi: number; mean_ev: number; n_clv: number; mean_clv: number | null; books: Record<string, number> };
export type QualityReport = {
  groups: Record<string, Record<string, QualityFamily>>;
  calibration: Record<string, Record<string, [number, number, number][]>>;
  value: Record<string, QualityValue>;
  monthly: { month: string; n: number; ll_model: number; ll_close: number }[];
  thresholds: { min_ev: number; min_probability: number; market_prior_sd: number };
  n_matches: number;
};
export type QualityRun = { id: number; created_at: string; window_start: string; window_end: string; model_version: string; report: QualityReport };

export const latestQuality = persist(async (): Promise<QualityRun | null> => {
  try {
    const rows = await all<{ id: number; created_at: string; window_start: string; window_end: string; model_version: string; report: string }>(
      "SELECT id, created_at, window_start, window_end, model_version, report FROM quality_runs ORDER BY id DESC LIMIT 1",
    );
    const r = rows[0];
    return r ? { ...r, report: parseJSON<QualityReport>(r.report, { groups: {}, calibration: {}, value: {}, monthly: [], thresholds: { min_ev: 0, min_probability: 0, market_prior_sd: 0 }, n_matches: 0 }) } : null;
  } catch {
    return null; // table created by the first weekly run
  }
}, "latestQuality", 600);
