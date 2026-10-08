"""Player cards of the coming matches (lineups tab: a click on a player opens them), published with the analysis (pub_players).

For every player who can take part in the match (our probable lineup's candidates, the stored official XI and bench), what he
does in a match he starts, from his last FotMob appearances: expected goals, shots on target / shots, fouls committed / fouls
won, and the probability of a booking and of an assist. Rates per 90 minutes are pulled toward the average of his role
(PRIOR_90[stat] spells of 90 minutes of it), so three appearances do not make a striker of a full back; then scaled to the
minutes he plays when he starts. Probabilities: negative binomial with shape DISP. The settings come from the replay
(player-eval --tune, 35,000 starts of 2026): the rare events (booking, assist) need a heavy role weight, the frequent ones
little; every line (shots 1+/2+/3+, on target 1+/2+, fouls 1+/2+, booked, assist) beat the earlier 10 appearances / weight 3.
Goalkeepers get their own card: shots on target faced, goals conceded and saves in their full matches, FotMob's own numbers
where read (from 2026-10-07 on) and before that the opponent's shots on target (team match stats) and our result, saves being
the difference; and the booking probability. FotMob players are matched to ours through fotmob_player_links. No request.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

LAST = 60            # appearances read per player (most recent first)
SINCE = timedelta(days=400)
# 90-minute spells of the role average added to each player's own minutes, per stat (xG not replayed: as shots on target)
PRIOR_90 = {"xg": 5.0, "shots_on": 5.0, "shots": 3.0, "fouls_committed": 5.0, "fouls_drawn": 3.0, "cards": 20.0, "assists": 40.0}
DISP = 4.0           # negative binomial shape of the counts
# match context (playercontext.py) weights: player-eval --tune, the opponent at 3/4 and the venue in full beat the player's
# average match on all 11 lines; the referee (known for 14% of the 2026 starts) changed nothing yet
CONTEXT = {"opp": 0.75, "venue": 1.0, "ref": 0.0}
MIN_MINUTES = 90     # below this (all appearances together) no card: too little to say anything
START_MINUTES = 80.0  # minutes of a start when the player has no start of his own in the window
STATS = ("xg", "shots_on", "shots", "fouls_committed", "fouls_drawn", "cards", "assists")
# where each stat sits in a row of player_cards' query
AT = {"xg": 2, "shots_on": 3, "shots": 4, "fouls_committed": 5, "fouls_drawn": 6, "cards": 7, "assists": 16}


def p_any(rate: float, disp: float = DISP) -> float:
    """P(at least one) for a count with mean `rate`, negative binomial with shape `disp` (Poisson when 0)."""
    if rate <= 0:
        return 0.0
    return 1.0 - ((disp / (disp + rate)) ** disp if disp > 0 else math.exp(-rate))


def expected_counts(apps: list[tuple[float, int, dict]], prior: dict[str, float], last: int = LAST,
                    prior_90: dict[str, float] | None = None) -> tuple[dict[str, float | None], float] | None:
    """Expected counts in a start from the player's appearances (minutes, starter, {stat: value or None}; most recent first)
    and his role's averages per 90 minutes, and the minutes of a start; None below MIN_MINUTES."""
    prior_90 = prior_90 or PRIOR_90
    a = apps[:last]
    if sum(r[0] for r in a) < MIN_MINUTES:
        return None
    starts = [r[0] for r in a if r[1]]
    mins = min(90.0, sum(starts) / len(starts)) if starts else START_MINUTES
    out: dict[str, float | None] = {}
    for k in STATS:
        seen = [(r[2][k], r[0]) for r in a if r[2].get(k) is not None]
        if not seen:
            out[k] = None
            continue
        m = sum(x[1] for x in seen)
        w = prior_90.get(k, 3.0)
        rate = (sum(x[0] for x in seen) + prior.get(k, 0.0) * w) / (m / 90 + w)  # per 90 minutes
        out[k] = rate * mins / 90
    return out, mins


