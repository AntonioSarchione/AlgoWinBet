"""Replay of the player numbers (lineups tab cards): are they right, and how should they be estimated?

Every start of a FotMob player in [start, end) is predicted from his own earlier FotMob appearances only, with
playercard.py's own function: his last `last` appearances, counts per 90 minutes pulled toward the average of his role with
`prior_90` spells of 90 minutes (per stat), scaled to the minutes he plays when he starts. Each count is then a probability for the
lines the bookmakers offer (Poisson, or negative binomial with shape `disp`): shots 1+/2+/3+, shots on target 1+/2+, fouls
committed 1+/2+, fouls won 1+/2+, booked, assist. Scored with the binary log loss, next to the role average alone
("ruolo") and the settings before the replay ("prima"); each variant against the published one ("modello") per line
with a 95% interval. No request.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from .playercard import CONTEXT, DISP, KEEPER_CONTEXT, KEEPER_FULL, KEEPER_K, KEEPER_LAST, LAST, PRIOR_90, expected_counts as card_counts

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
    prior_90: float | tuple = tuple(sorted(PRIOR_90.items()))  # one weight for every stat, or (stat, weight) pairs
    disp: float = DISP  # > 0: negative binomial with this shape, 0: Poisson
    opp: float = CONTEXT["opp"]  # weights of the match context factors (playercontext.py): opponent, venue, referee; 0 = left out
    venue: float = CONTEXT["venue"]
    ref: float = CONTEXT["ref"]

    def weights(self) -> dict[str, float]:
        return dict(self.prior_90) if isinstance(self.prior_90, tuple) else {k: float(self.prior_90) for k in STATS}


def _with(**kw) -> tuple:
    return tuple(sorted({**PRIOR_90, **kw}.items()))


# "modello": what the lineups tab publishes (playercard.py); "prima": the settings before the replay (10 appearances, weight 3,
# Poisson, no context); "senza contesto": the model in the player's average match
VARIANTS = {"modello": PlayerParams(), "senza contesto": PlayerParams(opp=0.0, venue=0.0, ref=0.0),
            "prima": PlayerParams(last=10, prior_90=3.0, disp=0.0, opp=0.0, venue=0.0, ref=0.0),
            "ruolo": PlayerParams(prior_90=1e9, opp=0.0, venue=0.0, ref=0.0)}
TUNE = {**VARIANTS, "avversario½": PlayerParams(opp=0.5), "avversario1": PlayerParams(opp=1.0), "arbitro½": PlayerParams(ref=0.5),
        "arbitro1": PlayerParams(ref=1.0)}


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
    role's averages per 90 minutes, computed as playercard.py does; None below its minimum minutes."""
    w = prm.weights()  # 1e9 everywhere: the role alone
    got = card_counts([(r[0], r[1], dict(zip(STATS, r[2:]))) for r in apps], prior, prm.last, w)
    return None if got is None else {k: v for k, v in got[0].items() if k in STATS}


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
    with_ref: int = 0  # starts whose match has a known referee


def load_appearances(store) -> tuple[list[tuple], dict[str, str], dict[str, tuple[str, str, str]], dict[str, str]]:
    """(kickoff, player, minutes, starter, *STATS, fixture, team) of every FotMob appearance with minutes, oldest first; each
    FotMob player's role (ours through fotmob_player_links, else keepers by FotMob position, else MID); each match's
    (competition, home, away); each match's referee key (SnapshotProvider.referees)."""
    from .snapshots import SnapshotProvider
    rows = store.db.execute(
        "SELECT r.kickoff, s.player_id, s.minutes, s.starter, s.shots, s.shots_on, s.fouls_committed, s.fouls_drawn, "
        "(CASE WHEN COALESCE(s.yellow, 0) + COALESCE(s.red, 0) > 0 THEN 1 ELSE 0 END), s.assists, s.position, s.fixture_id, s.team, "
        "r.competition, r.home, r.away, s.goals, s.saves, s.goals_conceded, r.home_goals, r.away_goals "
        "FROM fotmob_player_stats s JOIN results r ON r.fixture_id = s.fixture_id WHERE s.minutes > 0 ORDER BY r.kickoff").fetchall()
    ours = dict(store.db.execute("SELECT l.fotmob_id, p.position FROM fotmob_player_links l JOIN players p ON p.id = l.goal_id").fetchall())
    roles: dict[str, str] = {}
    fixtures: dict[str, tuple] = {}  # fixture -> (competition, home, away, home goals, away goals)
    out = []
    for r in rows:
        pid = r[1]
        if pid not in roles:
            roles[pid] = ours.get(pid) or ROLE_OF_POSITION.get(r[10], "MID")
        fixtures.setdefault(r[11], (r[13], r[14], r[15], r[19], r[20]))
        out.append((r[0], pid, r[2], r[3], *(v or 0 for v in r[4:10]), r[11], r[12], r[16] or 0, r[17], r[18]))
    from .snapshots import referee_key
    refs = SnapshotProvider(store).referees()  # under the canonical result id
    for fid, source, name in store.db.execute("SELECT fixture_id, source, name FROM referees").fetchall():  # and under its own
        key = referee_key(name)
        if key and (fid not in refs or source == "football-data"):
            refs[fid] = key
    return out, roles, fixtures, refs


