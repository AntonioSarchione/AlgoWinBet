const TZ = "Europe/Rome";

export const dayTime = (iso: string) =>
  new Date(iso).toLocaleString("it-IT", { timeZone: TZ, weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
export const dayLong = (iso: string) =>
  new Date(iso).toLocaleDateString("it-IT", { timeZone: TZ, weekday: "long", day: "numeric", month: "long" });
export const hour = (iso: string) => new Date(iso).toLocaleTimeString("it-IT", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
export const shortDate = (iso: string) => new Date(iso).toLocaleDateString("it-IT", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "2-digit" });
export const dayKey = (iso: string) => new Date(iso).toLocaleDateString("sv-SE", { timeZone: TZ });

export const pct = (p: number | null | undefined, digits = 0) => (p == null ? "–" : `${(p * 100).toFixed(digits)}%`);
export const fairOdds = (p: number | null | undefined) => (p == null || p <= 0 ? "–" : (1 / p).toFixed(2));
export const signed = (x: number | null | undefined) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`);

export function ago(iso: string | null, now = Date.now()) {
  if (!iso) return "–";
  const min = Math.round((now - new Date(iso).getTime()) / 60000);
  if (min < 1) return "adesso";
  if (min < 60) return `${min} min fa`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h fa` : `${Math.round(h / 24)} giorni fa`;
}

export const STATUS_LABEL: Record<string, string> = { STRONG: "Alta", CANDIDATE: "Media", FAIR: "Equa", WATCH: "Da osservare" };

export const COMP_SHORT: Record<string, string> = {
  "Serie A": "Serie A",
  "Premier League": "Premier",
  Bundesliga: "Bundesliga",
  "Ligue 1": "Ligue 1",
  "La Liga": "LaLiga",
  "Primeira Liga": "Liga Portugal",
  Eredivisie: "Eredivisie",
  "UEFA Champions League": "Champions",
  "UEFA Europa League": "Europa League",
  "UEFA Nations League": "Nations League",
};
export const compShort = (c: string) => COMP_SHORT[c] ?? c;
