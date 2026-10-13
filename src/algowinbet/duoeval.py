"""Sisal's DUO player markets (goal, assist, goal or assist, shots on target): the bet also wins when the player's direct
substitute does it. The site prices the substitute's minutes at the starter's own per-minute rate (web/lib/playermarkets.ts
duoSlot). This replay checks that assumption on the FotMob player rows of past matches, no API request.

Who replaced whom is not stored: a starter who left at minute m is paired with the one substitute of his team who played
90 - m minutes (FotMob minutes of a starter and of his substitute add up to 90). Several starters off at the same minute are
paired by role when the roles tell them apart, otherwise skipped; sent-off starters have no substitute.

Each player's per-minute rate comes from his appearances before the match day, shrunk toward his role (K minutes). Over the
starter's own minutes the rate is checked against what he did (the estimator's own calibration); over the substitute's
minutes against what the substitute did. Their ratio is the factor the substitute's minutes deserve (1 = the assumption holds).
The bet itself (did the slot do it at least once) is then scored the way the site prices it, for a few factors.

Replay 2025-07 - 2026-10, 7 leagues, 44991 starts (14777 with a paired substitute): substitutes produce more per minute than
the starter's rate (goals x1.36, assists x1.41, shots on target x1.45: the late minutes and the stoppage time are richer), still
above 1 when the team was winning by 2+. On the bet outcome the site's factor 1 has the best log loss for all three, f 1.25
about equal; f 1 under-predicts the substitute's share a little (goals -0.4 points, shots on target -1.1). Kept at 1.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

STATS = ("goals", "assists", "shots_on")
LABELS = {"goals": "goal", "assists": "assist", "shots_on": "tiri in porta"}
K = 900.0  # shrinkage toward the role rate, in minutes
MIN_ROLE_MINUTES = 90 * 50


@dataclass
class Tally:
    exp: float = 0.0
    act: float = 0.0

    def add(self, e: float, a: float) -> None:
        self.exp += e
        self.act += a

    @property
    def ratio(self) -> float | None:
        return self.act / self.exp if self.exp > 0 else None


@dataclass
class Binary:
    """Every judged slot: expected counts over the starter's and over the substitute's minutes (starter's rate), whether the
    starter did it, whether the slot did it (starter or substitute). Scored the way the site prices DUO: the starter's own
    probability a, calibrated here so that it matches how often starters did it (the site's a comes from calibrated models),
    and the substitute's minutes at f times the per-minute rate behind a: P = 1 - exp(-s (mu_own + f mu_sub))."""
    slots: list[tuple[float, float, int, int]] = field(default_factory=list)

    def add(self, mu_own: float, mu_sub: float, y_own: int, y_duo: int) -> None:
        self.slots.append((mu_own, mu_sub, y_own, y_duo))

    def scale(self) -> float:
        """s such that the starters' own probabilities average how often they did it."""
        target = sum(s[2] for s in self.slots) / max(len(self.slots), 1)
        lo, hi = 0.01, 10.0
        for _ in range(60):
            mid = (lo + hi) / 2
            mean = sum(1 - math.exp(-mid * a) for a, *_ in self.slots) / len(self.slots)
            lo, hi = (mid, hi) if mean < target else (lo, mid)
        return (lo + hi) / 2

    def score(self, f: float, s: float, subbed: bool = False) -> tuple[float, float, float, float, int]:
        """(mean log loss of the DUO outcome, mean predicted, actual rate, predicted - actual of the substitute's share, n)
        over every slot or over the replaced starters only."""
        loss = pred = act = inc_pred = inc_act = 0.0
        n = 0
        for a, b, y_own, y in self.slots:
            if subbed and b <= 0:
                continue
            p_own = 1 - math.exp(-s * a)
            p = min(max(1 - math.exp(-s * (a + f * b)), 1e-6), 1 - 1e-6)
            loss -= math.log(p if y else 1 - p)
            pred += p
            act += y
            inc_pred += p - p_own
            inc_act += y - y_own
            n += 1
        n1 = max(n, 1)
        return loss / n1, pred / n1, act / n1, (inc_pred - inc_act) / n1, n


@dataclass
class DuoReport:
    start: datetime
    starters: int = 0  # outfield starts judged
    subbed: int = 0  # of which replaced by a paired substitute
    skipped: Counter = field(default_factory=Counter)
    own: dict[str, Tally] = field(default_factory=lambda: {s: Tally() for s in STATS})
    sub: dict[str, Tally] = field(default_factory=lambda: {s: Tally() for s in STATS})
    sub_own_rate: dict[str, Tally] = field(default_factory=lambda: {s: Tally() for s in STATS})  # the substitute's own rate
    by: dict[tuple[str, str, str], Tally] = field(default_factory=lambda: defaultdict(Tally))  # (cut, value, stat), substitute minutes
    own_by: dict[tuple[str, str, str], Tally] = field(default_factory=lambda: defaultdict(Tally))
    binary: dict[str, Binary] = field(default_factory=lambda: {s: Binary() for s in STATS})

    def factor(self, stat: str, cut: tuple[str, str] | None = None) -> float | None:
        o = self.own_by[(*cut, stat)] if cut else self.own[stat]
        s = self.by[(*cut, stat)] if cut else self.sub[stat]
        return s.ratio / o.ratio if s.ratio is not None and o.ratio else None


FACTORS = (0.0, 0.5, 1.0, 1.25, 1.5, 2.0)


def pair_substitutes(rows: list[tuple], roles: dict[str, str]) -> tuple[dict[str, str], Counter]:
    """rows: (player, minutes, starter, red) of one team in one match. Returns starter -> substitute, and what was skipped."""
    skipped: Counter = Counter()
    off = defaultdict(list)
    for pid, mins, starter, red in rows:
        if starter and mins < 90 and not red:
            off[mins].append(pid)
    on = defaultdict(list)
    for pid, mins, starter, _ in rows:
        if not starter and mins > 0:
            on[90 - mins].append(pid)
    out: dict[str, str] = {}
    for m, starters in off.items():
        subs = on.get(m, [])
        if not subs:
            skipped["nessun sostituto con i minuti giusti"] += len(starters)
            continue
        if len(starters) == 1 and len(subs) == 1:
            out[starters[0]] = subs[0]
            continue
        if len(starters) == len(subs):
            rs = [roles.get(p, "MID") for p in starters]
            rb = [roles.get(p, "MID") for p in subs]
            if len(set(rs)) == len(rs) and sorted(rs) == sorted(rb):
                by_role = dict(zip(rb, subs))
                for p, r in zip(starters, rs):
                    out[p] = by_role[r]
                continue
        skipped["cambi nello stesso minuto, non distinguibili"] += len(starters)
    return out, skipped


def evaluate_duo(store, start: datetime, data: tuple | None = None, k: float = K,
                 competitions: set[str] | None = None) -> DuoReport:
    from .playereval import load_appearances
    apps, roles, fixtures, _ = data or load_appearances(store)
    # (ko, pid, mins, starter, shots, shots_on, fouls_c, fouls_d, card, assists, fixture, team, goals, saves, conceded)
    red = {(r[0], r[1]) for r in store.db.execute("SELECT fixture_id, player_id FROM fotmob_player_stats WHERE COALESCE(red, 0) > 0")}
    rep = DuoReport(start)
    s0 = start.isoformat()
    hist: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # goals, assists, shots_on, minutes
    role_sum: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])

    def rate(pid: str) -> list[float] | None:
        rs = role_sum[roles.get(pid, "MID")]
        if rs[3] < MIN_ROLE_MINUTES:
            return None
        h = hist[pid]
        return [(h[x] + k * rs[x] / rs[3]) / (h[3] + k) for x in range(3)]

    def vals(a: tuple) -> list[float]:
        return [a[12] or 0, a[9] or 0, a[5] or 0]

    i = 0
    while i < len(apps):
        day = apps[i][0][:10]
        j = i
        while j < len(apps) and apps[j][0][:10] == day:
            j += 1
        batch = apps[i:j]
        if day >= s0[:10]:
            teams: dict[tuple[str, str], list[tuple]] = defaultdict(list)
            for a in batch:
                teams[(a[10], a[11])].append(a)
            for (fid, team), rows in teams.items():
                comp, home, away, hg, ag = fixtures.get(fid, ("", "", "", None, None))
                if competitions is not None and comp not in competitions:
                    continue
                pairs, skipped = pair_substitutes([(a[1], a[2], a[3], (fid, a[1]) in red) for a in rows], roles)
                rep.skipped.update(skipped)
                by_pid = {a[1]: a for a in rows}
                diff = None
                if hg is not None and ag is not None and team in (home, away):
                    diff = (hg - ag) if team == home else (ag - hg)
                for a in rows:
                    pid, mins, starter = a[1], a[2], a[3]
                    if not starter or roles.get(pid) == "GK":
                        continue
                    r = rate(pid)
                    if r is None:
                        continue
                    rep.starters += 1
                    sub = pairs.get(pid)
                    mine = vals(a)
                    theirs = vals(by_pid[sub]) if sub else [0, 0, 0]
                    sub_mins = 90 - mins if sub else 0
                    if sub:
                        rep.subbed += 1
                    rsub = rate(sub) if sub else None
                    role = roles.get(pid, "MID")
                    when = "" if not sub else ("prima del 60'" if mins < 60 else "60'-74'" if mins < 75 else "dal 75'")
                    state = "" if diff is None else ("vince di 2+" if diff >= 2 else "perde di 2+" if diff <= -2 else "pari o di 1")
                    for x, stat in enumerate(STATS):
                        rep.own[stat].add(r[x] * mins, mine[x])
                        rep.own_by[("ruolo", role, stat)].add(r[x] * mins, mine[x])
                        if state:
                            rep.own_by[("risultato finale", state, stat)].add(r[x] * mins, mine[x])
                        if sub:
                            rep.sub[stat].add(r[x] * sub_mins, theirs[x])
                            rep.by[("ruolo", role, stat)].add(r[x] * sub_mins, theirs[x])
                            rep.by[("uscita", when, stat)].add(r[x] * sub_mins, theirs[x])
                            if state:
                                rep.by[("risultato finale", state, stat)].add(r[x] * sub_mins, theirs[x])
                            if rsub is not None:
                                rep.sub_own_rate[stat].add(rsub[x] * sub_mins, theirs[x])
                        if mins < 90 and not sub:
                            continue  # sent off or not paired: the slot is not known
                        rep.binary[stat].add(r[x] * mins, r[x] * sub_mins, int(mine[x] > 0), int(mine[x] + theirs[x] > 0))
        for a in batch:  # the day's matches enter the history only after all of them are judged
            pid = a[1]
            v = vals(a)
            h = hist[pid]
            rs = role_sum[roles.get(pid, "MID")]
            for x in range(3):
                h[x] += v[x]
                rs[x] += v[x]
            h[3] += a[2]
            rs[3] += a[2]
        i = j
    return rep


