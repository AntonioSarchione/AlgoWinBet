// Turso's own count of this billing cycle (Platform API): bytes synced to the embedded replicas, which only Turso measures.
// Needs a Platform API token in the Vercel environment (TURSO_PLATFORM_TOKEN, server side only) and the organization slug
// (TURSO_ORG). A failure is returned as a short reason for the page (never the token), and is not cached.
import { unstable_cache } from "next/cache";

export type TursoUsage = { bytesSynced: number; storageBytes: number; rowsRead: number; rowsWritten: number };
export type TursoUsageResult = { ok: true; usage: TursoUsage } | { ok: false; reason: string };

type Totals = { bytes_synced?: number; storage_bytes?: number; rows_read?: number; rows_written?: number };

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
  // organization.usage holds this cycle's consumption (seen 2026-10-09: bytes_synced 3385192448 with the dashboard at
  // 3.38 GB; the documentation calls it the plan's allowances, and the per-database totals came back null)
  const u = ((await r.json()) as { organization?: { usage?: Totals } }).organization?.usage;
  if (!u || typeof u.bytes_synced !== "number") throw new UsageError("risposta di Turso in un formato inatteso");
  return { bytesSynced: u.bytes_synced, storageBytes: u.storage_bytes ?? 0, rowsRead: u.rows_read ?? 0, rowsWritten: u.rows_written ?? 0 };
}

// one request every 30 minutes at most; a thrown error is not cached, so a fixed setting shows on the next visit
const cached = unstable_cache(fetchUsage, ["tursoUsage3"], { revalidate: 1800 });

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
