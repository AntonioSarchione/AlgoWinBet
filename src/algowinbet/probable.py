"""Probable lineups (our own, from data we already hold): each player's probability of starting and the expected XI.

For every team and match, the candidates are the players of the team's last sheets (official XI and bench of its previous
matches, every competition). A logistic model turns their recent history into a probability of starting:

  decayed start share (last match weighs most), starts in the last 10, started / on the bench in the last match,
  starts in a row, goalkeeper, a cup match ahead or a short rest after a start (rotation), doubtful in the injury list.

Players listed out or suspended (API-Football, read before the XI time) get 0. The probabilities of a team are then scaled so
that they add up to 1 goalkeeper and 10 outfield players (each capped at 1), and the expected XI is the top goalkeeper and the
top 10 outfield players.

The model is refitted each week on the matches before it (walk-forward); `evaluate_xi` scores it against the official XI:
starters guessed out of 11, log loss of the start probabilities, next to "same XI as last match" and the plain start rate of
the last 8 matches (what the match model used so far).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .meta import group_of

DECAY = 0.7  # weight of each older match in the decayed start share
WINDOW = 10  # sheets looked back
CANDIDATE_SHEETS = 6  # a player must be on one of the team's last 6 sheets to be a candidate
SHORT_REST = 4.0  # days
XI_TIME = timedelta(minutes=60)  # injury news read after this point before kickoff are not used (the XI is out)
L2 = 1.0
FEATURES = ("decayed", "rate10", "started_last", "bench_last", "streak", "gk", "cup_x_started", "rest_x_started", "doubtful")


@dataclass
class TeamSheet:
    fixture_id: str
    team: str
    competition: str
    kickoff: datetime
    starters: list[str]
    bench: list[str]


@dataclass
class XiData:
    sheets: dict[str, list[TeamSheet]]  # team -> sheets in time order
    roles: dict[str, str]
    names: dict[str, str]
    team_of: dict[str, str]
    status: dict[tuple[str, str], list[tuple[str, datetime]]]  # (fixture, player) -> [(status, observed_at)]


def load_xi_data(store, provider) -> XiData:
    """Every confirmed XI with its kickoff (results, else the calendar), the players table and the availability rows."""
    when: dict[str, tuple[str, datetime]] = {}
    for fid, comp, ko in store.db.execute("SELECT fixture_id, competition, kickoff FROM results").fetchall():
        when[fid] = (comp, datetime.fromisoformat(ko))
    for f in provider.list_fixtures(None, datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)):
        when.setdefault(f.id, (f.competition, f.kickoff))
    best: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for fid, team, s, b in store.db.execute("SELECT fixture_id, team, starters, bench FROM lineups WHERE status = 'confirmed' "
                                            "ORDER BY observed_at").fetchall():
        if fid in when:
            best[(fid, team)] = (provider._to_goal(json.loads(s or "[]")), provider._to_goal(json.loads(b or "[]")))
    sheets: dict[str, list[TeamSheet]] = defaultdict(list)
    for (fid, team), (s, b) in best.items():
        comp, ko = when[fid]
        if len(s) >= 10:
            sheets[team].append(TeamSheet(fid, team, comp, ko, s, b))
    for team in sheets:
        sheets[team].sort(key=lambda x: x.kickoff)
    roles, names, team_of = {}, {}, {}
    for pid, name, team, pos in store.db.execute("SELECT id, name, team, position FROM players").fetchall():
        roles[pid], names[pid], team_of[pid] = pos, name, team
    status: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
    for fid, pid, st, at in store.db.execute("SELECT fixture_id, player_id, status, observed_at FROM player_status").fetchall():
        for g in provider._to_goal([pid]):
            status[(fid, g)].append((st, datetime.fromisoformat(at)))
    return XiData(dict(sheets), roles, names, team_of, dict(status))


def _status(data: XiData, fid: str, pid: str, before: datetime) -> str | None:
    rows = [r for r in data.status.get((fid, pid), []) if r[1] <= before]
    return max(rows, key=lambda r: r[1])[0] if rows else None


@dataclass
class Candidate:
    player_id: str
    x: list[float]
    out: bool  # listed out / suspended


def candidates(data: XiData, team: str, kickoff: datetime, competition: str, fixture_id: str | None,
               live: bool = False) -> list[Candidate]:
    """Feature rows of the team's candidates for a match at `kickoff` (only sheets before it)."""
    past = [s for s in data.sheets.get(team, []) if s.kickoff < kickoff][-WINDOW:]
    if not past:
        return []
    recent = past[-CANDIDATE_SHEETS:]
    pool = {p for s in recent for p in s.starters + s.bench}
    if live:  # a player the players table now places in another team has moved
        pool = {p for p in pool if data.team_of.get(p, team) == team}
    last = past[-1]
    rest = (kickoff - last.kickoff).total_seconds() / 86400.0
    cup = group_of(competition) != "campionati"
    weights = [DECAY ** k for k in range(len(past))][::-1]  # oldest first, last match weight 1
    wsum = sum(weights)
    out = []
    for pid in sorted(pool):
        started = [pid in s.starters for s in past]
        streak = 0
        for st in reversed(started):
            if not st:
                break
            streak += 1
        dec = sum(w for w, st in zip(weights, started) if st) / wsum
        st_last = float(started[-1])
        status = _status(data, fixture_id, pid, kickoff - XI_TIME) if fixture_id else None
        x = [dec, sum(started) / len(past), st_last, float(pid in last.bench), min(streak, 5) / 5.0,
             float(data.roles.get(pid) == "GK"), float(cup) * st_last, float(rest < SHORT_REST) * st_last, float(status == "DOUBTFUL")]
        out.append(Candidate(pid, x, status in ("OUT", "SUSPENDED")))
    return out


