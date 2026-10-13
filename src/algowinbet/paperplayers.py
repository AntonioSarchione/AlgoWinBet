"""Paper trading of Sisal's player markets (Giocatori tab): every Sisal player price of the feed (player_quotes) for a starter of
an official XI is written down once, with our probability of that moment, and settled from the FotMob player rows.

Recorded at publish time, when both the official XI and Sisal's price exist (Sisal voids a player bet when the player does not
take part, so the probability is the one given that he starts, as on the site). The probability is the site's own
(web/app/_components/Scorers.tsx): the published goalscorer table (scorerpub.py) and player cards (playercard.py), with Sisal's
DUO rule on goals, two or more, assists and shots on target (the player or his direct substitute: his slot over the whole match,
the substitute at his own rate, measured by duo-eval). Total shots and fouls committed: his own minutes. First goalscorer is not
recorded: settling it needs the scorer of the first goal, which the stored data does not give reliably.

Every priced selection is recorded, so the calibration covers them all; the Registro calls "giocate" those the rules would
play (probability at least MIN_P, price at least MIN_ODDS, positive EV), 1 unit each.

Settlement after the match, from fotmob_player_stats: a player without minutes is a refund (void); a DUO market counts the
substitute paired by minutes (duoeval.pair_substitutes); a starter who went off with nobody pairable is settled only when he
already did it himself, otherwise not settleable. No FotMob rows after STAT_WAIT: not settleable.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_player_legs(id INTEGER PRIMARY KEY, fixture_id TEXT, competition TEXT, match TEXT, kickoff TEXT,
  team TEXT, player_id TEXT, player TEXT, market TEXT, line REAL NOT NULL, duo INTEGER, odds REAL, p REAL, ev REAL, run_id INTEGER,
  created_at TEXT, result TEXT, settled_at TEXT, own REAL, sub REAL, UNIQUE(fixture_id, player_id, market, line));
CREATE INDEX IF NOT EXISTS ix_paper_player_open ON paper_player_legs(result, kickoff);
"""
MARKETS = {"SCORER": ("goals", True), "TWO_PLUS": ("goals", True), "ASSIST": ("assists", True), "SHOTS_ON": ("shots_on", True),
           "SHOTS": ("shots", False), "FOULS": ("fouls_committed", False)}
DISP = 4.0  # negative binomial shape of the player counts (playercard.py, web/lib/playermarkets.ts)
MIN_P = 0.40  # user rules on a selection: probability and price floors
MIN_ODDS = 1.25
SETTLE_AFTER = timedelta(hours=2, minutes=30)
STAT_WAIT = timedelta(days=6)


def _migrate(store) -> None:
    store.db.executescript(SCHEMA)


# ---- the site's numbers (web/lib/playermarkets.ts, web/app/_components/Scorers.tsx)
def duo_slot(minutes: float | None) -> float:
    return 90 / min(90.0, max(30.0, minutes if minutes else 80.0))


def p_at_least(mu: float | None, k: int) -> float:
    if not mu or mu <= 0:
        return 0.0
    q = mu / (DISP + mu)
    pj = (DISP / (DISP + mu)) ** DISP
    below = pj
    for j in range(1, k):
        pj *= (DISP + j - 1) / j * q
        below += pj
    return max(0.0, 1 - below)


def nb_mean(p: float) -> float:
    """Mean of a negative binomial count with P(at least one) = p."""
    if p <= 0:
        return 0.0
    return DISP * ((1 - min(p, 0.999)) ** (-1 / DISP) - 1)


