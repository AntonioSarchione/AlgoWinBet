import Link from "next/link";
import { notFound } from "next/navigation";
import { AlertTriangle, ArrowLeft, CheckCircle2, History, Layers, LineChart, ListChecks, Percent, Shirt, Sigma } from "lucide-react";
import { fixtureDetail, parseJSON, quoteMenu, quotePath, type ModelMarket, type OppRow, type ResultRow } from "@/lib/db";
import { groupOf, lineName, MARKET_GROUPS, marketName, orderMarkets, quoteLabel, selectionName, sortSelections } from "@/app/_components/markets";
import { OddsChart, type Series } from "@/app/_components/OddsChart";
import { compShort, dayLong, dayTime, fairOdds, hour, pct, shortDate, signed, STATUS_LABEL } from "@/app/_components/format";
import { Empty, HBar, Ring, TeamBadge } from "@/app/_components/ui";
import { Lineups } from "@/app/_components/Lineups";

export const dynamic = "force-dynamic";

const TABS = [
  { v: "probabilita", l: "Probabilità", icon: Percent },
  { v: "mercati", l: "Mercati", icon: ListChecks },
  { v: "quote", l: "Quote", icon: LineChart },
  { v: "formazioni", l: "Formazioni", icon: Shirt },
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
  const tab = sp.tab ?? "probabilita";
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
        <div className="card-pad">
          {tab === "mercati" ? (
            <Markets mk={mk} opps={d.opps} />
          ) : tab === "quote" ? (
            <Quotes id={id} home={fx.home} away={fx.away} q={sp} />
          ) : tab === "formazioni" ? (
            <Lineups lineups={d.lineups} players={d.players} home={fx.home} away={fx.away} />
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
            <Ring value={fx.p_home} label={fx.home} sub={`quota equa ${fairOdds(fx.p_home)}`} color="var(--s1)" />
            <Ring value={fx.p_draw} label="Pareggio" sub={`quota equa ${fairOdds(fx.p_draw)}`} color="var(--s2)" />
            <Ring value={fx.p_away} label={fx.away} sub={`quota equa ${fairOdds(fx.p_away)}`} color="var(--s3)" />
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
          <h2 className="section">Gol/NoGol e gol squadra</h2>
          <div style={{ marginTop: 12 }}>{[...group("Gol/NoGol"), ...group("Gol squadra")].map((m) => <HBar key={m.l} label={m.l} p={m.p} />)}</div>
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
        {opps.length ? (
          <div className="col" style={{ marginTop: 12, gap: 12 }}>
            {opps.map((o, i) => {
              const f = parseJSON<{ positive_factors?: string[]; negative_factors?: string[] }>(o.factors, {});
              return (
                <div key={i} className="card card-pad" style={{ boxShadow: "none", background: "var(--card-2)" }}>
                  <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 10, alignItems: "center" }}>
                    <div>
                      <b>{o.market}</b> <span className="muted">· {o.bookmaker}</span>
                      <span className="sub">quota {o.odds.toFixed(2)} · equa {o.fair_odds.toFixed(2)} · p {pct(o.p_final, 1)}</span>
                    </div>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <b className={`num ${o.ev >= 0 ? "pos" : "neg"}`}>EV {signed(o.ev)}</b>
                      <span className={`status status-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span>
                    </div>
                  </div>
                  {(f.positive_factors?.length || f.negative_factors?.length) ? (
                    <ul className="checklist" style={{ marginTop: 10 }}>
                      {(f.positive_factors ?? []).map((x, k) => <li key={`p${k}`}><CheckCircle2 size={16} className="ok" aria-label="a favore" /> <span>{x}</span></li>)}
                      {(f.negative_factors ?? []).map((x, k) => <li key={`n${k}`}><AlertTriangle size={16} className="ko" aria-label="attenzione" /> <span>{x}</span></li>)}
                    </ul>
                  ) : null}
                </div>
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
          Probabilità e quota equa per ogni mercato: una quota del bookmaker più alta della quota equa indica valore.
        </p>
        {mk.length ? (
          <div className="rail" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
            {groups.map((g) => (
              <div key={g} className="card" style={{ boxShadow: "none" }}>
                <div className="card-head"><h3>{g}</h3></div>
                <table>
                  <thead><tr><th>Esito</th><th className="num">Probabilità</th><th className="num">Quota equa</th></tr></thead>
                  <tbody>
                    {mk.filter((m) => m.g === g).map((m) => (
                      <tr key={m.l}>
                        <td>{m.l}</td>
                        <td className="num">{pct(m.p, 1)}</td>
                        <td className="num muted">{fairOdds(m.p)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
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

  return (
    <div className="col">
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {groups.length > 1 && (
          <nav className="chips" aria-label="Parte di partita">
            {groups.map((x) => (
              <Link key={x.key} href={href({ g: x.key, m: "", s: "", l: "" })} scroll={false} aria-current={x.key === g ? "true" : undefined}>{x.label}</Link>
            ))}
          </nav>
        )}
        <nav className={groups.length > 1 ? "chips chips-sub" : "chips"} aria-label="Mercato">
          {markets.map((x) => (
            <Link key={x} href={href({ m: x, s: "", l: "" })} scroll={false} aria-current={x === m ? "true" : undefined}>{marketName(x)}</Link>
          ))}
        </nav>
        <nav className="chips chips-sub" aria-label="Esito">
          {sels.map((x) => (
            <Link key={x} href={href({ s: x })} scroll={false} aria-current={x === sel ? "true" : undefined}>{selectionName(m, x)}</Link>
          ))}
        </nav>
        {lines.length > 1 || (lines[0] ?? "") !== "" ? (
          <nav className="chips chips-sub" aria-label="Linea">
            {lines.map((x) => (
              <Link key={x} href={href({ l: x })} scroll={false} aria-current={x === line ? "true" : undefined}>{lineName(m, x)}</Link>
            ))}
          </nav>
        ) : null}
      </div>
      <div>
        <h2 className="section">Andamento · {label}</h2>
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
      <div>
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

function Form({ home, away, fh, fa, h2h }: { home: string; away: string; fh: ResultRow[]; fa: ResultRow[]; h2h: ResultRow[] }) {
  const LBL: Record<string, string> = { W: "V", D: "N", L: "P" };
  const block = (team: string, rows: ResultRow[]) => {
    const o = rows.map((r) => outcome(r, team));
    const avg = (k: "gf" | "ga") => (o.length ? (o.reduce((s, x) => s + x[k], 0) / o.length).toFixed(1) : "–");
    return (
      <div className="card" style={{ boxShadow: "none" }}>
        <div className="card-head">
          <h3><TeamBadge name={team} /> {team}</h3>
          <span className="form" aria-label={`Ultime partite: ${o.map((x) => LBL[x.res]).join(" ")}`}>
            {o.map((x, i) => <span key={i} className={x.res}>{LBL[x.res]}</span>)}
          </span>
        </div>
        <div className="card-pad">
          {!rows.length && <p className="muted">Nessuna partita di questa squadra nello storico raccolto.</p>}
          {rows.length > 0 && <div className="kv"><span>Gol fatti / subiti (media)</span><span className="num">{avg("gf")} / {avg("ga")}</span></div>}
          <table style={{ marginTop: 6 }}>
            <tbody>
              {rows.map((r) => (
                <tr key={r.fixture_id}>
                  <td className="muted num">{shortDate(r.kickoff)}</td>
                  <td>{r.home} - {r.away}<span className="sub">{compShort(r.competition)}</span></td>
                  <td className="num"><b>{r.home_goals}-{r.away_goals}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  };
  return (
    <div className="col">
      <div className="split" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        {block(home, fh)}
        {block(away, fa)}
      </div>
      <div>
        <h2 className="section">Precedenti diretti</h2>
        {h2h.length ? (
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table>
              <tbody>
                {h2h.map((r) => (
                  <tr key={r.fixture_id}>
                    <td className="muted num">{shortDate(r.kickoff)}</td>
                    <td>{r.home} - {r.away}<span className="sub">{compShort(r.competition)}</span></td>
                    <td className="num"><b>{r.home_goals}-{r.away_goals}</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">Nessun precedente nelle ultime due stagioni.</p>
        )}
      </div>
    </div>
  );
}
