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
  rho?: number | null; // Dixon-Coles low-score correction (absent on runs before My Combo)
  estimated?: number | null; // 1: Sisal does not price the match on the feed, prices estimated from Pinnacle (lib/books.ts)
  scorers?: string | null; // JSON: goalscorer probabilities per team (Fase 9, absent on older runs and teams with little history)
  trends?: string | null; // JSON: statistical streaks of the match (lib: app/_components/Trends.tsx)
  probable?: string | null; // JSON: our probable XI of the teams without an official one (app/_components/Lineups.tsx)
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
export type LineupRow = { team: string; status: string; formation: string | null; starters: string; bench: string; observed_at: string; detail: string | null };
export type ResultRow = { fixture_id: string; kickoff: string; competition: string; home: string; away: string; home_goals: number; away_goals: number };

// Rows become plain objects keyed by the column names the server reports (never the driver's Row objects, whose named
// properties were missing in production for some columns).
async function all<T>(sql: string, args: InValue[] = []): Promise<T[]> {
  let r;
  try {
    r = await db().execute({ sql, args });
  } catch {
    // one retry: a dropped connection or a busy database must not reach the page (nor the data cache) as "no data"
    await new Promise((ok) => setTimeout(ok, 400));
    r = await db().execute({ sql, args });
  }
  return r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]]))) as T[];
}

// Server-side data cache (shared by every request and page). A published run never changes, so reads keyed by run id can be
// kept for long. Moving between pages then costs no database round trip for data already read.
const RUN_TTL = 6 * 3600;
// every deploy starts a fresh cache: a cached result never outlives the code (optimizer rules, data shapes) that made it
export const DEPLOY = process.env.VERCEL_GIT_COMMIT_SHA ?? "dev";
// A time-based cache serves its stale copy to the first request after it expires (the refresh happens behind it), so a
// page could show the previous state until a second visit. Rule: what says "latest" (last analysis, budget, last tick) is
// read uncached on every request with tiny primary-key reads; everything else is cached under a key that changes with
// its data (run id, newest lineup / quote / settlement), so a cached copy is never stale.
function persist<A extends unknown[], R>(fn: (...args: A) => Promise<R>, name: string, seconds: number) {
  return cache(unstable_cache(fn, [name, DEPLOY], { revalidate: seconds }));
}

export const latestRun = cache(async (): Promise<Run | null> => {
  try {
    return (await all<Run>("SELECT * FROM pub_runs ORDER BY id DESC LIMIT 1"))[0] ?? null;
  } catch (e) {
    // tables not created yet: nothing published. Any other error is thrown, so the page shows an error with a retry
    // instead of caching "Nessuna analisi pubblicata" for a minute.
    if (/no such table/i.test(String(e))) return null;
    throw e;
  }
});

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

// "gol" became "goal" in every label on 2026-10-09: rows and JSON written before keep the old word, shown with the new one
export const goalify = (s: string) =>
  s.replace(/NoGol/g, "NoGoal").replace(/Multigol/g, "Multigoal").replace(/Gol/g, "Goal").replace(/gol/g, "goal");

