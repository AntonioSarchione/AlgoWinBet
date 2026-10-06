// Statistical streaks of a match ("ritardi", published with the analysis: pub_fixtures.trends). Each list is sorted by rarity:
// how often a streak this long happens with the average rates of the competition. The model's probability for the next match
// stands next to it: a streak alone does not make the event more likely.
import { Flame, Hourglass, Repeat } from "lucide-react";
import { Empty } from "@/app/_components/ui";

export type Trend = { t: string; r: number; m: number | null; k: "serie" | "digiuno" | "frequenza"; x: string | null };
export type TrendsData = { home: Trend[]; away: Trend[]; h2h: Trend[]; n_h2h: number; scorers: Trend[]; discipline: Trend[] };

const SHOWN = 5;
const KIND = { serie: { icon: Flame, l: "Serie" }, digiuno: { icon: Hourglass, l: "Ritardo" }, frequenza: { icon: Repeat, l: "Frequenza" } };

function Row({ s }: { s: Trend }) {
  const K = KIND[s.k] ?? KIND.serie;
  return (
    <li className="tr-row">
      <K.icon size={16} aria-hidden="true" className="tr-icon" />
      <div className="tr-main">
        <span className="tr-text">{s.t}</span>
        {s.x && <span className="tr-sub">{s.x}</span>}
      </div>
      <div className="tr-side">
        <span className={`tr-rare ${s.r >= 50 ? "hi" : s.r >= 15 ? "mid" : ""}`} title="quanto spesso capita con le medie del campionato">1 su {s.r.toLocaleString("it-IT")}</span>
        {s.m != null && <span className="tr-model">prossima: {Math.round(s.m * 100)}%</span>}
      </div>
    </li>
  );
}

function Block({ title, sub, items, empty }: { title: string; sub?: string; items: Trend[]; empty: string }) {
  return (
    <section className="tr-block">
      <div className="tr-head"><h3>{title}</h3>{sub && <span className="muted">{sub}</span>}</div>
      {items.length ? (
        <>
          <ul className="tr-list">{items.slice(0, SHOWN).map((s) => <Row key={s.t} s={s} />)}</ul>
          {items.length > SHOWN && (
            <details className="tr-more">
              <summary>Altri {items.length - SHOWN}</summary>
              <ul className="tr-list">{items.slice(SHOWN).map((s) => <Row key={s.t} s={s} />)}</ul>
            </details>
          )}
        </>
      ) : (
        <p className="note">{empty}</p>
      )}
    </section>
  );
}

export function Trends({ home, away, data }: { home: string; away: string; data: TrendsData | null }) {
  if (!data) {
    return <Empty icon={Hourglass} title="Nessun ritardo">Servono almeno 3 partite di storico per una delle due squadre.</Empty>;
  }
  return (
    <div className="col">
      <div className="split">
        <Block title={home} sub="ultime 10 partite" items={data.home} empty="Nessuna serie fuori dal comune." />
        <Block title={away} sub="ultime 10 partite" items={data.away} empty="Nessuna serie fuori dal comune." />
      </div>
      <div className="split">
        <Block title="Scontri diretti" sub={data.n_h2h ? `ultimi ${data.n_h2h} nello storico` : "nessuno nello storico"} items={data.h2h}
          empty={data.n_h2h >= 3 ? "Nessuna serie fuori dal comune." : "Servono almeno 3 precedenti."} />
        <Block title="Marcatori" sub="titolari delle due squadre" items={data.scorers}
          empty="Nessun digiuno o serie fuori dal comune rispetto ai gol attesi dal modello." />
      </div>
      <Block title="Falli e cartellini" sub="difensori e centrocampisti" items={data.discipline}
        empty="I dati di falli e cartellini per giocatore arrivano da API-Football dalle prossime partite: servono almeno 5 partite per giocatore." />
      <p className="note">
        &quot;1 su N&quot;: quanto spesso capita una serie così lunga con le medie del campionato (ultime due stagioni) o, per i marcatori, con i gol
        che il modello attende dal giocatore. Una serie da sola non rende l&apos;evento più probabile: accanto c&apos;è la probabilità del modello per
        la prossima partita, e xG o tiri quando ci sono, che dicono se è stata sfortuna. Solo paper trading.
      </p>
    </div>
  );
}
