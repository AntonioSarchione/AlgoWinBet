import Link from "next/link";
import { SearchX } from "lucide-react";

export default function NotFound() {
  return (
    <main className="login">
      <div className="card" style={{ maxWidth: 480 }}>
        <div className="empty">
          <SearchX size={32} strokeWidth={1.5} aria-hidden="true" />
          <b>Pagina non trovata</b>
          <p>Se cercavi una partita: non è nell&apos;ultima analisi pubblicata, potrebbe essere già giocata o fuori dai prossimi 7 giorni.</p>
          <Link href="/palinsesto" className="btn btn-primary">Vai al palinsesto</Link>
        </div>
      </div>
    </main>
  );
}
