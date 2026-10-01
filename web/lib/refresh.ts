// Manual refresh from the Home: starts the `collect` GitHub Actions workflow with the "refresh" input
// (Sisal snapshot + free price history). The monthly cap is enforced twice: here before dispatching,
// and in the job itself (src/algowinbet/autorun.py, manual_monthly), which is the authoritative one.
export const REPO = "AntonioSarchione/AlgoWinBet";
export const WORKFLOW = "collect.yml";
export const MANUAL_MONTHLY = 5;
