"use client";
// My Combo (Fase 8): pick selections of one match (goals, halves, corners, cards, team shots, players), type the price Sisal
// shows for them together, read probability, fair price and EV; or let "Genera My Combo" pick the likeliest combination in a
// price range, then change it. Everything is computed here from the published model numbers (lib/combo.ts): no request per click.
import { useMemo, useState } from "react";
import { Calculator, ChevronDown, Percent, Scale, Sparkles, Tag, Target, X } from "lucide-react";
import {
  buildModel, comboSelections, evaluateCombo, generateCombo, GEN, PLAYER_MARKETS, type ComboParams, type ComboPlayer, type ComboSel,
} from "@/lib/combo";

type Props = {
  home: string;
  away: string;
  xgHome: number;
  xgAway: number;
  rho: number;
  params: ComboParams | null; // half-time split and count models (absent on runs before them: goals only)
  players: ComboPlayer[];
  finals: Record<string, number>; // final probability of the selections the analysis priced
  prices: Record<string, number>; // Sisal price of the single selections, when quoted
};

const pct = (x: number | null, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const signed = (x: number | null) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`);
const num = (s: string) => {
  const q = Number(s.replace(",", "."));
  return Number.isFinite(q) && q > 1 ? q : null;
};
const SECTIONS = ["Esito e goal", "1° tempo", "2° tempo", "Goal squadra", "Corner", "Cartellini", "Tiri totali", "Tiri in porta"];

export function MyCombo({ home, away, xgHome, xgAway, rho, params, players, finals, prices }: Props) {
  const teamName = (t: 1 | 2) => (t === 1 ? home : away);
  const model = useMemo(() => buildModel(xgHome, xgAway, rho, params, players), [xgHome, xgAway, rho, params, players]);
  const all = useMemo(() => comboSelections(model, teamName), [model]); // eslint-disable-line react-hooks/exhaustive-deps
  const byKey = useMemo(() => new Map(all.map((s) => [s.key, s])), [all]);
  const [picked, setPicked] = useState<string[]>([]);
  const [odds, setOdds] = useState("");
  const [qMin, setQMin] = useState("");
  const [qMax, setQMax] = useState("");
  const [busy, setBusy] = useState(false);
  const [genMsg, setGenMsg] = useState<string | null>(null);
  const [who, setWho] = useState<Record<number, string>>({});
  const cache = useMemo(() => new Map<string, number>(), [model]);
  const sels = picked.map((k) => byKey.get(k)).filter((s): s is ComboSel => !!s);
  const r = evaluateCombo(model, sels, finals, prices, num(odds), cache);
  const toggle = (key: string) => {
    setGenMsg(null);
    setPicked((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]));
  };
  const side = (s: string) => s.replace(/\bcasa\b/g, home).replace(/\bospite\b/g, away).replace(/^Casa$/, home).replace(/^Ospite$/, away);
  const label = (s: ComboSel) => (s.player ? s.label : `${side(s.group)}: ${side(s.label)}`);

  const generate = () => {
    const lo = num(qMin), hi = num(qMax);
    if (!lo || !hi || hi < lo) {
      setGenMsg("Scrivi la quota minima e la quota massima (la massima non sotto la minima).");
      return;
    }
    setBusy(true);
    setGenMsg(null);
    setTimeout(() => { // let the button show the work first
      const g = generateCombo(model, all, finals, prices, lo, hi);
      setBusy(false);
      if (!g) {
        setGenMsg("Nessuna combinazione in questa fascia di quota con le regole (probabilità di ogni esito almeno 40%, quota almeno 1.25).");
        return;
      }
      setPicked(g.sels.map((s) => s.key));
      setOdds("");
      setGenMsg(`La più probabile tra quelle con quota stimata tra ${lo.toFixed(2)} e ${hi.toFixed(2)}: puoi aggiungere o togliere esiti.`);
    }, 20);
  };

  const chip = (s: ComboSel, text: string) => {
    const on = picked.includes(s.key);
    return (
      <button key={s.key} type="button" className={`chip ${on ? "on" : ""}`} aria-pressed={on} onClick={() => toggle(s.key)}>
        {text}
        {prices[s.key] ? <span className="chip-odds num">{prices[s.key].toFixed(2)}</span> : null}
      </button>
    );
  };
  const groupsOf = (list: ComboSel[]) => [...new Set(list.map((s) => s.group))];
  const nIn = (list: ComboSel[]) => list.filter((s) => picked.includes(s.key)).length;

  return (
    <div className="combo">
      <div className="combo-groups">
        {SECTIONS.map((sec, i) => {
          const list = all.filter((s) => s.section === sec);
          if (!list.length) return null;
          return (
            <details key={sec} className="combo-section" open={i === 0 || undefined}>
              <summary>
                <span>{sec}</span>
                <span className="fold-side">{nIn(list) ? <span className="count">{nIn(list)}</span> : null}<ChevronDown size={18} className="fold-chev" aria-hidden="true" /></span>
              </summary>
              <div className="combo-section-body">
                {groupsOf(list).map((g) => (
                  <fieldset key={g} className="combo-group">
                    <legend>{side(g)}</legend>
                    <div className="combo-chips">{list.filter((s) => s.group === g).map((s) => chip(s, side(s.label)))}</div>
                  </fieldset>
                ))}
              </div>
            </details>
          );
        })}
        {([1, 2] as const).map((t) => {
          const team = players.filter((p) => p.team === t);
          if (!team.length) return null;
          const list = all.filter((s) => s.player && model.players.get(s.player.pid)?.team === t);
          const cur = team.find((p) => p.pid === who[t]) ?? team[0];
          return (
            <details key={t} className="combo-section">
              <summary>
                <span>Giocatori {teamName(t)}</span>
                <span className="fold-side">{nIn(list) ? <span className="count">{nIn(list)}</span> : null}<ChevronDown size={18} className="fold-chev" aria-hidden="true" /></span>
              </summary>
              <div className="combo-section-body">
                <div className="field">
                  <label htmlFor={`combo-player-${t}`}>Giocatore</label>
                  <div className="control">
                    <select id={`combo-player-${t}`} value={cur.pid} onChange={(e) => setWho((w) => ({ ...w, [t]: e.target.value }))}>
                      {team.map((p) => (
                        <option key={p.pid} value={p.pid}>{p.name}{p.start < 1 ? ` (titolare ${pct(p.start, 0)})` : ""}</option>
                      ))}
                    </select>
                  </div>
                </div>
                {PLAYER_MARKETS.map((m) => {
                  const ss = list.filter((s) => s.player!.pid === cur.pid && s.player!.kind === m.kind);
                  return (
                    <fieldset key={m.kind} className="combo-group">
                      <legend>{m.label}{m.duo ? " (DUO)" : ""}</legend>
                      <div className="combo-chips">
                        {ss.map((s) => chip(s, m.ks.length > 1 ? `${s.player!.k}+` : "Sì"))}
                      </div>
                    </fieldset>
                  );
                })}
                <p className="note">
                  DUO: vale anche se a farlo è chi entra al suo posto. Probabilità con il giocatore titolare
                  {cur.start < 1 ? ` (oggi titolare al ${pct(cur.start, 0)} secondo la nostra probabile)` : ""}.
                </p>
              </div>
            </details>
          );
        })}
      </div>

      <aside className="card combo-side" aria-live="polite">
        <div className="card-head"><h2>La tua My Combo</h2><span className="count">{sels.length} selezioni</span></div>
        <div className="card-pad combo-body">
          <div className="combo-gen">
            <span className="field-label" id="gen-label">Genera My Combo</span>
            <div className="control" aria-labelledby="gen-label">
              <Tag size={16} aria-hidden="true" />
              <input type="number" inputMode="decimal" min="1.01" step="0.05" placeholder="quota min" value={qMin} onChange={(e) => setQMin(e.target.value)} aria-label="Quota minima della combo" />
              <span className="dash">–</span>
              <input type="number" inputMode="decimal" min="1.01" step="0.05" placeholder="max" value={qMax} onChange={(e) => setQMax(e.target.value)} aria-label="Quota massima della combo" />
            </div>
            <button type="button" className="btn btn-primary" onClick={generate} disabled={busy}>
              <Sparkles size={16} aria-hidden="true" /> {busy ? "Cerco…" : "Genera My Combo"}
            </button>
            {genMsg && <p className="note">{genMsg}</p>}
          </div>
          {sels.length ? (
            <ul className="combo-picked">
              {sels.map((s) => (
                <li key={s.key}>
                  <span>{label(s)}{prices[s.key] ? <span className="muted num"> · {prices[s.key].toFixed(2)}</span> : null}</span>
                  <button type="button" className="icon-btn" aria-label={`Togli ${label(s)}`} onClick={() => toggle(s.key)}><X size={16} aria-hidden="true" /></button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="note">Scegli almeno due esiti della partita, o generane una: la probabilità tiene conto di come si legano tra loro.</p>
          )}
          {picked.length > 0 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setPicked([]); setGenMsg(null); }}>Svuota</button>}
          <div className="field">
            <label htmlFor="combo-odds">Quota My Combo su Sisal</label>
            <div className="control">
              <Calculator size={16} aria-hidden="true" />
              <input id="combo-odds" type="number" inputMode="decimal" min="1.01" step="0.01" placeholder="es. 3.40" value={odds}
                onChange={(e) => setOdds(e.target.value)} />
            </div>
          </div>
          {r.impossible ? (
            <p className="field-error">Questi esiti non possono vincere insieme.</p>
          ) : sels.length ? (
            <div className="combo-kpis">
              <div><span><Percent size={14} aria-hidden="true" /> Probabilità</span><b className="num">{pct(r.p)}</b>
                <small>solo modello {pct(r.pModel)}{r.rescaled ? ` · ${r.rescaled} con la probabilità finale dell'analisi` : ""}</small></div>
              <div><span><Scale size={14} aria-hidden="true" /> Quota equa</span><b className="num">{r.fair ? r.fair.toFixed(2) : "–"}</b>
                <small>sotto questa quota la combo non ha valore</small></div>
              <div><span><Tag size={14} aria-hidden="true" /> Quota stimata Sisal</span><b className="num">{r.estOdds ? r.estOdds.toFixed(2) : "–"}</b>
                <small>quote singole Sisal (o stimate) corrette per il legame tra gli esiti: la quota vera la vedi su Sisal</small></div>
              <div><span><Target size={14} aria-hidden="true" /> EV alla tua quota</span>
                <b className={`num ${r.ev == null ? "" : r.ev >= 0 ? "pos" : "neg"}`}>{signed(r.ev)}</b>
                <small>scrivi la quota che vedi su Sisal</small></div>
            </div>
          ) : null}
          <p className="note">
            Gli esiti dello stesso tipo (goal e tempi, corner, cartellini, tiri) sono legati tra loro come nel modello. Tiri e corner seguono
            i goal: i tiri in porta includono i goal, e chi è in vantaggio tira e batte corner un po&apos; meno (misurato su 2.345 partite).
            Corner e tiri della stessa squadra sono legati anche oltre i goal (la giornata d&apos;attacco di una squadra); i cartellini sono
            indipendenti. Marcatore e assist seguono i goal della squadra. Tiri di squadra solo per i 7 campionati.
            Genera: esiti con probabilità almeno {pct(GEN.minProb, 0)} e quota almeno {GEN.minOdds.toFixed(2)}, solo giocatori con
            probabilità di essere titolari di almeno {pct(GEN.minStart, 0)}. Uso personale, solo paper trading.
          </p>
        </div>
      </aside>
    </div>
  );
}
