// Turso's own count of this billing cycle (Platform API): bytes synced to the embedded replicas, which only Turso measures.
// Needs a Platform API token in the Vercel environment (TURSO_PLATFORM_TOKEN, server side only) and the organization slug
// (TURSO_ORG). Without them, or on any error, null: the page says how to connect it.
export type TursoUsage = { bytesSynced: number; storageBytes: number };

type Totals = { bytes_synced?: number; storage_bytes?: number };

export async function tursoUsage(): Promise<TursoUsage | null> {
  const token = process.env.TURSO_PLATFORM_TOKEN;
  const org = process.env.TURSO_ORG;
  if (!token || !org) return null;
  try {
    const r = await fetch(`https://api.turso.tech/v1/organizations/${encodeURIComponent(org)}/usage`, {
      headers: { Authorization: `Bearer ${token}` },
      next: { revalidate: 1800 }, // one request every 30 minutes at most
    });
    if (!r.ok) return null;
    const j = (await r.json()) as { databases?: { total?: Totals }[] };
    // the cycle's consumption is the sum over the databases (organization.usage holds the plan's allowances)
    const dbs = j.databases ?? [];
    return {
      bytesSynced: dbs.reduce((a, d) => a + (d.total?.bytes_synced ?? 0), 0),
      storageBytes: dbs.reduce((a, d) => a + (d.total?.storage_bytes ?? 0), 0),
    };
  } catch {
    return null;
  }
}
