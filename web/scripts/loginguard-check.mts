// Checks of the login guard in lib/loginguard.ts on an in-memory database (run: npm run test:loginguard).
import assert from "node:assert/strict";
import { createClient } from "@libsql/client";
import { GLOBAL_MAX, LoginGuard, MAX_TRIES, clientIp, lockText } from "../lib/loginguard.ts";

const T0 = Date.parse("2026-10-06T12:00:00Z");
const MIN = 60_000;
const fresh = () => new LoginGuard(createClient({ url: ":memory:" }));

async function wrong(g: LoginGuard, ip: string, now: number) {
  const v = await g.begin(ip, now);
  if (!v.allowed) return v;
  return { ...v, locked: await g.failed(ip, now) };
}

const cases: [string, () => Promise<void>][] = [
  ["3 errori: blocco di 15 minuti, poi 1 ora, poi 24 ore", async () => {
    const g = fresh();
    assert.deepEqual(await wrong(g, "1.1.1.1", T0), { allowed: true, left: 2, locked: 0 });
    assert.deepEqual(await wrong(g, "1.1.1.1", T0), { allowed: true, left: 1, locked: 0 });
    assert.deepEqual(await wrong(g, "1.1.1.1", T0), { allowed: true, left: 0, locked: 15 });
    assert.deepEqual(await g.begin("1.1.1.1", T0 + 5 * MIN), { allowed: false, minutes: 10, global: false });
    assert.equal((await g.begin("2.2.2.2", T0 + 5 * MIN)).allowed, true); // another network is not locked
    let t = T0 + 16 * MIN;
    for (let i = 0; i < MAX_TRIES; i++) await wrong(g, "1.1.1.1", t);
    assert.deepEqual(await g.begin("1.1.1.1", t + MIN), { allowed: false, minutes: 59, global: false });
    t += 61 * MIN;
    for (let i = 0; i < MAX_TRIES; i++) await wrong(g, "1.1.1.1", t);
    assert.deepEqual(await g.begin("1.1.1.1", t + MIN), { allowed: false, minutes: 24 * 60 - 1, global: false });
  }],
  ["la password giusta azzera il conto", async () => {
    const g = fresh();
    await wrong(g, "1.1.1.1", T0);
    await wrong(g, "1.1.1.1", T0);
    assert.equal((await g.begin("1.1.1.1", T0)).allowed, true);
    await g.succeeded("1.1.1.1");
    assert.deepEqual(await g.begin("1.1.1.1", T0), { allowed: true, left: 2 });
  }],
  ["la password giusta durante il blocco non entra", async () => {
    const g = fresh();
    for (let i = 0; i < MAX_TRIES; i++) await wrong(g, "1.1.1.1", T0);
    assert.equal((await g.begin("1.1.1.1", T0 + MIN)).allowed, false); // begin comes before the password check
  }],
  ["tentativi in parallelo: solo 3 arrivano alla password", async () => {
    const g = fresh();
    const all = await Promise.all(Array.from({ length: 10 }, () => g.begin("1.1.1.1", T0)));
    assert.equal(all.filter((v) => v.allowed).length, MAX_TRIES);
  }],
  ["un giorno senza tentativi: si riparte da zero", async () => {
    const g = fresh();
    for (let i = 0; i < MAX_TRIES; i++) await wrong(g, "1.1.1.1", T0);
    const later = T0 + 25 * 60 * MIN;
    for (let i = 0; i < MAX_TRIES; i++) await wrong(g, "1.1.1.1", later);
    assert.deepEqual(await g.begin("1.1.1.1", later + MIN), { allowed: false, minutes: 14, global: false }); // 15 minutes again
  }],
  [`${GLOBAL_MAX} errori in un'ora da reti diverse: accesso sospeso per tutti per un'ora`, async () => {
    const g = fresh();
    for (let i = 0; i < GLOBAL_MAX; i++) await wrong(g, `10.0.0.${i}`, T0 + i * 1000);
    assert.deepEqual(await g.begin("9.9.9.9", T0 + 2 * MIN), { allowed: false, minutes: 59, global: true });
    assert.equal((await g.begin("9.9.9.9", T0 + 62 * MIN)).allowed, true);
  }],
  ["IP e testi", async () => {
    const h = (m: Record<string, string>) => ({ get: (k: string) => m[k] ?? null });
    assert.equal(clientIp(h({ "x-real-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8" })), "1.2.3.4");
    assert.equal(clientIp(h({ "x-forwarded-for": "5.6.7.8, 10.0.0.1" })), "5.6.7.8");
    assert.match(lockText(15, false), /riprova tra 15 minuti/);
    assert.match(lockText(24 * 60, false), /riprova tra 24 ore/);
    assert.match(lockText(60, true), /sospeso per tutti/);
  }],
];

let failed = 0;
for (const [name, fn] of cases) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${(e as Error).message}`);
  }
}
if (failed) process.exit(1);
