import "server-only";
import { createClient, type Client } from "@libsql/client";

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
};

export type SlipRow = {
  rank: number;
  total_odds: number;
  joint_probability: number;
  ev: number;
  ev_lower: number;
  stake: number;
  legs: string;
};

export type Leg = { match: string; competition: string; kickoff: string; market: string; odds: number; bookmaker: string; p: number };

export type Usage = { source: string; period: string; used: number };

function rows<T>(r: { rows: unknown[] }): T[] {
  return r.rows as unknown as T[];
}

export async function loadDashboard() {
  const c = db();
  const run = rows<Run>(await c.execute("SELECT * FROM pub_runs ORDER BY id DESC LIMIT 1"))[0];
  const now = new Date();
  const day = `D${now.toISOString().slice(0, 10)}`;
  const month = `M${now.toISOString().slice(0, 7)}`;
  const [usage, lastTick] = await Promise.all([
    c.execute({ sql: "SELECT source, period, used FROM api_usage WHERE period IN (?, ?)", args: [day, month] }),
    c.execute("SELECT MAX(fetched_at) AS t FROM raw_requests"),
  ]);
  if (!run) return { run: null, fixtures: [], opps: [], slips: [], usage: rows<Usage>(usage), lastTick: null };
  const [fixtures, opps, slips] = await Promise.all([
    c.execute({ sql: "SELECT * FROM pub_fixtures WHERE run_id = ? ORDER BY kickoff, competition", args: [run.id] }),
    c.execute({ sql: "SELECT * FROM pub_opportunities WHERE run_id = ? ORDER BY ev DESC", args: [run.id] }),
    c.execute({ sql: "SELECT * FROM pub_slips WHERE run_id = ? ORDER BY rank", args: [run.id] }),
  ]);
  return {
    run,
    fixtures: rows<FixtureRow>(fixtures),
    opps: rows<OppRow>(opps),
    slips: rows<SlipRow>(slips),
    usage: rows<Usage>(usage),
    lastTick: (lastTick.rows[0]?.t as string | null) ?? null,
  };
}
