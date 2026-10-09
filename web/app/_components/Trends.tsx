// Statistical streaks of a match ("ritardi", published with the analysis: pub_fixtures.trends). Each list is sorted by rarity:
// how often a streak this long happens with the average rates of the competition. The model's probability for the next match
// stands next to it: a streak alone does not make the event more likely.
import { Flame, Hourglass, Repeat, Sparkles, Swords, Target, ShieldAlert, type LucideIcon } from "lucide-react";
import { Empty, probColor, TeamBadge } from "@/app/_components/ui";

export type Trend = { t: string; r: number; m: number | null; k: "serie" | "digiuno" | "frequenza"; x: string | null };
export type TrendsData = { home: Trend[]; away: Trend[]; h2h: Trend[]; n_h2h: number; scorers: Trend[]; discipline: Trend[] };

const SHOWN = 5;
const KIND: Record<Trend["k"], { icon: LucideIcon; l: string; next: string }> = {
  serie: { icon: Flame, l: "Serie", next: "continua" },
  digiuno: { icon: Hourglass, l: "Ritardo", next: "si interrompe" },
  frequenza: { icon: Repeat, l: "Frequenza", next: "prossima" },
};
// rarity on a log scale: 1 in 2 is nothing, 1 in 1,000 or more fills the bar
const rareShare = (r: number) => Math.min(1, Math.max(0.04, Math.log10(Math.max(r, 1)) / 3));
const tier = (r: number) => (r >= 50 ? "hi" : r >= 15 ? "mid" : "lo");

function Row({ s }: { s: Trend }) {
  const K = KIND[s.k] ?? KIND.serie;
  return (
    <li className={`tr-row tr-${s.k}`}>
      <span className="tr-kind"><K.icon size={14} aria-hidden="true" />{K.l}</span>
      <div className="tr-main">
        <span className="tr-text">{s.t}</span>
        {s.x && <span className="tr-sub">{s.x}</span>}
        <span className={`tr-meter ${tier(s.r)}`} title="quanto spesso capita con le medie del campionato" aria-hidden="true">
          <i style={{ width: `${rareShare(s.r) * 100}%` }} />
        </span>
      </div>
      <div className="tr-side">
        <span className={`tr-rare ${tier(s.r)}`}>1 su {s.r.toLocaleString("it-IT")}</span>
        {s.m != null && (
          <span className="tr-model" title="probabilità del modello per la prossima partita">
            <span className="tr-dial" style={{ "--p": s.m, "--pc": probColor(s.m) } as React.CSSProperties}><b className="num">{Math.round(s.m * 100)}%</b></span>
            <small>{K.next}</small>
          </span>
        )}
        {s.m == null && (
          <span className="tr-model tr-nomodel" title="il modello stima solo i mercati dei gol: corner e cartellini no">
            <span className="tr-dial"><b>–</b></span>
            <small>nessun modello</small>
          </span>
        )}
      </div>
    </li>
  );
}

function Block({ title, head, sub, items, empty }: { title: string; head: React.ReactNode; sub?: string; items: Trend[]; empty: string }) {
  return (
    <section className="tr-block">
      <div className="tr-head">
        {head}
        <h3>{title}</h3>
        {items.length > 0 && <span className="count">{items.length}</span>}
        {sub && <span className="muted">{sub}</span>}
      </div>
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
        <p className="note tr-empty">{empty}</p>
      )}
    </section>
  );
}

const icon = (I: LucideIcon) => <span className="tr-ico"><I size={16} aria-hidden="true" /></span>;

export function Trends({ home, away, data }: { home: string; away: string; data: TrendsData | null }) {
  if (!data) {
    return <Empty icon={Hourglass} title="Nessun ritardo">Servono almeno 3 partite di storico per una delle due squadre.</Empty>;
  }
  const all = [...data.home, ...data.away, ...data.h2h, ...data.scorers, ...data.discipline];
  const rarest = all.reduce<Trend | null>((b, s) => (!b || s.r > b.r ? s : b), null);
  const count = (k: Trend["k"]) => all.filter((s) => s.k === k).length;
  return (
    <div className="col">
      <div className="tr-summary">
        {(["serie", "digiuno", "frequenza"] as const).map((k) => {
          const K = KIND[k];
          return (
            <div key={k} className={`tr-stat tr-${k}`}>
              <K.icon size={18} aria-hidden="true" />
              <b className="num">{count(k)}</b>
              <span>{k === "serie" ? "serie in corso" : k === "digiuno" ? "ritardi" : "frequenze"}</span>
            </div>
          );
        })}
        {rarest && (
          <div className="tr-rarest">
            <span className="tr-ico"><Sparkles size={16} aria-hidden="true" /></span>
            <div>
              <small>La più rara</small>
              <b>{rarest.t}</b>
            </div>
            <span className={`tr-rare ${tier(rarest.r)}`}>1 su {rarest.r.toLocaleString("it-IT")}</span>
          </div>
        )}
      </div>
      <div className="split">
        <Block title={home} head={<TeamBadge name={home} />} sub="ultime 10 partite" items={data.home} empty="Nessuna serie fuori dal comune." />
        <Block title={away} head={<TeamBadge name={away} />} sub="ultime 10 partite" items={data.away} empty="Nessuna serie fuori dal comune." />
      </div>
      <div className="split">
        <Block title="Scontri diretti" head={icon(Swords)} sub={data.n_h2h ? `ultimi ${data.n_h2h} nello storico` : "nessuno nello storico"} items={data.h2h}
          empty={data.n_h2h >= 3 ? "Nessuna serie fuori dal comune." : "Servono almeno 3 precedenti."} />
        <Block title="Marcatori" head={icon(Target)} sub="titolari delle due squadre" items={data.scorers}
          empty="Nessun digiuno o serie fuori dal comune rispetto ai gol attesi dal modello." />
      </div>
      <Block title="Falli e cartellini" head={icon(ShieldAlert)} sub="difensori e centrocampisti" items={data.discipline}
        empty="Nessuna serie fuori dal comune: servono almeno 5 partite per giocatore." />
      <p className="note">
        &quot;1 su N&quot;: quanto spesso capita una serie così lunga con le medie del campionato (ultime due stagioni) o, per i marcatori, con i gol
        che il modello attende dal giocatore; la barra è in scala logaritmica (piena da 1 su 1.000). Una serie da sola non rende l&apos;evento più
        probabile: accanto c&apos;è la probabilità del modello per la prossima partita (&quot;continua&quot;: la serie prosegue; &quot;si
        interrompe&quot;: l&apos;evento che manca accade), e xG o tiri quando ci sono, che dicono se è stata sfortuna. Solo paper trading.
      </p>
    </div>
  );
}
