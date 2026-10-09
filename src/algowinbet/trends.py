"""Statistical streaks of a match ("ritardi"), published with the analysis (pub_fixtures.trends).

Five lists per match, each sorted by how rare the streak is:
  home / away ... the team's last 10 matches (any competition): runs (won 4 in a row, no clean sheet in 7) and counts (Over 2.5
                  in 8 of the last 10)
  h2h ........... the last meetings of the two teams (up to 10, as many as the history holds), from the home side's view
  scorers ....... players of the two teams who have not scored for several starts while the scorer model expected goals from
                  them, or who keep scoring; shots on target added when API-Football player stats exist
  discipline .... players with many fouls and no booking for several matches (API-Football player stats only)

Rarity: the probability of the streak under the base rates of each match's own competition (league averages of the last two
seasons), e.g. 0.30^4 for four losses in a row. A streak never makes the event more likely by itself; next to each one stands
the model's probability for the next match, and the expected goals / shots behind it when known (bad luck shows there).
No API request: everything comes from the stored results, match stats, events and player stats.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

LAST = 10
TOP = 10
MIN_RUN = 3
MAX_P = 0.15  # streaks more likely than this are not interesting
STATS = ("expected_goals", "shots_on_target", "shots_on_goal", "corners", "yellow_cards", "red_cards", "fouls")


@dataclass
class View:
    """One past match from one team's side."""
    fid: str
    comp: str
    ko: datetime
    opp: str
    home: bool
    gf: int
    ga: int
    st: dict  # stat -> (for, against)


@dataclass
class Event:
    key: str
    yes: str     # text of a run of occurrences ("Vince"), followed by "da N partite"
    no: str      # text of a run without it ("Senza vittorie")
    count: str   # text of a count ("vittorie")
    test: Callable[[View], bool | None]
    model: Callable[[bool], str | None] | None = None  # label of the model market for the next match (home side?)


def _cards(v: View) -> float | None:
    y, r = v.st.get("yellow_cards"), v.st.get("red_cards")
    if y is None:
        return None
    return y[0] + y[1] + (r[0] + r[1] if r else 0.0)


def _corners(v: View) -> float | None:
    c = v.st.get("corners")
    return None if c is None else c[0] + c[1]


EVENTS = (
    Event("win", "Vince", "Senza vittorie", "vittorie", lambda v: v.gf > v.ga, lambda h: "1" if h else "2"),
    Event("draw", "Pareggia", "Senza pareggi", "pareggi", lambda v: v.gf == v.ga, lambda h: "X"),
    Event("loss", "Perde", "Senza sconfitte", "sconfitte", lambda v: v.gf < v.ga, lambda h: "2" if h else "1"),
    Event("scores", "Segna", "Senza segnare", "partite a segno", lambda v: v.gf > 0, lambda h: f"Over 0.5 {'casa' if h else 'ospite'}"),
    Event("clean", "Porta inviolata", "Subisce goal", "porte inviolate", lambda v: v.ga == 0, lambda h: f"!Over 0.5 {'ospite' if h else 'casa'}"),
    Event("o15", "Over 1.5", "Under 1.5", "Over 1.5", lambda v: v.gf + v.ga > 1.5, lambda h: "Over 1.5"),
    Event("o25", "Over 2.5", "Under 2.5", "Over 2.5", lambda v: v.gf + v.ga > 2.5, lambda h: "Over 2.5"),
    Event("o35", "Over 3.5", "Under 3.5", "Over 3.5", lambda v: v.gf + v.ga > 3.5, lambda h: "Over 3.5"),
    Event("btts", "Goal (entrambe a segno)", "NoGoal", "Goal", lambda v: v.gf > 0 and v.ga > 0, lambda h: "Goal"),
    Event("corners", "Over 9.5 corner", "Under 9.5 corner", "Over 9.5 corner",
          lambda v: None if _corners(v) is None else _corners(v) > 9.5),
    Event("cards", "Over 4.5 cartellini", "Under 4.5 cartellini", "Over 4.5 cartellini",
          lambda v: None if _cards(v) is None else _cards(v) > 4.5),
)
TEAM_EVENTS = {"win", "loss", "scores", "clean"}  # said of one side: named in the head-to-head list


