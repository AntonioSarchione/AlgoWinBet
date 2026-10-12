"use client";
// Player markets ("Giocatori" tab). Goals: the published probabilities of each player (analysis run, pub_fixtures.scorers).
// Assists, bookings, fouls and shots: the published player cards (playercard.py, verified by player-eval) times the player's
// chance of starting. Sisal's price is shown when its feed has it for the player (player_quotes: goalscorer, first goalscorer,
// 2+ goals, shots, shots on target); otherwise the user types it, like My Combo. Typed prices live in this page only.
// Sisal's DUO markets (goals, assists, goal or assist, shots on target) count the player or whoever replaces him: their
// probability covers his slot over the whole match (lib/playermarkets.ts duoSlot), the substitute at the player's own rate.
import { useState } from "react";
import { Shirt, Users } from "lucide-react";
import { nbMean } from "@/lib/combo";
import { duoSlot, pAtLeast, type PmData, type PmPlayer, type PmTeam, type SisalPrices } from "@/lib/playermarkets";

// a/f/d: anytime, first, two or more counting the chance of starting; as/fs/ds: the same given that he starts (absent on
// publications before 2026-10-09)
export type ScorerPlayer = { n: string; r: string; s: number; a: number; f: number; d: number; as?: number; fs?: number; ds?: number };
export type TeamScorers = { state: "ufficiale" | "probabile"; sheets: number; players: ScorerPlayer[] };
export type ScorersData = Record<string, TeamScorers>;

type Market = { k: string; l: string; hint: string; sisal?: string; duo?: true };
const GOAL_MARKETS: (Market & { k: "a" | "f" | "d" | "ga" })[] = [
  { k: "a", l: "Segna", hint: "almeno un goal nella partita (rigori inclusi, autogoal esclusi)", sisal: "SCORER", duo: true },
  { k: "f", l: "Primo marcatore", hint: "il primo goal della partita è suo", sisal: "FIRST_SCORER", duo: true },
  { k: "d", l: "Doppietta", hint: "almeno due goal", sisal: "TWO_PLUS", duo: true },
  // no Sisal price in the OddsPapi feed (2026-10-09): written by hand
  { k: "ga", l: "Goal o assist", hint: "segna o fa un assist (goal dal modello marcatori, assist dalla scheda del giocatore, presi come indipendenti: la probabilità può essere un po' alta)", duo: true },
];
// every player market: probability given that he starts, not times the chance of starting: Sisal voids a player bet when
// the player does not take part, so its price is a price given that he plays. Only the starters (official XI) or the likely
// starters (our probable lineup) are listed: a bench player who comes on plays a few minutes, a start would overstate him
type CardMarket = Market & { p: (c: PmPlayer["c"]) => number; outfield?: boolean };
/** P(assist) over his DUO slot, from his assist probability in his own minutes (negative binomial, playercard.py). */
const assistDuo = (c: PmPlayer["c"]) => pAtLeast(nbMean(c.as ?? 0) * duoSlot(c.min), 1);
const LIKELY_STARTER = 0.5;
const CARD_MARKETS: Record<string, CardMarket[]> = {
  assist: [{ k: "as", l: "Fa un assist", hint: "almeno un assist (dagli assist e dagli xA del giocatore)", p: assistDuo, outfield: true, sisal: "ASSIST", duo: true }],
  cartellini: [{ k: "cg", l: "Ammonito", hint: "almeno un cartellino (giallo o rosso)", p: (c) => c.cg }],
  falli: [
    { k: "fc1", l: "Falli fatti 1+", hint: "almeno un fallo commesso", p: (c) => pAtLeast(c.fc, 1), outfield: true, sisal: "FOULS@0.5" },
    { k: "fc2", l: "Falli fatti 2+", hint: "almeno due falli commessi", p: (c) => pAtLeast(c.fc, 2), outfield: true, sisal: "FOULS@1.5" },
    { k: "fd1", l: "Falli subiti 1+", hint: "almeno un fallo subito", p: (c) => pAtLeast(c.fd, 1), outfield: true },
    { k: "fd2", l: "Falli subiti 2+", hint: "almeno due falli subiti", p: (c) => pAtLeast(c.fd, 2), outfield: true },
  ],
  tiri: [
    { k: "sh1", l: "Tiri 1+", hint: "almeno un tiro (Over 0.5 tiri)", p: (c) => pAtLeast(c.sh, 1), outfield: true, sisal: "SHOTS@0.5" },
    { k: "sh2", l: "Tiri 2+", hint: "almeno due tiri (Over 1.5 tiri)", p: (c) => pAtLeast(c.sh, 2), outfield: true, sisal: "SHOTS@1.5" },
    { k: "sh3", l: "Tiri 3+", hint: "almeno tre tiri (Over 2.5 tiri)", p: (c) => pAtLeast(c.sh, 3), outfield: true, sisal: "SHOTS@2.5" },
    { k: "sot1", l: "In porta 1+", hint: "almeno un tiro in porta (Over 0.5)", p: (c) => pAtLeast((c.sot ?? 0) * duoSlot(c.min), 1), outfield: true, sisal: "SHOTS_ON@0.5", duo: true },
    { k: "sot2", l: "In porta 2+", hint: "almeno due tiri in porta (Over 1.5)", p: (c) => pAtLeast((c.sot ?? 0) * duoSlot(c.min), 2), outfield: true, sisal: "SHOTS_ON@1.5", duo: true },
  ],
};
const GROUPS = [
  { k: "goal", l: "Goal" },
  { k: "assist", l: "Assist" },
  { k: "cartellini", l: "Cartellini" },
  { k: "falli", l: "Falli" },
  { k: "tiri", l: "Tiri" },
] as const;
type GroupKey = (typeof GROUPS)[number]["k"];

