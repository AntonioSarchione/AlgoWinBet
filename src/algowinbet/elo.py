"""Elo ratings as a prior for the goal model.

Clubs: computed from our own results (every competition, so European cups put leagues on one scale). Clubs first seen
after the start of the history (promoted sides) start below average.
National teams: World Football Elo rules over the public international results dataset (github.com/martj42/
international_results, CC0), downloaded weekly by the scheduler: it covers qualifiers and friendlies our league feed
never sees, which is what national-team matches need most.

Ratings are kept as a timeline per team, so a backtest reads the rating a team had at its cutoff and never a later one.
"""
from __future__ import annotations

import bisect
import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .collector import CollectStats
from .names import TeamNames

INTL_SOURCE = "martj42"
INTL_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
INTL_ENDPOINT = "/international_results/results.csv"
USER_AGENT = "AlgoWinBet/1.0 (private non-commercial use; weekly download)"
BASE = 1500.0


@dataclass(frozen=True)
class EloMatch:
    available_at: datetime  # when the result was known (the rating update is visible from then on)
    home: str
    away: str
    home_goals: int
    away_goals: int
    k: float
    home_adv: float  # 0 on neutral ground


def goal_multiplier(diff: int) -> float:
    """World Football Elo: a 2-goal win counts 1.5x, larger wins (11 + diff) / 8."""
    d = abs(diff)
    return 1.0 if d <= 1 else 1.5 if d == 2 else (11 + d) / 8


class EloTimeline:
    def __init__(self, matches: Iterable[EloMatch], start: Callable[[str, datetime], float] | None = None):
        """start(team, first_seen) = initial rating (default BASE)."""
        self._t: dict[str, list[datetime]] = {}
        self._r: dict[str, list[float]] = {}
        cur: dict[str, float] = {}
        for m in sorted(matches, key=lambda m: m.available_at):
            for team in (m.home, m.away):
                if team not in cur:
                    cur[team] = start(team, m.available_at) if start else BASE
            rh, ra = cur[m.home], cur[m.away]
            exp_home = 1 / (1 + 10 ** ((ra - rh - m.home_adv) / 400))
            score = 1.0 if m.home_goals > m.away_goals else 0.5 if m.home_goals == m.away_goals else 0.0
            delta = m.k * goal_multiplier(m.home_goals - m.away_goals) * (score - exp_home)
            cur[m.home], cur[m.away] = rh + delta, ra - delta
            for team in (m.home, m.away):
                self._t.setdefault(team, []).append(m.available_at)
                self._r.setdefault(team, []).append(cur[team])

    def teams(self) -> set[str]:
        return set(self._t)

    def at(self, team: str, when: datetime) -> float | None:
        """Rating after the last result known at `when` (None: no result of the team known yet)."""
        ts = self._t.get(team)
        if not ts:
            return None
        k = bisect.bisect_right(ts, when)
        return self._r[team][k - 1] if k else None


# --------------------------------------------------------------------------- clubs
CLUB_K = 20.0
CLUB_HOME = 65.0
CLUB_NEWCOMER = 1400.0  # a club first seen after the history start (promoted) starts this far below average


def club_timeline(results, names: TeamNames | None = None) -> EloTimeline:
    names = names or TeamNames()
    results = list(results)
    if not results:
        return EloTimeline([])
    first = min(r.kickoff for r in results)
    ms = [EloMatch(r.available_at, names.canon(r.home), names.canon(r.away), r.home_goals, r.away_goals, CLUB_K, CLUB_HOME)
          for r in results]
    return EloTimeline(ms, start=lambda team, t: BASE if t - first < timedelta(days=60) else CLUB_NEWCOMER)


# ------------------------------------------------------------------------ national teams
def tournament_k(name: str) -> float:
    """World Football Elo weights: World Cup 60, continental finals 50, qualifiers and major tournaments 40, other
    tournaments 30, friendlies 20."""
    t = name.lower()
    if "qualification" in t:
        return 40.0
    if t == "fifa world cup":
        return 60.0
    if t in ("uefa euro", "copa américa", "copa america", "african cup of nations", "afc asian cup", "gold cup",
             "confederations cup", "ofc nations cup"):
        return 50.0
    if "nations league" in t:
        return 40.0
    if t == "friendly":
        return 20.0
    return 30.0