def _chunks(xs: list, n: int = 400):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def match_context(store, now: datetime):
    """MatchContext fed with the team totals of the FotMob matches of the last SINCE days, oldest first."""
    from .playercontext import CTX_STATS, MatchContext
    ctx = MatchContext()
    rows = store.db.execute(
        "SELECT s.fixture_id, s.team, r.competition, r.home, r.away, SUM(s.shots), SUM(s.shots_on), SUM(s.fouls_committed), "
        "SUM(s.fouls_drawn), SUM(CASE WHEN COALESCE(s.yellow, 0) + COALESCE(s.red, 0) > 0 THEN 1 ELSE 0 END), SUM(s.assists), SUM(s.goals) "
        "FROM fotmob_player_stats s JOIN results r ON r.fixture_id = s.fixture_id WHERE s.minutes > 0 AND r.kickoff >= ? AND r.kickoff < ? "
        "GROUP BY s.fixture_id, s.team ORDER BY r.kickoff", ((now - SINCE).isoformat(), now.isoformat())).fetchall()
    by_fx: dict[str, tuple] = {}
    for fid, team, comp, home, away, *vals in rows:
        info = by_fx.setdefault(fid, (comp, home, away, {}))
        info[3][team] = {k: float(v or 0) for k, v in zip(CTX_STATS, vals)}
    for comp, home, away, totals in by_fx.values():  # dicts keep the kickoff order of the query
        ctx.add(comp, home, away, totals)
    return ctx


def player_cards(store, pools: dict[str, set[str]], now: datetime, team_stats: dict | None = None,
                 fixtures: dict[str, tuple[str, str, str]] | None = None) -> dict[str, str]:
    """fixture id -> JSON {player id: card} for the players of `pools` (fixture id -> our player ids). Outfield card: "xg",
    "sot", "sh", "fc", "fd" per match started, "cg" = probability of at least one booking, "n" appearances, "min" minutes of a
    start. Goalkeeper card ("gk": 1): "gc" goals conceded and "sv" saves per full match, "cs" share of clean sheets, "cg", "n".
    team_stats: fixture id -> {stat: (home, away)} (provider.match_stat_values), for the saves. fixtures: fixture id ->
    (competition, home, away): the outfield numbers then follow the opponent and the venue (CONTEXT)."""
    wanted = sorted(set().union(*pools.values())) if pools else []
    if not wanted:
        return {}
    fm_of: dict[str, list[str]] = {}
    for part in _chunks(wanted):
        for f, g in store.db.execute(f"SELECT fotmob_id, goal_id FROM fotmob_player_links WHERE goal_id IN ({','.join('?' * len(part))})",
                                     part).fetchall():
            fm_of.setdefault(g, []).append(f)
    from .fotmobcollector import add_gk_columns
    add_gk_columns(store)
    fm_ids = sorted({f for fs in fm_of.values() for f in fs} | {p for p in wanted if p.startswith("fotmob:")})
    rows: dict[str, list[tuple]] = {}
    since = (now - SINCE).isoformat()
    for part in _chunks(fm_ids):
        for r in store.db.execute(
                "SELECT s.player_id, s.minutes, s.starter, s.xg, s.shots_on, s.shots, s.fouls_committed, s.fouls_drawn, "
                "CASE WHEN COALESCE(s.yellow, 0) + COALESCE(s.red, 0) > 0 THEN 1 ELSE 0 END, r.kickoff, s.fixture_id, s.team = r.home, "
                "r.home_goals, r.away_goals, s.saves, s.goals_conceded, s.shots_on_faced, s.assists, s.team "
                "FROM fotmob_player_stats s JOIN results r ON r.fixture_id = s.fixture_id "
                f"WHERE s.player_id IN ({','.join('?' * len(part))}) AND s.minutes > 0 AND r.kickoff >= ? AND r.kickoff < ?",
                (*part, since, now.isoformat())).fetchall():
            rows.setdefault(r[0], []).append(r[1:])
    roles = {}
    for part in _chunks(wanted):
        roles.update(store.db.execute(f"SELECT id, position FROM players WHERE id IN ({','.join('?' * len(part))})", part).fetchall())

    # each player's last LAST appearances, whichever FotMob id they were recorded under
    apps: dict[str, list[tuple]] = {}
    for g in wanted:
        mine = [r for f in (fm_of.get(g, []) + ([g] if g.startswith("fotmob:") else [])) for r in rows.get(f, [])]
        if mine:
            apps[g] = sorted(mine, key=lambda r: r[8], reverse=True)[:LAST]

    # role averages per 90 minutes over everyone read (xG only where FotMob gives it)
    prior: dict[str, dict[str, float]] = {}
    for role in {roles.get(g, "MID") for g in apps}:
        acc = {k: [0.0, 0.0] for k in STATS}
        for g, a in apps.items():
            if roles.get(g, "MID") != role:
                continue
            for r in a:
                for k in STATS:
                    if r[AT[k]] is not None:
                        acc[k][0] += r[AT[k]]
                        acc[k][1] += r[0]
        prior[role] = {k: (s_ / m * 90 if m else 0.0) for k, (s_, m) in acc.items()}

    keepers: dict[str, dict] = {}
    base: dict[str, tuple[dict, float, int, str]] = {}  # outfield: expected counts, minutes of a start, appearances, team
    for g, a in apps.items():
        total = sum(r[0] for r in a)
        if total < MIN_MINUTES:
            continue
        if roles.get(g) == "GK":
            keepers[g] = _keeper(a, prior["GK"]["cards"], team_stats or {})
            continue
        got = expected_counts([(r[0], r[1], {k: r[AT[k]] for k in STATS}) for r in a], prior[roles.get(g, "MID")])
        if got is not None:
            base[g] = (got[0], got[1], len(a), a[0][17])

    from .playercontext import apply
    ctx = match_context(store, now) if fixtures else None
    out = {}
    for fid, ids in pools.items():
        side = {}
        if ctx is not None and fid in fixtures:
            comp, home, away = fixtures[fid]
            side = {home: ctx.factors(away, comp, True), away: ctx.factors(home, comp, False)}
        mine = {}
        for g in ids:
            if g in keepers:
                mine[g] = keepers[g]
            elif g in base:
                exp, mins, n, team = base[g]
                if team in side:
                    exp = apply(exp, side[team], CONTEXT["opp"], CONTEXT["venue"], CONTEXT["ref"])
                mine[g] = {"xg": None if exp["xg"] is None else round(exp["xg"], 2), "sot": round(exp["shots_on"] or 0, 2),
                           "sh": round(exp["shots"] or 0, 2), "fc": round(exp["fouls_committed"] or 0, 2),
                           "fd": round(exp["fouls_drawn"] or 0, 2), "cg": round(p_any(exp["cards"] or 0), 3),
                           "as": round(p_any(exp["assists"] or 0), 3), "n": n, "min": round(mins)}
        if mine:
            out[fid] = json.dumps(mine, ensure_ascii=False, separators=(",", ":"))
    return out


