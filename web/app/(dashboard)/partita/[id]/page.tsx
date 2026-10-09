import Link from "next/link";
import { notFound } from "next/navigation";
import { AlertTriangle, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUpRight, CheckCircle2, History, Hourglass, Layers, LineChart, ListChecks, Percent, Shirt, Sigma, Users } from "lucide-react";
import { fixtureDetail, parseJSON, playerQuotes, quoteMenu, quotePath, type ModelMarket, type OppRow, type ResultRow } from "@/lib/db";
import { matchSisal, playerPool } from "@/lib/playermarkets";
import { groupOf, lineName, MARKET_GROUPS, marketName, orderMarkets, quoteLabel, selectionName, sortSelections } from "@/app/_components/markets";
import { OddsChart, type Series } from "@/app/_components/OddsChart";
import { compShort, dayLong, dayTime, fairOdds, hour, pct, shortDate, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, HBar, PBar, probColor, Ring, TeamBadge } from "@/app/_components/ui";
import { ChipRow } from "@/app/_components/ChipRow";
import { Lineups, type ProbableData } from "@/app/_components/Lineups";
import { Scorers, type ScorersData } from "@/app/_components/Scorers";
import { Trends, type TrendsData } from "@/app/_components/Trends";

export const dynamic = "force-dynamic";

const TABS = [
  { v: "probabilita", l: "Probabilità", icon: Percent },
  { v: "mercati", l: "Mercati", icon: ListChecks },
  { v: "quote", l: "Quote", icon: LineChart },
  { v: "formazioni", l: "Formazioni", icon: Shirt },
  { v: "giocatori", l: "Giocatori", icon: Users },
  { v: "ritardi", l: "Ritardi", icon: Hourglass },
  { v: "forma", l: "Forma e precedenti", icon: History },
] as const;

type QSP = { g?: string; m?: string; s?: string; l?: string };
type Props = { params: Promise<{ id: string }>; searchParams: Promise<{ tab?: string } & QSP> };

export async function generateMetadata({ params }: Props) {
  const d = await fixtureDetail(decodeURIComponent((await params).id));
  return { title: d ? `${d.fx.home} - ${d.fx.away}` : "Partita" };
}