class XiModel:
    """Logistic regression (Newton steps, small L2) on the candidates' features."""

    def __init__(self):
        self.w: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "XiModel":
        A = np.hstack([np.ones((len(X), 1)), X])
        w = np.zeros(A.shape[1])
        reg = np.eye(A.shape[1]) * L2
        reg[0, 0] = 0.0
        for _ in range(30):
            p = 1.0 / (1.0 + np.exp(-A @ w))
            g = A.T @ (p - y) + reg @ w
            H = (A * (p * (1 - p))[:, None]).T @ A + reg
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        self.w = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.w is None:
            return X[:, 0] if len(X) else X  # untrained: the decayed start share
        return 1.0 / (1.0 + np.exp(-(np.hstack([np.ones((len(X), 1)), X]) @ self.w)))


def scale_to_xi(probs: dict[str, float], roles: dict[str, str]) -> dict[str, float]:
    """1 goalkeeper and 10 outfield starters in expectation (each probability capped at 1)."""
    out = {}
    for group, total in ((True, 1.0), (False, 10.0)):
        mine = {p: v for p, v in probs.items() if (roles.get(p) == "GK") == group}
        for _ in range(20):  # capping moves mass: a few rounds settle it
            free = {p: v for p, v in mine.items() if v < 1.0}
            capped = len(mine) - len(free)
            s = sum(free.values())
            if s <= 0:
                break
            k = (total - capped) / s
            mine = {p: (min(1.0, v * k) if p in free else 1.0) for p, v in mine.items()}
            if abs(sum(mine.values()) - total) < 1e-9:
                break
        out.update(mine)
    return out


def expected_xi(probs: dict[str, float], roles: dict[str, str]) -> list[str]:
    gks = sorted((p for p in probs if roles.get(p) == "GK"), key=lambda p: -probs[p])[:1]
    field_ = sorted((p for p in probs if roles.get(p) != "GK"), key=lambda p: -probs[p])[:10]
    return gks + field_


@dataclass
class TeamXi:
    team: str
    probs: dict[str, float]  # player -> P(start)
    xi: list[str]


def predict(model: XiModel, data: XiData, team: str, kickoff: datetime, competition: str, fixture_id: str | None,
            live: bool = False) -> TeamXi | None:
    cands = candidates(data, team, kickoff, competition, fixture_id, live)
    if not cands:
        return None
    raw = model.predict(np.array([c.x for c in cands]))
    probs = {c.player_id: (0.0 if c.out else float(p)) for c, p in zip(cands, raw)}
    probs = scale_to_xi(probs, data.roles)
    return TeamXi(team, probs, expected_xi(probs, data.roles))