def evaluate_players(store, start: datetime, end: datetime, variants: dict[str, PlayerParams] | None = None,
                     data: tuple | None = None) -> PlayerReport:
    """Starts of [start, end), outfield players only, each predicted from the appearances (and match context) of earlier days."""
    from .playercontext import MatchContext, apply
    variants = variants or VARIANTS
    apps, roles, fixtures, refs = data or load_appearances(store)
    ctx = MatchContext()
    nstat = len(STATS)
    keep = max(p.last for p in variants.values())
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=keep))
    role_sum: dict[str, list[float]] = defaultdict(lambda: [0.0] * (len(STATS) + 1))  # role -> STATS sums, minutes
    scores = {v: {label: LineScore() for label, _, _ in LINES} for v in variants}
    s0, e0 = start.isoformat(), end.isoformat()
    n = with_ref = 0
    i = 0
    while i < len(apps):
        day = apps[i][0][:10]
        j = i
        while j < len(apps) and apps[j][0][:10] == day:
            j += 1
        batch = apps[i:j]
        for ko, pid, mins, starter, *rest in batch:
            vals, fid, team = rest[:nstat], rest[nstat], rest[nstat + 1]
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
            comp, home, away = fixtures.get(fid, ("", "", ""))[:3]
            with_ref += fid in refs
            if team in (home, away) and any(p.opp or p.venue or p.ref for p in variants.values()):
                f = ctx.factors(away if team == home else home, comp, team == home, refs.get(fid))
                got = {v: apply(g, f, p.opp, p.venue, p.ref) if (p.opp or p.venue or p.ref) else g
                       for (v, g), p in zip(got.items(), variants.values())}
            n += 1
            y = dict(zip(STATS, vals))
            for v, p in variants.items():
                for label, stat, k in LINES:
                    scores[v][label].add(at_least(got[v][stat], k, p.disp), int(y[stat] >= k))
        totals: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(STATS, 0.0)))
        for ko, pid, mins, starter, *rest in batch:  # the day's matches enter the history only after all of them are predicted
            vals, fid, team = rest[:nstat], rest[nstat], rest[nstat + 1]
            hist[pid].append((mins, starter, *vals))
            rs = role_sum[roles.get(pid, "MID")]
            for x, v in enumerate(vals):
                rs[x] += v
                totals[fid][team][STATS[x]] += v
            totals[fid][team]["goals"] = totals[fid][team].get("goals", 0.0) + (rest[nstat + 2] if len(rest) > nstat + 2 else 0)
            rs[-1] += mins
        for fid, by_team in totals.items():
            comp, home, away = fixtures.get(fid, ("", "", ""))[:3]
            ctx.add(comp, home, away, by_team, refs.get(fid))
        i = j
    return PlayerReport(start, end, n, scores, with_ref)


def _diff(a: LineScore, b: LineScore) -> tuple[float, float]:
    d = [x - y for x, y in zip(a.terms, b.terms)]
    if len(d) < 2:
        return 0.0, 0.0
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / (len(d) - 1)
    return m, 1.96 * math.sqrt(var / len(d))