export default async function Partita({ params, searchParams }: Props) {
  const id = decodeURIComponent((await params).id);
  const sp = await searchParams;
  const tab = sp.tab === "marcatori" ? "giocatori" : (sp.tab ?? "probabilita"); // old links to the scorers tab
  const d = await fixtureDetail(id);
  if (!d) notFound();
  const { fx } = d;
  const mk = parseJSON<ModelMarket[]>(fx.markets, []);
  const base = `/partita/${encodeURIComponent(id)}`;

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <Link href="/palinsesto" className="btn btn-ghost btn-sm"><ArrowLeft size={15} aria-hidden="true" /> Palinsesto</Link>
        <Link href={`/schedina?combo=${encodeURIComponent(id)}`} className="btn btn-ghost btn-sm"><Layers size={15} aria-hidden="true" /> Valuta una My Combo</Link>
      </div>

      <section className="card hero" aria-labelledby="match-title" style={{ gridTemplateColumns: "minmax(0,1fr)" }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 18 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, minWidth: 0 }}>
            <TeamBadge name={fx.home} size="lg" />
            <div style={{ minWidth: 0 }}>
              <span className="pill pill-good">{fx.competition}</span>
              <h1 id="match-title" style={{ margin: "8px 0 2px", fontSize: 24, letterSpacing: "-0.02em" }}>
                {fx.home} <span className="muted">vs</span> {fx.away}
              </h1>
              <p style={{ margin: 0, textTransform: "capitalize" }} className="text-2">
                {dayLong(fx.kickoff)} · ore {hour(fx.kickoff)}
              </p>
            </div>
            <TeamBadge name={fx.away} size="lg" />
          </div>
          <div className="hero-stats">
            <div className="hero-stat">
              <small>Gol attesi (modello)</small>
              <b className="num">{fx.xg_home != null && fx.xg_away != null ? `${fx.xg_home.toFixed(2)} – ${fx.xg_away.toFixed(2)}` : "–"}</b>
              <span className="note">{fx.lineup_state === "confirmed" ? "formazioni ufficiali incluse" : "formazioni non ancora ufficiali"}</span>
            </div>
            <div className="hero-stat">
              <small>Mercati con quote (24h)</small>
              <b className="num">{d.opps.length}</b>
              <span className="note">{d.nQuotes ? `${d.nQuotes.toLocaleString("it-IT")} rilevazioni di quote` : "quote non ancora raccolte"}</span>
            </div>
          </div>
        </div>
      </section>

      <section className="card">
        <nav className="tabs" aria-label="Sezioni dell'analisi">
          {TABS.map(({ v, l, icon: Icon }) => (
            <Link key={v} href={v === "probabilita" ? base : `${base}?tab=${v}`} scroll={false} aria-current={tab === v ? "page" : undefined}>
              <Icon size={16} aria-hidden="true" /> {l}
            </Link>
          ))}
        </nav>
        <div className="card-pad fade-in" key={tab}>
          {tab === "mercati" ? (
            <Markets mk={mk} opps={d.opps} />
          ) : tab === "quote" ? (
            <Quotes id={id} home={fx.home} away={fx.away} q={sp} />
          ) : tab === "formazioni" ? (
            <Lineups lineups={d.lineups} players={d.players} home={fx.home} away={fx.away} absences={d.absences}
              probable={parseJSON<ProbableData>(fx.probable, {})} analysedAt={d.run.created_at} cards={d.cards} />
          ) : tab === "giocatori" ? (
            fx.scorers || Object.keys(d.cards).length ? (
              <PlayersTab id={id} fx={fx} d={d} />
            ) : (
              <Empty icon={Users} title="Giocatori non disponibili">
                Servono almeno 5 partite di storico con formazioni e gol per entrambe le squadre (le nazionali spesso non le hanno ancora),
                e i gol attesi del modello.
              </Empty>
            )
          ) : tab === "ritardi" ? (
            <Trends home={fx.home} away={fx.away} data={parseJSON<TrendsData | null>(fx.trends, null)} />
          ) : tab === "forma" ? (
            <Form home={fx.home} away={fx.away} fh={d.formHome} fa={d.formAway} h2h={d.h2h} />
          ) : (
            <Probabilities fx={fx} mk={mk} opps={d.opps} />
          )}
        </div>
      </section>
      <p className="note">Probabilità stimate dal modello statistico, non certezze. Solo paper trading.</p>
    </>
  );
}

// Giocatori tab: goals from the published scorers table, the other markets from the published player cards, Sisal's player
// prices (when its feed has them) matched to our names
async function PlayersTab({ id, fx, d }: { id: string; fx: FixtureLike; d: NonNullable<Awaited<ReturnType<typeof fixtureDetail>>> }) {
  const data = parseJSON<ScorersData>(fx.scorers, {});
  const pm = playerPool([fx.home, fx.away], d.lineups, parseJSON(fx.probable, {}), d.players, d.cards);
  const names = [...Object.values(data).flatMap((t) => t.players.map((p) => p.n)), ...Object.values(pm).flatMap((t) => t.players.map((p) => p.n))];
  const sisal = matchSisal(names, await playerQuotes(id));
  return <Scorers home={fx.home} away={fx.away} xgHome={fx.xg_home} xgAway={fx.xg_away} data={data} pm={pm} sisal={sisal} />;
}
type FixtureLike = { home: string; away: string; xg_home: number | null; xg_away: number | null; scorers?: string | null; probable?: string | null };

