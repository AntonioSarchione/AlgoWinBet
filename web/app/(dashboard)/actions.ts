"use server";

import { cookies } from "next/headers";
import { manualRefreshesThisMonth } from "@/lib/db";
import { MANUAL_MONTHLY, REPO, WORKFLOW } from "@/lib/refresh";
import { SESSION_COOKIE, verifySessionToken } from "@/lib/session";

export type RefreshState = { ok?: string; error?: string };

// GITHUB_DISPATCH_TOKEN (Vercel env var, set by the owner): fine-grained token limited to this repository,
// permission "Actions: read and write" and nothing else. It never leaves the server.
export async function manualRefresh(_prev: RefreshState, _form: FormData): Promise<RefreshState> {
  const secret = process.env.DASHBOARD_PASSWORD;
  if (!secret || !(await verifySessionToken((await cookies()).get(SESSION_COOKIE)?.value, secret))) {
    return { error: "Sessione scaduta: rientra con la password." };
  }
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  if (!token) return { error: "Aggiornamento manuale non ancora attivo: manca GITHUB_DISPATCH_TOKEN nelle variabili del progetto Vercel." };

  const used = await manualRefreshesThisMonth();
  if (used >= MANUAL_MONTHLY) return { error: `Aggiornamenti manuali esauriti: ${used} su ${MANUAL_MONTHLY} questo mese.` };

  const gh = (path: string, init?: RequestInit) =>
    fetch(`https://api.github.com/repos/${REPO}${path}`, {
      ...init,
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
    });
  try {
    // a manual run already waiting or running: a second one would only buy the same prices again
    for (const status of ["queued", "in_progress"]) {
      const r = await gh(`/actions/workflows/${WORKFLOW}/runs?event=workflow_dispatch&status=${status}&per_page=1`);
      if (!r.ok) return { error: `GitHub ha risposto ${r.status}: controlla che il token abbia il permesso Actions (lettura e scrittura) su questo repository.` };
      if (((await r.json()) as { total_count: number }).total_count > 0) {
        return { error: "Un aggiornamento è già in corso: i nuovi dati arrivano entro qualche minuto." };
      }
    }
    const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: "main", inputs: { refresh: "true" } }),
    });
    if (r.status !== 204) return { error: `GitHub non ha avviato l'aggiornamento (risposta ${r.status}).` };
  } catch {
    return { error: "GitHub non raggiungibile in questo momento: riprova tra poco." };
  }
  return { ok: "Aggiornamento avviato: quote e schedine nuove tra circa 3-5 minuti. Poi ricarica la pagina." };
}