def parse_international(body: bytes, names: TeamNames | None = None) -> list[EloMatch]:
    names = names or TeamNames()
    out = []
    for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace"))):
        try:
            hg, ag = int(row["home_score"]), int(row["away_score"])  # upcoming matches carry NA
            day = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        neutral = str(row.get("neutral", "")).strip().upper() == "TRUE"
        out.append(EloMatch(day + timedelta(days=1), names.canon(row["home_team"]), names.canon(row["away_team"]), hg, ag,
                            tournament_k(row.get("tournament", "")), 0.0 if neutral else 100.0))
    return out


def national_timeline(body: bytes | None, names: TeamNames | None = None) -> EloTimeline:
    return EloTimeline(parse_international(body, names) if body else [])


# ------------------------------------------------------------------------ prior for the goal model
def elo_prior(teams: Iterable[str], when: datetime, clubs: EloTimeline | None, nations: EloTimeline | None,
              club_per_100: float, nation_per_100: float) -> dict[str, tuple[float, float]]:
    """team -> (attack, defence) centre of the ridge penalty: per_100 log-goals for every 100 Elo above the average of the
    teams of the same kind in this fit. National teams are those the international dataset knows; the rest are clubs."""
    teams = list(teams)
    kinds: dict[str, list[tuple[str, float]]] = {"nation": [], "club": []}
    for t in teams:
        r = nations.at(t, when) if nations and nation_per_100 else None
        if r is not None:
            kinds["nation"].append((t, r))
            continue
        r = clubs.at(t, when) if clubs and club_per_100 else None
        if r is not None:
            kinds["club"].append((t, r))
    out: dict[str, tuple[float, float]] = {}
    for kind, rows in kinds.items():
        if len(rows) < 2:
            continue
        per = nation_per_100 if kind == "nation" else club_per_100
        mean = sum(r for _, r in rows) / len(rows)
        for t, r in rows:
            v = per * (r - mean) / 100
            out[t] = (v, v)
    return out


def is_national(team: str, nations: EloTimeline | None) -> bool:
    return bool(nations) and team in nations.teams()


# ------------------------------------------------------------------------ weekly download
def international_due(store, now: datetime, every_days: float = 7.0) -> bool:
    row = store.db.execute("SELECT done_at FROM jobs WHERE name=?", (f"dataset-check:{INTL_SOURCE}",)).fetchone()
    return not (row and row[0] and now - datetime.fromisoformat(row[0]) < timedelta(days=every_days))


def sync_international(store, now: datetime, fetch: Callable[[str, dict], tuple[int, bytes]] | None = None,
                       every_days: float = 7.0) -> CollectStats | None:
    """Downloads the international results CSV at most every `every_days` (ETag: an unchanged file costs no body)."""
    import json
    import urllib.error
    import urllib.request

    job = f"dataset-check:{INTL_SOURCE}"
    if not international_due(store, now, every_days):
        return None
    row = store.db.execute("SELECT done_at, detail FROM jobs WHERE name=?", (job,)).fetchone()
    etag = (json.loads(row[1] or "{}") if row else {}).get("etag")

    def http(url: str, headers: dict) -> tuple[int, bytes, str | None]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            return e.code, b"", None

    st = CollectStats("nazionali")
    headers = {"If-None-Match": etag} if etag else {}
    try:
        if fetch:
            status, body = fetch(INTL_URL, headers)
            new_etag = None
        else:
            status, body, new_etag = http(INTL_URL, headers)
    except OSError as e:
        st.errors.append(f"risultati internazionali: {e}")
        store.mark_job(job, now, row[1] if row else "")
        return st
    st.requests = 1
    if status == 304:
        st.skipped.append("risultati internazionali: invariati")
        store.mark_job(job, now, row[1] if row else "")
        return st
    if status != 200 or not body:
        st.errors.append(f"risultati internazionali: HTTP {status}")
        store.mark_job(job, now, row[1] if row else "")
        return st
    store.put_raw(INTL_SOURCE, INTL_ENDPOINT, {}, 200, body, now, cost=0)
    n = len(parse_international(body))
    st.add("partite internazionali", n)
    store.mark_job(job, now, json.dumps({"etag": new_etag, "matches": n}))
    return st


def latest_international(store) -> bytes | None:
    row = store.db.execute("SELECT id FROM raw_requests WHERE source=? AND endpoint=? AND status=200 ORDER BY id DESC LIMIT 1",
                           (INTL_SOURCE, INTL_ENDPOINT)).fetchone()
    return store.raw_body(row[0]) if row else None


def expected_score(diff: float) -> float:
    return 1 / (1 + 10 ** (-diff / 400))

