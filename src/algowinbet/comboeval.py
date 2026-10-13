"""My Combo (web/lib/combo.ts) takes goals, corners, cards and team shots as independent of each other: the joint probability of
selections from different blocks is the product of their probabilities. This replay checks that on past matches of the 7
domestic leagues, no API request: every Monday the goal model (Engine.fit) and the count models (Engine.count_model, the
published settings) are fitted on what was known then and price that week's matches.

For each pair of events from two different blocks:
  joint ........ how often both happened / sum of pA x pB (what My Combo would have said): 1 = right on average
  netto ........ the same divided by the two events' own fatti/attesi, so a miscalibrated single event does not count
  corr ......... correlation of the two events' residuals (happened - probability): what the models leave unexplained
                 together; about +-2/sqrt(n) is chance
and, for goals with shots on target or total shots, the same against a structural joint ("strutt."): a team's shots on
target are its goals plus a negative binomial rest (mean: expected shots on target - expected goals, the count model's
size), its total shots those plus a negative binomial of shots off target; the side ahead shoots and wins corners a little
less (gamma). sigma > 0: a side's shots off target and corners share an attacking factor (correlated rho between the sides),
so corners and shots are linked beyond the goals.

Result (2,345 matches since 2025-08-01, gamma 0.03 / 0.06, sigma 0.3, rho -1/3; netto, 1 = right):
  Over 2.5 + tiri in porta Over 8.5 ...... independent 1.275, structural 0.968
  Under 1.5 + tiri in porta Over 8.5 ..... 0.388 -> 0.834
  Corner Over 9.5 + tiri Over 24.5 ....... 1.145 -> 0.974
  Corner casa di più + tiri casa di più .. 1.191 -> 1.075
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

DOMESTIC = ["Serie A", "Premier League", "La Liga", "Bundesliga", "Ligue 1", "Primeira Liga", "Eredivisie"]
STATS = ("corners", "cards", "shots", "shots_on")


def _grid(n: int) -> tuple[np.ndarray, np.ndarray]:
    i, j = np.indices((n, n))
    return i, j


# (label, block, probability from the block's grid, outcome from the counts)
EVENTS = [
    ("Over 2.5 goal", "goal", lambda i, j: i + j > 2.5),
    ("Under 1.5 goal", "goal", lambda i, j: i + j < 1.5),
    ("Goal (entrambe)", "goal", lambda i, j: (i > 0) & (j > 0)),
    ("Vince casa", "goal", lambda i, j: i > j),
    ("Vince ospite", "goal", lambda i, j: i < j),
    ("Corner Over 9.5", "corners", lambda i, j: i + j > 9.5),
    ("Corner casa di più", "corners", lambda i, j: i > j),
    ("Cartellini Over 4.5", "cards", lambda i, j: i + j > 4.5),
    ("Cartellini ospite di più", "cards", lambda i, j: i < j),
    ("Tiri Over 24.5", "shots", lambda i, j: i + j > 24.5),
    ("Tiri casa di più", "shots", lambda i, j: i > j),
    ("Tiri in porta Over 8.5", "shots_on", lambda i, j: i + j > 8.5),
]
# shots on target and total shots are already joint in My Combo (thinning): not a pair to check
JOINT_ALREADY = {frozenset(("shots", "shots_on"))}


@dataclass
class Pair:
    n: int = 0
    p_ab: float = 0.0
    y_ab: int = 0
    p_a: float = 0.0
    y_a: int = 0
    p_b: float = 0.0
    y_b: int = 0
    cov: float = 0.0
    var_a: float = 0.0
    var_b: float = 0.0
    ns: int = 0  # structural joint: matches, sum of P(A and B), both happened, sum of P(B) (same matches)
    ps_ab: float = 0.0
    ys_ab: int = 0
    ps_b: float = 0.0
    ys_b: int = 0
    ps_a: float = 0.0
    ys_a: int = 0

    def add_struct(self, p_ab: float, pa: float, pb: float, ya: int, yb: int) -> None:
        self.ns += 1
        self.ps_ab += p_ab
        self.ys_ab += ya * yb
        self.ps_a += pa
        self.ys_a += ya
        self.ps_b += pb
        self.ys_b += yb

    def add(self, pa: float, ya: int, pb: float, yb: int) -> None:
        self.n += 1
        self.p_ab += pa * pb
        self.y_ab += ya * yb
        self.p_a += pa
        self.y_a += ya
        self.p_b += pb
        self.y_b += yb
        self.cov += (ya - pa) * (yb - pb)
        self.var_a += (ya - pa) ** 2
        self.var_b += (yb - pb) ** 2

    @property
    def joint(self) -> float | None:
        return self.y_ab / self.p_ab if self.p_ab > 0 else None

    @property
    def net(self) -> float | None:
        if not (self.p_ab and self.y_a and self.y_b):
            return None
        return (self.y_ab / self.p_ab) / ((self.y_a / self.p_a) * (self.y_b / self.p_b))

    @property
    def joint_struct(self) -> float | None:
        return self.ys_ab / self.ps_ab if self.ps_ab > 0 else None

    @property
    def net_struct(self) -> float | None:
        if not (self.ps_ab and self.ys_a and self.ys_b):
            return None
        return (self.ys_ab / self.ps_ab) / ((self.ys_a / self.ps_a) * (self.ys_b / self.ps_b))

    @property
    def corr(self) -> float | None:
        d = math.sqrt(self.var_a * self.var_b)
        return self.cov / d if d > 0 else None


@dataclass
class ComboReport:
    start: datetime
    end: datetime
    matches: int = 0
    pairs: dict[tuple[str, str], Pair] = field(default_factory=lambda: defaultdict(Pair))
    single: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))  # n, sum p, hits
    marginal: dict[str, list[tuple[float, float, int]]] = field(default_factory=lambda: defaultdict(list))  # (model, structural, outcome)


def _nb(mean: float, size: float, n: int) -> np.ndarray:
    from scipy.stats import nbinom, poisson
    g = np.arange(n)
    p = poisson.pmf(g, mean) if math.isinf(size) else nbinom.pmf(g, size, size / (size + mean))
    return p / p.sum()


LEAD = 2  # goal difference beyond which the game state counts the same


def _lead(gh: int, ga: int) -> int:
    return max(-LEAD, min(LEAD, gh - ga))


def _conditional(goal_n: int, dh: dict[int, np.ndarray], da: dict[int, np.ndarray], fn, shift: bool = True) -> np.ndarray:
    """Q[gh, ga] = P(fn(gh + U, ga + V)) (shift: the goals are part of the count) or P(fn(U, V)): U ~ dh[home lead],
    V ~ da[away lead], the statistic given the goals."""
    nr = len(dh[0])
    i, j = _grid(goal_n + nr)
    ind = fn(i, j).astype(float)
    q = np.empty((goal_n, goal_n))
    for gh in range(goal_n):
        for ga in range(goal_n):
            d = _lead(gh, ga)
            x, y = (gh, ga) if shift else (0, 0)
            q[gh, ga] = dh[d] @ ind[x:x + nr, y:y + nr] @ da[-d]
    return q


def _rest(gm: np.ndarray, mean: float, size: float, n: int, gamma: float, home: bool) -> dict[int, np.ndarray]:
    """The team's count beyond its goals for each lead: mean x exp(-gamma lead) (a side ahead shoots less), scaled so that its
    average over the goal grid stays `mean`."""
    ng = gm.shape[0]
    f = {d: math.exp(-gamma * d) for d in range(-LEAD, LEAD + 1)}
    avg = sum(gm[gh, ga] * f[_lead(gh, ga) if home else -_lead(gh, ga)] for gh in range(ng) for ga in range(ng))
    return {d: _nb(mean * f[d] / avg, size, n) for d in f}


ATTACK_X = [-math.sqrt(3), 0.0, math.sqrt(3)]  # 3 Gauss-Hermite nodes of a standard normal


def _frailty(sigma: float) -> tuple[list[tuple[float, float]], float]:
    """A side's attacking intensity beyond its goals, shared by its shots off target and its corners: z = exp(sigma x), x a
    standard normal on 3 Gauss-Hermite nodes, scaled to mean 1. Returns [(weight, z)] and Var(z)."""
    x, w = ATTACK_X, [1 / 6, 2 / 3, 1 / 6]
    e = [math.exp(sigma * v) for v in x]
    m = sum(a * b for a, b in zip(w, e))
    z = [v / m for v in e]
    return list(zip(w, z)), sum(a * b * b for a, b in zip(w, z)) - 1


def _pair_weight(wi: float, wj: float, i: int, j: int, rho: float) -> float:
    """Joint weight of the home node i and the away node j: correlation rho between the two sides' factors (|rho| <= 1/3 on
    these nodes), each side's own weights unchanged. Negative: one side's attacking day is the other's quiet one."""
    return wi * wj * (1 + rho * ATTACK_X[i] * ATTACK_X[j])


