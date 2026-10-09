// "Senza regole": the slip of lib/freeslip.ts (home page and manual slip), shown instead of the profiles. Never recorded:
// it skips the model's rules.
import { CircleSlash, Gauge, Percent, Ticket, TrendingUp, Unlock, type LucideIcon } from "lucide-react";
import type { FreeResult } from "@/lib/freeslip";
import { isEstimated } from "@/lib/books";
import { compShort, hour, pct, signed } from "./format";
import { Empty, MatchCell, PBar } from "./ui";

export function FreeCard({ r, scope }: { r: FreeResult; scope: string }) {
  const s = r.slip;
  return (
    <section className="card ticket" aria-labelledby="free-title">
      <div className="card-head">
        <h2 id="free-title">
          <Unlock size={17} aria-hidden="true" /> Schedina senza regole {s && <span className="count">{s.legs.length} {s.legs.length === 1 ? "evento" : "eventi"}</span>}
        </h2>
        {s && <span className="muted">Quota totale <b className="num pos">{s.odds.toFixed(2)}</b></span>}
      </div>
      {s ? (
        <>
          <div className="table-wrap">
            <table className="compact">
              <thead>
                <tr><th>#</th><th>Evento / Mercato</th><th className="num">Quota</th><th className="num">Probabilità</th><th className="num">EV</th></tr>
              </thead>
              <tbody>
                {s.legs.map((l, i) => {
                  const [home, away] = l.match.split(" - ");
                  const ev = l.p * l.odds - 1;
                  return (
                    <tr key={l.fixture_id}>
                      <td className="muted num">{i + 1}</td>
                      <td className="wrap">
                        <MatchCell home={home} away={away ?? ""} sub={<>{l.market} · {compShort(l.competition)} · {hour(l.kickoff)}</>} href={`/partita/${encodeURIComponent(l.fixture_id)}`} />
                      </td>
                      <td className="num">
                        <span className="odds-chip">{l.odds.toFixed(2)}</span>
                        {isEstimated(l.bookmaker) && <span className="sub">Sisal stimata</span>}
                      </td>
                      <td className="num">{pct(l.p, 1)}<PBar p={l.p} mark={l.p_market} /></td>
                      <td className="num"><span className={`ev-chip ${ev >= 0 ? "pos" : "neg"}`}>{signed(ev)}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="ticket-foot">
            <Mini icon={Gauge} label="Quota totale" value={s.odds.toFixed(2)} />
            <Mini icon={Percent} label="Probabilità complessiva" value={pct(s.p, 1)} />
            <Mini icon={TrendingUp} label="EV stimato" value={signed(s.ev)} tone={s.ev >= 0 ? "pos" : "neg"} />
            <Mini icon={Ticket} label="Eventi" value={String(s.legs.length)} />
          </div>
        </>
      ) : (
        <Empty icon={CircleSlash} title="Nessuna schedina">{r.reason}</Empty>
      )}
      <p className="note card-pad">
        Senza regole: niente stato, valore atteso, soglia del 40%, rischio e mercati. Restano quota evento di almeno 1.25 e mai nazionali con
        club. Per ogni partita conta la probabilità, con un piccolo peso alla quota; tra le combinazioni che rispettano quota totale, quota per
        evento e numero di eventi ({scope}) esce la più probabile. Selezioni Sisal nel range: {r.options.toLocaleString("it-IT")} su{" "}
        {r.matches} partite. Non entra nel registro. Solo paper trading.
      </p>
    </section>
  );
}

function Mini({ icon: Icon, label, value, tone }: { icon: LucideIcon; label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div className="ticket-stat">
      <span className="note"><Icon size={13} aria-hidden="true" /> {label}</span>
      <b className={`num ${tone ?? ""}`}>{value}</b>
    </div>
  );
}