def print_player_eval(rep: PlayerReport, calibration: tuple[str, ...] = ("ammonito", "tiri in porta 1+", "assist")) -> None:
    print(f"giocatori, replay {rep.start:%Y-%m-%d} - {rep.end:%Y-%m-%d}: {rep.starts} partite da titolare (portieri esclusi), "
          f"{rep.with_ref} con l'arbitro noto")
    base = rep.scores.get("modello")
    for label, _, _ in LINES:
        print(f"  {label}")
        for v, by in rep.scores.items():
            s = by[label]
            if not s.terms:
                continue
            line = f"    {v:16} log loss {sum(s.terms) / len(s.terms):.4f}  succede {s.hits} (attesi {s.exp:.0f})"
            if base is not None and v != "modello":
                m, hw = _diff(s, base[label])
                line += f"  vs modello {m:+.4f} ± {hw:.4f}" + ("  (meglio)" if m + hw < 0 else "  (peggio)" if m - hw > 0 else "")
            print(line)
    for v in [x for x in ("modello", "prima") if x in rep.scores]:
        for label in calibration:
            s = rep.scores[v][label]
            if not s.terms:
                continue
            print(f"  calibrazione {v}, {label}: fascia, giocatori, probabilità media, frequenza vera")
            for b in sorted(s.bins):
                c, p, y = s.bins[b]
                lo = BIN_EDGES[b - 1] if b else 0.0
                print(f"    {lo:.2f}-{min(BIN_EDGES[b], 1.0):.2f}  {c:6d}  {p / c:.3f}  {y / c:.3f}")


# ------------------------------------------------------------------------------------------------------------ keepers
KEEPER_LINES = (("parate 2+", "sv", 2), ("parate 3+", "sv", 3), ("parate 4+", "sv", 4), ("tiri in porta subiti 4+", "ts", 4),
                ("tiri in porta subiti 6+", "ts", 6), ("porta inviolata", "gc", 0), ("goal subiti 2+", "gc", 2))


@dataclass(frozen=True)
class KeeperParams:  # defaults: what the lineups tab publishes (playercard.py)
    last: int = KEEPER_LAST  # full matches read
    k: float = KEEPER_K  # matches of the keepers' average added to his own
    disp: float = 0.0  # saves and shots on target faced: negative binomial shape (0: Poisson); goals conceded always Poisson
    opp: float = KEEPER_CONTEXT["opp"]  # opponent's own attack (shots on target, goals) and venue factors
    venue: float = KEEPER_CONTEXT["venue"]


KEEPER_VARIANTS = {"modello": KeeperParams(), "media": KeeperParams(last=10, k=0.0, opp=0.0, venue=0.0),
                   "senza contesto": KeeperParams(opp=0.0, venue=0.0)}
KEEPER_TUNE = {**KEEPER_VARIANTS, "k10": KeeperParams(k=10.0), "k40": KeeperParams(k=40.0), "ultime40": KeeperParams(last=40),
               "contesto3/4": KeeperParams(opp=0.75), "disp8": KeeperParams(disp=8.0)}


def keeper_rates(hist: list[tuple[float, float, float]], mu: tuple[float, float, float], prm: KeeperParams) -> tuple[float, float, float]:
    """Expected shots on target faced, goals conceded, saves in a full match from his last full matches (most recent first)
    and the keepers' average, with prm.k matches of it."""
    h = hist[:prm.last]
    n = len(h)
    return tuple((sum(x[i] for x in h) + prm.k * mu[i]) / (n + prm.k) if n + prm.k > 0 else mu[i] for i in range(3))