def _mixed_size(size: float, s2: float) -> float:
    """Negative binomial size that, mixed over a mean-1 factor of variance s2, keeps the count's variance (as far as it can:
    near Poisson when the factor alone already gives more)."""
    if math.isinf(size) or s2 <= 0:
        return size
    return (1 + s2) / max(1 / size - s2, 1e-3)


def evaluate_combo(provider, cfg, start: datetime, end: datetime, competitions: list[str] | None = None,
                   gamma: float = 0.03, gamma_corners: float | None = 0.06, sigma: float = 0.3, rho: float = -1 / 3) -> ComboReport:
    from .engine import Engine
    from .modeleval import _Frozen
    comps = set(competitions or DOMESTIC)
    prov = _Frozen(provider, end)
    eng = Engine(prov, cfg, use_lineups=False)
    counts = {s: provider.stat_counts(s) for s in STATS}
    rep = ComboReport(start, end)
    monday = (start - timedelta(days=start.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    while monday < end:
        nxt = monday + timedelta(days=7)
        tests = [r for r in prov.rows if monday <= r.kickoff < nxt and r.kickoff < end and r.competition in comps]
        for r in tests:
            fitted = eng.fit(r.competition, monday)
            if fitted is None or not (fitted[0].knows(r.home) and fitted[0].knows(r.away)):
                continue
            grids = {"goal": (np.asarray(fitted[0].score_matrix(r.home, r.away)), (r.home_goals, r.away_goals))}
            means: dict[str, tuple[float, float, float]] = {}
            for st in STATS:
                cm = eng.count_model(st, monday)
                c = counts[st].get(r.fixture_id)
                if cm is None or c is None or not (cm.knows(r.home) and cm.knows(r.away)):
                    continue
                if cm.sample_size(r.home, r.away) < cfg.model.stat_min_team_matches:
                    continue
                grids[st] = (np.asarray(cm.matrix(r.home, r.away, r.competition)), (int(c[0]), int(c[1])))
                lh, la = cm.expected(r.home, r.away, r.competition)
                means[st] = (float(lh), float(la), cm.size)
            if len(grids) < 2:
                continue
            rep.matches += 1
            ev = {}
            for label, block, fn in EVENTS:
                if block not in grids:
                    continue
                m, (h, a) = grids[block]
                i, j = _grid(m.shape[0])
                p = float(m[fn(i, j)].sum() / m.sum())
                y = int(bool(fn(np.array(h), np.array(a))))
                ev[label] = (block, p, y)
                s = rep.single[label]
                s[0] += 1
                s[1] += p
                s[2] += y
            struct: dict[tuple[str, str], tuple[float, float, float]] = {}
            gm = grids["goal"][0] / grids["goal"][0].sum()
            ng = gm.shape[0]
            dists: dict[str, tuple[dict, dict, bool]] = {}  # block -> (home given lead, away given lead, goals included)
            # shots off target and corners of a side share its frailty z (sigma > 0): per node, then mixed over the nodes
            frail = sigma > 0 and "shots" in means and "shots_on" in means and "corners" in means and gamma_corners is not None
            nodes, s2 = _frailty(sigma) if frail else ([(1.0, 1.0)], 0.0)
            mix = lambda per: {d: sum(w * pz[d] for (w, _), pz in zip(nodes, per)) for d in per[0]}  # noqa: E731
            per_z: dict[str, tuple[list, list, bool]] = {}  # block -> (home per node, away per node, goals included)
            if "shots_on" in means:
                xg_h, xg_a = float((gm.sum(axis=1) * np.arange(ng)).sum()), float((gm.sum(axis=0) * np.arange(ng)).sum())
                sh, sa, ssz = means["shots_on"]
                rest_h = _rest(gm, max(0.3, sh - xg_h), ssz, 30, gamma, True)
                rest_a = _rest(gm, max(0.3, sa - xg_a), ssz, 30, gamma, False)
                dists["shots_on"] = (rest_h, rest_a, True)
                if "shots" in means:
                    th, ta, tsz = means["shots"]
                    tsz = _mixed_size(tsz, s2)
                    off_h = [_rest(gm, max(0.5, th - sh) * z, tsz, 50, gamma, True) for _, z in nodes]
                    off_a = [_rest(gm, max(0.5, ta - sa) * z, tsz, 50, gamma, False) for _, z in nodes]
                    tot_h = [{d: np.convolve(rest_h[d], o[d]) for d in rest_h} for o in off_h]
                    tot_a = [{d: np.convolve(rest_a[d], o[d]) for d in rest_a} for o in off_a]
                    per_z["shots"] = (tot_h, tot_a, True)
                    dists["shots"] = (mix(tot_h), mix(tot_a), True)
            if "corners" in means and gamma_corners is not None:
                ch, ca, csz = means["corners"]
                csz = _mixed_size(csz, s2)
                cor_h = [_rest(gm, ch * z, csz, 30, gamma_corners, True) for _, z in nodes]
                cor_a = [_rest(gm, ca * z, csz, 30, gamma_corners, False) for _, z in nodes]
                per_z["corners"] = (cor_h, cor_a, False)
                dists["corners"] = (mix(cor_h), mix(cor_a), False)
            gi, gj = _grid(ng)
            qs = {sl: (sb, _conditional(ng, dists[sb][0], dists[sb][1], sf, dists[sb][2]))
                  for sl, sb, sf in EVENTS if sb in dists and sl in ev}
            # per (home node, away node): only where two statistics share the frailty
            qz = {sl: [[_conditional(ng, per_z[sb][0][i], per_z[sb][1][j], sf, per_z[sb][2]) for j in range(len(nodes))]
                       for i in range(len(nodes))] for sl, sb, sf in EVENTS if frail and sb in per_z and sl in ev}
            for slabel, (sblock, q) in qs.items():
                pb = float((gm * q).sum())
                rep.marginal[slabel].append((ev[slabel][1], pb, ev[slabel][2]))
                for glabel, gblock, gfn in EVENTS:
                    if gblock == "goal" and glabel in ev:
                        struct[(glabel, slabel)] = (float((gm * gfn(gi, gj) * q).sum()), ev[glabel][1], pb)
                for olabel, (oblock, q2) in qs.items():  # two statistics: independent given the goals (and the frailty)
                    if oblock != sblock and frozenset((oblock, sblock)) not in JOINT_ALREADY:
                        if slabel in qz and olabel in qz:
                            pab = sum(_pair_weight(wi, wj, i, j, rho) * float((gm * qz[slabel][i][j] * qz[olabel][i][j]).sum())
                                      for i, (wi, _) in enumerate(nodes) for j, (wj, _) in enumerate(nodes))
                        else:
                            pab = float((gm * q * q2).sum())
                        struct[(olabel, slabel)] = (pab, float((gm * q2).sum()), pb)
            labels = list(ev)
            for x in range(len(labels)):
                for z in range(x + 1, len(labels)):
                    (ba, pa, ya), (bb, pb, yb) = ev[labels[x]], ev[labels[z]]
                    if ba == bb or frozenset((ba, bb)) in JOINT_ALREADY:
                        continue
                    rep.pairs[(labels[x], labels[z])].add(pa, ya, pb, yb)
                    if (labels[x], labels[z]) in struct:
                        rep.pairs[(labels[x], labels[z])].add_struct(*struct[(labels[x], labels[z])], ya, yb)
        monday = nxt
    return rep


def print_combo_eval(rep: ComboReport) -> None:
    print(f"My Combo, blocchi indipendenti: {rep.matches} partite dal {rep.start:%Y-%m-%d} al {rep.end:%Y-%m-%d}")
    print("\nsingoli eventi: prevista / vera")
    for label, (n, p, y) in rep.single.items():
        print(f"  {label:<26} {n:5d}  {p / n:.3f} / {y / n:.3f}")
    print("\ncoppie (blocchi diversi): n, entrambi veri / attesi da indipendenza, joint, netto, corr (caso ~ +-2/sqrt(n))")
    rows = sorted(rep.pairs.items(), key=lambda kv: -abs(kv[1].corr or 0))
    for (a, b), p in rows:
        if not p.n:
            continue
        flag = " *" if p.corr is not None and abs(p.corr) > 2 / math.sqrt(p.n) else ""
        extra = f"   strutt.: {p.ys_ab} / {p.ps_ab:.1f} joint {p.joint_struct:.3f} netto {p.net_struct:.3f}" if p.ns else ""
        print(f"  {a:<24} + {b:<26} {p.n:5d}  {p.y_ab:5d} / {p.p_ab:7.1f}  joint {p.joint:.3f}  netto {p.net:.3f}  corr {p.corr:+.3f}{flag}{extra}")
