// Manual refresh from the Home: starts the `collect` GitHub Actions workflow with the "refresh" input
// (Sisal snapshot + free price history). The monthly cap is enforced twice: here before dispatching,
// and in the job itself (src/algowinbet/autorun.py, manual_monthly), which is the authoritative one.
export const REPO = "AntonioSarchione/AlgoWinBet";
export const WORKFLOW = "collect.yml";
export const MANUAL_MONTHLY = 5;

// The external pinger calls /api/tick every 30 minutes; inside the collection hours a run starts only when it can matter:
// a match within MATCH_BEFORE / MATCH_AFTER of now (lineups, pre-kickoff prices, results and closing lines), or no run for
// MAX_GAP (price-path checkpoints far from kickoff, the daily snapshot). Quiet hours cost no Actions minutes.
export const MATCH_BEFORE_MS = 3 * 3600e3;
export const MATCH_AFTER_MS = 3.5 * 3600e3;
// one run an hour at most when no match is near (was 3 hours while the private repo paid Actions minutes); 55 minutes, not
// 60: the pinger's call an hour after a run comes a few seconds short of the hour and would skip to the next one
export const MAX_GAP_MS = 55 * 60e3;
// Lineup watch: a pinger may call every 10 minutes. The ordinary run starts only on the half-hour slots (minute 0-9 and 30-39);
// the pings in between start a run only while a match kicking off in LINEUP_WATCH_FROM..LINEUP_WATCH_UNTIL still lacks both
// official XI, so the lineups show 30-60 minutes before kickoff instead of 15-30 (2026-10-05: XI of the 20:45 matches
// stored at 20:32). With a 30-minute pinger every ping is a slot and nothing changes.
export const LINEUP_WATCH_FROM_MS = 5 * 60e3;
export const LINEUP_WATCH_UNTIL_MS = 62 * 60e3; // the job asks for the XI from 60 minutes before kickoff
export function isHalfHourSlot(d: Date): boolean {
  return d.getUTCMinutes() % 30 < 10;
}
// outside the collection hours: one or two runs for a match that kicked off 2.5-3.5 hours ago (result due, registry settled)
export const RESULTS_FROM_MS = 2.5 * 3600e3;
export const RESULTS_UNTIL_MS = 3.5 * 3600e3;

export function isDailySlot(d: Date): boolean {
  return d.getUTCHours() === 6 && d.getUTCMinutes() < 30;
}

// Same hours as the cron of .github/workflows/collect.yml (UTC): keeps the Actions minutes within the monthly budget.
export function inCollectionHours(d: Date): boolean {
  const h = d.getUTCHours();
  const day = d.getUTCDay(); // 0 = Sunday
  if (h === 6 && d.getUTCMinutes() < 30) return true; // daily run: fixtures, results, closing lines, daily snapshot
  if ([5, 6, 0, 1].includes(day)) return h >= 9 && h <= 20; // Fri-Mon: domestic leagues
  return h >= 15 && h <= 19; // Tue-Thu: Champions / Europa League and midweek rounds
}
