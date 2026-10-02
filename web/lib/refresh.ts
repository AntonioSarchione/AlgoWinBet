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
export const MAX_GAP_MS = 3 * 3600e3;

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
