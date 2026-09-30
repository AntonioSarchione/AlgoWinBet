import type { OppRow } from "@/lib/db";
import { compShort, dayTime, pct, signed, STATUS_LABEL } from "./format";
import { MatchCell } from "./ui";

export function OppTable({ rows }: { rows: OppRow[] }) {
  return (
    <div className="table-wrap">
      <table className="compact">
        <thead>
          <tr>
            <th>Partita</th><th>Mercato</th><th className="num">Quota</th><th className="num">Equa</th>
            <th className="num">p modello</th><th className="num">p mercato</th><th className="num">EV</th><th>Stato</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((o, i) => {
            const [home, away] = (o.match ?? "").split(" - ");
            return (
              <tr key={i}>
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