def _keeper(a: list[tuple], card_prior: float, team_stats: dict) -> dict:
    """Per match he played (nearly) whole: shots on target faced, goals conceded and saves. FotMob's own numbers when the
    row has them (r[13:16]: saves, goals conceded, shots on target faced), else the team's: the opponent's shots on target
    (match stats), the goals of our result, saves = shots on target faced - goals conceded."""
    full = [r for r in a if r[0] >= 80]
    ts, gc, sv = [], [], []
    for r in full:
        fm_sv, fm_gc, fm_ts = r[13], r[14], r[15]
        conceded = fm_gc if fm_gc is not None else (None if r[11] is None or r[12] is None else (r[12] if r[10] else r[11]))
        sot = (team_stats.get(r[9], {}).get("shots_on_target") or team_stats.get(r[9], {}).get("shots_on_goal"))
        faced = fm_ts if fm_ts is not None else (None if not sot else (sot[1] if r[10] else sot[0]))
        if fm_ts is None and fm_sv is not None and conceded is not None:
            faced = fm_sv + conceded
        saves = fm_sv if fm_sv is not None else (None if faced is None or conceded is None else max(0.0, faced - conceded))
        if conceded is not None:
            gc.append(conceded)
        if faced is not None:
            ts.append(faced)
        if saves is not None:
            sv.append(saves)
    mins = sum(r[0] for r in a)
    w = PRIOR_90["cards"]
    rate = (sum(r[7] for r in a) + card_prior * w) / (mins / 90 + w)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    return {"gk": 1, "ts": avg(ts), "gc": avg(gc), "sv": avg(sv),
            "cs": round(sum(1 for c in gc if c == 0) / len(gc), 3) if gc else None, "cg": round(p_any(rate), 3), "n": len(a)}


def match_pools(prov, fixtures, xi) -> dict[str, set[str]]:
    """fixture id -> the players who can take part: our probable lineup's candidates (when the lineup data is loaded) and the
    stored lineups' starters and bench."""
    from .probable import candidates
    out: dict[str, set[str]] = {}
    for f in fixtures:
        ids: set[str] = set()
        for l in prov.get_lineups(f.id):
            ids.update(prov._to_goal(l.starters + l.bench))
        if xi is not None:
            data, _ = xi
            for team in (f.home, f.away):
                ids.update(c.player_id for c in candidates(data, team, f.kickoff, f.competition, f.id, live=True))
        ids = {i for i in ids if i.startswith(("goal:", "fotmob:"))}
        if ids:
            out[f.id] = ids
    return out
