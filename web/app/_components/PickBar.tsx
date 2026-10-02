"use client";
// Manual slip: the schedule's checkboxes (tied to form#pick) choose the matches; the choice survives a search or a
// competition filter (kept for this tab in sessionStorage, ticked matches no longer on screen travel as hidden fields).
import { useEffect, useState } from "react";
import { ListChecks } from "lucide-react";
import { MAX_PICK } from "@/lib/pick";
const KEY = "algowinbet.pick";

const boxes = () => [...document.querySelectorAll<HTMLInputElement>('input[form="pick"][name="fx"][type="checkbox"]')];

export function PickBar({ initial }: { initial: string[] }) {
  const [sel, setSel] = useState<string[]>(initial);
  const [visible, setVisible] = useState<string[]>([]);

  useEffect(() => {
    let start = initial;
    if (!start.length) {
      try {
        start = JSON.parse(sessionStorage.getItem(KEY) ?? "[]");
      } catch {
        start = [];
      }
    }
    setSel(start.slice(0, MAX_PICK));
    setVisible(boxes().map((b) => b.value));
    const onChange = (e: Event) => {
      const b = e.target as HTMLInputElement;
      if (b?.form?.id !== "pick" || b.type !== "checkbox") return;
      setSel((cur) => (b.checked ? [...new Set([...cur, b.value])].slice(0, MAX_PICK) : cur.filter((x) => x !== b.value)));
    };
    document.addEventListener("change", onChange);
    return () => document.removeEventListener("change", onChange);
  }, [initial]);

  useEffect(() => {
    for (const b of boxes()) {
      b.checked = sel.includes(b.value);
      b.disabled = b.dataset.off === "1" || (!b.checked && sel.length >= MAX_PICK);
    }
    try {
      sessionStorage.setItem(KEY, JSON.stringify(sel));
    } catch {
      /* private window: the choice simply is not remembered */
    }
  }, [sel, visible]);

  const n = sel.length;
  return (
    <form id="pick" action="/schedina" method="get" className={`pickbar ${n ? "on" : ""}`} aria-label="Schedina manuale">
      {sel.filter((id) => !visible.includes(id)).map((id) => (
        <input key={id} type="hidden" name="fx" value={id} />
      ))}
      <span>
        <b className="num">{n}</b> {n === 1 ? "partita scelta" : "partite scelte"}
        <span className="muted"> · massimo {MAX_PICK}. Il modello sceglie l&apos;esito migliore di ognuna</span>
      </span>
      <span style={{ display: "flex", gap: 8 }}>
        {n > 0 && <button type="button" className="btn" onClick={() => setSel([])}>Azzera</button>}
        <button type="submit" className="btn btn-primary" disabled={!n}>
          <ListChecks size={17} aria-hidden="true" /> Crea schedina con queste
        </button>
      </span>
    </form>
  );
}
