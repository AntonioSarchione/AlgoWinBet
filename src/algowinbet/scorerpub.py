"""Goalscorer table of the upcoming matches (Fase 9), published with the analysis (pub_fixtures.scorers).

For each team: the model's expected goals (Dixon-Coles), split among its players by their decayed share of the team's goals
(scorers.py). The players come from the official XI when stored, otherwise from our probable lineup (probable.py: each
player's probability of starting). No API request: everything is read from the stored lineups, events and players.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .probable import XiModel, _status, fit_until, load_xi_data, predict as predict_xi, training_rows
from .scorers import HALF_LIFE, Params, Tally, load_scorer_data, predict_team

MIN_SHEETS = 5      # team sheets with goal events needed before its players are priced
TOP = 14            # players listed per team
MIN_ANYTIME = 0.01  # below this a player is not listed
XI_MODEL_JOB = "xi-model"  # jobs row: the probable-lineup weights of the current week (refit weekly, not at every publication)


@dataclass
class TeamScorers:
    state: str  # "ufficiale" (official XI) or "probabile" (our probable lineup)
    sheets: int  # team sheets with goal events the shares come from
    players: list[dict]


def _week(t: datetime) -> datetime:
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def xi_model(store, xi_data, now: datetime) -> XiModel:
    """The probable-lineup model fitted on every sheet before this Monday; the weights are kept in the jobs table, so the fit
    (a few seconds over thousands of sheets) runs once a week instead of at every publication."""
    monday = _week(now)
    name = f"{XI_MODEL_JOB}:{monday:%Y-%m-%d}"
    row = store.db.execute("SELECT detail FROM jobs WHERE name = ?", (name,)).fetchone()
    m = XiModel()
    if row and row[0]:
        m.w = np.array(json.loads(row[0]))
        return m
    fitted = fit_until(training_rows(xi_data), monday)
    if fitted is None:
        return m  # untrained: the decayed start share
    store.mark_job(name, now, json.dumps([round(float(x), 6) for x in fitted.w]))
    return fitted


ALTERNATES = 5     # players listed after the probable XI
ALT_MIN = 0.15     # start probability an alternative needs to be listed
ROLE_ORDER = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}


def load_xi(store, prov, now: datetime):
    data = load_xi_data(store, prov)
    return data, xi_model(store, data, now)


def probable_lineups(store, prov, fixtures, now: datetime, xi=None) -> tuple[dict[str, str], object]:
    """fixture id -> JSON of our probable XI per team still without an official one (the lineups tab shows it until the
    official XI arrives): {team: {"formation": "4-3-3", "xi": [[id, name, role, p, status]], "alt": [...]}}, the XI ordered
    goalkeeper, defenders, midfielders, forwards; p = probability of starting, status = the player's latest reported status
    (OUT, SUSPENDED, DOUBTFUL) or null. Returns also the loaded lineup data and model, for the goalscorer table."""
    out: dict[str, str] = {}
    for f in fixtures:
        official = {l.team for l in prov.get_lineups(f.id) if l.status == "confirmed" and len(l.starters) >= 11}
        teams = {}
        for team in (f.home, f.away):
            if team in official:
                continue
            if xi is None:
                xi = load_xi(store, prov, now)
            data, model = xi
            got = predict_xi(model, data, team, f.kickoff, f.competition, f.id, live=True)
            if got is None or len(got.xi) < 10:  # a team with no goalkeeper known still shows its ten
                continue
            role = lambda pid: data.roles.get(pid, "MID")  # noqa: E731
            row = lambda pid: [pid, data.names.get(pid, pid.split(":")[-1]), role(pid), round(got.probs[pid], 2),  # noqa: E731
                               (lambda st: st if st and st != "AVAILABLE" else None)(_status(data, f.id, pid, now))]
            ids = sorted(got.xi, key=lambda pid: (ROLE_ORDER.get(role(pid), 2), -got.probs[pid]))
            counts = [sum(1 for pid in ids if role(pid) == r) for r in ("DEF", "MID", "FWD")]
            alt = sorted((pid for pid in got.probs if pid not in got.xi and got.probs[pid] >= ALT_MIN), key=lambda pid: -got.probs[pid])
            teams[team] = {"formation": "-".join(str(c) for c in counts if c), "xi": [row(pid) for pid in ids],
                           "alt": [row(pid) for pid in alt[:ALTERNATES]]}
        if teams:
            out[f.id] = json.dumps(teams, ensure_ascii=False)
    return out, xi


def team_scorers(store, prov, fixtures, xg: dict[str, tuple[float, float]], now: datetime, xi=None) -> dict[str, dict[str, TeamScorers]]:
    """fixture id -> team -> TeamScorers, for the fixtures with expected goals whose teams have MIN_SHEETS sheets. xi: the
    probable-lineup data and model when already loaded (probable_lineups)."""
    data = load_scorer_data(store)
    tally = Tally(HALF_LIFE)
    n_sheets: dict[str, int] = {}
    for sh in data.sheets:
        if sh.kickoff >= now:
            break
        sh.starters, sh.bench = prov._to_goal(sh.starters), prov._to_goal(sh.bench)
        tally.add(sh, data.roles)
        n_sheets[sh.team] = n_sheets.get(sh.team, 0) + 1
    xi_data, model = xi if xi is not None else (None, None)
    prm = Params()
    out: dict[str, dict[str, TeamScorers]] = {}
    for f in fixtures:
        if f.id not in xg or min(n_sheets.get(f.home, 0), n_sheets.get(f.away, 0)) < MIN_SHEETS:
            continue
        lam = {f.home: xg[f.id][0], f.away: xg[f.id][1]}
        official = {l.team: l for l in prov.get_lineups(f.id) if l.status == "confirmed" and len(l.starters) >= 11}
        teams = {}
        for team, opp in ((f.home, f.away), (f.away, f.home)):
            if team in official:
                l = official[team]
                preds = predict_team(tally, team, lam[team], lam[opp], prm, data.roles, data.names,
                                     starters=prov._to_goal(l.starters), bench=prov._to_goal(l.bench))
                state = "ufficiale"
            else:
                if xi_data is None:
                    xi_data, model = load_xi(store, prov, now)
                got = predict_xi(model, xi_data, team, f.kickoff, f.competition, f.id, live=True)
                preds = predict_team(tally, team, lam[team], lam[opp], prm, data.roles, data.names,
                                     squad=got.probs if got else None)
                state = "probabile"
            rows = [{"n": p.name if p.name != p.player_id else p.player_id.split(":")[-1], "r": p.role, "s": round(p.starter, 2),
                     "a": round(p.anytime, 4), "f": round(p.first, 4), "d": round(p.two_plus, 4)}
                    for p in preds if p.anytime >= MIN_ANYTIME][:TOP]
            if rows:
                teams[team] = TeamScorers(state, n_sheets.get(team, 0), rows)
        if teams:
            out[f.id] = teams
    return out


def scorers_json(teams: dict[str, TeamScorers] | None) -> str | None:
    if not teams:
        return None
    return json.dumps({t: {"state": s.state, "sheets": s.sheets, "players": s.players} for t, s in teams.items()}, ensure_ascii=False)
