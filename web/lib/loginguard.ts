// Brute-force guard for the dashboard login: at most MAX_TRIES wrong passwords per IP, then a lock that grows (15 minutes,
// 1 hour, 24 hours), plus a lock for every IP when the whole site sees GLOBAL_MAX wrong passwords in an hour (many IPs).
// The counts live in a small Turso database of their own (LOGIN_DB_URL, LOGIN_DB_TOKEN): the app's token on the main
// database stays read-only. Every try takes its number with one atomic UPDATE before the password is checked, so a burst
// of parallel requests cannot all pass as "first try". Without that database (not configured, or down) the login works as
// before, with only the delay on a wrong password: a broken guard must never lock the owner out.
import type { Client } from "@libsql/client";

export const MAX_TRIES = 3;
export const LOCKS_MIN = [15, 60, 24 * 60]; // first, second, third and later lock
export const GLOBAL_MAX = 20; // wrong passwords in GLOBAL_WINDOW_MIN from every IP together
export const GLOBAL_WINDOW_MIN = 60;
export const GLOBAL_LOCK_MIN = 60;
const FORGET_MIN = 24 * 60; // a day without tries: the IP starts again from zero
const ALL = "*"; // the row of the site-wide lock

const MIN = 60_000;

export type Verdict = { allowed: true; left: number } | { allowed: false; minutes: number; global: boolean };

export function lockMinutes(lockouts: number): number {
  return LOCKS_MIN[Math.min(Math.max(lockouts, 1), LOCKS_MIN.length) - 1];
}

/** The client IP as Vercel reports it (it sets both headers itself; a client cannot forge the first hop). */
export function clientIp(h: { get(name: string): string | null }): string {
  return (h.get("x-real-ip") || h.get("x-forwarded-for")?.split(",")[0] || "unknown").trim();
}

const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT PRIMARY KEY, tries INTEGER NOT NULL DEFAULT 0, lockouts INTEGER NOT NULL DEFAULT 0, " +
    "locked_until INTEGER NOT NULL DEFAULT 0, last_try INTEGER NOT NULL DEFAULT 0)",
  "CREATE TABLE IF NOT EXISTS login_failures(at INTEGER NOT NULL)",
  "CREATE INDEX IF NOT EXISTS ix_login_failures_at ON login_failures(at)",
];

export class LoginGuard {
  private ready: Promise<unknown> | null = null;
  private db: Client;

  constructor(db: Client) {
    this.db = db;
  }

  private schema() {
    this.ready ??= this.db.batch(SCHEMA, "write").catch((e) => {
      this.ready = null; // try again on the next request
      throw e;
    });
    return this.ready;
  }

  /** Called before the password is checked: refuses a locked IP (or a site-wide lock), else takes this try's number. */
  async begin(ip: string, now = Date.now()): Promise<Verdict> {
    await this.schema();
    const locks = await this.db.execute({ sql: "SELECT ip, locked_until FROM login_attempts WHERE ip IN (?, ?) AND locked_until > ?", args: [ip, ALL, now] });
    if (locks.rows.length) {
      const row = locks.rows.find((r) => r[0] === ALL) ?? locks.rows[0];
      return { allowed: false, minutes: Math.ceil((Number(row[1]) - now) / MIN), global: row[0] === ALL };
    }
    const r = await this.db.execute({
      sql:
        "INSERT INTO login_attempts(ip, tries, lockouts, locked_until, last_try) VALUES(?, 1, 0, 0, ?) ON CONFLICT(ip) DO UPDATE SET " +
        "lockouts = CASE WHEN last_try < ? THEN 0 ELSE lockouts END, tries = CASE WHEN last_try < ? THEN 1 ELSE tries + 1 END, " +
        "last_try = excluded.last_try RETURNING tries",
      args: [ip, now, now - FORGET_MIN * MIN, now - FORGET_MIN * MIN],
    });
    const tries = Number(r.rows[0][0]);
    if (tries > MAX_TRIES) {
      // a try beyond the limit taken in parallel with the one that set the lock: refused without looking at the password
      return { allowed: false, minutes: await this.lock(ip, now), global: false };
    }
    return { allowed: true, left: MAX_TRIES - tries };
  }

  /** After a wrong password: the minutes of the lock it set (0 = none yet). */
  async failed(ip: string, now = Date.now()): Promise<number> {
    await this.db.batch(
      [
        { sql: "INSERT INTO login_failures(at) VALUES(?)", args: [now] },
        { sql: "DELETE FROM login_failures WHERE at < ?", args: [now - GLOBAL_WINDOW_MIN * MIN] },
      ],
      "write",
    );
    const all = await this.db.execute({ sql: "SELECT COUNT(*) FROM login_failures WHERE at >= ?", args: [now - GLOBAL_WINDOW_MIN * MIN] });
    if (Number(all.rows[0][0]) >= GLOBAL_MAX) {
      await this.db.execute({
        sql: "INSERT INTO login_attempts(ip, tries, lockouts, locked_until, last_try) VALUES(?, 0, 1, ?, ?) " +
          "ON CONFLICT(ip) DO UPDATE SET locked_until = excluded.locked_until, last_try = excluded.last_try",
        args: [ALL, now + GLOBAL_LOCK_MIN * MIN, now],
      });
    }
    const r = await this.db.execute({ sql: "SELECT tries FROM login_attempts WHERE ip = ?", args: [ip] });
    return r.rows.length && Number(r.rows[0][0]) >= MAX_TRIES ? this.lock(ip, now) : 0;
  }

  /** The right password: the IP starts again from zero. */
  async succeeded(ip: string): Promise<void> {
    await this.db.execute({ sql: "DELETE FROM login_attempts WHERE ip = ?", args: [ip] });
  }

  /** Locks the IP once for this round of tries (parallel callers get the same lock) and returns its minutes left. */
  private async lock(ip: string, now: number): Promise<number> {
    await this.db.execute({
      sql: "UPDATE login_attempts SET lockouts = lockouts + 1, tries = 0, " +
        "locked_until = ? + 60000 * (CASE WHEN lockouts + 1 >= 3 THEN ? WHEN lockouts + 1 = 2 THEN ? ELSE ? END) " +
        "WHERE ip = ? AND tries >= ? AND locked_until <= ?",
      args: [now, LOCKS_MIN[2], LOCKS_MIN[1], LOCKS_MIN[0], ip, MAX_TRIES, now],
    });
    const r = await this.db.execute({ sql: "SELECT locked_until FROM login_attempts WHERE ip = ?", args: [ip] });
    return Math.max(1, Math.ceil((Number(r.rows[0][0]) - now) / MIN));
  }
}

let guard: LoginGuard | null | undefined;

/** The guard of this deployment, or null when LOGIN_DB_URL is not set. */
export async function loginGuard(): Promise<LoginGuard | null> {
  if (guard === undefined) {
    const url = process.env.LOGIN_DB_URL;
    if (!url) {
      guard = null;
    } else {
      const { createClient } = await import("@libsql/client");
      guard = new LoginGuard(createClient({ url, authToken: process.env.LOGIN_DB_TOKEN }));
    }
  }
  return guard;
}

export function lockText(minutes: number, global: boolean): string {
  const span = minutes >= 120 ? `${Math.ceil(minutes / 60)} ore` : minutes > 1 ? `${minutes} minuti` : "1 minuto";
  return global
    ? `Troppi tentativi sbagliati sul sito: accesso sospeso per tutti, riprova tra ${span}. Chi è già entrato resta dentro.`
    : `Troppi tentativi sbagliati: accesso bloccato da questa rete, riprova tra ${span}.`;
}
