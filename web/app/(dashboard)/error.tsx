"use client";

import { startTransition } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, RotateCw } from "lucide-react";

// reset() alone only re-renders the client tree: the failed server render must be requested again with router.refresh().
export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const router = useRouter();
  const retry = () =>
    startTransition(() => {
      router.refresh();
      reset();
    });
  return (
    <div className="card">
      <div className="empty">
        <AlertTriangle size={32} strokeWidth={1.5} aria-hidden="true" />
        <b>Impossibile mostrare la pagina</b>
        <p>Qualcosa è andato storto nel leggere o mostrare i dati. Riprova tra qualche secondo.</p>
        <button type="button" className="btn btn-primary" onClick={retry}>
          <RotateCw size={16} aria-hidden="true" /> Riprova
        </button>
        {error.digest && <span className="note">Codice errore: {error.digest}</span>}
      </div>
    </div>
  );
}