const withMatch = (rows: (OppRow & { match_label?: string })[]) =>
  rows.map(({ match_label, ...o }) => ({ ...o, market: goalify(o.market), match: match_label ?? o.match ?? "" }));

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
    // best first: status (Alta, Media, Equa, Da osservare), then the prudent EV (the EV at the low end of the probability range)
    all<OppRow>(
      `SELECT ${OPP_COLS} FROM pub_opportunities WHERE ${w.where} ORDER BY CASE status WHEN 'STRONG' THEN 0 WHEN 'CANDIDATE' THEN 1 WHEN 'FAIR' THEN 2 ELSE 3 END, ev_lower DESC LIMIT ${limit}`,
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

// manual slips: every published selection of the chosen matches (all statuses shown on the site)
export const fixtureCandidates = persist(async (runId: number, ids: string[]) => {
  if (!ids.length) return [] as OppRow[];
  return all<OppRow>(
    `SELECT ${OPP_COLS}, sel_key, home, away, score, disagreement, dq_lineup FROM pub_opportunities ` +
      `WHERE run_id = ? AND fixture_id IN (${ids.map(() => "?").join(",")}) AND sel_key IS NOT NULL`,
    [runId, ...ids],
  ).then(withMatch);
}, "fixtureCandidates", RUN_TTL);

// manual slips: every playable selection of the chosen matches (pub_book, any status; missing on runs before it existed)
export type BookSel = { k: string; m: string; o: number; b: string; p: number; s: number; pm: number | null; e: number; u: number; sc: number; d: number; l: number; st: string };
export const fixtureBook = persist(async (runId: number, ids: string[]) => {
  if (!ids.length) return [] as { fixture_id: string; sels: string }[];
  return all<{ fixture_id: string; sels: string }>(
    `SELECT fixture_id, sels FROM pub_book WHERE run_id = ? AND fixture_id IN (${ids.map(() => "?").join(",")})`,
    [runId, ...ids],
  ).catch((e) => (/no such table/i.test(String(e)) ? [] : Promise.reject(e)));
}, "fixtureBook", RUN_TTL);

export const runSlips = cache((runId: number) => all<SlipRow>("SELECT * FROM pub_slips WHERE run_id = ? ORDER BY rank", [runId]));

export const usage = cache(async () => {
  const now = new Date();
  const day = `D${now.toISOString().slice(0, 10)}`;
  const month = `M${now.toISOString().slice(0, 7)}`;
  const rows = await all<Usage>("SELECT source, period, used FROM api_usage WHERE period IN (?, ?)", [day, month]);
  const get = (src: string, kind: "D" | "M") => rows.find((u) => u.source === src && u.period.startsWith(kind))?.used ?? 0;
  return { goalDay: get("goal-api", "D"), apifDay: get("api-football", "D"), oddsMonth: get("oddspapi", "M"), oddsDay: get("oddspapi", "D"), manualMonth: get("manual-refresh", "M"), ghMonth: get("actions-minutes", "M") };
});

/** Uncached: the manual refresh button checks the monthly cap right before starting a run. */
// scheduler gate (/api/tick): is any match kicking off in [from, to]? Uncached on purpose: the answer drives a run.
export async function matchesBetween(from: Date, to: Date): Promise<boolean> {
  const rows = await all<{ x: number }>(
    "SELECT 1 AS x FROM fixtures WHERE kickoff BETWEEN ? AND ? AND status NOT IN ('POSTPONED', 'CANCELLED') LIMIT 1",
    [from.toISOString().replace("Z", "+00:00"), to.toISOString().replace("Z", "+00:00")],
  );
  return rows.length > 0;
}

/** Uncached: a match kicking off between `from` and `to` still without both official XI (any source), read by the tick route
 * to start an extra run between the half-hour ticks while lineups are due. */
export async function lineupsPending(from: Date, to: Date): Promise<boolean> {
  const iso = (d: Date) => d.toISOString().replace("Z", "+00:00");
  const rows = await all<{ x: number }>(
    "SELECT 1 AS x FROM fixtures f WHERE f.kickoff BETWEEN ? AND ? AND f.status NOT IN ('POSTPONED', 'CANCELLED') " +
      "AND (SELECT COUNT(DISTINCT l.team) FROM lineups l WHERE l.fixture_id = f.fixture_id AND l.status = 'confirmed') < 2 LIMIT 1",
    [iso(from), iso(to)],
  );
  return rows.length > 0;
}

/** Uncached: Sisal's latest player prices of one match (player_quotes, filled from the OddsPapi snapshots; the table may not
 * exist yet on an older database). */
export type PlayerQuoteRow = { market: string; line_key: string; player_key: string; player_name: string; odds: number; observed_at: string };
export async function playerQuotes(id: string): Promise<PlayerQuoteRow[]> {
  return all<PlayerQuoteRow>(
    "SELECT market, line_key, player_key, player_name, odds, observed_at FROM player_quotes WHERE fixture_id = ? AND bookmaker LIKE 'sisal%'",
    [id],
  ).catch(() => []);
}

export async function manualRefreshesThisMonth(): Promise<number> {
  const rows = await all<{ used: number }>("SELECT used FROM api_usage WHERE source = 'manual-refresh' AND period = ?", [
    `M${new Date().toISOString().slice(0, 7)}`,
  ]);
  return rows[0]?.used ?? 0;
}

export const lastTick = cache(async () => {
  // newest row by primary key (ids grow with time): no scan of the whole table
  const r = await all<{ t: string | null }>("SELECT fetched_at AS t FROM raw_requests ORDER BY id DESC LIMIT 1");
  return r[0]?.t ?? null;
});

// Absent and doubtful players of coming matches (FotMob pre-match page, API-Football injuries): latest status per player,
// those back in the squad dropped. `starts` = official XI the player started among the team's last REGULAR_SHEETS
// (the same rule the FotMob run uses to call a change relevant); null when the id is not one of our lineups' ids.
export type Absence = {
  fixture_id: string; team: string; player_id: string; name: string; status: string; reason: string | null;
  observed_at: string; source: string; starts: number | null;
};
export const REGULAR_SHEETS = 3;
// what a player does in a match he starts (src/algowinbet/playercard.py); goalkeepers carry gk = 1
export type PlayerCardData = {
  xg?: number | null; sot?: number; sh?: number; fc?: number; fd?: number; cg: number; as?: number; n: number; min?: number;
  gk?: 1; ts?: number | null; gc?: number | null; sv?: number | null; cs?: number | null;
};

export async function absencesFor(ids: string[]): Promise<Record<string, Absence[]>> {
  const out: Record<string, Absence[]> = {};
  const fids = [...new Set(ids)].filter(Boolean);
  if (!fids.length) return out;
  const rows = await all<Omit<Absence, "name" | "starts"> & { player_name: string | null }>(
    `SELECT fixture_id, team, player_id, player_name, status, reason, observed_at, source FROM player_status WHERE fixture_id IN (${fids.map(() => "?").join(",")}) ORDER BY observed_at`,
    fids,
  ).catch((e) => (/no such table/i.test(String(e)) ? [] : Promise.reject(e)));
  const latest = new Map<string, (typeof rows)[number]>();
  for (const r of rows) latest.set(`${r.fixture_id}|${r.player_id}`, r); // newest row wins, whichever source
  const live = [...latest.values()].filter((r) => r.status !== "AVAILABLE");
  if (!live.length) return out;
  const teams = [...new Set(live.map((r) => r.team))];
  const since = new Date(Date.now() - 120 * 86400_000).toISOString();
  const sheets = await all<{ team: string; fixture_id: string; starters: string; kickoff: string }>(
    `SELECT l.team, l.fixture_id, l.starters, r.kickoff FROM lineups l JOIN results r ON r.fixture_id = l.fixture_id WHERE l.status = 'confirmed' ` +
      `AND l.team IN (${teams.map(() => "?").join(",")}) AND r.kickoff >= ? ORDER BY r.kickoff DESC`,
    [...teams, since],
  );
  const starts = new Map<string, Map<string, number>>(); // team -> player -> starts in the last REGULAR_SHEETS
  const seen = new Map<string, Set<string>>();
  for (const s of sheets) {
    const done = seen.get(s.team) ?? seen.set(s.team, new Set()).get(s.team)!;
    if (done.has(s.fixture_id) || done.size >= REGULAR_SHEETS) continue; // one lineup per match (several sources)
    done.add(s.fixture_id);
    const m = starts.get(s.team) ?? starts.set(s.team, new Map()).get(s.team)!;
    for (const p of parseJSON<string[]>(s.starters, [])) m.set(p, (m.get(p) ?? 0) + 1);
  }
  const unnamed = [...new Set(live.filter((r) => !r.player_name).map((r) => r.player_id))];
  const names: Record<string, string> = {};
  if (unnamed.length) {
    for (const r of await all<{ id: string; name: string }>(`SELECT id, name FROM players WHERE id IN (${unnamed.map(() => "?").join(",")})`, unnamed)) names[r.id] = r.name;
  }
  const rank: Record<string, number> = { SUSPENDED: 0, OUT: 1, DOUBTFUL: 2 };
  for (const r of live) {
    const known = r.player_id.startsWith("goal:") && seen.has(r.team);
    const a: Absence = {
      fixture_id: r.fixture_id, team: r.team, player_id: r.player_id, name: r.player_name || names[r.player_id] || r.player_id,
      status: r.status, reason: r.reason, observed_at: r.observed_at, source: r.source, starts: known ? (starts.get(r.team)?.get(r.player_id) ?? 0) : null,
    };
    (out[r.fixture_id] ??= []).push(a);
  }
  for (const list of Object.values(out)) {
    list.sort((x, y) => (y.starts ?? -1) - (x.starts ?? -1) || (rank[x.status] ?? 3) - (rank[y.status] ?? 3) || x.name.localeCompare(y.name));
  }
  return out;
}

// keyed by the analysis, the newest lineup and the newest player status of the match: a new run, a lineup or an absence
// shows on the first visit
export const fixtureDetail = cache(async (id: string) => {
  const run = await latestRun();
  if (!run) return null;
  const v = await all<{ t: string | null; s: string | null }>(
    "SELECT (SELECT MAX(observed_at) FROM lineups WHERE fixture_id = ?) AS t, (SELECT MAX(observed_at) FROM player_status WHERE fixture_id = ?) AS s",
    [id, id],
  ).catch(() => all<{ t: string | null; s: string | null }>("SELECT MAX(observed_at) AS t, NULL AS s FROM lineups WHERE fixture_id = ?", [id]));
  return fixtureDetailAt(id, run, `${v[0]?.t ?? ""}|${v[0]?.s ?? ""}`);
});
const fixtureDetailAt = persist(fixtureDetailUncached, "fixtureDetail", 6 * 3600);

async function fixtureDetailUncached(id: string, run: Run, _lineupsAt: string) {
  const fx = (await all<FixtureRow>("SELECT * FROM pub_fixtures WHERE run_id = ? AND fixture_id = ?", [run.id, id]))[0];
  if (!fx) return null;
  const [opps, nq, lineups, formHome, formAway, h2h] = await Promise.all([
    all<OppRow>(`SELECT ${OPP_COLS.replace("NULL AS factors", "factors")} FROM pub_opportunities WHERE run_id = ? AND fixture_id = ? ORDER BY ev DESC`, [
      run.id,
      id,
    ]).then(withMatch),
    all<{ n: number }>("SELECT COUNT(*) AS n FROM quotes WHERE fixture_id = ? AND bookmaker LIKE 'sisal%'", [id]),
    // detail (shirt numbers, pitch positions) arrives with the API-Football collector; older databases lack the column
    all<LineupRow>("SELECT team, status, formation, starters, bench, observed_at, detail FROM lineups WHERE fixture_id = ? ORDER BY observed_at DESC", [id]).catch(
      (e) =>
        /no such column/i.test(String(e))
          ? all<LineupRow>("SELECT team, status, formation, starters, bench, observed_at, NULL AS detail FROM lineups WHERE fixture_id = ? ORDER BY observed_at DESC", [id])
          : Promise.reject(e),
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
  // newest per team, but a lineup with numbers and positions (API-Football) wins over one without
  for (const l of lineups) if (!latest.has(l.team) || (!latest.get(l.team)!.detail && l.detail)) latest.set(l.team, l);
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
  const absences = (await absencesFor([id]))[id] ?? [];
  // player cards of the lineups tab (kept for the last two runs only; absent on runs before them)
  const cardRows = await all<{ cards: string }>("SELECT cards FROM pub_players WHERE run_id = ? AND fixture_id = ?", [run.id, id]).catch(() => []);
  const cards = parseJSON<Record<string, PlayerCardData>>(cardRows[0]?.cards, {});
  return { run, fx, opps, nQuotes: Number(nq[0]?.n ?? 0), lineups: [...latest.values()], players, absences, cards, formHome, formAway, h2h };
}

// Only Sisal is playable: the odds tab lists and draws Sisal prices (Pinnacle stays an internal reference of the model).
const PLAYABLE = "bookmaker LIKE 'sisal%'";

// Every priced selection of a fixture (market, selection, line): the menu of the odds-trend tab.
export type QuoteKey = { market_code: string; selection: string; line_key: string; n: number };
// keyed by the newest stored quote of the match (index on fixture_id, observed_at)
const quotesAt = cache(async (id: string) =>
  (await all<{ t: string | null }>("SELECT MAX(observed_at) AS t FROM quotes WHERE fixture_id = ?", [id]))[0]?.t ?? "",
);
const quoteMenuAt = persist(quoteMenuUncached, "quoteMenu", 6 * 3600);
export const quoteMenu = async (id: string) => quoteMenuAt(id, await quotesAt(id));
function quoteMenuUncached(id: string, _quotesAt: string) {
  return all<QuoteKey>(
    `SELECT market_code, selection, COALESCE(line_key, '') AS line_key, COUNT(*) AS n FROM quotes WHERE fixture_id = ? AND ${PLAYABLE} ` +
      "GROUP BY market_code, selection, COALESCE(line_key, '')",
    [id],
  );
}

// Price path of one market line (all its selections, all bookmakers), oldest first.
const quotePathAt = persist(quotePathUncached, "quotePath", 6 * 3600);
export const quotePath = async (id: string, market: string, lineKey: string) => quotePathAt(id, market, lineKey, await quotesAt(id));
function quotePathUncached(id: string, market: string, lineKey: string, _quotesAt: string) {
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
    return JSON.parse(goalify(raw)) as T;
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
  n_meta?: number; ll_meta?: number; ll_meta_same?: number; ll_meta_mov_same?: number;
  n_alone?: number; ll_model_alone?: number; ll_meta_alone?: number;
};
export type QualityMeta = { a: number; b: number; c: number[]; n: number; kind: "pool" | "calib"; text: string };
export type QualityValue = { n: number; hits: number; roi: number; mean_ev: number; n_clv: number; mean_clv: number | null; books: Record<string, number> };
export type QualityReport = {
  groups: Record<string, Record<string, QualityFamily>>;
  calibration: Record<string, Record<string, [number, number, number][]>>;
  value: Record<string, QualityValue>;
  monthly: { month: string; n: number; ll_model: number; ll_close: number }[];
  thresholds: { min_ev: number; min_probability: number; market_prior_sd: number };
  n_matches: number;
  meta?: Record<string, QualityMeta>;
  calib_methods?: Record<string, { n: number; platt: number; beta: number; isotonic: number; chosen: "platt" | "beta" | "isotonic" }>;
  // prudent EV: share of the model's edge over the market that the picks of the value test earned (ci: 90% interval)
  edge_shrink?: { lambda: number; raw: number | null; n: number; ci?: [number, number]; raw_ci?: [number, number] };
};
export type QualityRun = { id: number; created_at: string; window_start: string; window_end: string; model_version: string; report: QualityReport };

// Morning health check (src/algowinbet/health.py): one small row, read uncached so the page shows the latest report.
export type HealthCheck = { key: string; label: string; level: "ok" | "warn" | "error"; detail: string };
export type Health = { at: string; ok: boolean; checks: HealthCheck[]; dbBytes: number | null; tables: Record<string, number>; weekGrowth: number | null };
export const latestHealth = cache(async (): Promise<Health | null> => {
  try {
    const r = await all<{ at: string; ok: number; report: string }>("SELECT at, ok, report FROM health ORDER BY id DESC LIMIT 1");
    if (!r[0]) return null;
    const rep = parseJSON<{ checks?: HealthCheck[]; db_bytes?: number | null; tables?: Record<string, number>; week_growth_bytes?: number | null }>(r[0].report, {});
    return { at: r[0].at, ok: Boolean(r[0].ok), checks: rep.checks ?? [], dbBytes: rep.db_bytes ?? null, tables: rep.tables ?? {}, weekGrowth: rep.week_growth_bytes ?? null };
  } catch (e) {
    if (/no such table/i.test(String(e))) return null; // created by the first morning check
    throw e;
  }
});

export const latestQuality = cache(async (): Promise<QualityRun | null> => {
  try {
    const last = await all<{ id: number }>("SELECT id FROM quality_runs ORDER BY id DESC LIMIT 1");
    return last[0] ? qualityRun(last[0].id) : null;
  } catch (e) {
    if (/no such table/i.test(String(e))) return null; // table created by the first weekly run
    throw e;
  }
});
const qualityRun = persist(async (id: number): Promise<QualityRun | null> => {
  {
    const rows = await all<{ id: number; created_at: string; window_start: string; window_end: string; model_version: string; report: string }>(
      "SELECT id, created_at, window_start, window_end, model_version, report FROM quality_runs WHERE id = ?", [id],
    );
    const r = rows[0];
    return r ? { ...r, report: parseJSON<QualityReport>(r.report, { groups: {}, calibration: {}, value: {}, monthly: [], thresholds: { min_ev: 0, min_probability: 0, market_prior_sd: 0 }, n_matches: 0 }) } : null;
  }
}, "qualityRun", 24 * 3600);

// ---- paper trading registry (Fase 6): written by the analysis, settled by the collection ticks ----
export type PaperLeg = {
  id: number; fixture_id: string; sel_key: string; competition: string; match: string; kickoff: string; market: string; status: string;
  odds: number; p: number; p_market: number | null; ev: number; created_at: string; result: string | null; score: string | null;
  close_odds: number | null; close_fair: number | null; close_sisal_fair: number | null; model_version: string | null;
};
export type PaperSlip = {
  id: number; created_at: string; legs: string; total_odds: number; bonus: number; joint: number; ev: number; ev_lower: number;
  first_kickoff: string; last_kickoff: string; result: string | null; payout: number | null; clv: number | null;
};

type Registry = { legs: PaperLeg[]; slips: PaperSlip[]; lastSettled: string | null };

// The registry is cached under its version (last recorded / settled row of selections and slips), read uncached on every
// request: a time-based cache serves its stale copy to the first visit after it expires, so a settlement could show only
// on the second visit.
export const paperRegistry = cache(async (): Promise<Registry | null> => {
  let version: string;
  try {
    const v = await all<{ a: string | null; b: number | null; c: string | null; d: number | null }>(
      "SELECT (SELECT MAX(settled_at) FROM paper_legs) AS a, (SELECT MAX(id) FROM paper_legs) AS b, " +
        "(SELECT MAX(settled_at) FROM paper_slips) AS c, (SELECT MAX(id) FROM paper_slips) AS d",
    );
    version = [v[0]?.a, v[0]?.b, v[0]?.c, v[0]?.d].join("|");
  } catch (e) {
    if (/no such table/i.test(String(e))) return null; // created by the first publish after Fase 6
    throw e;
  }
  return registryAt(version);
});

const registryAt = persist(async (_version: string): Promise<Registry | null> => {
  try {
    const legSql = (sisal: string) =>
      "SELECT id, fixture_id, sel_key, competition, match, kickoff, market, status, odds, p, p_market, ev, created_at, result, score, close_odds, close_fair, " +
      `${sisal} AS close_sisal_fair, model_version FROM paper_legs ORDER BY kickoff, id`;
    const [legs, slips, last] = await Promise.all([
      // close_sisal_fair arrives with the first settlement after the pass criterion: until then the column is missing
      all<PaperLeg>(legSql("close_sisal_fair")).catch((e) => (/no such column/i.test(String(e)) ? all<PaperLeg>(legSql("NULL")) : Promise.reject(e))),
      // every slip, not the latest 200: the counts and the slip criterion must match the bankroll (~25 slips a day)
      all<PaperSlip>(
        "SELECT id, created_at, legs, total_odds, bonus, joint, ev, ev_lower, first_kickoff, last_kickoff, result, payout, clv FROM paper_slips ORDER BY id DESC",
      ),
      all<{ t: string | null }>("SELECT MAX(settled_at) AS t FROM paper_legs"),
    ]);
    return { legs, slips, lastSettled: last[0]?.t ?? null };
  } catch (e) {
    if (/no such table/i.test(String(e))) return null; // created by the first publish after Fase 6
    throw e;
  }
}, "paperRegistry", 6 * 3600); // the version in the key changes with every recording / settlement

// ---- Bankroll (Fase 9-bis): every recorded slip, oldest first, for the stake simulation (lib/bankroll.ts) ----
export type BankSlip = {
  id: number; created_at: string; settled_at: string | null; first_kickoff: string; total_odds: number; bonus: number | null; joint: number;
  ev: number; result: string | null; payout: number | null; profile: string | null; legs: string;
};

export const bankrollSlips = cache(async (): Promise<BankSlip[] | null> => {
  let version: string;
  try {
    const v = await all<{ c: string | null; d: number | null }>("SELECT (SELECT MAX(settled_at) FROM paper_slips) AS c, (SELECT MAX(id) FROM paper_slips) AS d");
    version = `${v[0]?.c}|${v[0]?.d}`;
  } catch (e) {
    if (/no such table/i.test(String(e))) return null;
    throw e;
  }
  return bankrollAt(version);
});

const bankrollAt = persist(
  async (_version: string): Promise<BankSlip[]> =>
    all<BankSlip>(
      "SELECT id, created_at, settled_at, first_kickoff, total_odds, bonus, joint, ev, result, payout, profile, legs FROM paper_slips ORDER BY created_at, id",
    ),
  "bankrollSlips",
  6 * 3600, // the version in the key changes with every recorded / settled slip
);
