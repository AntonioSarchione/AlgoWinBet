// Turso's own count of this billing cycle (Platform API): bytes synced to the embedded replicas, which only Turso measures.
// Needs a Platform API token in the Vercel environment (TURSO_PLATFORM_TOKEN, server side only) and the organization slug
// (TURSO_ORG). A failure is returned as a short reason for the page (never the token), and is not cached.
import { unstable_cache } from "next/cache";

export type TursoUsage = { bytesSynced: number; storageBytes: number };
export type TursoUsageResult = { ok: true; usage: TursoUsage } | { ok: false; reason: string };

type Totals = { bytes_synced?: number; storage_bytes?: number };

class UsageError extends Error {}

async function fetchUsage(org: string): Promise<TursoUsage> {
  const r = await fetch(`https://api.turso.tech/v1/organizations/${encodeURIComponent(org)}/usage`, {
    headers: { Authorization: `Bearer ${process.env.TURSO_PLATFORM_TOKEN}` },
    cache: "no-store",
  });
  if (!r.ok) {
    const why = r.status === 401 || r.status === 403 ? "token non valido o non della piattaforma" : r.status === 404 ? "organizzazione non trovata (TURSO_ORG)" : "";
    throw new UsageError(`Turso ha risposto ${r.status}${why ? `: ${why}` : ""}`);
  }
  const j = (await r.json()) as { organization?: { usage?: Totals; databases?: { total?: Totals }[] } };
  const dbs = j.organization?.databases;
  if (!Array.isArray(dbs)) {
    console.error("tursoUsage: chiavi della risposta", Object.keys(j), Object.keys(j.organization ?? {}));
    throw new UsageError("risposta di Turso in un formato inatteso");
  }
  // TEMPORARY (2026-10-09): which counter holds the cycle's bytes synced. Counters only, no token or name.
  console.log("tursoUsage org", JSON.stringify(j.organization?.usage), "dbs", JSON.stringify(dbs.map((d) => d.total)));
  // the cycle's consumption is the sum over the databases (organization.usage is documented as the plan's allowances)
  return {
    bytesSynced: dbs.reduce((a, d) => a + (d.total?.bytes_synced ?? 0), 0),
    storageBytes: dbs.reduce((a, d) => a + (d.total?.storage_bytes ?? 0), 0),
  };
}

// one request every 30 minutes at most; a thrown error is not cached, so a fixed setting shows on the next visit
const cached = unstable_cache(fetchUsage, ["tursoUsage2"], { revalidate: 1800 });

export async function tursoUsage(): Promise<TursoUsageResult> {
  const org = process.env.TURSO_ORG?.trim();
  if (!process.env.TURSO_PLATFORM_TOKEN || !org) {
    return { ok: false, reason: "servono le variabili TURSO_PLATFORM_TOKEN e TURSO_ORG su Vercel (token API della piattaforma Turso)" };
  }
  try {
    return { ok: true, usage: await cached(org) };
  } catch (e) {
    console.error("tursoUsage", e instanceof UsageError ? e.message : e);
    return { ok: false, reason: e instanceof UsageError ? e.message : "Turso non raggiungibile" };
  }
}
