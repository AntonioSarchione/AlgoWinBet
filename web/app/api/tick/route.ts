import { NextResponse, type NextRequest } from "next/server";
import { lineupsPending, matchesBetween } from "@/lib/db";
import {
  inCollectionHours, isDailySlot, isHalfHourSlot, LINEUP_WATCH_FROM_MS, LINEUP_WATCH_UNTIL_MS, MATCH_AFTER_MS, MATCH_BEFORE_MS, MAX_GAP_MS, REPO, RESULTS_FROM_MS, RESULTS_UNTIL_MS, WORKFLOW,
} from "@/lib/refresh";

// Scheduler tick from an external pinger (GitHub's own cron starts most runs hours late or never). The pinger calls this
// URL every 30 minutes with "Authorization: Bearer <CRON_SECRET>"; inside the collection hours (lib/refresh.ts) it starts one
// ordinary `collect` run (the job itself decides what is worth doing, as for a cron run).
// CRON_SECRET and GITHUB_DISPATCH_TOKEN are Vercel env vars set by the owner; neither ever reaches the browser.
export const dynamic = "force-dynamic";

// Set to true to stop starting runs (the pinger still gets a 200).
const COLLECTION_PAUSED = false;
// Turso blocked until the quota reset on 2026-11-01: the runs work on a copy in the Actions cache (.github/actions/offline-db)
// and the site cannot read the database. With DB_OFFLINE the tick decides on the clock alone: one run per half-hour slot
// of the collection hours (no lineup watch between slots, no late run for evening results: the morning run settles them).
// Set back to false when Turso is back.
const DB_OFFLINE = true;

function authorized(req: NextRequest, secret: string): boolean {
  const got = req.headers.get("authorization") ?? "";
  const want = `Bearer ${secret}`;
  if (got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

async function tick(req: NextRequest) {
  const secret = process.env.CRON_SECRET;
  if (!secret || !authorized(req, secret)) return NextResponse.json({ error: "non autorizzato" }, { status: 401 });
  // collection suspended (Turso free plan over its monthly sync quota, 2026-10-09): no database read, no run, a 200 for the pinger
  if (COLLECTION_PAUSED) return NextResponse.json({ skipped: "raccolta sospesa" });
  const now = new Date();
  const force = req.nextUrl.searchParams.get("force") === "1";
  // evening matches end after the collection hours: a run when their results are due settles the registry the same night
  // half-hour slots only: with a 15-minute pinger every ping of that hour would start a run (2026-10-05: 4 runs, 2 needed)
  const resultsDue =
    !DB_OFFLINE && !force && !inCollectionHours(now) && isHalfHourSlot(now) &&
    (await matchesBetween(new Date(now.getTime() - RESULTS_UNTIL_MS), new Date(now.getTime() - RESULTS_FROM_MS)).catch(() => false));
  if (!inCollectionHours(now) && !resultsDue && !force) {
    return NextResponse.json({ skipped: "fuori dalle ore di raccolta" });
  }
  // between the half-hour slots (10-minute pinger): a run only while official lineups are due (database unreachable: no run)
  if (!force && !resultsDue && !isHalfHourSlot(now)) {
    if (DB_OFFLINE) return NextResponse.json({ skipped: "tra due giri (database non leggibile)" });
    const due = await lineupsPending(new Date(now.getTime() + LINEUP_WATCH_FROM_MS), new Date(now.getTime() + LINEUP_WATCH_UNTIL_MS)).catch(() => false);
    if (!due) return NextResponse.json({ skipped: "tra due giri: nessuna formazione in attesa" });
  }
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  if (!token) return NextResponse.json({ error: "manca GITHUB_DISPATCH_TOKEN" }, { status: 500 });
  const gh = (path: string, init?: RequestInit) =>
    fetch(`https://api.github.com/repos/${REPO}${path}`, {
      ...init,
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
    });
  try {
    for (const status of ["queued", "in_progress", "pending", "waiting"]) {
      const r = await gh(`/actions/workflows/${WORKFLOW}/runs?status=${status}&per_page=1`);
      if (!r.ok) return NextResponse.json({ error: `GitHub ${r.status}` }, { status: 502 });
      if (((await r.json()) as { total_count: number }).total_count > 0) return NextResponse.json({ skipped: `un run è già ${status}` });
    }
    if (!DB_OFFLINE && !isDailySlot(now) && !force && !resultsDue) {
      // database unreachable: run anyway (a wasted minute is better than a missed lineup)
      const near = await matchesBetween(new Date(now.getTime() - MATCH_BEFORE_MS), new Date(now.getTime() + MATCH_AFTER_MS)).catch(() => true);
      if (!near) {
        const r = await gh(`/actions/workflows/${WORKFLOW}/runs?status=completed&per_page=1`);
        const last = r.ok ? ((await r.json()) as { workflow_runs: { created_at: string }[] }).workflow_runs[0]?.created_at : undefined;
        if (last && now.getTime() - Date.parse(last) < MAX_GAP_MS) {
          return NextResponse.json({ skipped: "nessuna partita vicina e ultimo run recente" });
        }
      }
    }
    const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, { method: "POST", body: JSON.stringify({ ref: "main" }) });
    if (r.status !== 204) return NextResponse.json({ error: `GitHub ${r.status}` }, { status: 502 });
  } catch {
    return NextResponse.json({ error: "GitHub non raggiungibile" }, { status: 502 });
  }
  return NextResponse.json({ started: now.toISOString() });
}

export const GET = tick;
export const POST = tick;
