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

from .probable import XiModel, fit_until, load_xi_data, predict as predict_xi, training_rows
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


def team_scorers(store, prov, fixtures, xg: dict[str, tuple[float, float]], now: datetime) -> dict[str, dict[str, TeamScorers]]:
    """fixture id -> team -> TeamScorers, for the fixtures with expected goals whose teams have MIN_SHEETS sheets."""
    data = load_scorer_data(store)
    tally = Tally(HALF_LIFE)
    n_sheets: dict[str, int] = {}
    for sh in data.sheets:
        if sh.kickoff >= now:
            break
        sh.starters, sh.bench = prov._to_goal(sh.starters), prov._to_goal(sh.bench)
        tally.add(sh, data.roles)
        n_sheets[sh.team] = n_sheets.get(sh.team, 0) + 1
    xi_data = model = None
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
                    xi_data = load_xi_data(store, prov)
                    model = xi_model(store, xi_data, now)
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
