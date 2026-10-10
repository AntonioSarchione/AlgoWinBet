import { DatabaseZap } from "lucide-react";

// Served by the proxy for every page while DB_PAUSED is set (web/proxy.ts): reads nothing.
export default function Pausa() {
  return (
    <div className="card">
      <div className="empty">
        <DatabaseZap size={32} strokeWidth={1.5} aria-hidden="true" />
        <b>Database in pausa fino al 1° novembre</b>
        <p>
          Il piano gratuito di Turso ha esaurito la quota del mese: il sito non può leggere i dati fino al rinnovo. La raccolta continua su
          GitHub e i dati di queste settimane compariranno qui dal rientro.
        </p>
      </div>
    </div>
  );
}