function Probabilities({ fx, mk, opps }: { fx: { home: string; away: string; p_home: number | null; p_draw: number | null; p_away: number | null }; mk: ModelMarket[]; opps: OppRow[] }) {
  if (fx.p_home == null) {
    return <Empty icon={Sigma} title="Storico insufficiente">Il modello non ha abbastanza partite di queste squadre per stimare le probabilità.</Empty>;
  }
  const group = (g: string) => mk.filter((m) => m.g === g);
  const scores = group("Risultato esatto");
  const top = Math.max(...scores.map((s) => s.p), 0.01);
  return (
    <div className="col">
      <div className="split">
        <div>
          <h2 className="section">Probabilità 1X2</h2>
          <div className="rings" style={{ marginTop: 12 }}>
            <Ring value={fx.p_home} label={fx.home} sub={`quota equa ${fairOdds(fx.p_home)}`} />
            <Ring value={fx.p_draw} label="Pareggio" sub={`quota equa ${fairOdds(fx.p_draw)}`} />
            <Ring value={fx.p_away} label={fx.away} sub={`quota equa ${fairOdds(fx.p_away)}`} />
          </div>
        </div>
        <div>
          <h2 className="section">Risultati esatti più probabili</h2>
          <div style={{ marginTop: 12 }}>{scores.map((s) => <HBar key={s.l} label={s.l} p={s.p} max={top} />)}</div>
        </div>
      </div>
      <div className="split">
        <div>
          <h2 className="section">Under / Over gol totali</h2>
          <div style={{ marginTop: 12 }}>{group("Under/Over").filter((m) => m.l.startsWith("Over")).map((m) => <HBar key={m.l} label={m.l} p={m.p} />)}</div>
        </div>
        <div>
          <h2 className="section">Gol/NoGol</h2>
          <div style={{ marginTop: 12 }}>{group("Gol/NoGol").map((m) => <HBar key={m.l} label={m.l} p={m.p} />)}</div>
        </div>
      </div>
      <div>
        <h2 className="section">Gol squadra</h2>
        <div className="split" style={{ marginTop: 12 }}>
          {([["casa", fx.home], ["ospite", fx.away]] as const).map(([side, team]) => (
            <div key={side}>
              <h3 className="team-sub"><TeamBadge name={team} /> {team}</h3>
              {group("Gol squadra").filter((m) => m.l.endsWith(` ${side}`)).map((m) => (
                <HBar key={m.l} label={m.l.replace(` ${side}`, "")} p={m.p} />
              ))}
            </div>
          ))}
        </div>
      </div>
      {opps.length > 0 && (
        <div>
          <h2 className="section">Cosa dicono i modelli</h2>
          <p className="note" style={{ margin: "4px 0 10px" }}>
            Modello strutturale (statistiche e formazioni) contro mercato (quote senza margine): la stima finale li combina.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Mercato</th><th className="num">Strutturale</th><th className="num">Mercato</th><th className="num">Finale</th><th className="num">Intervallo</th></tr>
              </thead>
              <tbody>
                {opps.map((o, i) => (
                  <tr key={i}>
                    <td>{o.market}</td>
                    <td className="num">{pct(o.p_struct, 1)}</td>
                    <td className="num">{pct(o.p_market, 1)}</td>
                    <td className="num"><b>{pct(o.p_final, 1)}</b></td>
                    <td className="num muted">{o.p_low != null && o.p_high != null ? `${pct(o.p_low, 1)} – ${pct(o.p_high, 1)}` : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Markets({ mk, opps }: { mk: ModelMarket[]; opps: OppRow[] }) {
  const groups = [...new Set(mk.map((m) => m.g))];
  return (
    <div className="col">
      <div>
        <h2 className="section">Quote reali (ultime 24 ore)</h2>
        <p className="note" style={{ margin: "4px 0 10px" }}>
          Quota del bookmaker contro quota equa del modello. La barra è la probabilità finale, la tacca quella del mercato.
        </p>
        {opps.length ? (
          <div className="mkt-grid">
            {opps.map((o, i) => {
              const f = parseJSON<{ positive_factors?: string[]; negative_factors?: string[] }>(o.factors, {});
              const nf = (f.positive_factors?.length ?? 0) + (f.negative_factors?.length ?? 0);
              return (
                <article key={i} className={`mkt-card mkt-${o.ev >= 0 ? "pos" : "neg"}`}>
                  <header className="mkt-head">
                    <span>
                      <b>{o.market}</b>
                      <small>{o.bookmaker}</small>
                    </span>
                    <span className={`status status-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span>
                  </header>
                  <div className="mkt-odds">
                    <span><small>Quota</small><b className="num">{o.odds.toFixed(2)}</b></span>
                    <ArrowRight size={16} className="muted" aria-hidden="true" />
                    <span><small>Equa</small><b className="num muted">{o.fair_odds.toFixed(2)}</b></span>
                    <span className={`ev-chip ${o.ev >= 0 ? "pos" : "neg"}`}>EV {signed(o.ev)}</span>
                  </div>
                  <div className="mkt-prob">
                    <span className="note">Probabilità <b className="num">{pct(o.p_final, 1)}</b>{o.p_market != null && <> · mercato {pct(o.p_market, 1)}</>}</span>
                    <PBar p={o.p_final} mark={o.p_market} />
                  </div>
                  {nf > 0 && (
                    <details className="mkt-why">
                      <summary>Perché ({nf})</summary>
                      <ul className="checklist">
                        {(f.positive_factors ?? []).map((x, k) => <li key={`p${k}`}><CheckCircle2 size={16} className="ok" aria-label="a favore" /> <span>{x}</span></li>)}
                        {(f.negative_factors ?? []).map((x, k) => <li key={`n${k}`}><AlertTriangle size={16} className="ko" aria-label="attenzione" /> <span>{x}</span></li>)}
                      </ul>
                    </details>
                  )}
                </article>
              );
            })}
          </div>
        ) : (
          <Empty icon={LineChart} title="Nessuna quota nelle ultime 24 ore">
            Le quote Sisal e Pinnacle vengono raccolte il giorno della partita e poco prima del calcio d&apos;inizio.
          </Empty>
        )}
      </div>
      <div>
        <h2 className="section">Tutti i mercati del modello</h2>
        <p className="note" style={{ margin: "4px 0 10px" }}>
          Probabilità e quota equa per ogni mercato (in evidenza l&apos;esito più probabile del gruppo): una quota del bookmaker più alta
          della quota equa indica valore.
        </p>
        {mk.length ? (
          <div className="mkt-groups">
            {groups.map((g) => {
              const rows = mk.filter((m) => m.g === g);
              const top = Math.max(...rows.map((m) => m.p));
              return (
                <section key={g} className="mkt-group">
                  <h3>{g} <span className="count">{rows.length}</span></h3>
                  <ul>
                    <li className="mkt-hd" aria-hidden="true"><span>Esito</span><span /><span>Prob.</span><span>Equa</span></li>
                    {rows.map((m) => (
                      <li key={m.l} className={m.p === top ? "top" : undefined}>
                        <span className="mkt-sel" title={m.l}>{m.l}</span>
                        <span className="mkt-bar"><i style={{ width: `${Math.min(m.p, 1) * 100}%`, background: probColor(m.p) }} /></span>
                        <b className="num">{pct(m.p, 1)}</b>
                        <span className="num muted">{fairOdds(m.p)}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>
        ) : (
          <Empty icon={Sigma} title="Mercati non disponibili">Arrivano con la prossima analisi pubblicata.</Empty>
        )}
      </div>
    </div>
  );
}

const BOOK_STYLE = [
  { color: "var(--s1)", dash: undefined },
  { color: "var(--s2)", dash: "6 4" },
  { color: "var(--s3)", dash: "2 4" },
];

// Odds tab: pick a market, then a selection, then (for markets with lines) the line; the chart shows that one price over
// time, one line per bookmaker. Every choice is a link (?m=&s=&l=), so the view is shareable and works without JavaScript.
async function Quotes({ id, home, away, q }: { id: string; home: string; away: string; q: QSP }) {
  const menu = await quoteMenu(id);
  if (!menu.length) {
    return (
      <Empty icon={LineChart} title="Andamento quote non ancora disponibile">
        Le quote arrivano dalla fotografia giornaliera e dallo storico dei bookmaker appena la partita è in palinsesto.
      </Empty>
    );
  }
  const all = orderMarkets([...new Set(menu.map((k) => k.market_code))]);
  // market group first (whole match, 1st half, 2nd half, both halves): Sisal lists dozens of markets per match
  const groups = MARKET_GROUPS.filter((g) => all.some((c) => groupOf(c) === g.key));
  const g = groups.find((x) => x.key === q.g)?.key ?? (q.m && all.includes(q.m) ? groupOf(q.m) : groups[0]?.key ?? "ft");
  const markets = all.filter((c) => groupOf(c) === g);
  const m = markets.includes(q.m ?? "") ? q.m! : markets[0];
  const sels = [...new Set(menu.filter((k) => k.market_code === m).map((k) => k.selection))].sort(sortSelections);
  const sel = sels.includes(q.s ?? "") ? q.s! : sels[0];
  const lines = [...new Set(menu.filter((k) => k.market_code === m && k.selection === sel).map((k) => k.line_key))].sort(
    (a, b) => Number(a) - Number(b),
  );
  const preferred = m === "TOTAL_GOALS" ? "2.5" : m.startsWith("TEAM_TOTAL") ? "1.5" : ""; // otherwise the line closest to 0
  const line = lines.includes(q.l ?? "") && q.l != null
    ? q.l!
    : lines.includes(preferred)
      ? preferred
      : ([...lines].sort((a, b) => Math.abs(Number(a)) - Math.abs(Number(b)))[0] ?? "");
  const path = await quotePath(id, m, line);
  const books = [...new Set(path.map((p) => p.bookmaker))].sort((a, b) => Number(b.includes("pinnacle")) - Number(a.includes("pinnacle")));
  const label = quoteLabel(m, sel, line);
  const series: Series[] = books
    .map((b, i) => ({
      key: b, short: b.replace(/\..*$/, ""), label: b, ...BOOK_STYLE[i % BOOK_STYLE.length],
      points: path.filter((p) => p.bookmaker === b && p.selection === sel).map((p) => ({ t: new Date(p.observed_at).getTime(), v: p.odds })),
    }))
    .filter((s) => s.points.length);
  const lineSels = [...new Set(path.map((p) => p.selection))].sort(sortSelections);
  const latest = (b: string, s: string) => path.filter((p) => p.bookmaker === b && p.selection === s).at(-1);
  const href = (next: Partial<QSP>) => {
    const u = new URLSearchParams({ tab: "quote", g, m, s: sel, l: line, ...next });
    for (const [k, v] of [...u]) if (!v) u.delete(k);
    return `/partita/${encodeURIComponent(id)}?${u}`;
  };
  // the chosen selection per bookmaker: latest price and its move since the first observation
  const moves = books
    .map((b) => {
      const pts = path.filter((p) => p.bookmaker === b && p.selection === sel);
      return pts.length ? { b, first: pts[0].odds, last: pts.at(-1)!.odds, at: pts.at(-1)!.observed_at, n: pts.length } : null;
    })
    .filter((x): x is NonNullable<typeof x> => x != null);

  return (
    <div className="col">
      <div className="qpick">
        {groups.length > 1 && (
          <ChipRow label="Tempo" variant="seg" active={g} items={groups.map((x) => ({ key: x.key, label: x.label, href: href({ g: x.key, m: "", s: "", l: "" }) }))} />
        )}
        <ChipRow label="Mercato" active={m} items={markets.map((x) => ({ key: x, label: marketName(x), href: href({ m: x, s: "", l: "" }) }))} />
        <ChipRow key={`s|${m}`} label="Esito" variant={sels.length > 6 ? "scroll" : "seg"} active={sel}
          items={sels.map((x) => ({ key: x, label: selectionName(m, x), href: href({ s: x }) }))} />
        {lines.length > 1 || (lines[0] ?? "") !== "" ? (
          <ChipRow key={`l|${m}|${sel}`} label="Linea" variant={lines.length > 6 ? "scroll" : "seg"} active={line}
            items={lines.map((x) => ({ key: x, label: lineName(m, x), href: href({ l: x }) }))} />
        ) : null}
      </div>
      <div className="fade-in" key={`${m}|${sel}|${line}`}>
        <h2 className="section">Andamento · {label}</h2>
        {moves.length > 0 && (
          <div className="qtiles">
            {moves.map((x) => {
              const d = x.last / x.first - 1;
              const Icon = d > 0.004 ? ArrowUpRight : d < -0.004 ? ArrowDownRight : ArrowRight;
              return (
                <div key={x.b} className="qtile">
                  <small>{x.b}</small>
                  <b className="num">{x.last.toFixed(2)}</b>
                  <span className="note">
                    <Icon size={13} aria-hidden="true" /> {x.n > 1 ? `da ${x.first.toFixed(2)} (${d >= 0 ? "+" : "−"}${Math.abs(d * 100).toFixed(1)}%)` : "una rilevazione"} · {dayTime(x.at)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        <div className="legend" style={{ margin: "8px 0" }}>
          {series.map((s) => (
            <span key={s.key}>
              <svg width="22" height="6" aria-hidden="true"><line x1="0" x2="22" y1="3" y2="3" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash} /></svg>
              {s.label}
            </span>
          ))}
        </div>
        {series.length ? <OddsChart series={series} title={`Andamento quota ${label}, ${home} - ${away}`} /> : null}
        <p className="note">Una linea per bookmaker. Usa le frecce sinistra/destra sul grafico per scorrere le rilevazioni.</p>
      </div>
      <div className="fade-in" key={`t|${m}|${line}`}>
        <h2 className="section">Ultime quote · {marketName(m)}{line ? ` ${lineName(m, line)}` : ""}{groupOf(m) === "h1" ? " · 1° tempo" : groupOf(m) === "h2" ? " · 2° tempo" : ""}</h2>
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Bookmaker</th>
                {lineSels.map((x) => <th key={x} className="num">{selectionName(m, x)}</th>)}
                <th>Rilevata</th>
              </tr>
            </thead>
            <tbody>
              {books.map((b) => {
                const last = lineSels.map((x) => latest(b, x));
                const when = last.map((r) => r?.observed_at ?? "").sort().at(-1);
                return (
                  <tr key={b}>
                    <td>{b}</td>
                    {last.map((r, i) => <td key={i} className="num">{r?.odds.toFixed(2) ?? "–"}</td>)}
                    <td className="muted">{when ? dayTime(when) : "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}


function outcome(r: ResultRow, team: string) {
  const gf = r.home === team ? r.home_goals : r.away_goals;
  const ga = r.home === team ? r.away_goals : r.home_goals;
  return { gf, ga, res: gf > ga ? "W" : gf === ga ? "D" : "L" };
}

const RES_LABEL: Record<string, string> = { W: "V", D: "N", L: "P" };
const share = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : "–");

// goal-line facts of a list of matches (from one team's side when `team` is given)
function goalFacts(rows: ResultRow[]) {
  const tot = rows.map((r) => r.home_goals + r.away_goals);
  return {
    avg: rows.length ? (tot.reduce((s, x) => s + x, 0) / rows.length).toFixed(1) : "–",
    over: share(tot.filter((x) => x > 2.5).length, rows.length),
    btts: share(rows.filter((r) => r.home_goals > 0 && r.away_goals > 0).length, rows.length),
  };
}

function Form({ home, away, fh, fa, h2h }: { home: string; away: string; fh: ResultRow[]; fa: ResultRow[]; h2h: ResultRow[] }) {
  const block = (team: string, rows: ResultRow[]) => {
    const o = rows.map((r) => outcome(r, team));
    const n = (k: string) => o.filter((x) => x.res === k).length;
    const avg = (k: "gf" | "ga") => (o.length ? (o.reduce((s, x) => s + x[k], 0) / o.length).toFixed(1) : "–");
    const g = goalFacts(rows);
    const big = Math.max(1, ...o.map((x) => Math.abs(x.gf - x.ga)));
    return (
      <section className="fm-card">
        <header className="fm-head">
          <TeamBadge name={team} />
          <h3>{team}</h3>
          <span className="form" aria-label={`Ultime partite: ${o.map((x) => RES_LABEL[x.res]).join(" ")}`}>
            {o.map((x, i) => <span key={i} className={x.res}>{RES_LABEL[x.res]}</span>)}
          </span>
        </header>
        {!rows.length ? (
          <p className="muted">Nessuna partita di questa squadra nello storico raccolto.</p>
        ) : (
          <>
            <div className="fm-wdl" aria-label={`${n("W")} vinte, ${n("D")} pareggiate, ${n("L")} perse`}>
              {(["W", "D", "L"] as const).map((k) => n(k) > 0 && <i key={k} className={k} style={{ flex: n(k) }}><b>{n(k)}</b></i>)}
            </div>
            <div className="fm-tiles">
              <span><small>Gol fatti</small><b className="num">{avg("gf")}</b></span>
              <span><small>Gol subiti</small><b className="num">{avg("ga")}</b></span>
              <span><small>Over 2.5</small><b className="num">{g.over}</b></span>
              <span><small>Gol/Gol</small><b className="num">{g.btts}</b></span>
              <span><small>Porta inviolata</small><b className="num">{share(o.filter((x) => x.ga === 0).length, o.length)}</b></span>
            </div>
            <div className="fm-diff" aria-hidden="true" title="differenza reti partita per partita (dalla più recente)">
              {o.map((x, i) => (
                <span key={i} className={x.res}><i style={{ height: `${Math.max(12, (Math.abs(x.gf - x.ga) / big) * 100)}%` }} /></span>
              ))}
            </div>
            <ul className="fm-list">
              {rows.map((r, i) => {
                const at = r.home === team;
                return (
                  <li key={r.fixture_id}>
                    <span className="muted num">{shortDate(r.kickoff)}</span>
                    <span className="fm-opp">
                      <span className="fm-venue">{at ? "C" : "T"}</span>
                      {at ? r.away : r.home}
                      <small>{compShort(r.competition)}</small>
                    </span>
                    <b className={`fm-score ${o[i].res}`}>{r.home_goals}-{r.away_goals}</b>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </section>
    );
  };
  const hw = h2h.filter((r) => outcome(r, home).res === "W").length;
  const dr = h2h.filter((r) => r.home_goals === r.away_goals).length;
  const aw = h2h.length - hw - dr;
  const g = goalFacts(h2h);
  return (
    <div className="col">
      <div className="split" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}>
        {block(home, fh)}
        {block(away, fa)}
      </div>
      <section className="fm-card">
        <header className="fm-head">
          <span className="tr-ico"><History size={16} aria-hidden="true" /></span>
          <h3>Precedenti diretti</h3>
          {h2h.length > 0 && <span className="count">{h2h.length}</span>}
        </header>
        {h2h.length ? (
          <>
            <div className="fm-h2h">
              <div className="fm-h2h-row">
                <TeamBadge name={home} size="lg" />
                <div className="fm-h2h-bar" role="img" aria-label={`${home} ${hw} vittorie, ${dr} pareggi, ${away} ${aw} vittorie`}>
                  {([["W", hw, `${home}`], ["D", dr, "Pareggi"], ["A", aw, `${away}`]] as const).map(([k, v, l]) =>
                    v > 0 && <i key={k} className={k} style={{ flex: v }} title={`${l}: ${v}`}><b className="num">{v}</b></i>)}
                </div>
                <TeamBadge name={away} size="lg" />
              </div>
              <ul className="fm-h2h-legend">
                {([["W", hw, home, home], ["D", dr, "Pareggi", null], ["A", aw, away, away]] as const).map(([k, v, l, t]) => (
                  <li key={k}><i className={k} aria-hidden="true" />{t && <TeamBadge name={t} />}{l}<b className="num">{v}</b><small>{share(v, h2h.length)}</small></li>
                ))}
              </ul>
            </div>
            <p className="note fm-h2h-note">{g.avg} gol a partita · Over 2.5 {g.over} · Gol/Gol {g.btts}</p>
            <ul className="fm-list">
              {h2h.map((r) => {
                const res = outcome(r, home).res;
                return (
                  <li key={r.fixture_id}>
                    <span className="muted num">{shortDate(r.kickoff)}</span>
                    <span className="fm-opp">{r.home} - {r.away}<small>{compShort(r.competition)}</small></span>
                    <b className={`fm-score ${res === "W" ? "W" : res === "D" ? "D" : "A"}`}>{r.home_goals}-{r.away_goals}</b>
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          <p className="muted">Nessun precedente nelle ultime due stagioni.</p>
        )}
      </section>
    </div>
  );
}