def _fmt(t: Tally) -> str:
    return f"{t.act:7.0f} su {t.exp:7.1f} attesi ({t.ratio:.2f})" if t.ratio is not None else "–"


def print_duo_eval(rep: DuoReport) -> None:
    print(f"DUO dal {rep.start:%Y-%m-%d}: {rep.starters} titolari di movimento, {rep.subbed} sostituiti da un cambio accoppiato "
          f"({rep.subbed / max(rep.starters, 1):.0%})")
    for why, n in rep.skipped.most_common():
        print(f"  non accoppiati: {n} ({why})")
    print("\nfatti / attesi al ritmo del titolare (rapporto); fattore = rapporto del sostituto / rapporto del titolare")
    for stat in STATS:
        f = rep.factor(stat)
        print(f"  {LABELS[stat]:<14} titolare {_fmt(rep.own[stat])}   sostituto {_fmt(rep.sub[stat])}   "
              f"fattore {f:.2f}" if f is not None else f"  {LABELS[stat]}: –")
        print(f"  {'':<14} sostituto al suo ritmo: {_fmt(rep.sub_own_rate[stat])}")
    for cut in ("ruolo", "uscita", "risultato finale"):
        print(f"\nfattore per {cut}:")
        values = sorted({v for (c, v, _) in rep.by if c == cut})
        for v in values:
            parts = []
            for stat in STATS:
                s = rep.by[(cut, v, stat)]
                own = rep.own_by.get((cut, v, stat))
                base = own.ratio if own and own.ratio else rep.own[stat].ratio
                parts.append(f"{LABELS[stat]} {s.act:.0f}/{s.exp:.1f} = {s.ratio / base:.2f}" if s.ratio is not None and base else f"{LABELS[stat]} –")
            print(f"  {v:<16} " + "   ".join(parts))
    print("\nesito DUO (almeno uno tra titolare e sostituto) come lo prezza il sito: probabilità del titolare calibrata, minuti del "
          "sostituto a f volte il suo ritmo. Log loss (più basso = meglio); quota del sostituto prevista - vera")
    for stat in STATS:
        b = rep.binary[stat]
        if not b.slots:
            continue
        sc = b.scale()
        for subbed, what in ((False, "tutti i titolari"), (True, "solo i sostituiti")):
            rows = {f: b.score(f, sc, subbed) for f in FACTORS}
            n, act = rows[1.0][4], rows[1.0][2]
            print(f"  {LABELS[stat]:<14} {what}: {n} slot, frequenza vera {act:.4f}")
            print("    " + "  ".join(f"f={f:.2f}: {r[0]:.5f} ({r[3]:+.4f})" for f, r in rows.items()))
