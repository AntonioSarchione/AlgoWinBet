import type { QualityReport } from "@/lib/db";
import { pct } from "./format";

/** Prudent EV on the model quality page: how much of the model's edge over the Pinnacle price the analysis trusts, the
 * share the value test's picks earned (quality.edge_shrink), with its 90% interval when the replay has one. */
export function EdgeShrink({ es }: { es: NonNullable<QualityReport["edge_shrink"]> }) {
  return (
    <div className="card-pad shrink">
      <h3>EV prudente</h3>
      <p>
        Il modello si fida di <b className="num">{pct(es.lambda)}</b> del suo vantaggio sul prezzo Pinnacle
        {es.ci && <> (intervallo 90% <span className="num">{pct(es.ci[0])}–{pct(es.ci[1])}</span>)</>}: è la parte del vantaggio previsto che le {es.n} giocate
        qui sopra hanno davvero incassato
        {es.raw != null && <>, grezza <span className="num">{es.raw.toFixed(2)}</span>{es.raw_ci && <> ({es.raw_ci[0].toFixed(2)} / {es.raw_ci[1].toFixed(2)})</>}</>}.
      </p>
      <p className="note">
        {es.lambda < 0.05
          ? "Oggi vale il prezzo del mercato: nell'EV prudente la probabilità del modello lascia il posto a quella Pinnacle, quindi una selezione è Alta solo se Sisal paga più della quota equa Pinnacle, e la puntata segue lo stesso margine."
          : `L'EV prudente usa il prezzo Pinnacle più ${pct(es.lambda)} della differenza con il modello: è Alta solo se resta positivo anche così, e la puntata segue lo stesso calcolo.`}{" "}
        Con poche giocate il valore è tirato verso il 50%; si aggiorna a ogni replay del lunedì e l&apos;analisi live usa il valore centrale, mai l&apos;intervallo.
      </p>
    </div>
  );
}
