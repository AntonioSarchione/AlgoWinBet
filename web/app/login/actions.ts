"use server";

import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { clientIp, lockText, loginGuard, type LoginGuard } from "@/lib/loginguard";
import { createSessionToken, passwordMatches, safeNext, SESSION_COOKIE, SESSION_MAX_AGE } from "@/lib/session";

export type LoginState = { error?: string };

// The guard is best effort: its database down must never lock the owner out (the delay still slows guessing).
async function guarded<T>(what: string, fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch (e) {
    console.error(`login guard (${what}):`, e);
    return null;
  }
}

export async function login(_prev: LoginState, form: FormData): Promise<LoginState> {
  const secret = process.env.DASHBOARD_PASSWORD;
  if (!secret) return { error: "Accesso non configurato: imposta DASHBOARD_PASSWORD nelle variabili del progetto Vercel." };
  const password = String(form.get("password") ?? "");
  if (!password) return { error: "Inserisci la password." };
  const ip = clientIp(await headers());
  const guard: LoginGuard | null = await loginGuard();
  const verdict = guard && (await guarded("begin", () => guard.begin(ip)));
  if (verdict && !verdict.allowed) {
    return { error: lockText(verdict.minutes, verdict.global) }; // the password is not even checked while locked
  }
  if (!(await passwordMatches(password, secret))) {
    await new Promise((r) => setTimeout(r, 800)); // slows down guessing
    const locked = guard && (await guarded("failed", () => guard.failed(ip)));
    if (locked) return { error: `Password errata. ${lockText(locked, false)}` };
    if (verdict?.allowed) {
      return { error: verdict.left === 1 ? "Password errata: ancora 1 tentativo." : `Password errata: ancora ${verdict.left} tentativi.` };
    }
    return { error: "Password errata." };
  }
  if (guard) await guarded("succeeded", () => guard.succeeded(ip));
  (await cookies()).set(SESSION_COOKIE, await createSessionToken(secret), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE,
  });
  redirect(safeNext(form.get("next")));
}

export async function logout(): Promise<void> {
  (await cookies()).delete(SESSION_COOKIE);
  redirect("/login");
}
