"use client";

import { useActionState, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { manualRefresh, type RefreshState } from "@/app/(dashboard)/actions";

// Two-step button: each manual refresh uses one of the month's few, so the first click only asks for confirmation.
export function RefreshButton({ used, limit, configured }: { used: number; limit: number; configured: boolean }) {
  const [state, action, pending] = useActionState<RefreshState, FormData>(manualRefresh, {});
  const [confirm, setConfirm] = useState(false);
  const left = Math.max(limit - used, 0);
  const done = Boolean(state.ok);

  return (
    <form action={action} onSubmit={() => setConfirm(false)} className="refresh">
      <div className="kv" style={{ padding: 0 }}>
        <span>Aggiornamenti manuali</span>
        <span className="num">{left} su {limit} rimasti</span>
      </div>
      {confirm ? (
        <div style={{ display: "flex", gap: 8 }}>
          <button type="submit" className="btn btn-primary btn-sm" style={{ flex: 1 }}>Conferma</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirm(false)}>Annulla</button>
        </div>
      ) : (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={pending || done || !left || !configured}
          onClick={() => setConfirm(true)}
        >
          {pending ? <Loader2 size={15} className="spin" aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
          {pending ? "Avvio in corso…" : "Aggiorna quote adesso"}
        </button>
      )}
      <p className="note" style={{ margin: 0 }} role={state.error || state.ok ? "status" : undefined}>
        {state.error ?? state.ok ??
          (configured
            ? "Fotografia Sisal (2 richieste conteggiate) e storico Sisal + Pinnacle (richieste libere), poi schedine ricalcolate."
            : "Non ancora attivo: serve il token GitHub nelle variabili del progetto Vercel.")}
      </p>
    </form>
  );
}
