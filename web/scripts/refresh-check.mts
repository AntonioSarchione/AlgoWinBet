// Checks of the scheduler rules in lib/refresh.ts (run: npm run test:refresh). The minutes thresholds must match
// src/algowinbet/actionsminutes.py: the cases below are the same as tests/test_actionsminutes.py.
import assert from "node:assert/strict";
import { inCollectionHours, isDailySlot, minutesLevel } from "../lib/refresh.ts";

const at = (s: string) => new Date(s);
const cases: [string, () => void][] = [
  ["livelli minuti come in Python", () => {
    const mid = at("2026-10-16T12:00:00Z"); // half of October gone
    assert.equal(minutesLevel(700, mid), 0); // on pace for ~1,400
    assert.equal(minutesLevel(900, mid), 1); // on pace for ~1,800: economy
    assert.equal(minutesLevel(1900, mid), 2);
    assert.equal(minutesLevel(300, at("2026-10-01T01:00:00Z")), 1); // a heavy first day already projects high
    assert.equal(minutesLevel(0, at("2026-10-01T00:00:00Z")), 0);
  }],
  ["giro del mattino 06:00-06:29 UTC", () => {
    assert.equal(isDailySlot(at("2026-10-05T06:05:00Z")), true);
    assert.equal(isDailySlot(at("2026-10-05T06:30:00Z")), false);
    assert.equal(isDailySlot(at("2026-10-05T07:05:00Z")), false);
  }],
  ["ore di raccolta", () => {
    assert.equal(inCollectionHours(at("2026-10-04T14:00:00Z")), true); // Sunday afternoon: leagues
    assert.equal(inCollectionHours(at("2026-10-04T22:00:00Z")), false);
    assert.equal(inCollectionHours(at("2026-10-07T12:00:00Z")), false); // Wednesday midday
    assert.equal(inCollectionHours(at("2026-10-07T18:00:00Z")), true); // Wednesday evening: European cups
    assert.equal(inCollectionHours(at("2026-10-07T06:10:00Z")), true); // morning run every day
  }],
];
let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${(e as Error).message}`);
  }
}
if (failed) process.exit(1);