def _views(results, stats: dict) -> dict[str, list[View]]:
    out: dict[str, list[View]] = defaultdict(list)
    for r in results:
        s = stats.get(r.fixture_id, {})
        out[r.home].append(View(r.fixture_id, r.competition, r.kickoff, r.away, True, r.home_goals, r.away_goals,
                                {k: v for k, v in s.items()}))
        out[r.away].append(View(r.fixture_id, r.competition, r.kickoff, r.home, False, r.away_goals, r.home_goals,
                                {k: (v[1], v[0]) for k, v in s.items()}))
    return out


def base_rates(views: dict[str, list[View]], since: datetime) -> dict[tuple[str, str], float]:
    """(competition, event) -> share of team-matches where it happened since `since` (both sides of every match)."""
    hit: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for vs in views.values():
        for v in vs:
            if v.ko < since:
                continue
            for e in EVENTS:
                y = e.test(v)
                if y is None:
                    continue
                c = hit[(v.comp, e.key)]
                c[0] += int(y)
                c[1] += 1
    return {k: (a + 1) / (n + 2) for k, (a, n) in hit.items() if n >= 40}


def _binom_tail(n: int, x: int, p: float, upper: bool) -> float:
    ks = range(x, n + 1) if upper else range(0, x + 1)
    return sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in ks)


