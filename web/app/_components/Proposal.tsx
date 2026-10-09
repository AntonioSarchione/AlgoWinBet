// "Proposta" tab of the match page: a short reading of the match and three single selections, one per profile (most likely,
// balanced, most value), chosen among the playable Sisal selections (pub_book) with the same rules the slips follow
// (status, leg probability, leg odds, national-team value floor: web/lib/optimizer.ts). Nothing is loosened: with no
// selection passing the rules the tab says so.
import { Gem, Scale, ShieldCheck, Sparkles, type LucideIcon } from "lucide-react";
import { parseJSON, type BookSel, type ResultRow } from "@/lib/db";
import { legReason, MIN_LEG_ODDS, NATIONAL_COMPETITIONS, type OptSettings } from "@/lib/optimizer";
import { isEstimated } from "@/lib/books";
import { fairOdds, pct, signed, STATUS_LABEL } from "./format";
import { Empty, PBar, probColor } from "./ui";
import type { TrendsData } from "./Trends";

type Fx = {
  home: string; away: string; competition: string; p_home: number | null; p_draw: number | null; p_away: number | null;
  p_over25: number | null; p_btts: number | null; xg_home: number | null; xg_away: number | null; lineup_state: string;
};
type Pick = { kind: "prob" | "bal" | "value"; sel: BookSel };

const KIND: Record<Pick["kind"], { l: string; icon: LucideIcon; why: string }> = {
  prob: { l: "Massima probabilità", icon: ShieldCheck, why: "la selezione più probabile tra quelle che rispettano le regole" },
  bal: { l: "Equilibrata", icon: Scale, why: "il miglior compromesso tra probabilità e valore" },
  value: { l: "Massimo valore", icon: Gem, why: "il valore atteso più alto, anche se meno probabile" },
};

function eligible(sels: BookSel[], competition: string, s: OptSettings | null): BookSel[] {
  const o = s?.optimizer;
  const ok = new Set(o?.statuses ?? ["STRONG", "CANDIDATE", ...(o?.include_watch ? ["WATCH"] : []), ...(o?.include_fair ? ["FAIR"] : [])]);
  const minP = o?.min_leg_probability ?? 0.4;
  const minOdds = Math.max(o?.min_leg_odds ?? MIN_LEG_ODDS, MIN_LEG_ODDS);
  const national = (o?.national_competitions ?? NATIONAL_COMPETITIONS).some((k) => competition.toLowerCase().includes(k.toLowerCase()));
  const natMin = o?.national_value_min_probability ?? 0;
  return sels.filter((x) => ok.has(x.st) && x.p >= minP && x.o >= minOdds && !(national && (x.st === "STRONG" || x.st === "CANDIDATE") && x.p < natMin));
}

// three different selections: the most likely, the highest EV, and between them the best sum of the two ranks
function choose(c: BookSel[]): Pick[] {
  if (!c.length) return [];
  const byP = [...c].sort((a, b) => b.p - a.p || b.e - a.e);
  const byE = [...c].sort((a, b) => b.e - a.e || b.p - a.p);
  const prob = byP[0];
  const value = byE.find((x) => x.k !== prob.k);
  const rank = (arr: BookSel[], x: BookSel) => arr.findIndex((y) => y.k === x.k);
  const bal = [...c].filter((x) => x.k !== prob.k && x.k !== value?.k).sort((a, b) => rank(byP, a) + rank(byE, a) - (rank(byP, b) + rank(byE, b)))[0];
  return [{ kind: "prob", sel: prob }, ...(bal ? [{ kind: "bal" as const, sel: bal }] : []), ...(value ? [{ kind: "value" as const, sel: value }] : [])];
}

function form(rows: ResultRow[], team: string) {
  const o = rows.slice(0, 5).map((r) => {
    const gf = r.home === team ? r.home_goals : r.away_goals;
    const ga = r.home === team ? r.away_goals : r.home_goals;
    return gf > ga ? "V" : gf === ga ? "N" : "P";
  });
  return { s: o.join(""), w: o.filter((x) => x === "V").length, l: o.filter((x) => x === "P").length };
}

