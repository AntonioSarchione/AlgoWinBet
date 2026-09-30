"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { createSessionToken, passwordMatches, safeNext, SESSION_COOKIE, SESSION_MAX_AGE } from "@/lib/session";

export type LoginState = { error?: string };

export async function login(_prev: LoginState, form: FormData): Promise<LoginState> {
  const secret = process.env.DASHBOARD_PASSWORD;
  if (!secret) return { error: "Accesso non configurato: imposta DASHBOARD_PASSWORD nelle variabili del progetto Vercel." };
  const password = String(form.get("password") ?? "");
  if (!password) return { error: "Inserisci la password." };
  if (!(await passwordMatches(password, secret))) {
    await new Promise((r) => setTimeout(r, 800)); // slows down guessing
    return { error: "Password errata." };
  }
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
