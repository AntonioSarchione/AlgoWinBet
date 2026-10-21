"""Fase 7 check: the registry on the 7 domestic leagues. Calibration of every recorded selection, value bets at the Pinnacle
close (match-grouped 95%), Under vs Over, by market, slips.

    python scripts/league_eval.py data/algowinbet.db      (a decrypted secure-copy of the archive)

First run 2026-10-21 (114 matches, 9-19/10): 3.33 goals a match against 2.88 expected (history 2.85), Unders 72% predicted
63% won with the model, the market at entry and the Pinnacle close all agreeing; value bets +5.1% +-4.5% at the Pinnacle close.
Decision: no Under cap, check again after 3-4 more league rounds."""
import math
import sqlite3
import sys
from collections import defaultdict

DOM = ["Serie A", "Premier League", "La Liga", "Bundesliga", "Ligue 1", "Primeira Liga", "Eredivisie"]
c = sqlite3.connect(sys.argv[1])
q = ",".join("?" * len(DOM))
legs = c.execute(f"""select fixture_id, sel_key, competition, status, odds, p, p_market, close_fair, close_odds, result, kickoff, created_at
  from paper_legs where competition in ({q}) and result in ('won','lost','void')""", DOM).fetchall()
print(f"campionati: {len(legs)} selezioni chiuse, {len({l[0] for l in legs})} partite, dal {min(l[10] for l in legs)[:10]} al {max(l[10] for l in legs)[:10]}")


def ci(xs, gs):
    if not xs:
        return None, None, 0
    m = sum(xs) / len(xs)
    r = defaultdict(float)
    for x, g in zip(xs, gs):
        r[g] += x - m
    k = len(r)
    h = 1.96 * math.sqrt(k / (k - 1) * sum(v * v for v in r.values())) / len(xs) if k > 1 else float("nan")
    return m, h, k


def pc(m, h):
    return "–" if m is None else f"{m * 100:+.1f}±{h * 100:.1f}%"


def kind(key):
    code, sel = key.split("|")[:2]
    if sel == "UNDER":
        return "Under"
    if sel == "OVER":
        return "Over"
    if code.startswith("MATCH_1X2") or code in ("DOUBLE_CHANCE", "DRAW_NO_BET", "EURO_HANDICAP", "ASIAN_HANDICAP"):
        return "esito/handicap"
    if code.startswith("BTTS"):
        return "Goal/NoGoal"
    return "altro"


def block(title, rows):
    dec = [r for r in rows if r[9] in ("won", "lost")]
    n = len(dec)
    if not n:
        return
    pexp = sum(r[5] for r in dec) / n
    hit = sum(r[9] == "won" for r in dec) / n
    ll = -sum(math.log(r[5] if r[9] == "won" else 1 - r[5]) for r in dec) / n
    mk = [r for r in dec if r[6]]
    llm = -sum(math.log(r[6] if r[9] == "won" else 1 - r[6]) for r in mk) / len(mk) if mk else float("nan")
    llc = [r for r in dec if r[7]]
    llcl = -sum(math.log(r[7] if r[9] == "won" else 1 - r[7]) for r in llc) / len(llc) if llc else float("nan")
    ev = ci([(r[4] * r[7] - 1) if r[7] else 0 for r in rows], [r[0] for r in rows])
    cl = [r for r in rows if r[8]]
    clv = ci([r[4] / r[8] - 1 for r in cl], [r[0] for r in cl])
    roi = sum((r[4] - 1) if r[9] == "won" else (-1 if r[9] == "lost" else 0) for r in rows) / len(rows)
    cov = sum(r[7] is not None for r in rows) / len(rows)
    print(f"  {title:22} n {len(rows):4d} part {ev[2]:3d} | prevista {pexp:.3f} vera {hit:.3f} | LL modello {ll:.4f} mercato-ingresso {llm:.4f}"
          f" (chiusura {llcl:.4f}, n {len(llc)}) | Pinnacle {pc(ev[0], ev[1])} CLV {pc(clv[0], clv[1])} ROI {roi * 100:+.1f}% copertura {cov:.0%}")


val = [l for l in legs if l[3] in ("STRONG", "CANDIDATE")]
fair = [l for l in legs if l[3] == "FAIR"]
print("\nTUTTE / VALORE / EQUE")
block("tutte", legs)
block("valore", val)
block("eque", fair)
print("\nPER TIPO (tutte le selezioni)")
for k in ("Under", "Over", "esito/handicap", "Goal/NoGoal", "altro"):
    block(k, [l for l in legs if kind(l[1]) == k])
print("\nPER TIPO (solo valore)")
for k in ("Under", "Over", "esito/handicap", "Goal/NoGoal", "altro"):
    block(k, [l for l in val if kind(l[1]) == k])
print("\nPER CAMPIONATO (tutte)")
for comp in DOM:
    block(comp, [l for l in legs if l[2] == comp])
print("\nCALIBRAZIONE PER FASCIA (tutte le decise)")
dec = [l for l in legs if l[9] in ("won", "lost")]
for lo in [x / 10 for x in range(2, 10)]:
    b = [l for l in dec if lo <= l[5] < lo + 0.1]
    if len(b) >= 15:
        p = sum(l[5] for l in b) / len(b)
        y = sum(l[9] == "won" for l in b) / len(b)
        se = math.sqrt(p * (1 - p) / len(b))
        print(f"  {lo:.1f}-{lo + 0.1:.1f}: n {len(b):4d} prevista {p:.3f} vera {y:.3f} ({(y - p) / se:+.1f} errori std, non raggruppati)")

# slips of the leagues only
slips = c.execute("select legs, total_odds, joint, result, payout from paper_slips where result in ('won','lost')").fetchall()
import json
fxcomp = dict(c.execute("select fixture_id, competition from fixtures"))
lg = []
for legs_json, odds, joint, res, pay in slips:
    comps = {fxcomp.get(x["fixture_id"]) for x in json.loads(legs_json)}
    if comps and comps <= set(DOM):
        lg.append((odds, joint, res, pay))
if lg:
    w = sum(x[2] == "won" for x in lg)
    print(f"\nSCHEDINE solo campionati: {len(lg)} chiuse, vinte {w}, attese {sum(x[1] or 0 for x in lg):.1f}, saldo {sum((x[3] or 0) - 1 for x in lg):+.1f} u")
under_share = sum(kind(l[1]) == "Under" for l in val) / max(1, len(val))
print(f"quota Under tra le giocate di valore dei campionati: {under_share:.0%}")