function story(fx: Fx, fh: ResultRow[], fa: ResultRow[], h2h: ResultRow[], trends: TrendsData | null): string[] {
  const out: string[] = [];
  if (fx.p_home != null && fx.p_draw != null && fx.p_away != null) {
    const fav = fx.p_home >= fx.p_away ? { t: fx.home, p: fx.p_home } : { t: fx.away, p: fx.p_away };
    const gap = Math.abs(fx.p_home - fx.p_away);
    out.push(gap < 0.08
      ? `Partita equilibrata per il modello: ${fx.home} ${pct(fx.p_home)}, pareggio ${pct(fx.p_draw)}, ${fx.away} ${pct(fx.p_away)}.`
      : `${fav.t} favorito: ${pct(fav.p)} di vittoria, pareggio ${pct(fx.p_draw)}.`);
  }
  if (fx.xg_home != null && fx.xg_away != null) {
    const tot = fx.xg_home + fx.xg_away;
    out.push(`Goal attesi ${fx.xg_home.toFixed(2)} – ${fx.xg_away.toFixed(2)}: ${tot >= 3 ? "partita da goal" : tot <= 2.2 ? "partita chiusa, pochi goal attesi" : "goal nella media"}`
      + (fx.p_over25 != null ? ` (Over 2.5 ${pct(fx.p_over25)}` + (fx.p_btts != null ? `, Goal ${pct(fx.p_btts)})` : ")") : "") + ".");
  }
  const h = form(fh, fx.home);
  const a = form(fa, fx.away);
  if (h.s || a.s) out.push(`Forma, ultime 5: ${fx.home} ${h.s || "–"}, ${fx.away} ${a.s || "–"}.`);
  if (h2h.length) {
    const hw = h2h.filter((r) => (r.home === fx.home ? r.home_goals > r.away_goals : r.away_goals > r.home_goals)).length;
    const dr = h2h.filter((r) => r.home_goals === r.away_goals).length;
    out.push(`Precedenti: ${hw} vittorie ${fx.home}, ${dr} pareggi, ${h2h.length - hw - dr} vittorie ${fx.away} negli ultimi ${h2h.length}.`);
  }
  const all = trends ? [...trends.home, ...trends.away, ...trends.h2h] : [];
  const rare = all.sort((x, y) => y.r - x.r)[0];
  if (rare && rare.r >= 15) out.push(`Serie più rara: ${rare.t} (1 su ${rare.r.toLocaleString("it-IT")}).`);
  out.push(fx.lineup_state === "confirmed" ? "Formazioni ufficiali già incluse nel calcolo." : "Formazioni non ancora ufficiali: le probabilità useranno quelle vere quando escono.");
  return out;
}

export function Proposal({ fx, sels, settings, fh, fa, h2h, trends, factors }: {
  fx: Fx; sels: BookSel[]; settings: OptSettings | null; fh: ResultRow[]; fa: ResultRow[]; h2h: ResultRow[]; trends: TrendsData | null;
  factors: Record<string, { positive_factors?: string[]; negative_factors?: string[] }>;
}) {
  const picks = choose(eligible(sels, fx.competition, settings));
  return (
    <div className="col">
      <section className="pr-story">
        <span className="tr-ico"><Sparkles size={16} aria-hidden="true" /></span>
        <div>
          <h2 className="section" style={{ margin: 0 }}>La partita</h2>
          <ul>{story(fx, fh, fa, h2h, trends).map((t) => <li key={t}>{t}</li>)}</ul>
        </div>
      </section>
      {picks.length ? (
        <div className="pr-grid">
          {picks.map(({ kind, sel: x }) => {
            const K = KIND[kind];
            const why = legReason({ odds: x.o, p_final: x.p, p_market: x.pm, status: x.st, dq_lineup: x.l });
            const f = factors[x.m]; // the published opportunity of the same selection (by its description)
            return (
              <article key={kind} className={`pr-card pr-${kind}`}>
                <header>
                  <span className="pr-kind"><K.icon size={15} aria-hidden="true" />{K.l}</span>
                  <span className={`status status-${x.st}`}>{STATUS_LABEL[x.st] ?? x.st}</span>
                </header>
                <h3>{x.m}</h3>
                <div className="pr-odds">
                  <span><small>{isEstimated(x.b) ? "Sisal stimata" : "Quota Sisal"}</small><b className="num">{x.o.toFixed(2)}</b></span>
                  <span><small>Quota equa</small><b className="num">{fairOdds(x.p)}</b></span>
                  <span className={`ev-chip ${x.e >= 0 ? "pos" : "neg"}`}>EV {signed(x.e)}</span>
                </div>
                <div className="pr-probs">
                  <div><small>Probabilità finale</small><b className="num" style={{ color: probColor(x.p) }}>{pct(x.p, 1)}</b></div>
                  <div><small>Modello</small><b className="num">{pct(x.s, 1)}</b></div>
                  <div><small>Mercato</small><b className="num">{x.pm != null ? pct(x.pm, 1) : "–"}</b></div>
                </div>
                <PBar p={x.p} mark={x.pm} />
                <p className="pr-why"><b>Perché:</b> {K.why}. {why.text}</p>
                {f && ((f.positive_factors?.length ?? 0) + (f.negative_factors?.length ?? 0)) > 0 && (
                  <ul className="pr-factors">
                    {(f.positive_factors ?? []).slice(0, 3).map((t) => <li key={t} className="pos">{t}</li>)}
                    {(f.negative_factors ?? []).slice(0, 2).map((t) => <li key={t} className="neg">{t}</li>)}
                  </ul>
                )}
              </article>
            );
          })}
        </div>
      ) : (
        <Empty icon={Scale} title="Nessuna proposta">
          Nessuna selezione Sisal di questa partita rispetta le regole delle schedine (valore o quota equa, probabilità di almeno il
          40%, quota di almeno 1.25). Le regole non vengono allentate.
        </Empty>
      )}
      <p className="note">
        Probabilità finale: il modello corretto verso il mercato, quella usata dalle schedine. Modello: il nostro modello da solo. Mercato:
        le quote Pinnacle senza margine. Quota equa = 1 / probabilità finale: se la quota Sisal è più alta c&apos;è valore (EV positivo).
        Selezioni scelte con le stesse regole delle schedine{picks.length < 3 && picks.length > 0 ? "; meno di tre quando non ce ne sono abbastanza" : ""}. Solo paper trading.
      </p>
    </div>
  );
}

export function parseBook(rows: { fixture_id: string; sels: string }[], id: string): BookSel[] {
  return parseJSON<BookSel[]>(rows.find((r) => r.fixture_id === id)?.sels, []);
}