# --------------------------------------------------------------------------------------------------------- replay


@dataclass
class XiScore:
    teams: int = 0
    guessed: int = 0  # starters guessed (of the official ones)
    ll: float = 0.0
    rows: int = 0
    per_team: list[int] = field(default_factory=list)

    def line(self) -> str:
        return (f"{self.guessed / max(self.teams, 1):.2f}/11 indovinati in media"
                + (f", log loss {self.ll / self.rows:.4f}" if self.rows else "")
                + f", 10-11 giusti nel {sum(g >= 10 for g in self.per_team) / max(self.teams, 1):.0%} dei casi")


@dataclass
class XiReport:
    start: datetime
    end: datetime
    scores: dict[str, XiScore]
    weights: dict[str, float]


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def evaluate_xi(data: XiData, start: datetime, end: datetime, min_history: int = 3, competitions: list[str] | None = None) -> XiReport:
    """Every team sheet of [start, end) predicted before kickoff, model refitted each Monday on all earlier sheets."""
    targets = sorted(((s.kickoff, team, s) for team, ss in data.sheets.items() for s in ss
                      if start <= s.kickoff < end and (not competitions or s.competition in competitions)), key=lambda t: t[0])
    scores = {"modello": XiScore(), "stessa dell'ultima": XiScore(), "titolarità ultime 8": XiScore()}
    # the features of a sheet never depend on the refit date: computed once, filtered by kickoff at each refit
    rows: list[tuple[datetime, list[float], float]] = []
    for t2, ss in data.sheets.items():
        for k, s in enumerate(ss):
            if k >= min_history:
                rows += [(s.kickoff, c.x, float(c.player_id in s.starters)) for c in candidates(data, t2, s.kickoff, s.competition, s.fixture_id)]
    rows.sort(key=lambda r: r[0])
    model, fitted_for = XiModel(), None
    for ko, team, sh in targets:
        monday = (ko - timedelta(days=ko.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        if fitted_for != monday:
            train = [r for r in rows if r[0] < monday]
            if len(train) > 500:
                model = XiModel().fit(np.array([r[1] for r in train]), np.array([r[2] for r in train]))
            fitted_for = monday
        past = [s for s in data.sheets[team] if s.kickoff < ko]
        if len(past) < min_history:
            continue
        actual = set(sh.starters)
        pred = predict(model, data, team, ko, sh.competition, sh.fixture_id)
        if pred is None:
            continue
        last = past[-1].starters
        last8 = past[-8:]
        rate8 = {p: sum(p in s.starters for s in last8) / len(last8) for s in last8 for p in s.starters + s.bench}
        rate8 = scale_to_xi(rate8, data.roles)
        for name, probs, xi in (("modello", pred.probs, pred.xi), ("stessa dell'ultima", {p: 1.0 for p in last}, last),
                                ("titolarità ultime 8", rate8, expected_xi(rate8, data.roles))):
            sc = scores[name]
            g = len(actual & set(xi))
            sc.teams += 1
            sc.guessed += g
            sc.per_team.append(g)
            if name != "stessa dell'ultima":
                for p in set(probs) | actual:
                    sc.ll += _ll(probs.get(p, 0.0), int(p in actual))
                    sc.rows += 1
    return XiReport(start, end, scores, dict(zip(("intercetta",) + FEATURES, model.w.tolist())) if model.w is not None else {})


def print_xi_eval(rep: XiReport) -> None:
    n = next(iter(rep.scores.values())).teams
    print(f"probabili formazioni, replay {rep.start:%Y-%m-%d} - {rep.end:%Y-%m-%d}: {n} formazioni")
    for name, sc in rep.scores.items():
        print(f"  {name:22} {sc.line()}")
    if rep.weights:
        print("  pesi del modello: " + ", ".join(f"{k} {v:+.2f}" for k, v in rep.weights.items()))
