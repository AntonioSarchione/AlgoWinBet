"use client";
// My Combo (Fase 8): pick selections of one match, type the price Sisal shows for them together, read probability, fair
// price and EV. Everything is computed here from the published expected goals (lib/combo.ts): no request per click.
import { useMemo, useState } from "react";
import { Calculator, Percent, Scale, Target, X } from "lucide-react";
import { COMBO_GROUPS, COMBO_SELECTIONS, evaluateCombo, scoreMatrix } from "@/lib/combo";

type Props = {
  home: string;
  away: string;
  xgHome: number;
  xgAway: number;
  rho: number;
  finals: Record<string, number>; // final probability of the selections the analysis priced
  prices: Record<string, number>; // Sisal price of the single selections, when quoted
};

const pct = (x: number | null, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const signed = (x: number | null) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`);

export function MyCombo({ home, away, xgHome, xgAway, rho, finals, prices }: Props) {
  const m = useMemo(() => scoreMatrix(xgHome, xgAway, rho), [xgHome, xgAway, rho]);
  const [picked, setPicked] = useState<string[]>([]);
  const [odds, setOdds] = useState("");
  const sels = COMBO_SELECTIONS.filter((s) => picked.includes(s.key));
  const q = Number(odds.replace(",", "."));
  const r = evaluateCombo(m, sels, finals, q > 1 ? q : null);
  const product = sels.length && sels.every((s) => prices[s.key]) ? sels.reduce((a, s) => a * prices[s.key], 1) : null;
  const toggle = (key: string) => setPicked((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]));
  const label = (key: string) => {
    const s = COMBO_SELECTIONS.find((x) => x.key === key)!;
    return `${s.group}: ${s.label}`;
  };

  return (
    <div className="combo">
      <div className="combo-groups">
        {COMBO_GROUPS.map((g) => (
          <fieldset key={g} className="combo-group">
            <legend>{g.replace("casa", home).replace("ospite", away)}</legend>
            <div className="combo-chips">
              {COMBO_SELECTIONS.filter((s) => s.group === g).map((s) => {
                const on = picked.includes(s.key);
                return (
                  <button key={s.key} type="button" className={`chip ${on ? "on" : ""}`} aria-pressed={on} onClick={() => toggle(s.key)}>
                    {s.label}
                    {prices[s.key] ? <span className="chip-odds num">{prices[s.key].toFixed(2)}</span> : null}
                  </button>
                );
              })}
            </div>
          </fieldset>
        ))}
      </div>

      <aside className="card combo-side" aria-live="polite">
        <div className="card-head"><h2>La tua My Combo</h2><span className="count">{sels.length} selezioni</span></div>
        <div className="card-pad combo-body">
          {sels.length ? (
            <ul className="combo-picked">
              {picked.map((k) => (
                <li key={k}>
                  <span>{label(k)}</span>
                  <button type="button" className="icon-btn" aria-label={`Togli ${label(k)}`} onClick={() => toggle(k)}><X size={16} aria-hidden="true" /></button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="note">Scegli almeno due selezioni della partita: la probabilità tiene conto di come si legano tra loro.</p>
          )}
          <div className="field">
            <label htmlFor="combo-odds">Quota My Combo su Sisal</label>
            <div className="control">
              <Calculator size={16} aria-hidden="true" />
              <input id="combo-odds" type="number" inputMode="decimal" min="1.01" step="0.01" placeholder="es. 3.40" value={odds}
                onChange={(e) => setOdds(e.target.value)} />
            </div>
          </div>
          {r.impossible ? (
            <p className="field-error">Queste selezioni non possono vincere insieme: nessun risultato le soddisfa tutte.</p>
          ) : sels.length ? (
            <div className="combo-kpis">
              <div><span><Percent size={14} aria-hidden="true" /> Probabilità</span><b className="num">{pct(r.p)}</b>
                <small>solo modello {pct(r.pModel)}{r.rescaled ? ` · ${r.rescaled} con la probabilità finale dell'analisi` : ""}</small></div>
              <div><span><Scale size={14} aria-hidden="true" /> Quota equa</span><b className="num">{r.fair ? r.fair.toFixed(2) : "–"}</b>
                <small>sotto questa quota la combo non ha valore</small></div>
              <div><span><Target size={14} aria-hidden="true" /> EV alla tua quota</span>
                <b className={`num ${r.ev == null ? "" : r.ev >= 0 ? "pos" : "neg"}`}>{signed(r.ev)}</b>
                <small>{product ? `singole moltiplicate: ${product.toFixed(2)}` : "scrivi la quota che vedi su Sisal"}</small></div>
            </div>
          ) : null}
          <p className="note">
            Solo selezioni sui goal (esito, doppia chance, Goal/NoGoal, under/over, goal squadra, multigoal, vince a zero, pari/dispari). Corner,
            cartellini e marcatori non entrano nel calcolo. Uso personale, solo paper trading.
          </p>
        </div>
      </aside>
    </div>
  );
}
