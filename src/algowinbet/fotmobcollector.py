"""FotMob collector: per player and match expected goals (Opta xG, xGOT, xA), shots, fouls, cards and minutes, plus who was
missing, for the 7 domestic leagues and the European cups. Public pages, no key, no cookie.

Why FotMob: API-Football has no player xG and its free plan reads only matches linked when they were played; Understat has no
Liga Portugal and no Eredivisie; FBref lost the Opta data in January 2026. A FotMob match page carries the whole match in its
`__NEXT_DATA__` JSON, finished matches of past seasons included (verified 2026-10-06 on Eredivisie and Liga Portugal).

Not an official API: the site may change without notice, so the collector is optional. Nothing reads its tables yet
(fotmob_player_stats, fotmob_absences): a feature that wants them is measured first, like every new source.

Per tick:
  seasons ... /leagues/{id}/fixtures/x?season=S (one page per league and season, every match of that season): links FotMob
              matches to our results. Past seasons once; the current one again when a finished match is still unlinked.
  matches ... /match/{id} for finished linked matches without player stats: the last RECENT_DAYS first (at most
              RECENT_PER_TICK a tick), then the history newest first within the morning's history_seconds.
Every page is stored in the snapshot store as the compact JSON the collector reads (not the 1 MB HTML).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .apifcollector import _sim
from .collector import CollectStats
from .names import TeamNames
from .providers.goalapi import Transport, urllib_transport
from .snapshots import SnapshotProvider, SnapshotStore

SOURCE = "fotmob"
BASE_URL = "https://www.fotmob.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
MIN_INTERVAL_S = 2.0  # one page every 2 seconds at most: a small, polite load
RECENT_DAYS = 10
RECENT_PER_TICK = 12
SEASON_REFRESH = timedelta(hours=6)
FINISHED_AFTER = timedelta(hours=2, minutes=30)  # from kickoff
HISTORY_MIN_S = 60  # less than this is not worth the day's history slot
GIVE_UP_AFTER = timedelta(days=3)  # a finished match still without player stats then has none (lower leagues, abandoned)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixture_links(source TEXT, ext_id TEXT, fixture_id TEXT, linked_at TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS fotmob_player_stats(fixture_id TEXT, team TEXT, player_id TEXT, name TEXT, position INTEGER, starter INTEGER,
  minutes INTEGER, rating REAL, goals INTEGER, assists INTEGER, xg REAL, npxg REAL, xgot REAL, xa REAL, shots INTEGER, shots_on INTEGER,
  key_passes INTEGER, touches_box INTEGER, fouls_committed INTEGER, fouls_drawn INTEGER, yellow INTEGER, red INTEGER, tackles INTEGER,
  interceptions INTEGER, observed_at TEXT, PRIMARY KEY(fixture_id, player_id));
CREATE TABLE IF NOT EXISTS fotmob_absences(fixture_id TEXT, team TEXT, player_id TEXT, name TEXT, kind TEXT, expected_return TEXT,
  market_value INTEGER, observed_at TEXT, PRIMARY KEY(fixture_id, player_id));
"""

# FotMob stat keys (language independent) -> our columns
STAT_KEYS = {"minutes_played": "minutes", "rating_title": "rating", "goals": "goals", "assists": "assists", "expected_goals": "xg",
             "expected_goals_non_penalty": "npxg", "expected_goals_on_target_variant": "xgot", "expected_assists": "xa",
             "total_shots": "shots", "ShotsOnTarget": "shots_on", "chances_created": "key_passes", "touches_opp_box": "touches_box",
             "fouls": "fouls_committed", "was_fouled": "fouls_drawn", "matchstats.headers.tackles": "tackles",
             "interceptions": "interceptions"}
