"""Goalscorer model (Fase 9): probability that a player scores, first goalscorer, 2+ goals.

The team's expected goals come from Dixon-Coles (the same numbers as 1X2 and Under/Over, so every market of a match agrees).
They are split into three parts: own goals (a fixed league share), penalties (the team's penalty share of its goals, shrunk
to the league share) and the rest. A player gets a share of the rest and a share of the penalties:

  share (non-penalty) .. his non-penalty goals / the team's non-penalty goals in the matches he started, shrunk towards the
                         share of his role (forward, midfielder, defender, keeper) with strength K team goals; older matches
                         weigh less (half-life HALF_LIFE days). A match on the bench has its own, much smaller share.
  penalty share ........ his penalty goals / the team's penalty goals in the matches he started (the taker), shrunk the same way.

With the official XI the shares of the 11 starters and of the bench are normalised so that the team's non-penalty goals are
all scored by someone on the sheet. Before the XI each player of the squad weighs by his recent start rate.
Goals: Poisson with mean lam_np * share + lam_pen * pen_share; first goalscorer: his rate over both teams' total rate, times
P(at least one goal).

Everything is learnt from the GOAL lineups and goal events (match_events), only from matches before the moment of the
prediction: `evaluate_scorers` replays it week by week and scores it against the outcome (binary log loss, Brier,
calibration), next to two simpler references (role only, player without shrinkage).
"""
from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config

ROLES = ("GK", "DEF", "MID", "FWD")
GOAL_KINDS = ("GOAL", "PENALTY")
HALF_LIFE = 365.0  # days
K = 6.0  # shrinkage strength, in team goals scored while the player was on the pitch (tuned by the replay)
K_PEN = 4.0
BENCH_PRIOR = 0.25  # a bench player's share relative to a starter of his role, before data (most never enter)
OWN_GOAL_SHARE = 0.03
PEN_PRIOR = 0.10  # league share of goals from penalties
PEN_K = 30.0
RECENT_XI = 8  # squad before the XI: players seen in the team's last 8 sheets


def _decay(days: float, half_life: float) -> float:
    return 0.5 ** (max(days, 0.0) / half_life)


@dataclass
class Sheet:
    """One team's sheet in one finished match, with its goals."""
    fixture_id: str
    team: str
    kickoff: datetime
    starters: list[str]
    bench: list[str]
    np_goals: dict[str, int] = field(default_factory=dict)  # player -> non-penalty goals
    pen_goals: dict[str, int] = field(default_factory=dict)
    team_np: int = 0  # the team's non-penalty, non-own goals (scorer known or not)
    team_pen: int = 0
    team_goals: int = 0  # everything, own goals of the opponent included


@dataclass
class ScorerData:
    sheets: list[Sheet]  # time order
    roles: dict[str, str]  # player -> GK/DEF/MID/FWD
    names: dict[str, str]
    team_of: dict[str, str]  # player -> current team (players table)


def load_scorer_data(store, competitions: list[str] | None = None) -> ScorerData:
    """Sheets of every finished match with confirmed XI and goal events read (three reads)."""
    res = {r[0]: r[1:] for r in store.db.execute(
        "SELECT r.fixture_id, r.competition, r.home, r.away, r.kickoff, r.home_goals, r.away_goals FROM results r "
        "JOIN event_reads e ON e.fixture_id = r.fixture_id").fetchall() if not competitions or r[1] in competitions}
    xi: dict[tuple[str, str], tuple[list, list, str]] = {}
    for fid, team, starters, bench, at in store.db.execute(
            "SELECT fixture_id, team, starters, bench, observed_at FROM lineups WHERE status = 'confirmed' ORDER BY observed_at").fetchall():
        if fid in res:
            xi[(fid, team)] = (json.loads(starters or "[]"), json.loads(bench or "[]"), at)
    goals: dict[tuple[str, str], list[tuple[str, str | None]]] = defaultdict(list)
    for fid, team, kind, pid in store.db.execute("SELECT fixture_id, team, kind, player_id FROM match_events "
                                                 "WHERE kind IN ('GOAL', 'PENALTY', 'OWN_GOAL')").fetchall():
        if fid in res:
            goals[(fid, team)].append((kind, pid))
    sheets = []
    for fid, (comp, home, away, ko, hg, ag) in res.items():
        for team, tg in ((home, hg), (away, ag)):
            if (fid, team) not in xi:
                continue
            s, b, _ = xi[(fid, team)]
            sh = Sheet(fid, team, datetime.fromisoformat(ko), s, b, team_goals=int(tg or 0))
            for kind, pid in goals.get((fid, team), []):
                if kind == "OWN_GOAL":
                    continue
                if kind == "PENALTY":
                    sh.team_pen += 1
                    if pid:
                        sh.pen_goals[pid] = sh.pen_goals.get(pid, 0) + 1
                else:
                    if pid:
                        sh.np_goals[pid] = sh.np_goals.get(pid, 0) + 1
            # goals the events list without a scorer still count for the team (not for any player)
            sh.team_np = max(sh.team_goals - sh.team_pen - sum(k == "OWN_GOAL" for k, _ in goals.get((fid, team), [])), sum(sh.np_goals.values()))
            sheets.append(sh)
    sheets.sort(key=lambda x: x.kickoff)
    roles, names, team_of = {}, {}, {}
    for pid, name, team, pos in store.db.execute("SELECT id, name, team, position FROM players").fetchall():
        roles[pid], names[pid], team_of[pid] = pos, name, team
    return ScorerData(sheets, roles, names, team_of)


