// Stateless session for the private dashboard: the cookie holds an expiry and an HMAC of it, keyed by DASHBOARD_PASSWORD.
// Nothing is stored server-side; changing the password in Vercel logs every browser out.
// Web Crypto only, so the same code runs in the proxy and in server actions.

export const SESSION_COOKIE = "awb_session";
export const SESSION_MAX_AGE = 60 * 60 * 24 * 30; // 30 days

const enc = new TextEncoder();

async function hmac(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
  return btoa(String.fromCharCode(...sig)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function sameString(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Constant-time password check: both sides are hashed first, so neither length nor content leaks through timing. */
export async function passwordMatches(candidate: string, expected: string): Promise<boolean> {
  const [a, b] = await Promise.all([hmac(expected, `pw:${candidate}`), hmac(expected, `pw:${expected}`)]);
  return sameString(a, b);
}

export async function createSessionToken(secret: string, now = Date.now()): Promise<string> {
  const exp = Math.floor(now / 1000) + SESSION_MAX_AGE;
  return `${exp}.${await hmac(secret, `session:${exp}`)}`;
}

export async function verifySessionToken(token: string | undefined, secret: string, now = Date.now()): Promise<boolean> {
  if (!token) return false;
  const [expRaw, sig] = token.split(".");
  const exp = Number(expRaw);
  if (!Number.isFinite(exp) || !sig || exp * 1000 < now) return false;
  return sameString(sig, await hmac(secret, `session:${exp}`));
}

/** Only same-site relative paths are allowed as a post-login destination (no open redirect). */
export function safeNext(next: unknown): string {
  return typeof next === "string" && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
}