INT_COLS = {"minutes", "goals", "assists", "shots", "shots_on", "key_passes", "touches_box", "fouls_committed", "fouls_drawn",
            "tackles", "interceptions"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def pid(fotmob_id) -> str:
    return f"fotmob:{fotmob_id}"


class FotMobError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class FotMobClient:
    """Reads the JSON a FotMob page embeds. No key and no cookie: a plain GET with a browser user agent."""

    def __init__(self, store: SnapshotStore | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), base_url: str = BASE_URL):
        self.store, self.transport = store, transport or urllib_transport(timeout=30.0)
        self.sleep, self.clock, self.now, self.base = sleep, clock, now, base_url
        self._last = -1e9
        self.requests_sent = 0

    def page(self, path: str, params: dict | None = None) -> dict:
        """pageProps of the page at path (with its query), or FotMobError."""
        wait = MIN_INTERVAL_S - (self.clock() - self._last)
        if wait > 0:
            self.sleep(wait)
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        status, _, body = self.transport(f"{self.base}{path}" + (f"?{query}" if query else ""),
                                         {"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en"})
        self._last = self.clock()
        self.requests_sent += 1
        if status != 200:
            raise FotMobError(f"{status} su {path}", status)
        m = _NEXT_DATA.search(body.decode("utf-8", "replace"))
        if not m:
            raise FotMobError(f"pagina senza __NEXT_DATA__: {path}", status)
        try:
            return json.loads(m.group(1))["props"]["pageProps"]
        except (ValueError, KeyError, TypeError) as e:
            raise FotMobError(f"JSON della pagina non leggibile: {path}", status) from e

    def keep(self, path: str, params: dict | None, payload: dict) -> None:
        """Stores what the collector reads from a page (the page itself is about 1 MB of HTML)."""
        if self.store:
            self.store.put_raw(SOURCE, path, params, 200, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(), self.now(), cost=0)


@dataclass
class FotMobLeague:
    fotmob_id: int
    competition: str