@dataclass
class Params:
    half_life: float = HALF_LIFE
    k: float = K
    k_pen: float = K_PEN
    bench_prior: float = BENCH_PRIOR
    normalise: bool = True
    shrink: bool = True  # False: the player's raw share (reference "giocatore")
    role_only: bool = False  # True: the role share only (reference "ruolo")


class Tally:
    """Decayed sums per player and per team, updated match by match (O(1) per sheet line)."""

    def __init__(self, half_life: float):
        self.h = half_life
        self.p: dict[str, list[float]] = {}  # player -> [t, start G, start g, bench G, bench g, start P, start p, starts, sheets]
        self.t: dict[str, list[float]] = {}  # team -> [t, goals, pen goals]
        self.role: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # role -> start G, g, bench G, g, P, p
        self.last_xi: dict[str, list[tuple[list[str], list[str]]]] = defaultdict(list)

    @staticmethod
    def _age(vec: list[float], at: float, h: float) -> None:
        f = _decay(at - vec[0], h)
        for i in range(1, len(vec)):
            vec[i] *= f
        vec[0] = at

    def add(self, sh: Sheet, roles: dict[str, str]) -> None:
        at = sh.kickoff.timestamp() / 86400.0
        for pid in sh.starters + sh.bench:
            v = self.p.setdefault(pid, [at, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            self._age(v, at, self.h)
            r = self.role[roles.get(pid, "MID")]
            if pid in sh.starters:
                v[1] += sh.team_np
                v[2] += sh.np_goals.get(pid, 0)
                v[5] += sh.team_pen
                v[6] += sh.pen_goals.get(pid, 0)
                v[7] += 1
                r[0] += sh.team_np
                r[1] += sh.np_goals.get(pid, 0)
                r[4] += sh.team_pen
                r[5] += sh.pen_goals.get(pid, 0)
            else:
                v[3] += sh.team_np
                v[4] += sh.np_goals.get(pid, 0) + sh.pen_goals.get(pid, 0)
                r[2] += sh.team_np
                r[3] += sh.np_goals.get(pid, 0)
            v[8] += 1
        tv = self.t.setdefault(sh.team, [at, 0.0, 0.0])
        self._age(tv, at, self.h)
        tv[1] += sh.team_goals
        tv[2] += sh.team_pen
        self.last_xi[sh.team] = (self.last_xi[sh.team] + [(sh.starters, sh.bench)])[-RECENT_XI:]

    def role_shares(self) -> dict[str, tuple[float, float, float]]:
        """role -> (starter share, bench share, penalty share): the priors."""
        out = {}
        for role in ROLES:
            r = self.role[role]
            s = r[1] / r[0] if r[0] > 0 else {"GK": 0.0005, "DEF": 0.03, "MID": 0.08, "FWD": 0.2}[role]
            b = r[3] / r[2] if r[2] > 5 else s * BENCH_PRIOR
            p = r[5] / r[4] if r[4] > 0 else {"GK": 0.0, "DEF": 0.03, "MID": 0.08, "FWD": 0.15}[role]
            out[role] = (max(s, 1e-4), max(b, 1e-4), max(p, 1e-4))
        return out

    def pen_share(self, team: str) -> float:
        tv = self.t.get(team)
        if not tv:
            return PEN_PRIOR
        return (tv[2] + PEN_K * PEN_PRIOR) / (tv[1] + PEN_K)


def _share(g: float, G: float, prior: float, k: float, prm: Params) -> float:
    if prm.role_only:
        return prior
    if not prm.shrink:
        return g / G if G > 0 else prior
    return (g + k * prior) / (G + k)


@dataclass
class PlayerPrediction:
    player_id: str
    name: str
    role: str
    starter: float  # probability of starting (1/0 with the official XI)
    rate: float  # expected goals
    anytime: float
    first: float
    two_plus: float


def predict_team(tally: Tally, team: str, lam: float, lam_opp: float, prm: Params, roles: dict[str, str], names: dict[str, str],
                 starters: list[str] | None = None, bench: list[str] | None = None, squad: dict[str, float] | None = None) -> list[PlayerPrediction]:
    """Scorer probabilities for one team. With the official XI: `starters` and `bench`. Before it: `squad` {player: P(start)}
    (default: every player of the team's last RECENT_XI sheets, weighed by how often he started)."""
    priors = tally.role_shares()
    if starters is None:
        if squad is None:
            seen = tally.last_xi.get(team, [])
            n = max(len(seen), 1)
            squad = defaultdict(float)
            for s, b in seen:
                for p in s:
                    squad[p] += 1.0 / n
                for p in b:
                    squad[p] += 0.0
        members = {p: (q, 1.0 - q) for p, q in squad.items()}
    else:
        members = {p: (1.0, 0.0) for p in starters}
        members.update({p: (0.0, 1.0) for p in bench or [] if p not in members})
    raw_np, raw_pen = {}, {}
    for pid, (ps, pb) in members.items():
        role = roles.get(pid, "MID")
        s0, b0, p0 = priors.get(role, priors["MID"])
        v = tally.p.get(pid, [0.0] * 9)
        share_s = _share(v[2], v[1], s0, prm.k, prm)
        share_b = _share(v[4], v[3], b0, prm.k, prm) if not prm.role_only else b0
        pen = _share(v[6], v[5], p0, prm.k_pen, prm)
        raw_np[pid] = ps * share_s + pb * share_b
        raw_pen[pid] = ps * pen
    pen_frac = tally.pen_share(team)
    lam_pen = lam * pen_frac
    lam_np = lam * max(1.0 - pen_frac - OWN_GOAL_SHARE, 0.0)
    if prm.normalise:
        tot = sum(raw_np.values())
        if tot > 0:
            raw_np = {p: x / tot for p, x in raw_np.items()}
        tp = sum(raw_pen.values())
        if tp > 0:
            raw_pen = {p: x / tp for p, x in raw_pen.items()}
    total_rate = lam + lam_opp
    p_any_goal = 1.0 - math.exp(-total_rate)
    out = []
    for pid, (ps, _pb) in members.items():
        rate = lam_np * raw_np[pid] + lam_pen * raw_pen[pid]
        out.append(PlayerPrediction(pid, names.get(pid, pid), roles.get(pid, "MID"), ps, rate, 1.0 - math.exp(-rate),
                                    (rate / total_rate) * p_any_goal if total_rate > 0 else 0.0,
                                    1.0 - math.exp(-rate) * (1.0 + rate)))
    return sorted(out, key=lambda x: -x.rate)


# --------------------------------------------------------------------------------------------------------- replay


BIN_EDGES = (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 1.01)


@dataclass
class Score:
    n: int = 0
    ll: float = 0.0
    brier: float = 0.0
    hits: int = 0
    exp: float = 0.0
    ll_terms: list[float] = field(default_factory=list)
    bins: dict[int, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))

    def add(self, p: float, y: int) -> None:
        p = min(max(p, 1e-6), 1 - 1e-6)
        term = -(y * math.log(p) + (1 - y) * math.log(1 - p))
        self.n += 1
        self.ll += term
        self.ll_terms.append(term)
        self.brier += (p - y) ** 2
        self.hits += y
        self.exp += p
        b = self.bins[next(i for i, edge in enumerate(BIN_EDGES) if p < edge)]
        b[0] += 1
        b[1] += p
        b[2] += y


@dataclass
class ScorerReport:
    start: datetime
    end: datetime
    matches: int
    scores: dict[str, dict[str, Score]]  # variant -> "xi" / "prima" -> Score
    diffs: dict[str, tuple[float, float]]  # "modello - ruolo (xi)" -> (mean, half-width 95%)


def _diff(a: Score, b: Score) -> tuple[float, float]:
    d = [x - y for x, y in zip(a.ll_terms, b.ll_terms)]
    if not d:
        return 0.0, 0.0
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / max(len(d) - 1, 1)
    return m, 1.96 * math.sqrt(var / len(d))


VARIANTS = {"modello": Params(), "giocatore": Params(shrink=False), "ruolo": Params(role_only=True)}


def evaluate_scorers(store, provider, cfg: Config, start: datetime, end: datetime, competitions: list[str],
                     variants: dict[str, Params] | None = None, data: ScorerData | None = None, expected=None) -> ScorerReport:
    """Week-by-week replay: each match of [start, end) predicted with the Dixon-Coles expected goals of its Monday and the
    player history before its kickoff; scored on every player of the sheet (with XI) and of the squad (before XI).
    `expected(row, monday)` -> (home, away) expected goals replaces Dixon-Coles (tests)."""
    from .engine import Engine
    from .modeleval import _Frozen
    variants = variants or VARIANTS
    data = data or load_scorer_data(store, competitions)
    frozen = _Frozen(provider, end)
    eng = Engine(frozen, copy.deepcopy(cfg), use_lineups=False)
    tallies = {name: Tally(p.half_life) for name, p in variants.items()}
    scores = {name: {"xi": Score(), "prima": Score()} for name in variants}
    by_fx: dict[str, list[Sheet]] = defaultdict(list)
    for sh in data.sheets:
        by_fx[sh.fixture_id].append(sh)
    i, n_matches = 0, 0
    sheets = data.sheets
    hist = {h.fixture_id: h for h in frozen.rows}
    for fid in sorted({s.fixture_id for s in sheets if start <= s.kickoff < end}, key=lambda f: by_fx[f][0].kickoff):
        pair = by_fx[fid]
        ko = pair[0].kickoff
        while i < len(sheets) and sheets[i].kickoff < ko:
            for name, t in tallies.items():
                t.add(sheets[i], data.roles)
            i += 1
        if len(pair) != 2:
            continue
        monday = (ko - timedelta(days=ko.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        r = hist.get(fid)
        if r is None:
            continue
        if expected is not None:
            got = expected(r, monday)
        else:
            fitted = eng.fit(r.competition, monday)
            got = fitted[0].expected_goals(r.home, r.away) if fitted and fitted[0].knows(r.home) and fitted[0].knows(r.away) else None
        if got is None:
            continue
        lh, la = got
        lam = {r.home: lh, r.away: la}
        n_matches += 1
        for sh in pair:
            opp = r.away if sh.team == r.home else r.home
            scored = set(sh.np_goals) | set(sh.pen_goals)
            for name, prm in variants.items():
                t = tallies[name]
                for pp in predict_team(t, sh.team, lam[sh.team], lam[opp], prm, data.roles, data.names, sh.starters, sh.bench):
                    scores[name]["xi"].add(pp.anytime, int(pp.player_id in scored))
                for pp in predict_team(t, sh.team, lam[sh.team], lam[opp], prm, data.roles, data.names):
                    scores[name]["prima"].add(pp.anytime, int(pp.player_id in scored))
    diffs = {}
    for ref in variants:
        if ref != "modello" and "modello" in variants:
            for when in ("xi", "prima"):
                diffs[f"modello - {ref} ({when})"] = _diff(scores["modello"][when], scores[ref][when])
    return ScorerReport(start, end, n_matches, scores, diffs)


def print_scorer_eval(rep: ScorerReport) -> None:
    print(f"marcatori, replay {rep.start:%Y-%m-%d} - {rep.end:%Y-%m-%d}: {rep.matches} partite")
    for name, by in rep.scores.items():
        for when, s in by.items():
            if s.n:
                print(f"  {name:10} {when:6} giocatori {s.n:6}  log loss {s.ll / s.n:.4f}  Brier {s.brier / s.n:.4f}  "
                      f"segnano {s.hits} (attesi {s.exp:.0f})")
    for k, (m, hw) in rep.diffs.items():
        print(f"  {k}: {m:+.4f} ± {hw:.4f}" + ("  (meglio)" if m + hw < 0 else ""))
    s = rep.scores.get("modello", {}).get("xi")
    if s and s.n:
        print("  calibrazione (modello, con formazione): fascia di probabilità, giocatori, probabilità media, frequenza vera")
        for b in sorted(s.bins):
            n, p, y = s.bins[b]
            lo = BIN_EDGES[b - 1] if b else 0.0
            print(f"    {lo:.2f}-{min(BIN_EDGES[b], 1.0):.2f}  {n:6d}  {p / n:.3f}  {y / n:.3f}")
