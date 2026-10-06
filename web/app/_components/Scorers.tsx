"use client";
// Goalscorer table (Fase 9): the published probabilities of each player (analysis run, pub_fixtures.scorers), the fair price,
// and the EV at the Sisal price the user types. Sisal's scorer prices are not on the feed for the leagues: typed by hand,
// like My Combo. Nothing is sent anywhere: the typed prices live in this page only.
import { useState } from "react";
import { Shirt, Users } from "lucide-react";

export type ScorerPlayer = { n: string; r: string; s: number; a: number; f: number; d: number };
export type TeamScorers = { state: "ufficiale" | "probabile"; sheets: number; players: ScorerPlayer[] };
export type ScorersData = Record<string, TeamScorers>;

const MARKETS = [
  { k: "a", l: "Segna", hint: "almeno un gol nella partita (rigori inclusi, autogol esclusi)" },
  { k: "f", l: "Primo marcatore", hint: "il primo gol della partita è suo" },
  { k: "d", l: "Doppietta", hint: "almeno due gol" },
] as const;
type MarketKey = (typeof MARKETS)[number]["k"];

const ROLE: Record<string, string> = { GK: "Portiere", DEF: "Difensore", MID: "Centrocampista", FWD: "Attaccante" };
const pct = (x: number) => `${(x * 100).toFixed(x < 0.1 ? 1 : 0)}%`;
const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`;

function starter(s: number, state: TeamScorers["state"]) {
  if (state === "ufficiale") return s >= 1 ? "titolare" : "panchina";
  return `titolare ${Math.round(s * 100)}%`;
}

function TeamTable({ team, xg, t, market, prices, setPrice }: {
  team: string; xg: number | null; t: TeamScorers; market: MarketKey;
  prices: Record<string, string>; setPrice: (key: string, v: string) => void;
}) {
  const rows = [...t.players].sort((x, y) => y[market] - x[market]);
  return (
    <div className="sc-team">
      <div className="sc-team-head">
        <h3>{team}</h3>
        <span className={`pill ${t.state === "ufficiale" ? "pill-good" : ""}`}>
          {t.state === "ufficiale" ? <Shirt size={13} aria-hidden="true" /> : <Users size={13} aria-hidden="true" />}
          {t.state === "ufficiale" ? "Formazione ufficiale" : "Formazione probabile"}
        </span>
        <span className="muted">{xg != null ? `${xg.toFixed(2)} gol attesi · ` : ""}{t.sheets} partite di storico</span>
      </div>
      <div className="table-wrap">
        <table className="compact sc-table">
          <thead>
            <tr>
              <th>Giocatore</th>
              <th className="num">Probabilità</th>
              <th className="num">Sisal</th>
              <th className="num">EV</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const key = `${team}|${p.n}|${market}`;
              const q = Number((prices[key] ?? "").replace(",", "."));
              const ev = q > 1 ? p[market] * q - 1 : null;
              return (
                <tr key={p.n}>
                  <td>
                    <span className="sc-name">{p.n}</span>
                    <span className="sc-sub">{ROLE[p.r] ?? p.r} · {starter(p.s, t.state)}</span>
                  </td>
                  <td className="num">
                    {pct(p[market])}
                    <span className="sc-sub">equa {p[market] > 0 ? (1 / p[market]).toFixed(2) : "–"}</span>
                  </td>
                  <td className="num">
                    <input className="sc-input" type="number" inputMode="decimal" min="1.01" step="0.01" placeholder="–"
                      aria-label={`Quota Sisal ${MARKETS.find((m) => m.k === market)!.l} ${p.n}`}
                      value={prices[key] ?? ""} onChange={(e) => setPrice(key, e.target.value)} />
                  </td>
                  <td className={`num ${ev == null ? "muted" : ev >= 0 ? "pos" : "neg"}`}>{ev == null ? "–" : signed(ev)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Scorers({ home, away, xgHome, xgAway, data }: {
  home: string; away: string; xgHome: number | null; xgAway: number | null; data: ScorersData;
}) {
  const [market, setMarket] = useState<MarketKey>("a");
  const [prices, setPrices] = useState<Record<string, string>>({});
  const setPrice = (key: string, v: string) => setPrices((cur) => ({ ...cur, [key]: v }));
  const m = MARKETS.find((x) => x.k === market)!;
  return (
    <div className="col">
      <div className="sc-bar">
        <div className="combo-chips" role="radiogroup" aria-label="Mercato marcatori">
          {MARKETS.map((x) => (
            <button key={x.k} type="button" role="radio" aria-checked={market === x.k} className={`chip ${market === x.k ? "on" : ""}`}
              onClick={() => setMarket(x.k)}>
              {x.l}
            </button>
          ))}
        </div>
        <span className="note">{m.hint}</span>
      </div>
      <div className="split">
        {data[home] ? <TeamTable team={home} xg={xgHome} t={data[home]} market={market} prices={prices} setPrice={setPrice} /> : null}
        {data[away] ? <TeamTable team={away} xg={xgAway} t={data[away]} market={market} prices={prices} setPrice={setPrice} /> : null}
      </div>
      <p className="note">
        I gol attesi di ogni squadra sono divisi tra i giocatori secondo la loro quota dei gol della squadra (ultimo anno pesato di più,
        rigoristi a parte), ristretta verso la media del ruolo quando i dati sono pochi. Prima della formazione ufficiale ogni giocatore
        conta per la sua probabilità di partire titolare. Scrivi la quota che vedi su Sisal per leggere l&apos;EV: la quota equa è quella
        sotto cui la giocata non ha valore. Modello in prova: la verifica sulle partite passate è ancora in corso, quindi un EV piccolo
        non basta. Solo paper trading.
      </p>
    </div>
  );
}