class FotMobCollector:
    def __init__(self, client: FotMobClient, store: SnapshotStore, leagues: list[FotMobLeague], names: TeamNames | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), history_seasons: int = 2,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.store, self.now, self.clock = client, store, now, clock
        self.leagues = leagues
        self.names = names or TeamNames()
        self.history_seasons = history_seasons
        self.provider = SnapshotProvider(store)
        self._history = None
        store.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ links
    def _links(self) -> dict[str, str]:
        """our fixture id -> FotMob match id"""
        return {fid: ext for ext, fid in self.store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall()}

    def _results(self, league: FotMobLeague):
        """Our results of the league (one read of the results table per tick, shared by every league)."""
        if self._history is None:
            self._history = {}
            for r in self.provider.list_history([lg.competition for lg in self.leagues], self.now()):
                self._history.setdefault(r.competition, []).append(r)
        return self._history.get(league.competition, [])

    def _match(self, m: dict, results) -> str | None:
        """Our result for a FotMob match: same teams within a day and a half of the kickoff (dates differ by time zone between
        sources), the best pair by name similarity when the names are spelled differently."""
        try:
            ko = datetime.fromisoformat(str(m["status"]["utcTime"]).replace("Z", "+00:00"))
            home, away = self.names.canon(m["home"]["name"]), self.names.canon(m["away"]["name"])
        except (KeyError, TypeError, ValueError):
            return None
        near = [r for r in results if abs(r.kickoff - ko) <= timedelta(hours=36)]
        exact = [r for r in near if r.home == home and r.away == away]
        if len(exact) == 1:
            return exact[0].fixture_id
        scored = sorted(((min(_sim(home, r.home), _sim(away, r.away)), r) for r in near), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.15):
            return scored[0][1].fixture_id
        # one name spelled very differently ("PSG" / "Paris Saint-Germain"): a team plays once in three days, so the only
        # match of the window where the other team is the same, on the same side, is this one
        one = [r for r in near if max(_sim(home, r.home), _sim(away, r.away)) >= 0.9]
        return one[0].fixture_id if len(one) == 1 else None

    def _season_page(self, league: FotMobLeague, season: str | None) -> tuple[list[dict], list[str]]:
        params = {"season": season} if season else None
        pp = self.client.page(f"/leagues/{league.fotmob_id}/fixtures/x", params)
        matches = [{"id": str(m.get("id")), "home": {"name": (m.get("home") or {}).get("name")}, "away": {"name": (m.get("away") or {}).get("name")},
                    "status": {"utcTime": (m.get("status") or {}).get("utcTime"), "finished": bool((m.get("status") or {}).get("finished"))}}
                   for m in ((pp.get("fixtures") or {}).get("allMatches") or [])]
        seasons = [str(s) for s in pp.get("allAvailableSeasons") or []]
        self.client.keep(f"/leagues/{league.fotmob_id}/fixtures", {"season": season or "current"}, {"matches": matches, "seasons": seasons})
        return matches, seasons

    def _link(self, st: CollectStats, league: FotMobLeague, matches: list[dict], results) -> None:
        have = set(self._links())
        rows, missed = [], []
        first = min((r.kickoff for r in results), default=None)
        for m in matches:
            if not m["status"]["finished"]:
                continue
            fid = self._match(m, results)
            if fid is None:
                ko = str(m["status"]["utcTime"] or "")
                if first is not None and ko >= first.strftime("%Y-%m-%d"):  # before our history it is simply not ours
                    missed.append(f"{m['home']['name']}-{m['away']['name']} {ko[:10]}")
            elif fid not in have:
                rows.append((SOURCE, m["id"], fid, self.now().isoformat()))
                have.add(fid)
        st.add("partite collegate", self.store._bulk("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at)", rows))
        if missed:  # a name to add to configs/team_aliases.json, or a match our results do not have
            st.skipped.append(f"{league.competition}: {len(missed)} partite FotMob finite senza risultato nostro (es. {', '.join(missed[:3])})")

    def _last_fetch(self, path: str, params: dict) -> datetime | None:
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint=? AND params=?",
                                    (SOURCE, path, json.dumps(params, sort_keys=True))).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def sync_seasons(self, st: CollectStats, history: bool) -> None:
        """Current season when a finished match of the last RECENT_DAYS is unlinked; with history, the previous
        history_seasons once each (the seasons our results backfill holds)."""
        t, links = self.now(), self._links()
        for lg in self.leagues:
            results = self._results(lg)
            recent = [r for r in results if t - r.kickoff <= timedelta(days=RECENT_DAYS) and r.fixture_id not in links]
            path = f"/leagues/{lg.fotmob_id}/fixtures"
            last = self._last_fetch(path, {"season": "current"})
            seasons: list[str] = []
            if recent and (last is None or t - last >= SEASON_REFRESH) or (history and last is None):
                try:
                    matches, seasons = self._season_page(lg, None)
                except FotMobError as e:
                    st.errors.append(f"{lg.competition}: {e}")
                    continue
                st.requests += 1
                self._link(st, lg, matches, results)
            if not history:
                continue
            if not seasons:
                got = self.store.last_raw(SOURCE, path)
                seasons = (json.loads(self.store.raw_body(got[0])).get("seasons") or []) if got else []
            for season in seasons[1:1 + self.history_seasons]:
                job = f"fotmob-season:{lg.fotmob_id}:{season}"
                if self.store.job_done(job):
                    continue
                try:
                    matches, _ = self._season_page(lg, season)
                except FotMobError as e:
                    st.errors.append(f"{lg.competition} {season}: {e}")
                    continue
                st.requests += 1
                self._link(st, lg, matches, results)
                self.store.mark_job(job, t, f"{len(matches)} partite")
        self.store.db.commit()

    # ------------------------------------------------------------------ matches
    def due(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """(recent, history): (our fixture id, FotMob id) of linked finished matches without player stats, newest first."""
        t = self.now()
        have = {r[0] for r in self.store.db.execute("SELECT DISTINCT fixture_id FROM fotmob_player_stats").fetchall()}
        gone = {r[0][len("fotmob-none:"):] for r in self.store.db.execute("SELECT name FROM jobs WHERE name LIKE 'fotmob-none:%'").fetchall()}
        links = self._links()
        recent, history = [], []
        for lg in self.leagues:
            for r in self._results(lg):
                if r.fixture_id in links and r.fixture_id not in have and r.fixture_id not in gone and t - r.kickoff >= FINISHED_AFTER:
                    (recent if t - r.kickoff <= timedelta(days=RECENT_DAYS) else history).append((r.kickoff, r.fixture_id, links[r.fixture_id]))
        order = lambda xs: [(fid, ext) for _, fid, ext in sorted(xs, reverse=True)]
        return order(recent), order(history)

    def sync_matches(self, st: CollectStats, todo: list[tuple[str, str]], max_n: int | None, deadline: float | None) -> None:
        for fid, ext in todo[:max_n]:
            if deadline is not None and self.clock() > deadline:
                st.skipped.append("tempo FotMob esaurito: il resto al prossimo giro")
                break
            try:
                pp = self.client.page(f"/match/{ext}")
            except FotMobError as e:
                st.errors.append(f"partita {ext}: {e}")
                if e.status == 404:
                    self.store.mark_job(f"fotmob-none:{fid}", self.now(), "pagina FotMob assente")
                continue
            st.requests += 1
            r = self.provider.result_of(fid)
            stats, absences, kept = self.parse_match(pp, fid, r.home if r else None, r.away if r else None)
            self.client.keep(f"/match/{ext}", None, kept)
            if stats:
                st.add("statistiche giocatori FotMob", self.store._bulk(
                    "INSERT OR REPLACE INTO fotmob_player_stats(fixture_id,team,player_id,name,position,starter,minutes,rating,goals,assists,"
                    "xg,npxg,xgot,xa,shots,shots_on,key_passes,touches_box,fouls_committed,fouls_drawn,yellow,red,tackles,interceptions,"
                    "observed_at)", stats))
                if absences:
                    st.add("assenti FotMob", self.store._bulk(
                        "INSERT OR REPLACE INTO fotmob_absences(fixture_id,team,player_id,name,kind,expected_return,market_value,observed_at)",
                        absences))
            elif r is not None and self.now() - r.kickoff > GIVE_UP_AFTER:
                self.store.mark_job(f"fotmob-none:{fid}", self.now(), "FotMob senza statistiche giocatori")
        self.store.db.commit()

    def parse_match(self, pp: dict, fid: str, home: str | None, away: str | None) -> tuple[list[tuple], list[tuple], dict]:
        """Rows for fotmob_player_stats and fotmob_absences, and the compact payload kept in the snapshot store. Teams are named
        as in our result (FotMob home team -> our home team). No rows when the page has no player stats or teams do not map."""
        g, c = pp.get("general") or {}, pp.get("content") or {}
        side = {}
        for key, ours in (("homeTeam", home), ("awayTeam", away)):
            tid = (g.get(key) or {}).get("id")
            if tid is not None:
                side[int(tid)] = ours or self.names.canon(str((g.get(key) or {}).get("name") or ""))
        lineup = c.get("lineup") or {}
        starters = {int(p["id"]) for k in ("homeTeam", "awayTeam") for p in (lineup.get(k) or {}).get("starters") or [] if p.get("id") is not None}
        cards: dict[int, list[int]] = {}
        for e in ((c.get("matchFacts") or {}).get("events") or {}).get("events") or []:
            if e.get("type") == "Card" and e.get("playerId") is not None:
                y_r = cards.setdefault(int(e["playerId"]), [0, 0])
                kind = str(e.get("card") or "")
                if kind == "Yellow":
                    y_r[0] += 1
                elif kind in ("Red", "YellowRed"):
                    y_r[1] = 1
                    if kind == "YellowRed":
                        y_r[0] = max(y_r[0], 1)  # second yellow: yellow 1 + red 1, as the API-Football rows read
        at = self.now().isoformat()
        stats, kept_players = [], {}
        for p in (c.get("playerStats") or {}).values():
            try:
                fm_id, team = int(p["id"]), side.get(int(p.get("teamId")))
            except (KeyError, TypeError, ValueError):
                continue
            vals: dict[str, float | None] = {}
            for grp in p.get("stats") or []:
                for s in (grp.get("stats") or {}).values():
                    col = STAT_KEYS.get(str(s.get("key")))
                    if col and col not in vals:
                        v = (s.get("stat") or {}).get("value")
                        vals[col] = v if isinstance(v, (int, float)) else None
            if not team or not vals.get("minutes"):
                continue  # unused substitutes have no minutes
            y, red = cards.get(fm_id, [0, 0])
            row = {col: (int(vals[col]) if col in INT_COLS and vals.get(col) is not None else vals.get(col)) for col in STAT_KEYS.values()}
            stats.append((fid, team, pid(fm_id), p.get("name"), p.get("positionId"), int(fm_id in starters), row["minutes"], row["rating"],
                          row["goals"] or 0, row["assists"] or 0, row["xg"], row["npxg"], row["xgot"], row["xa"], row["shots"] or 0,
                          row["shots_on"] or 0, row["key_passes"] or 0, row["touches_box"] or 0, row["fouls_committed"] or 0,
                          row["fouls_drawn"] or 0, y, red, row["tackles"] or 0, row["interceptions"] or 0, at))
            kept_players[str(fm_id)] = {"name": p.get("name"), "teamId": p.get("teamId"), "positionId": p.get("positionId"), **row,
                                        "shots": [{k: s.get(k) for k in ("min", "expectedGoals", "expectedGoalsOnTarget", "eventType",
                                                                          "situation", "isOnTarget", "isOwnGoal", "x", "y")}
                                                  for s in p.get("shotmap") or []]}
        absences, kept_out = [], []
        for key, ours in (("homeTeam", home), ("awayTeam", away)):
            t = lineup.get(key) or {}
            team = ours or (side.get(int(t["id"])) if t.get("id") is not None else None)
            for u in t.get("unavailable") or []:
                un = u.get("unavailability") or {}
                if u.get("id") is None or not team:
                    continue
                absences.append((fid, team, pid(u["id"]), u.get("name"), str(un.get("type") or ""), str(un.get("expectedReturn") or ""),
                                 u.get("marketValue"), at))
                kept_out.append({"team": key, "id": u["id"], "name": u.get("name"), **un})
        kept = {"matchId": g.get("matchId"), "kickoff": g.get("matchTimeUTCDate"), "finished": g.get("finished"),
                "home": g.get("homeTeam"), "away": g.get("awayTeam"), "cards": {str(k): v for k, v in cards.items()},
                "starters": sorted(starters), "players": kept_players, "unavailable": kept_out}
        return stats, (absences if stats else []), kept

    # ------------------------------------------------------------------ tick
    def history_due(self) -> bool:
        """The history runs once a UTC day, in the first tick with at least HISTORY_MIN_S to spare (normally the morning run),
        a slice a day until nothing is left."""
        return not self.store.job_done(f"fotmob-history:{self.now().date().isoformat()}")

    def run(self, history_seconds: float = 0.0) -> CollectStats:
        st = CollectStats("fotmob")
        self._history = None
        history = history_seconds >= HISTORY_MIN_S and self.history_due()
        self.sync_seasons(st, history)
        recent, past = self.due()
        self.sync_matches(st, recent, RECENT_PER_TICK, None)
        if history:
            self.sync_matches(st, past, None, self.clock() + history_seconds)
            self.store.mark_job(f"fotmob-history:{self.now().date().isoformat()}", self.now(), f"{len(past)} partite storiche da leggere")
        return st