const ROLE: Record<string, string> = { GK: "Portiere", DEF: "Difensore", MID: "Centrocampista", FWD: "Attaccante" };
const TOP = 14;       // players listed per team
const MIN_P = 0.01;   // below this a player is not listed
const pct = (x: number) => `${(x * 100).toFixed(x < 0.1 ? 1 : 0)}%`;
const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`;

function starter(s: number, state: TeamScorers["state"]) {
  if (state === "ufficiale") return s >= 1 ? "titolare" : "panchina";
  return `titolare ${Math.round(s * 100)}%`;
}

type Row = { n: string; sub: string; p: number };

function TeamTable({ team, head, state, rows, market, sisal, prices, setPrice }: {
  team: string; head: string; state: PmTeam["state"]; rows: Row[]; market: Market; sisal: SisalPrices;
  prices: Record<string, string>; setPrice: (key: string, v: string) => void;
}) {
  return (
    <div className="sc-team">
      <div className="sc-team-head">
        <h3>{team}</h3>
        <span className={`pill ${state === "ufficiale" ? "pill-good" : ""}`}>
          {state === "ufficiale" ? <Shirt size={13} aria-hidden="true" /> : <Users size={13} aria-hidden="true" />}
          {state === "ufficiale" ? "Formazione ufficiale" : "Formazione probabile"}
        </span>
        {head && <span className="muted">{head}</span>}
      </div>
      {rows.length ? (
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
                const key = `${team}|${p.n}|${market.k}`;
                const feed = market.sisal ? sisal[p.n]?.[market.sisal] : undefined;
                const q = feed ?? Number((prices[key] ?? "").replace(",", "."));
                const ev = q > 1 ? p.p * q - 1 : null;
                return (
                  <tr key={p.n}>
                    <td>
                      <span className="sc-name">{p.n}</span>
                      <span className="sc-sub">{p.sub}</span>
                    </td>
                    <td className="num">
                      {pct(p.p)}
                      <span className="sc-sub">equa {p.p > 0 ? (1 / p.p).toFixed(2) : "–"}</span>
                    </td>
                    <td className="num">
                      {feed ? (
                        <span className="odds-chip" title="Quota Sisal dall'ultima fotografia delle quote">{feed.toFixed(2)}</span>
                      ) : (
                        <input className="sc-input" type="number" inputMode="decimal" min="1.01" step="0.01" placeholder="–"
                          aria-label={`Quota Sisal ${market.l} ${p.n}`} value={prices[key] ?? ""} onChange={(e) => setPrice(key, e.target.value)} />
                      )}
                    </td>
                    <td className="num">
                      {ev == null ? <span className="muted">–</span> : <span className={`ev-chip ${ev >= 0 ? "pos" : "neg"}`}>{signed(ev)}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="note">Nessun giocatore con storico sufficiente per questo mercato.</p>
      )}
    </div>
  );
}

export function Scorers({ home, away, xgHome, xgAway, data, pm = {}, sisal = {} }: {
  home: string; away: string; xgHome: number | null; xgAway: number | null; data: ScorersData; pm?: PmData; sisal?: SisalPrices;
}) {
  const [group, setGroup] = useState<GroupKey>("goal");
  const [mk, setMk] = useState<Record<string, string>>({ goal: "a", assist: "as", cartellini: "cg", falli: "fc1", tiri: "sh1" });
  const [prices, setPrices] = useState<Record<string, string>>({});
  const setPrice = (key: string, v: string) => setPrices((cur) => ({ ...cur, [key]: v }));
  const markets: Market[] = group === "goal" ? GOAL_MARKETS : CARD_MARKETS[group];
  const market = markets.find((x) => x.k === mk[group]) ?? markets[0];
  const nSisal = Object.keys(sisal).length;

  const goalRows = (t: TeamScorers, team: string): Row[] => {
    const given = (p: ScorerPlayer, k: "a" | "f" | "d") => (p.s >= 1 ? p[k] : p[`${k}s`] ?? p[k] / Math.max(p.s, 0.01));
    const key = (n: string) => n.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
    const cards = new Map((pm[team]?.players ?? []).map((p) => [key(p.n), p.c] as const));
    // DUO: his own start (published) plus whoever replaces him for the rest of the match, scoring at his rate (Poisson)
    const prob = (p: ScorerPlayer): number | undefined => {
      const c = cards.get(key(p.n));
      const whole = duoSlot(c?.min);
      const a = Math.min(given(p, "a"), 0.95);
      const mu = -Math.log(1 - a) * (whole - 1); // the substitute's expected goals
      const none = Math.exp(-mu);
      const anytime = 1 - (1 - a) * none;
      if (market.k === "a") return anytime;
      if (market.k === "d") return 1 - (1 - a) * none - (a - given(p, "d")) * none - (1 - a) * mu * none;
      if (market.k === "f") return given(p, "f") * whole; // first goal: his share of the team's goals over the whole slot (as My Combo)
      return c?.as == null ? undefined : 1 - (1 - anytime) * (1 - assistDuo(c));
    };
    return t.players
      .filter((p) => p.s >= LIKELY_STARTER)
      .flatMap((p) => {
        const v = prob(p);
        return v == null ? [] : [{ n: p.n, sub: `${ROLE[p.r] ?? p.r} · ${starter(p.s, t.state)}`, p: Math.min(v, 0.99) }];
      })
      .sort((a, b) => b.p - a.p);
  };
  const cardRows = (t: PmTeam): Row[] => {
    const m = market as CardMarket;
    return t.players
      .filter((p) => !(m.outfield && p.c.gk) && p.s >= LIKELY_STARTER)
      .map((p) => ({ n: p.n, sub: [ROLE[p.r] ?? p.r, starter(p.s, t.state), `${p.c.n} presenze`].filter(Boolean).join(" · "), p: m.p(p.c) }))
      .filter((r) => r.p >= MIN_P)
      .sort((a, b) => b.p - a.p)
      .slice(0, TOP);
  };
  const side = (team: string, xg: number | null) => {
    if (group === "goal") {
      const t = data[team];
      return t ? (
        <TeamTable key={team} team={team} state={t.state} rows={goalRows(t, team)} market={market} sisal={sisal} prices={prices} setPrice={setPrice}
          head={`${xg != null ? `${xg.toFixed(2)} goal attesi · ` : ""}${t.sheets} partite di storico`} />
      ) : null;
    }
    const t = pm[team];
    return t ? (
      <TeamTable key={team} team={team} state={t.state} rows={cardRows(t)} market={market} sisal={sisal} prices={prices} setPrice={setPrice} head="" />
    ) : null;
  };
  const anyData = group === "goal" ? Boolean(data[home] || data[away]) : Boolean(pm[home] || pm[away]);

  return (
    <div className="col">
      <div className="seg" role="radiogroup" aria-label="Categoria di mercati del giocatore">
        {GROUPS.map((x) => (
          <button key={x.k} type="button" role="radio" aria-checked={group === x.k} onClick={() => setGroup(x.k)}>
            {x.l}
          </button>
        ))}
      </div>
      <div className="sc-bar chips-sub fade-in" key={group}>
        {markets.length > 1 && (
          <div className="combo-chips" role="radiogroup" aria-label="Mercato">
            {markets.map((x) => (
              <button key={x.k} type="button" role="radio" aria-checked={market.k === x.k} className={`chip ${market.k === x.k ? "on" : ""}`}
                onClick={() => setMk((cur) => ({ ...cur, [group]: x.k }))}>
                {x.l}{x.duo ? " · DUO" : ""}
              </button>
            ))}
          </div>
        )}
        <span className="note">
          {markets.length > 1 ? market.hint : `${market.l}: ${market.hint}`}
          {market.duo ? ". DUO: vale anche se lo fa chi entra al suo posto" : ""}
        </span>
      </div>
      {anyData ? (
        <div className="split fade-in" key={`${group}|${market.k}`}>
          {side(home, xgHome)}
          {side(away, xgAway)}
        </div>
      ) : (
        <p className="note">
          Nessuna scheda giocatore pubblicata per questa partita: servono la formazione (ufficiale o la nostra probabile) e lo storico
          FotMob dei giocatori.
        </p>
      )}
      <p className="note">
        {group === "goal"
          ? "I goal attesi di ogni squadra sono divisi tra i giocatori secondo la loro quota dei goal della squadra (ultimo anno pesato di più, rigoristi a parte), ristretta verso la media del ruolo quando i dati sono pochi."
          : "Dalle ultime 60 presenze FotMob del giocatore, ristrette verso la media del suo ruolo, scalate ai minuti che gioca da titolare e corrette per avversario e campo (verificate sulle partite passate con player-eval)."}{" "}
        La probabilità è quella di una partita da titolare: Sisal rimborsa la giocata se il giocatore non scende in campo. Nei
        mercati DUO (goal, assist, goal o assist, tiri in porta) conta anche chi entra al suo posto: la probabilità copre i suoi
        minuti più quelli del sostituto, che segna e tira come lui. Sono
        elencati solo i titolari (formazione ufficiale) o, prima, i titolari probabili (almeno 50%).{" "}
        {nSisal > 0
          ? "La quota Sisal compare da sola quando la fotografia delle quote la contiene (marcatori, assist, tiri e falli: cartellini non offerti nel feed); altrimenti scrivila tu per leggere l'EV."
          : "Scrivi la quota che vedi su Sisal per leggere l'EV."}{" "}
        La quota equa è quella sotto cui la giocata non ha valore. Nessun prezzo Pinnacle di confronto sui giocatori: un EV piccolo non basta.
        Solo paper trading.
      </p>
    </div>
  );
}
