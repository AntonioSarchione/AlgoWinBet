import Link from "next/link";
import { SearchX } from "lucide-react";

export default function NotFound() {
  return (
    <div className="card">
      <div className="empty">
        <SearchX size={32} strokeWidth={1.5} aria-hidden="true" />
        <b>Partita non trovata</b>
        <p>Non è nell&apos;ultima analisi pubblicata: potrebbe essere già giocata o fuori dai prossimi 14 giorni.</p>
        <Link href="/palinsesto" className="btn btn-primary">Vai al palinsesto</Link>
      </div>
    </div>
  );
}