def model_probability(market: str, k: int, card: dict | None, scorer: dict | None) -> float | None:
    """Probability given that he starts of `market` with threshold k (k = line + 0.5, 1 for yes/no markets)."""
    whole = duo_slot((card or {}).get("min"))
    if market in ("SCORER", "TWO_PLUS"):
        if not scorer:
            return None
        s = scorer.get("s") or 0.0

        def given(x: str) -> float:
            v = scorer.get(f"{x}s")
            return v if v is not None else (scorer[x] if s >= 1 else scorer[x] / max(s, 0.01))
        a = min(given("a"), 0.95)
        mu = -math.log(1 - a) * (whole - 1)
        none = math.exp(-mu)
        if market == "SCORER":
            return min(1 - (1 - a) * none, 0.99)
        d = given("d")
        return min(max(0.0, 1 - (1 - a) * none - (a - d) * none - (1 - a) * mu * none), 0.99)
    if not card or card.get("gk"):
        return None
    if market == "ASSIST":
        return p_at_least(nb_mean(card.get("as") or 0.0) * whole, 1)
    if market == "SHOTS_ON":
        return p_at_least((card.get("sot") or 0.0) * whole, k)
    if market == "SHOTS":
        return p_at_least(card.get("sh"), k)
    if market == "FOULS":
        return p_at_least(card.get("fc"), k)
    return None


# ---- Sisal's names ("Surname, Name") to ours, as web/lib/playermarkets.ts matchSisal
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _words(s: str) -> list[str]:
    return [w for w in _norm(s).split(" ") if w]


def _score(ours: list[str], sisal: str) -> int:
    sur, _, given = sisal.partition(",")
    s, g = _words(sur), _words(given)
    if not s or not all(t in ours for t in s):
        return 0
    if g and not any(t[0] == g[0][0] for t in ours):
        return 0
    return 3 if g and all(t in ours for t in g) else 2 if g else 1


def match_sisal(names: list[str], sisal_names: list[str]) -> dict[str, str]:
    """Sisal name -> our name, when exactly one of ours fits best."""
    ours = [(n, _words(n)) for n in dict.fromkeys(names)]
    out = {}
    for sn in dict.fromkeys(sisal_names):
        scored = sorted(((v, n) for n, w in ours if (v := _score(w, sn)) > 0), reverse=True)
        if scored and not (len(scored) > 1 and scored[1][0] == scored[0][0]):
            out[sn] = scored[0][1]
    return out


