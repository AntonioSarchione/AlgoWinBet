"use client";

import { AlertTriangle, RotateCw } from "lucide-react";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="card">
      <div className="empty">
        <AlertTriangle size={32} strokeWidth={1.5} aria-hidden="true" />
        <b>Impossibile leggere i dati</b>
        <p>Il database non ha risposto. Riprova tra qualche secondo.</p>
        <button type="button" className="btn btn-primary" onClick={reset}>
          <RotateCw size={16} aria-hidden="true" /> Riprova
        </button>
      </div>
    </div>
  );
}