def _xg(vs: list[View]) -> str | None:
    xs = [v.st["expected_goals"] for v in vs if v.st.get("expected_goals") and v.st["expected_goals"][0] is not None]
    if len(xs) < max(2, len(vs) // 2):
        return None
    return f"xG medio {sum(x[0] for x in xs) / len(xs):.2f} fatti, {sum(x[1] for x in xs) / len(xs):.2f} subiti"


def _model(markets: dict[str, float], label: str) -> float | None:
    """The model market's probability; "!label": its complement (a clean sheet is the opponent's Under 0.5). Corners and
    cards have no model market: None."""
    if label.startswith("!"):
        p = markets.get(label[1:])
        return None if p is None else 1.0 - p
    return markets.get(label)


def streaks(vs: list[View], base: dict, home_next: bool, markets: dict[str, float], subject: str = "") -> list[dict]:
    """Runs and counts of every event over the views (newest first). `m` is the model's probability that the event happens in
    the next match (it ends a drought, it extends a run). `subject` names the side in the head-to-head list."""
    out = []
    for e in EVENTS:
        seq = [(e.test(v), base.get((v.comp, e.key))) for v in vs]
        seq = [(y, p) for y, p in seq if y is not None and p is not None]
        if len(seq) < MIN_RUN:
            continue
        model = _model(markets, e.model(home_next)) if e.model and markets else None
        label = f"{subject}: " if subject and e.key in TEAM_EVENTS else ""
        # run from the newest match back
        first = seq[0][0]
        k, prob = 0, 1.0
        for y, p in seq:
            if y != first:
                break
            k += 1
            prob *= p if y else 1 - p
        if k >= MIN_RUN and prob <= MAX_P:
            text = f"{e.yes if first else e.no} da {k} partite" + (" di fila" if first else "")
            out.append({"t": label + text, "p": prob, "m": model, "e": e.key, "k": "serie" if first else "digiuno",
                        "x": _xg([v for v, _ in zip(vs, range(k))]) if e.key in ("win", "scores", "clean", "o25", "btts", "loss") else None})
        # counts over the last 5 and the last 10
        for n in (5, 10):
            if len(seq) < n:
                continue
            x = sum(y for y, _ in seq[:n])
            p = sum(pp for _, pp in seq[:n]) / n
            if x / n > p:
                tail = _binom_tail(n, x, p, True)
            else:
                tail = _binom_tail(n, x, p, False)
            if tail <= MAX_P and not (x in (0, n) and k >= n):  # an all-or-nothing count is already the run above
                out.append({"t": f"{label}{e.count[0].upper() + e.count[1:]} in {x} delle ultime {n}", "p": tail, "m": model, "e": e.key,
                            "k": "frequenza", "x": None})
    best: dict[str, dict] = {}
    for s in out:  # one line per event: the rarest
        if s["e"] not in best or s["p"] < best[s["e"]]["p"]:
            best[s["e"]] = s
    return sorted(best.values(), key=lambda s: s["p"])[:TOP]


def _round(items: list[dict]) -> list[dict]:
    return [{"t": s["t"], "r": max(2, round(1 / s["p"])) if s["p"] > 0 else 9999, "m": None if s.get("m") is None else round(s["m"], 3),
             "k": s["k"], "x": s.get("x")} for s in items]


# ------------------------------------------------------------------------------------------------- players


def scorer_streaks(sheets_by_team: dict, tally, roles: dict, names: dict, team: str, avg_goals: float, pstats: dict) -> list[dict]:
    """Starters of the team's recent sheets: goalless runs (and scoring runs) against the goals the scorer model expected."""
    from .scorers import Params, _share
    prm = Params()
    priors = tally.role_shares()
    recent = sheets_by_team.get(team, [])[-LAST:]
    players = {p for sh in recent[-3:] for p in sh.starters}
    out = []
    for pid in players:
        role = roles.get(pid, "MID")
        v = tally.p.get(pid)
        if not v:
            continue
        s0 = priors.get(role, priors["MID"])[0]
        rate = _share(v[2], v[1], s0, prm.k, prm) * avg_goals  # expected non-penalty goals per start
        if rate < 0.2:
            continue
        starts = [sh for sh in reversed(recent) if pid in sh.starters]
        if len(starts) < MIN_RUN:
            continue
        scored = [sh.np_goals.get(pid, 0) + sh.pen_goals.get(pid, 0) > 0 for sh in starts]
        first, k = scored[0], 0
        for y in scored:
            if y != first:
                break
            k += 1
        if k < MIN_RUN:
            continue
        p_one = 1 - math.exp(-rate)
        prob = p_one ** k if first else math.exp(-rate * k)
        if prob > MAX_P:
            continue
        name = names.get(pid, pid.split(":")[-1])
        if first:
            text = f"{name}: a segno da {k} partite da titolare di fila"
        else:
            text = f"{name}: nessun goal da {k} partite da titolare (il modello gliene attendeva {rate * k:.1f})"
        extra = pstats.get(name)
        out.append({"t": text, "p": prob, "m": round(p_one, 3), "k": "serie" if first else "digiuno", "x": extra})
    return sorted(out, key=lambda s: s["p"])[:TOP]


def player_stat_lines(store, teams: list[str], before: datetime) -> tuple[dict[str, str], list[dict]]:
    """From API-Football player stats (when stored): shots context per player name, and discipline streaks (many fouls, no
    booking for several matches)."""
    try:
        rows = store.db.execute(
            "SELECT p.team, p.name, p.position, p.minutes, p.shots, p.shots_on, p.fouls_committed, p.yellow, p.red, r.kickoff "
            f"FROM player_match_stats p JOIN results r ON r.fixture_id = p.fixture_id WHERE p.team IN ({','.join('?' * len(teams))}) "
            "AND r.kickoff < ? ORDER BY r.kickoff DESC", (*teams, before.isoformat())).fetchall()
    except Exception:  # noqa: BLE001 - table created by the API-Football collector
        return {}, []
    by: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for team, name, pos, mins, sh, on, fouls, y, r, ko in rows:
        if len(by[(team, name)]) < LAST:
            by[(team, name)].append((pos, mins or 0, sh or 0, on or 0, fouls or 0, y or 0, r or 0))
    shots: dict[str, str] = {}
    cards: list[dict] = []
    for (team, name), ms in by.items():
        if len(ms) >= 3:
            on = sum(m[3] for m in ms[:5])
            if on:
                shots[name] = f"{on} tiri in porta e {sum(m[2] for m in ms[:5])} tiri nelle ultime {min(5, len(ms))}"
        if len(ms) >= 5 and ms[0][0] in ("D", "M"):
            fouls = sum(m[4] for m in ms) / len(ms)
            booked = sum(1 for m in ms if m[5] or m[6])
            k = 0
            for m in ms:
                if m[5] or m[6]:
                    break
                k += 1
            p_card = min(0.6, (booked + 0.18 * 5) / (len(ms) + 5) + 0.05 * max(0.0, fouls - 1.5))
            prob = (1 - p_card) ** k
            if fouls >= 1.5 and k >= 4 and prob <= MAX_P:
                cards.append({"t": f"{name} ({team}): nessun cartellino da {k} partite con {fouls:.1f} falli a partita", "p": prob,
                              "m": round(p_card, 3), "k": "digiuno", "x": None})
    return shots, sorted(cards, key=lambda s: s["p"])[:TOP]


# ------------------------------------------------------------------------------------------------- per fixture


def match_trends(store, prov, fixtures, markets_of: dict[str, list[dict]], now: datetime) -> dict[str, str]:
    """fixture id -> trends JSON for every fixture whose teams have history."""
    results = prov.list_history(None, now)
    stats = prov.match_stat_values(STATS)
    for fid, s in stats.items():  # GOAL calls shots on target "shots_on_goal"
        if "shots_on_target" not in s and "shots_on_goal" in s:
            s["shots_on_target"] = s["shots_on_goal"]
    views = _views(results, stats)
    for team in views:
        views[team].sort(key=lambda v: v.ko, reverse=True)
    base = base_rates(views, now - timedelta(days=730))
    scorer = None
    out = {}
    for f in fixtures:
        vh, va = views.get(f.home, [])[:LAST], views.get(f.away, [])[:LAST]
        if len(vh) < MIN_RUN and len(va) < MIN_RUN:
            continue
        mk = {m["l"]: m["p"] for m in markets_of.get(f.id, []) if m.get("g") in ("1X2", "Under/Over", "Goal/NoGoal", "Goal squadra")}
        h2h = [v for v in views.get(f.home, []) if v.opp == f.away][:LAST]
        if scorer is None:
            scorer = _scorer_context(store, prov, now)
        sheets, tally, roles, names, avg = scorer
        shots, discipline = player_stat_lines(store, [f.home, f.away], f.kickoff)
        sc = scorer_streaks(sheets, tally, roles, names, f.home, avg.get(f.home, 1.3), shots) + \
            scorer_streaks(sheets, tally, roles, names, f.away, avg.get(f.away, 1.1), shots)
        data = {"home": _round(streaks(vh, base, True, mk)), "away": _round(streaks(va, base, False, mk)),
                "h2h": _round(streaks(h2h, base, True, mk, f.home)) if len(h2h) >= MIN_RUN else [],
                "n_h2h": len(h2h), "scorers": _round(sorted(sc, key=lambda s: s["p"])[:TOP]), "discipline": _round(discipline)}
        if any(data[k] for k in ("home", "away", "h2h", "scorers", "discipline")):
            out[f.id] = json.dumps(data, ensure_ascii=False)
    return out


def _scorer_context(store, prov, now: datetime):
    from .scorers import HALF_LIFE, Tally, load_scorer_data
    data = load_scorer_data(store)
    tally = Tally(HALF_LIFE)
    by_team: dict[str, list] = defaultdict(list)
    goals: dict[str, list[int]] = defaultdict(list)
    for sh in data.sheets:
        if sh.kickoff >= now:
            break
        sh.starters, sh.bench = prov._to_goal(sh.starters), prov._to_goal(sh.bench)
        tally.add(sh, data.roles)
        by_team[sh.team].append(sh)
        goals[sh.team].append(sh.team_np)
    avg = {t: sum(g[-20:]) / len(g[-20:]) for t, g in goals.items() if g}
    return by_team, tally, data.roles, data.names, avg
