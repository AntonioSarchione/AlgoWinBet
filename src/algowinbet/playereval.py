"""Replay of the player numbers (lineups tab cards): are they right, and how should they be estimated?

Every start of a FotMob player in [start, end) is predicted from his own earlier FotMob appearances only, the way
playercard.py does it: his last `last` appearances, counts per 90 minutes pulled toward the average of his role with
`prior_90` spells of 90 minutes, scaled to the minutes he plays when he starts. Each count is then a probability for the
lines the bookmakers offer (Poisson, or negative binomial with shape `disp`): shots 1+/2+/3+, shots on target 1+/2+, fouls
committed 1+/2+, fouls won 1+/2+, booked, assist. Scored with the binary log loss, next to the role average alone
("ruolo"); each variant against the current settings ("attuale") per line with a 95% interval. No request.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from .playercard import LAST, MIN_MINUTES, PRIOR_90, START_MINUTES

# (label, stat, at least)
LINES = (("tiri 1+", "shots", 1), ("tiri 2+", "shots", 2), ("tiri 3+", "shots", 3), ("tiri in porta 1+", "shots_on", 1),
         ("tiri in porta 2+", "shots_on", 2), ("falli fatti 1+", "fouls_committed", 1), ("falli fatti 2+", "fouls_committed", 2),
         ("falli subiti 1+", "fouls_drawn", 1), ("falli subiti 2+", "fouls_drawn", 2), ("ammonito", "cards", 1), ("assist", "assists", 1))
STATS = ("shots", "shots_on", "fouls_committed", "fouls_drawn", "cards", "assists")
ROLE_OF_POSITION = {11: "GK"}  # FotMob positionId when the player has no role of ours: keepers only, the rest by their links
BIN_EDGES = (0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 1.01)


@dataclass(frozen=True)
class PlayerParams:
    last: int = LAST
    prior_90: float = PRIOR_90
    disp: float = 0.0  # > 0: negative binomial with this shape


VARIANTS = {"attuale": PlayerParams(), "ruolo": PlayerParams(prior_90=1e9)}
TUNE = {"attuale": PlayerParams(), "ruolo": PlayerParams(prior_90=1e9),
        **{f"u{n}p{k:g}": PlayerParams(last=n, prior_90=k) for n in (20, 40) for k in (1.0, 3.0, 8.0, 20.0)},
        **{f"u{n}p{k:g}d4": PlayerParams(last=n, prior_90=k, disp=4.0) for n in (20, 40) for k in (3.0, 20.0)}}


def at_least(rate: float, k: int, disp: float = 0.0) -> float:
    """P(X >= k) for a count with mean `rate`: Poisson, or negative binomial with shape `disp`."""
    if rate <= 0:
        return 0.0
    if disp > 0:
        q = disp / (disp + rate)
        pk, cdf = q ** disp, 0.0
        for j in range(k):
            cdf += pk
            pk *= (disp + j) / (j + 1) * (1 - q)
    else:
        pk, cdf = math.exp(-rate), 0.0
        for j in range(k):
            cdf += pk
            pk *= rate / (j + 1)
    return min(max(1.0 - cdf, 0.0), 1.0)


def expected_counts(apps: list[tuple], prior: dict[str, float], prm: PlayerParams) -> dict[str, float] | None:
    """Expected counts in a start from the player's appearances (minutes, starter, then STATS; most recent first) and his
    role's averages per 90 minutes; None below MIN_MINUTES."""
    a = apps[:prm.last]
    total = sum(r[0] for r in a)
    if total < MIN_MINUTES:
        return None
    starts = [r[0] for r in a if r[1]]
    mins = min(90.0, sum(starts) / len(starts)) if starts else START_MINUTES
    out = {}
    for i, k in enumerate(STATS):
        if prm.prior_90 >= 1e8:
            out[k] = prior[k] * mins / 90
            continue
        rate = (sum(r[2 + i] for r in a) + prior[k] * prm.prior_90) / (total / 90 + prm.prior_90)
        out[k] = rate * mins / 90
    return out


@dataclass
class LineScore:
    terms: list[float] = field(default_factory=list)
    hits: int = 0
    exp: float = 0.0
    bins: dict[int, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))

    def add(self, p: float, y: int) -> None:
        p = min(max(p, 1e-6), 1 - 1e-6)
        self.terms.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
        self.hits += y
        self.exp += p
        b = self.bins[next(i for i, e in enumerate(BIN_EDGES) if p < e)]
        b[0] += 1
        b[1] += p
        b[2] += y


@dataclass
class PlayerReport:
    start: datetime
    end: datetime
    starts: int
    scores: dict[str, dict[str, LineScore]]  # variant -> line -> score