def _key(n: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", n) if unicodedata.category(c) != "Mn").lower()


def register_players(store, run_id: int, now: datetime, fixtures, scorers: dict[str, str | None], cards: dict[str, str]) -> int:
    """fixtures: the published ones; scorers / cards: fixture id -> the published JSON. Returns the selections added."""
    _migrate(store)
    rows = []
    for f in fixtures:
        if f.kickoff <= now or f.id not in cards:
            continue
        quotes = store.db.execute(
            "SELECT q.player_name, q.market, q.line_key, q.odds FROM player_quotes q JOIN (SELECT player_key, market, line_key, "
            "MAX(observed_at) AS t FROM player_quotes WHERE fixture_id = ? AND bookmaker LIKE '%sisal%' AND observed_at <= ? "
            "GROUP BY player_key, market, line_key) l ON l.player_key = q.player_key AND l.market = q.market AND l.line_key = q.line_key "
            "AND l.t = q.observed_at WHERE q.fixture_id = ? AND q.bookmaker LIKE '%sisal%'", (f.id, now.isoformat(), f.id)).fetchall()
        if not quotes:
            continue
        xi: dict[str, list[str]] = {}
        for team, starters in store.db.execute(
                "SELECT team, starters FROM lineups WHERE fixture_id = ? AND status = 'confirmed' ORDER BY observed_at", (f.id,)).fetchall():
            xi[team] = json.loads(starters or "[]")  # the newest confirmed XI of each team
        if not xi:
            continue
        card_of = json.loads(cards[f.id])
        sc = json.loads(scorers.get(f.id) or "{}")
        ids = [pid for team in xi.values() for pid in team]
        names = dict(store.db.execute(f"SELECT id, name FROM players WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()) if ids else {}
        who = {names[pid]: (team, pid) for team, team_ids in xi.items() for pid in team_ids if pid in names}
        by_name = {(team, _key(p["n"])): p for team, t in sc.items() for p in t.get("players", [])}
        sisal = match_sisal(list(who), [q[0] for q in quotes])
        for sname, market, line_key, odds in quotes:
            if market not in MARKETS or sname not in sisal or not odds or odds <= 1:
                continue
            ours = sisal[sname]
            team, pid = who[ours]
            line = float(line_key) if line_key else 1.5 if market == "TWO_PLUS" else 0.5  # always a line: one row per selection
            k = int(line + 0.5)
            p = model_probability(market, k, card_of.get(pid), by_name.get((team, _key(ours))))
            if p is None or p <= 0:
                continue
            rows.append((f.id, f.competition, f"{f.home} - {f.away}", f.kickoff.isoformat(), team, pid, ours, market, line,
                         int(MARKETS[market][1]), odds, round(p, 5), round(p * odds - 1, 5), run_id, now.isoformat()))
    if not rows:
        return 0
    return store._bulk("INSERT OR IGNORE INTO paper_player_legs(fixture_id,competition,match,kickoff,team,player_id,player,market,line,"
                       "duo,odds,p,ev,run_id,created_at)", rows)


def settle_players(store, now: datetime | None = None) -> dict[str, int]:
    """Settles the recorded player selections whose match is over and whose FotMob player rows are stored."""
    from .duoeval import pair_substitutes
    _migrate(store)
    t = now or datetime.now(timezone.utc)
    open_rows = store.db.execute("SELECT id, fixture_id, kickoff, player_id, market, line, duo FROM paper_player_legs "
                                 "WHERE result IS NULL AND kickoff <= ?", ((t - SETTLE_AFTER).isoformat(),)).fetchall()
    done = {"won": 0, "lost": 0, "void": 0, "non valutabile": 0}
    if not open_rows:
        return done
    cache: dict[str, tuple] = {}
    updates = []
    for lid, fid, ko, pid, market, line, duo in open_rows:
        if fid not in cache:
            rows = store.db.execute(
                "SELECT s.player_id, s.team, s.minutes, s.starter, COALESCE(s.red, 0), s.goals, s.assists, s.shots_on, s.shots, "
                "s.fouls_committed, l.goal_id, p.position FROM fotmob_player_stats s LEFT JOIN fotmob_player_links l ON l.fotmob_id = s.player_id "
                "LEFT JOIN players p ON p.id = l.goal_id WHERE s.fixture_id = ?", (fid,)).fetchall()
            cache[fid] = rows
        rows = cache[fid]
        if not rows:
            if t - datetime.fromisoformat(ko) > STAT_WAIT:
                updates.append(("non valutabile", t.isoformat(), None, None, lid))
                done["non valutabile"] += 1
            continue
        stat = MARKETS[market][0]
        col = {"goals": 5, "assists": 6, "shots_on": 7, "shots": 8, "fouls_committed": 9}[stat]
        mine = next((r for r in rows if r[10] == pid), None)
        if mine is None or not (mine[2] or 0) > 0:
            updates.append(("void", t.isoformat(), None, None, lid))  # he did not play: Sisal refunds
            done["void"] += 1
            continue
        k = int(line + 0.5)
        own = mine[col] or 0
        sub = 0
        res = None
        if duo and mine[3] and (mine[2] or 0) < 90 and own < k:
            team = [r for r in rows if r[1] == mine[1]]
            roles = {r[0]: r[11] or "MID" for r in team}
            pairs, _ = pair_substitutes([(r[0], r[2] or 0, r[3], bool(r[4])) for r in team], roles)
            if mine[0] in pairs:
                s = next(r for r in team if r[0] == pairs[mine[0]])
                sub = s[col] or 0
            elif not mine[4]:
                res = "non valutabile"  # replaced, but by whom is not known
        if res is None:
            res = "won" if own + sub >= k else "lost"
        updates.append((res, t.isoformat(), own, sub, lid))
        done[res] += 1
    for u in updates:
        store.db.execute("UPDATE paper_player_legs SET result=?, settled_at=?, own=?, sub=? WHERE id=?", u)
    store.db.commit()
    return done
