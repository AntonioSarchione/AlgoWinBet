import "server-only";
import { cache } from "react";
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

export const latestRun = cache(async (): Promise<Run | null> => {
  try {
    return (await all<Run>("SELECT * FROM pub_runs ORDER BY id DESC LIMIT 1"))[0] ?? null;
  } catch {
    return null; // tables not created yet: nothing published
  }
});

export const runFixtures = cache((runId: number) =>
  all<FixtureRow>("SELECT * FROM pub_fixtures WHERE run_id = ? ORDER BY kickoff, competition, home", [runId]),
);

// Lists never need the per-market explanation (factors): the match page reads it on its own.
const OPP_COLS =
  "fixture_id, kickoff, competition, match, market, bookmaker, odds, fair_odds, p_final, p_market, ev, ev_lower, uncertainty, " +
  "data_quality, status, odds_stale, lineup_state, p_struct, p_low, p_high, n_books, edge, NULL AS factors";

export const runOpps = cache((runId: number) =>
  all<OppRow>(`SELECT ${OPP_COLS} FROM pub_opportunities WHERE run_id = ? ORDER BY ev DESC`, [runId]),
);

export const runSlips = cache((runId: number) => all<SlipRow>("SELECT * FROM pub_slips WHERE run_id = ? ORDER BY rank", [runId]));

export const usage = cache(async () => {
  const now = new Date();
  const day = `D${now.toISOString().slice(0, 10)}`;
  const month = `M${now.toISOString().slice(0, 7)}`;
  const rows = await all<Usage>("SELECT source, period, used FROM api_usage WHERE period IN (?, ?)", [day, month]);
  const get = (src: string, kind: "D" | "M") => rows.find((u) => u.source === src && u.period.startsWith(kind))?.used ?? 0;
  return { goalDay: get("goal-api", "D"), oddsMonth: get("oddspapi", "M"), oddsDay: get("oddspapi", "D") };
});

export const lastTick = cache(async () => {
  const r = await all<{ t: string | null }>("SELECT MAX(fetched_at) AS t FROM raw_requests");
  return r[0]?.t ?? null;
});

export async function fixtureDetail(id: string) {
  const run = await latestRun();
  if (!run) return null;
  const fx = (await all<FixtureRow>("SELECT * FROM pub_fixtures WHERE run_id = ? AND fixture_id = ?", [run.id, id]))[0];
  if (!fx) return null;
  const [opps, nq, lineups, formHome, formAway, h2h] = await Promise.all([
    all<OppRow>("SELECT * FROM pub_opportunities WHERE run_id = ? AND fixture_id = ? ORDER BY ev DESC", [run.id, id]),
    all<{ n: number }>("SELECT COUNT(*) AS n FROM quotes WHERE fixture_id = ?", [id]),
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
  const players = new Map<string, { name: string; position: string }>();
  for (let k = 0; k < ids.length; k += 200) {
    const chunk = ids.slice(k, k + 200);
    const rows = await all<{ id: string; name: string; position: string }>(
      `SELECT id, name, position FROM players WHERE id IN (${chunk.map(() => "?").join(",")})`,
      chunk,
    );
    for (const r of rows) players.set(r.id, { name: r.name, position: r.position });
  }
  return { run, fx, opps, nQuotes: Number(nq[0]?.n ?? 0), lineups: [...latest.values()], players, formHome, formAway, h2h };
}

// Every priced selection of a fixture (market, selection, line): the menu of the odds-trend tab.
export type QuoteKey = { market_code: string; selection: string; line_key: string; n: number };
export function quoteMenu(id: string) {
  return all<QuoteKey>(
    "SELECT market_code, selection, line_key, COUNT(*) AS n FROM quotes WHERE fixture_id = ? GROUP BY market_code, selection, line_key",
    [id],
  );
}

// Price path of one market line (all its selections, all bookmakers), oldest first.
export function quotePath(id: string, market: string, lineKey: string) {
  return all<QuotePoint>(
    "SELECT selection, bookmaker, odds, observed_at FROM quotes WHERE fixture_id = ? AND market_code = ? AND line_key = ? ORDER BY observed_at",
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