def evaluate_keepers(store, start: datetime, end: datetime, variants: dict[str, KeeperParams] | None = None,
                     data: tuple | None = None) -> PlayerReport:
    """Full matches of keepers in [start, end), each predicted from the keeper's earlier full matches: shots on target
    faced (the opponent's players' shots on target), goals conceded (our result) and saves (FotMob's when read, else the
    difference)."""
    from .playercontext import MatchContext, apply
    variants = variants or KEEPER_VARIANTS
    apps, roles, fixtures, _refs = data or load_appearances(store)
    nstat = len(STATS)
    ctx = MatchContext()
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=max(p.last for p in variants.values())))
    acc = [0.0, 0.0, 0.0, 0]  # keepers' sums of ts, gc, sv and full matches
    scores = {v: {label: LineScore() for label, _, _ in KEEPER_LINES} for v in variants}
    s0, e0 = start.isoformat(), end.isoformat()
    n = 0
    i = 0
    while i < len(apps):
        day = apps[i][0][:10]
        j = i
        while j < len(apps) and apps[j][0][:10] == day:
            j += 1
        batch = apps[i:j]
        totals: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(STATS, 0.0)))
        for ko, pid, mins, starter, *rest in batch:
            vals, fid, team = rest[:nstat], rest[nstat], rest[nstat + 1]
            for x, v in enumerate(vals):
                totals[fid][team][STATS[x]] += v
            totals[fid][team]["goals"] = totals[fid][team].get("goals", 0.0) + rest[nstat + 2]
        done = []
        for ko, pid, mins, starter, *rest in batch:
            fid, team, fm_sv, fm_gc = rest[nstat], rest[nstat + 1], rest[nstat + 3], rest[nstat + 4]
            if roles.get(pid) != "GK" or mins < KEEPER_FULL or fid not in fixtures:
                continue
            comp, home, away, hg, ag = fixtures[fid]
            if team not in (home, away) or hg is None or ag is None:
                continue
            opp = away if team == home else home
            if opp not in totals[fid]:
                continue
            ts = totals[fid][opp]["shots_on"]
            gc = fm_gc if fm_gc is not None else (ag if team == home else hg)
            sv = fm_sv if fm_sv is not None else max(0.0, ts - gc)
            done.append((pid, ts, gc, sv))
            if s0 <= ko < e0 and acc[3] >= 50 and hist[pid]:
                mu = (acc[0] / acc[3], acc[1] / acc[3], acc[2] / acc[3])
                mine = list(reversed(hist[pid]))
                f = ctx.attack(opp, comp, team == home)
                n += 1
                y = {"ts": ts, "gc": gc, "sv": sv}
                for v, prm in variants.items():
                    r_ts, r_gc, r_sv = keeper_rates(mine, mu, prm)
                    m = apply({"shots_on": r_ts, "goals": r_gc}, f, prm.opp, prm.venue)
                    scale = m["shots_on"] / r_ts if r_ts > 0 else 1.0
                    rate = {"ts": m["shots_on"], "gc": m["goals"], "sv": r_sv * scale}
                    for label, stat, k in KEEPER_LINES:
                        if stat == "gc" and k == 0:
                            p = 1.0 - at_least(rate["gc"], 1)
                            hit = int(y["gc"] == 0)
                        else:
                            p = at_least(rate[stat], k, prm.disp if stat != "gc" else 0.0)
                            hit = int(y[stat] >= k)
                        scores[v][label].add(p, hit)
        for pid, ts, gc, sv in done:  # the day's matches enter the history after all of them are predicted
            hist[pid].append((ts, gc, sv))
            acc[0] += ts
            acc[1] += gc
            acc[2] += sv
            acc[3] += 1
        for fid, by_team in totals.items():
            if fid in fixtures:
                comp, home, away = fixtures[fid][:3]
                ctx.add(comp, home, away, by_team)
        i = j
    return PlayerReport(start, end, n, scores)


def print_keeper_eval(rep: PlayerReport, base: str = "modello") -> None:
    print(f"portieri, replay {rep.start:%Y-%m-%d} - {rep.end:%Y-%m-%d}: {rep.starts} partite intere")
    for label, _, _ in KEEPER_LINES:
        print(f"  {label}")
        for v, by in rep.scores.items():
            s = by[label]
            if not s.terms:
                continue
            line = f"    {v:20} log loss {sum(s.terms) / len(s.terms):.4f}  succede {s.hits} (attesi {s.exp:.0f})"
            if v != base and base in rep.scores:
                m, hw = _diff(s, rep.scores[base][label])
                line += f"  vs {base} {m:+.4f} ± {hw:.4f}" + ("  (meglio)" if m + hw < 0 else "  (peggio)" if m - hw > 0 else "")
            print(line)