def load_appearances(store) -> tuple[list[tuple], dict[str, str]]:
    """(kickoff, player, minutes, starter, *STATS) of every FotMob appearance with minutes, oldest first, and each FotMob
    player's role (ours through fotmob_player_links, else keepers by FotMob position, else MID)."""
    rows = store.db.execute(
        "SELECT r.kickoff, s.player_id, s.minutes, s.starter, s.shots, s.shots_on, s.fouls_committed, s.fouls_drawn, "
        "(CASE WHEN COALESCE(s.yellow, 0) + COALESCE(s.red, 0) > 0 THEN 1 ELSE 0 END), s.assists, s.position "
        "FROM fotmob_player_stats s JOIN results r ON r.fixture_id = s.fixture_id WHERE s.minutes > 0 ORDER BY r.kickoff").fetchall()
    ours = dict(store.db.execute("SELECT l.fotmob_id, p.position FROM fotmob_player_links l JOIN players p ON p.id = l.goal_id").fetchall())
    roles: dict[str, str] = {}
    out = []
    for r in rows:
        pid = r[1]
        if pid not in roles:
            roles[pid] = ours.get(pid) or ROLE_OF_POSITION.get(r[10], "MID")
        out.append((r[0], pid, r[2], r[3], *(v or 0 for v in r[4:10])))
    return out, roles


def evaluate_players(store, start: datetime, end: datetime, variants: dict[str, PlayerParams] | None = None,
                     data: tuple[list[tuple], dict[str, str]] | None = None) -> PlayerReport:
    """Starts of [start, end), outfield players only, each predicted from the appearances of earlier days."""
    variants = variants or VARIANTS
    apps, roles = data or load_appearances(store)
    keep = max(p.last for p in variants.values())
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=keep))
    role_sum: dict[str, list[float]] = defaultdict(lambda: [0.0] * (len(STATS) + 1))  # role -> STATS sums, minutes
    scores = {v: {label: LineScore() for label, _, _ in LINES} for v in variants}
    s0, e0 = start.isoformat(), end.isoformat()
    n = 0
    i = 0
    while i < len(apps):
        day = apps[i][0][:10]
        j = i
        while j < len(apps) and apps[j][0][:10] == day:
            j += 1
        batch = apps[i:j]
        for ko, pid, mins, starter, *vals in batch:
            if not starter or not (s0 <= ko < e0) or roles.get(pid) == "GK":
                continue
            rs = role_sum[roles.get(pid, "MID")]
            if rs[-1] < 90 * 50:  # the role average needs some matches first
                continue
            prior = {k: rs[x] / rs[-1] * 90 for x, k in enumerate(STATS)}
            mine = list(reversed(hist[pid]))
            got = {v: expected_counts(mine, prior, p) for v, p in variants.items()}
            if any(g is None for g in got.values()):
                continue
            n += 1
            y = dict(zip(STATS, vals))
            for v, p in variants.items():
                for label, stat, k in LINES:
                    scores[v][label].add(at_least(got[v][stat], k, p.disp), int(y[stat] >= k))
        for ko, pid, mins, starter, *vals in batch:  # the day's matches enter the history only after all of them are predicted
            hist[pid].append((mins, starter, *vals))
            rs = role_sum[roles.get(pid, "MID")]
            for x, v in enumerate(vals):
                rs[x] += v
            rs[-1] += mins
        i = j
    return PlayerReport(start, end, n, scores)


def _diff(a: LineScore, b: LineScore) -> tuple[float, float]:
    d = [x - y for x, y in zip(a.terms, b.terms)]
    if len(d) < 2:
        return 0.0, 0.0
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / (len(d) - 1)
    return m, 1.96 * math.sqrt(var / len(d))


def print_player_eval(rep: PlayerReport, calibration: tuple[str, ...] = ("ammonito", "tiri in porta 1+", "assist")) -> None:
    print(f"giocatori, replay {rep.start:%Y-%m-%d} - {rep.end:%Y-%m-%d}: {rep.starts} partite da titolare (portieri esclusi)")
    base = rep.scores.get("attuale")
    for label, _, _ in LINES:
        print(f"  {label}")
        for v, by in rep.scores.items():
            s = by[label]
            if not s.terms:
                continue
            line = f"    {v:16} log loss {sum(s.terms) / len(s.terms):.4f}  succede {s.hits} (attesi {s.exp:.0f})"
            if base is not None and v != "attuale":
                m, hw = _diff(s, base[label])
                line += f"  vs attuale {m:+.4f} ± {hw:.4f}" + ("  (meglio)" if m + hw < 0 else "  (peggio)" if m - hw > 0 else "")
            print(line)
    for v in rep.scores:
        for label in calibration:
            s = rep.scores[v][label]
            if not s.terms:
                continue
            print(f"  calibrazione {v}, {label}: fascia, giocatori, probabilità media, frequenza vera")
            for b in sorted(s.bins):
                c, p, y = s.bins[b]
                lo = BIN_EDGES[b - 1] if b else 0.0
                print(f"    {lo:.2f}-{min(BIN_EDGES[b], 1.0):.2f}  {c:6d}  {p / c:.3f}  {y / c:.3f}")
