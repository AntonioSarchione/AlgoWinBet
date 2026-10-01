"use client";

import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { OppRow } from "@/lib/db";
import { compShort, dayTime, pct, signed, STATUS_LABEL } from "./format";
import { MatchCell } from "./ui";

type Key = "match" | "market" | "odds" | "fair_odds" | "p_final" | "p_market" | "ev" | "status";
type Sort = { key: Key; dir: "asc" | "desc" } | null;

const STATUS_RANK: Record<string, number> = { STRONG: 0, CANDIDATE: 1, FAIR: 2, WATCH: 3 };

const COLS: { key: Key; label: string; num?: boolean; first: "asc" | "desc" }[] = [
  { key: "match", label: "Partita", first: "asc" },
  { key: "market", label: "Mercato", first: "asc" },
  { key: "odds", label: "Quota", num: true, first: "desc" },
  { key: "fair_odds", label: "Equa", num: true, first: "asc" },
  { key: "p_final", label: "p modello", num: true, first: "desc" },
  { key: "p_market", label: "p mercato", num: true, first: "desc" },
  { key: "ev", label: "EV", num: true, first: "desc" },
  { key: "status", label: "Stato", first: "asc" },
];

// Sort value per column. Missing values (no market price) always go last, whatever the direction.
function value(o: OppRow, key: Key): number | string | null {
  switch (key) {
    case "match":
      return o.kickoff + (o.match ?? ""); // by kickoff, then by teams
    case "market":
      return o.market;
    case "status":
      return STATUS_RANK[o.status] ?? 9;
    default:
      return (o[key] as number | null) ?? null;
  }
}

// Rows arrive in the server's order (status, then prudent EV); a click on a header sorts by that column,
// a second click reverses it, a third one returns to the server's order.
export function OppTable({ rows }: { rows: OppRow[] }) {
  const [sort, setSort] = useState<Sort>(null);
  const shown = useMemo(() => {
    if (!sort) return rows;
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = value(a, sort.key);
      const vb = value(b, sort.key);
      if (va === null || vb === null) return va === vb ? 0 : va === null ? 1 : -1;
      if (typeof va === "string" && typeof vb === "string") return sign * va.localeCompare(vb, "it", { numeric: true });
      return sign * ((va as number) - (vb as number));
    });
  }, [rows, sort]);

  const toggle = (key: Key, first: "asc" | "desc") =>
    setSort((s) => (s?.key !== key ? { key, dir: first } : s.dir === first ? { key, dir: first === "asc" ? "desc" : "asc" } : null));

  return (
    <div className="table-wrap">
      <table className="compact">
        <thead>
          <tr>
            {COLS.map((c) => {
              const active = sort?.key === c.key;
              const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
              return (
                <th
                  key={c.key}
                  className={c.num ? "num" : undefined}
                  aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                >
                  <button type="button" className={`th-sort${active ? " active" : ""}`} onClick={() => toggle(c.key, c.first)}>
                    {c.label}
                    <Icon size={13} aria-hidden="true" />
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {shown.map((o) => {
            const [home, away] = (o.match ?? "").split(" - ");
            return (
              <tr key={`${o.fixture_id}|${o.market}|${o.bookmaker}`}>
                <td className="wrap">
                  <MatchCell home={home} away={away ?? ""} sub={<>{compShort(o.competition)} · {dayTime(o.kickoff)}</>} href={`/partita/${encodeURIComponent(o.fixture_id)}`} />
                </td>
                <td className="wrap" style={{ minWidth: 150 }}>
                  {o.market}
                  <span className="sub">{o.bookmaker}{o.odds_stale ? " · quota da ricontrollare" : o.lineup_state === "confirmed" ? " · XI ufficiali" : ""}</span>
                </td>
                <td className="num">{o.odds.toFixed(2)}</td>
                <td className="num muted">{o.fair_odds.toFixed(2)}</td>
                <td className="num">{pct(o.p_final, 1)}</td>
                <td className="num muted">{pct(o.p_market, 1)}</td>
                <td className={`num ${o.ev >= 0 ? "pos" : "neg"}`}>{signed(o.ev)}</td>
                <td><span className={`status status-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
