// Registro, player markets (Giocatori tab): every Sisal player price recorded with the official XI (src/algowinbet/paperplayers.py).
// "Giocate" are the ones the rules would play (probability at least 40%, price at least 1.25, positive EV), 1 unit each;
// the calibration reads every recorded price, played or not.
import { Users } from "lucide-react";
import type { PlayerPaperLeg } from "@/lib/db";
import { dayTime, pct, signed } from "./format";
import { Empty } from "./ui";

const RESULT_LABEL: Record<string, string> = { won: "Vinta", lost: "Persa", void: "Rimborsata", "non valutabile": "Non valutabile" };
const RESULT_CLASS: Record<string, string> = { won: "status-STRONG", lost: "status-AVOID", void: "status-WATCH", "non valutabile": "status-WATCH" };
const MIN_P = 0.4;
const MIN_ODDS = 1.25;
const SHOWN = 40;

export const isPlay = (l: PlayerPaperLeg) => l.p >= MIN_P && l.odds >= MIN_ODDS && l.ev > 0;

function marketLabel(l: PlayerPaperLeg): string {
  const k = Math.round(l.line + 0.5);
  const base: Record<string, string> = {
    SCORER: "Segna", TWO_PLUS: "Doppietta", ASSIST: "Assist", SHOTS_ON: `Tiri in porta ${k}+`, SHOTS: `Tiri ${k}+`, FOULS: `Falli fatti ${k}+`,
  };
  return `${base[l.market] ?? l.market}${l.duo ? " · DUO" : ""}`;
}
const group = (l: PlayerPaperLeg) => ({ SCORER: "Segna", TWO_PLUS: "Doppietta", ASSIST: "Assist", SHOTS_ON: "Tiri in porta", SHOTS: "Tiri", FOULS: "Falli fatti" })[l.market] ?? l.market;

type Row = { n: number; decided: number; p: number; hits: number; plays: number; playsDecided: number; pnl: number };
function tally(legs: PlayerPaperLeg[]): Row {
  const r: Row = { n: legs.length, decided: 0, p: 0, hits: 0, plays: 0, playsDecided: 0, pnl: 0 };
  for (const l of legs) {
    const decided = l.result === "won" || l.result === "lost";
    if (decided) {
      r.decided++;
      r.p += l.p;
      r.hits += l.result === "won" ? 1 : 0;
    }
    if (isPlay(l)) {
      r.plays++;
      if (decided) {
        r.playsDecided++;
        r.pnl += l.result === "won" ? l.odds - 1 : -1;
      }
    }
  }
  return r;
}

export function PlayerRegistry({ legs }: { legs: PlayerPaperLeg[] | null }) {
  const all = legs ?? [];
  const tot = tally(all);
  const groups = [...new Set(all.map(group))];
  const shown = all.filter(isPlay).slice(0, SHOWN);
  return (
    <section className="card" id="giocatori" aria-labelledby="reg-players">
      <div className="card-head">
        <h2 id="reg-players">Giocatori</h2>
        <span className="count">{tot.plays} giocate · {tot.n} quote registrate</span>
      </div>
      {!all.length ? (
        <Empty icon={Users} title="Ancora nessuna quota giocatore">
          Si registrano quando ci sono sia la formazione ufficiale sia la quota Sisal del giocatore nel feed.
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="compact">
              <caption className="sr-only">Quote giocatore registrate e giocate, per mercato</caption>
              <thead>
                <tr>
                  <th>Mercato</th><th className="num">Quote</th><th className="num">Chiuse</th><th className="num">Prevista</th>
                  <th className="num">Vera</th><th className="num">Giocate chiuse</th><th className="num">Saldo giocate</th>
                </tr>
              </thead>
              <tbody>
                {[...groups.map((g) => [g, tally(all.filter((l) => group(l) === g))] as const), ["Totale", tot] as const].map(([g, r]) => (
                  <tr key={g}>
                    <td>{g === "Totale" ? <b>{g}</b> : g}</td>
                    <td className="num">{r.n}</td>
                    <td className="num">{r.decided}</td>
                    <td className="num">{r.decided ? pct(r.p / r.decided, 1) : "–"}</td>
                    <td className="num">{r.decided ? pct(r.hits / r.decided, 1) : "–"}</td>
                    <td className="num">{r.playsDecided}</td>
                    <td className={`num ${r.pnl >= 0 ? "pos" : "neg"}`}>
                      {r.playsDecided ? `${r.pnl >= 0 ? "+" : "−"}${Math.abs(r.pnl).toFixed(2)} u (${signed(r.pnl / r.playsDecided)})` : "–"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {shown.length > 0 && (
            <div className="table-wrap">
              <table className="compact">
                <caption className="sr-only">Giocate sui giocatori, le più recenti</caption>
                <thead>
                  <tr>
                    <th>Partita</th><th>Giocatore</th><th>Mercato</th><th className="num">Quota</th><th className="num">Probabilità</th>
                    <th className="num">EV</th><th>Esito</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((l) => (
                    <tr key={l.id}>
                      <td><span className="sc-name">{l.match}</span><span className="sc-sub">{dayTime(l.kickoff)}</span></td>
                      <td>{l.player}</td>
                      <td>{marketLabel(l)}</td>
                      <td className="num">{l.odds.toFixed(2)}</td>
                      <td className="num">{pct(l.p, 1)}</td>
                      <td className={`num ${l.ev >= 0 ? "pos" : "neg"}`}>{signed(l.ev)}</td>
                      <td>
                        {l.result ? (
                          <span className={`status ${RESULT_CLASS[l.result] ?? ""}`}
                            title={l.sub ? `lui ${l.own ?? 0}, il sostituto ${l.sub}` : undefined}>
                            {RESULT_LABEL[l.result] ?? l.result}{l.sub ? " (sostituto)" : ""}
                          </span>
                        ) : <span className="muted">in attesa</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="note card-pad">
            Ogni quota giocatore di Sisal nel feed si registra una volta, quando c&apos;è la formazione ufficiale, con la nostra probabilità
            di quel momento (quella della tab Giocatori, con la regola DUO). &ldquo;Prevista&rdquo; e &ldquo;Vera&rdquo; confrontano la
            probabilità media con la frequenza vera su tutte le quote chiuse. Le giocate sono quelle che le regole giocherebbero
            (probabilità almeno {pct(MIN_P, 0)}, quota almeno {MIN_ODDS.toFixed(2)}, EV positivo), 1 unità ciascuna. Chi non entra in campo:
            rimborsata. DUO con un cambio di cui non si sa chi è entrato al suo posto: non valutabile. Primo marcatore non registrato.
          </p>
        </>
      )}
    </section>
  );
}
