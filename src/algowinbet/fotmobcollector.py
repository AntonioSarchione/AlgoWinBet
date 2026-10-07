"""FotMob collector: per player and match expected goals (Opta xG, xGOT, xA), shots, fouls, cards and minutes, plus who was
missing, for the 7 domestic leagues and the European cups. Public pages, no key, no cookie.

Why FotMob: API-Football has no player xG and its free plan reads only matches linked when they were played; Understat has no
Liga Portugal and no Eredivisie; FBref lost the Opta data in January 2026. A FotMob match page carries the whole match in its
`__NEXT_DATA__` JSON, finished matches of past seasons included (verified 2026-10-06 on Serie A, Eredivisie and Liga Portugal).

Not an official API: the site may change without notice, so the collector is optional. Nothing reads its tables yet
(fotmob_player_stats, fotmob_absences): a feature that wants them is measured first, like every new source.

Per run (its own workflow, fotmob.yml, started by the collect ticks when pages are due; the coming matches are read first
by fotmobprematch.py):
  seasons ... /leagues/{id}/fixtures/x?season=S (one page per league and season, every match of that season, the coming ones
              with their id too): links FotMob matches to our results. A new result is first linked from the stored page, with no
              request; the current season is read again only when the stored page is older than an unlinked match's kickoff
              (a new season, a postponed match). Past seasons once.
  matches ... /match/{id} for finished linked matches without player stats: the last RECENT_DAYS first (at most
              RECENT_PER_TICK a tick), then the history newest first within the morning's history_seconds.
Every page is stored in the snapshot store as the compact JSON the collector reads (not the 1 MB HTML).

FotMob is optional, so it never fails a tick: a network error, or PAGE_STREAK pages in a row answering an error (5xx), ends
FotMob's part of the tick (no further request to a site that is not answering); a single broken page is skipped and tried again
later (PAGE_FAILS, PAGE_SKIP, PAGE_GIVE_UP); a refusal (401, 403, 429, a page without the data) pauses it for PAUSE_AFTER_BLOCK,
and any other error becomes a line of the tick's log. Only a dropped connection to Turso goes up, to the tick's own recovery.
"""
from __future__ import annotations

import http.client
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
PAUSE_JOB = "fotmob-pause"
BASE_URL = "https://www.fotmob.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
MIN_INTERVAL_S = 2.0  # one page every 2 seconds at most: a small, polite load
RECENT_DAYS = 10
RECENT_PER_TICK = 12
PAUSE_AFTER_BLOCK = timedelta(hours=12)
BLOCK_STATUS = {401, 403, 429}
TIMEOUT_S = 20.0
PAGE_FAILS = 3  # a page failing this many ticks in a row (a broken page, not the site) ...
PAGE_SKIP = timedelta(hours=24)  # ... waits this long before the next try, so it never stalls the pages after it
PAGE_GIVE_UP = timedelta(days=3)  # a match page still failing this long after its first failure is given up, like a 404
PAGE_STREAK = 3  # pages failing in a row (5xx...): then the site is the problem, not a page, and the tick stops
LINK_WINDOW = timedelta(hours=36)  # dates differ by time zone between sources
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
             "interceptions": "interceptions",
             # goalkeepers (read from 2026-10-07: the history before has none, the player cards estimate them from team stats)
             "saves": "saves", "goals_conceded": "goals_conceded", "expected_goals_on_target_faced": "xgot_faced"}
INT_COLS = {"minutes", "goals", "assists", "shots", "shots_on", "key_passes", "touches_box", "fouls_committed", "fouls_drawn",
            "tackles", "interceptions", "saves", "goals_conceded"}
GK_COLUMNS = {"saves": "INTEGER", "goals_conceded": "INTEGER", "xgot_faced": "REAL", "shots_on_faced": "INTEGER"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _utc(m: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(m["status"]["utcTime"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def _season_year(season: str) -> int:
    """'2025/2026' -> 2025 (and '2026' -> 2026); 0 when unreadable."""
    m = re.match(r"\d{4}", season)
    return int(m.group(0)) if m else 0


def add_gk_columns(store) -> None:
    have = {r[1] for r in store.db.execute("PRAGMA table_info(fotmob_player_stats)").fetchall()}
    for name, kind in GK_COLUMNS.items():
        if name not in have:
            store.db.execute(f"ALTER TABLE fotmob_player_stats ADD COLUMN {name} {kind}")
    store.db.commit()


def pid(fotmob_id) -> str:
    return f"fotmob:{fotmob_id}"


class FotMobError(RuntimeError):
    """status 0: no answer (network). blocked: a refusal, or a page without the data (a challenge page)."""

    def __init__(self, message: str, status: int = 0, blocked: bool = False):
        super().__init__(message)
        self.status, self.blocked = status, blocked

    @property
    def halts(self) -> bool:
        """Every error but a missing page (404) can end FotMob's part of the tick: a page's own error (5xx) only the
        PAGE_STREAK-th in a row (see FotMobCollector._fail)."""
        return self.status != 404


class FotMobClient:
    """Reads the JSON a FotMob page embeds. No key and no cookie: a plain GET with a browser user agent."""

    def __init__(self, store: SnapshotStore | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), base_url: str = BASE_URL,
                 min_interval: float = MIN_INTERVAL_S):
        self.store, self.transport = store, transport or urllib_transport(timeout=TIMEOUT_S)
        self.min_interval = min_interval
        self.sleep, self.clock, self.now, self.base = sleep, clock, now, base_url
        self._last = -1e9
        self.requests_sent = 0

    def page(self, path: str, params: dict | None = None) -> dict:
        """pageProps of the page at path (with its query), or FotMobError."""
        wait = self.min_interval - (self.clock() - self._last)
        if wait > 0:
            self.sleep(wait)
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        try:
            status, _, body = self.transport(f"{self.base}{path}" + (f"?{query}" if query else ""),
                                             {"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en"})
        except (OSError, http.client.HTTPException) as e:  # timeout, refused or reset connection, DNS, TLS, truncated body
            raise FotMobError(f"nessuna risposta su {path} ({type(e).__name__}: {e})", 0) from e
        finally:
            self._last = self.clock()
            self.requests_sent += 1
        if status != 200:
            raise FotMobError(f"{status} su {path}", status, blocked=status in BLOCK_STATUS)
        m = _NEXT_DATA.search(body.decode("utf-8", "replace"))
        if not m:
            raise FotMobError(f"pagina senza __NEXT_DATA__: {path}", status, blocked=True)
        try:
            return json.loads(m.group(1))["props"]["pageProps"]
        except (ValueError, KeyError, TypeError) as e:
            raise FotMobError(f"JSON della pagina non leggibile: {path}", status) from e

    def keep(self, path: str, params: dict | None, payload: dict) -> int | None:
        """Stores what the collector reads from a page (the page itself is about 1 MB of HTML); the stored row's id."""
        if self.store:
            return self.store.put_raw(SOURCE, path, params, 200, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(), self.now(), cost=0)
        return None


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
        self._halted = False
        self._streak = 0  # page errors in a row
        self._fails: dict[str, tuple[int, datetime]] = {}
        self._fail_at: dict[str, datetime] = {}
        store.db.executescript(SCHEMA)
        add_gk_columns(store)

    def _fail(self, st: CollectStats, what: str, e: FotMobError, page: str | None = None) -> None:
        st.errors.append(f"{what}: {e}")
        page_error = bool(page) and e.status not in (0, 404) and not e.blocked  # an error of this page (5xx...), not of the network or a refusal
        if page_error:
            n, first = self._fails.get(page, (0, self.now()))
            self._fails[page] = (n + 1, first)
            self.store.mark_job(f"fotmob-fail:{page}", self.now(), f"{n + 1} {first.isoformat()}")
            self._streak += 1
        if not e.halts or self._halted:
            return
        if page_error and self._streak < PAGE_STREAK:
            return  # one broken page: the pages after it are read (it is tried again on a later tick)
        self._halted = True
        if e.blocked:
            until = self.now() + PAUSE_AFTER_BLOCK
            self.store.mark_job(PAUSE_JOB, self.now(), until.isoformat())
            st.skipped.append(f"FotMob rifiuta le richieste: in pausa fino alle {until:%H:%M} UTC del {until:%d/%m}")
        else:
            st.skipped.append("FotMob non risponde: il resto al prossimo giro")

    def _load_fails(self) -> None:
        self._fails, self._fail_at = {}, {}
        for name, at, detail in self._jobs("fotmob-fail:"):
            try:
                n, first = detail.split(" ", 1)
                self._fails[name[len("fotmob-fail:"):]] = (int(n), datetime.fromisoformat(first))
                self._fail_at[name[len("fotmob-fail:"):]] = datetime.fromisoformat(at)
            except ValueError:
                continue

    def _skip(self, page: str) -> bool:
        """A page that failed PAGE_FAILS ticks in a row waits PAGE_SKIP after its last failure."""
        n, _ = self._fails.get(page, (0, None))
        return n >= PAGE_FAILS and self.now() - self._fail_at.get(page, self.now()) < PAGE_SKIP

    def _ok(self, page: str) -> None:
        self._streak = 0
        if page in self._fails:
            del self._fails[page]
            self.store.db.execute("DELETE FROM jobs WHERE name=?", (f"fotmob-fail:{page}",))

    def paused_until(self) -> datetime | None:
        row = self.store.db.execute("SELECT detail FROM jobs WHERE name=?", (PAUSE_JOB,)).fetchone()
        try:
            until = datetime.fromisoformat(row[0]) if row and row[0] else None
        except ValueError:
            return None
        return until if until and until > self.now() else None

    def _jobs(self, prefix: str) -> list[tuple[str, str, str]]:
        """jobs rows whose name starts with prefix, through the primary key (a LIKE would read the whole table)."""
        return self.store.db.execute("SELECT name, done_at, detail FROM jobs WHERE name >= ? AND name < ?",
                                     (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1))).fetchall()

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
        ko = _utc(m)
        try:
            home, away = self.names.canon(m["home"]["name"]), self.names.canon(m["away"]["name"])
        except (KeyError, TypeError):
            return None
        if ko is None:
            return None
        near = [r for r in results if abs(r.kickoff - ko) <= LINK_WINDOW]
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
        # the seasons before the one the page shows, newest first, whatever order FotMob lists them in (newest first, 2026-10-06)
        shown = str((pp.get("details") or {}).get("selectedSeason") or season or "")
        seasons = sorted({str(x) for x in pp.get("allAvailableSeasons") or []} - {shown}, key=_season_year, reverse=True)
        seasons = [x for x in seasons if not shown or _season_year(x) < _season_year(shown)]
        raw = self.client.keep(f"/leagues/{league.fotmob_id}/fixtures", {"season": season or "current"},
                               {"matches": matches, "seasons": seasons, "shown": shown})
        self.store.mark_job(f"fotmob-page:{league.fotmob_id}:{season or 'current'}", self.now(), str(raw or ""))
        return matches, seasons

    def _stored_pages(self, league: FotMobLeague) -> dict[str, tuple[datetime, dict]]:
        """season ('current' or 'AAAA/AAAA') -> (when it was read, its stored payload), with no request."""
        out = {}
        for name, at, raw in self._jobs(f"fotmob-page:{league.fotmob_id}:"):
            body = self.store.raw_body(int(raw)) if raw and raw.isdigit() else b""
            if body:
                out[name.split(":", 2)[2]] = (datetime.fromisoformat(at), json.loads(body))
        return out

    def _link(self, st: CollectStats, league: FotMobLeague, matches: list[dict], results, report: bool = True) -> None:
        """Links the page's matches to our results (only a finished match has one: a coming match simply finds none).
        report: list the finished FotMob matches without a result of ours (only for a page just read: a stored one is stale)."""
        have = set(self._links())
        rows, missed = [], []
        first = min((r.kickoff for r in results), default=None)
        for m in matches:
            fid = self._match(m, results)
            if fid is None:
                ko = str(m["status"]["utcTime"] or "")
                if report and m["status"]["finished"] and first is not None and ko >= first.strftime("%Y-%m-%d"):  # older: not ours
                    missed.append(f"{m['home']['name']}-{m['away']['name']} {ko[:10]}")
            elif fid not in have:
                rows.append((SOURCE, m["id"], fid, self.now().isoformat()))
                have.add(fid)
        st.add("partite collegate", self.store._bulk("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at)", rows))
        if missed:  # a name to add to configs/team_aliases.json, or a match our results do not have
            st.skipped.append(f"{league.competition}: {len(missed)} partite FotMob finite senza risultato nostro (es. {', '.join(missed[:3])})")

    def sync_seasons(self, st: CollectStats, history: bool) -> None:
        """Links the results of the last RECENT_DAYS from the stored season pages first (no request). The current season is
        read again only when an unlinked one kicked off after the stored page was read: that page may carry the match's old
        date, or be last season's. With history, the stored pages link everything again (an alias added since) and the
        previous history_seasons are read once each (the seasons our results backfill holds)."""
        t = self.now()
        for lg in self.leagues:
            if self._halted:
                break
            results, links = self._results(lg), self._links()
            recent = [r for r in results if t - r.kickoff <= timedelta(days=RECENT_DAYS) and r.fixture_id not in links]
            if not recent and not history:
                continue
            pages = self._stored_pages(lg)
            if history:
                for _, payload in pages.values():
                    self._link(st, lg, payload.get("matches") or [], results, report=False)
            else:  # only the stored matches around the unlinked results: a result FotMob lacks costs nothing each tick
                lo, hi = min(r.kickoff for r in recent) - LINK_WINDOW, max(r.kickoff for r in recent) + LINK_WINDOW
                for _, payload in pages.values():
                    near = [m for m in payload.get("matches") or [] if lo <= (_utc(m) or lo - LINK_WINDOW) <= hi]
                    self._link(st, lg, near, recent, report=False)
            links = self._links()
            recent = [r for r in recent if r.fixture_id not in links]
            last, stored = pages.get("current", (None, {}))
            seasons = [str(x) for x in stored.get("seasons") or []]
            page = f"season:{lg.fotmob_id}:current"
            if ((last is None and (recent or history)) or (last is not None and any(r.kickoff > last for r in recent))) and not self._skip(page):
                try:
                    matches, seasons = self._season_page(lg, None)
                except FotMobError as e:
                    self._fail(st, lg.competition, e, page)
                    continue
                self._ok(page)
                st.requests += 1
                self._link(st, lg, matches, results)
            if not history:
                continue
            for season in seasons[:self.history_seasons]:
                page = f"season:{lg.fotmob_id}:{season}"
                if season in pages or self._halted or self._skip(page):
                    continue
                try:
                    matches, _ = self._season_page(lg, season)
                except FotMobError as e:
                    self._fail(st, f"{lg.competition} {season}", e, page)
                    continue
                self._ok(page)
                st.requests += 1
                self._link(st, lg, matches, results)
        self.store.db.commit()

    # ------------------------------------------------------------------ matches
    def due(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """(recent, history): (our fixture id, FotMob id) of linked finished matches without player stats, newest first."""
        t = self.now()
        have = {r[0] for r in self.store.db.execute("SELECT DISTINCT fixture_id FROM fotmob_player_stats").fetchall()}
        gone = {name[len("fotmob-none:"):] for name, _, _ in self._jobs("fotmob-none:")}
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
            if self._halted:
                break
            if deadline is not None and self.clock() > deadline:
                st.skipped.append("tempo FotMob esaurito: il resto al prossimo giro")
                break
            page = f"match:{ext}"
            if self._skip(page):
                continue
            try:
                pp = self.client.page(f"/match/{ext}")
            except FotMobError as e:
                self._fail(st, f"partita {ext}", e, page)
                if e.status == 404:
                    self.store.mark_job(f"fotmob-none:{fid}", self.now(), "pagina FotMob assente")
                elif page in self._fails and self._fails[page][0] >= PAGE_FAILS and self.now() - self._fails[page][1] >= PAGE_GIVE_UP:
                    self.store.mark_job(f"fotmob-none:{fid}", self.now(), f"pagina FotMob in errore da {PAGE_GIVE_UP.days} giorni ({e})")
                    st.skipped.append(f"partita {ext} abbandonata: pagina FotMob in errore da {PAGE_GIVE_UP.days} giorni")
                continue
            self._ok(page)
            st.requests += 1
            r = self.provider.result_of(fid)
            stats, absences, kept = self.parse_match(pp, fid, r.home if r else None, r.away if r else None)
            self.client.keep(f"/match/{ext}", None, kept)
            if stats:
                st.add("statistiche giocatori FotMob", self.store._bulk(
                    "INSERT OR REPLACE INTO fotmob_player_stats(fixture_id,team,player_id,name,position,starter,minutes,rating,goals,assists,"
                    "xg,npxg,xgot,xa,shots,shots_on,key_passes,touches_box,fouls_committed,fouls_drawn,yellow,red,tackles,interceptions,"
                    "observed_at,saves,goals_conceded,xgot_faced,shots_on_faced)", stats))
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
                          row["fouls_drawn"] or 0, y, red, row["tackles"] or 0, row["interceptions"] or 0, at, row["saves"],
                          row["goals_conceded"], row["xgot_faced"],
                          None if row["saves"] is None or row["goals_conceded"] is None else row["saves"] + row["goals_conceded"]))
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

    def backfill(self, seconds: float, progress: Callable[[str], None] | None = None) -> tuple[CollectStats, int]:
        """The whole history in one long run (fotmob-backfill workflow), instead of a slice a day: season pages, then the
        past matches newest first until `seconds` run out. Takes the day's history slot first, so the ticks running at the
        same time leave the history alone (they keep the recent matches); the next day's slot too, for a run that goes past
        midnight. Returns the stats and the matches still to read (-1: unknown). Never raises: a dropped connection to Turso
        ends the run like a network error, and the workflow launches the next one."""
        from .autorun import transient_db_error
        st = CollectStats("fotmob-backfill")
        self._history, self._halted, self._streak = None, False, 0
        left = -1
        try:
            if self.paused_until():
                st.skipped.append(f"FotMob in pausa fino alle {self.paused_until():%H:%M} UTC")
                return st, left
            self._load_fails()
            for d in (self.now().date(), self.now().date() + timedelta(days=1)):
                self.store.mark_job(f"fotmob-history:{d.isoformat()}", self.now(), "caricamento storico")
            deadline = self.clock() + seconds
            self.sync_seasons(st, True)
            _, past = self.due()
            for k in range(0, len(past), 100):  # progress every 100 matches
                if self._halted or self.clock() > deadline:
                    break
                self.sync_matches(st, past[k:k + 100], None, deadline)
                if progress:
                    progress(f"{min(k + 100, len(past))}/{len(past)} partite storiche · {st.requests} pagine · "
                             f"{st.saved.get('statistiche giocatori FotMob', 0)} righe giocatore · {len(st.errors)} errori")
            left = len(self.due()[1])
        except Exception as e:  # noqa: BLE001 - a long unattended run: everything is a log line, the next run goes on
            what = "connessione a Turso caduta" if transient_db_error(e) else "errore imprevisto"
            st.errors.append(f"FotMob: {what} ({type(e).__name__}: {e})")
        return st, left

    def run(self, history_seconds: float = 0.0) -> CollectStats:
        """One tick of FotMob. Never raises, but for a dropped connection to Turso (see the module docstring)."""
        from .autorun import transient_db_error
        st = CollectStats("fotmob")
        self._history, self._halted, self._streak = None, False, 0
        try:
            if self.paused_until():
                return st  # the tick that paused it wrote the reason in its log
            self._load_fails()
            history = history_seconds >= HISTORY_MIN_S and self.history_due()
            self.sync_seasons(st, history)
            recent, past = self.due()
            self.sync_matches(st, recent, RECENT_PER_TICK, None)
            if history and not self._halted:
                self.sync_matches(st, past, None, self.clock() + history_seconds)
                if not self._halted:  # a slice cut short by an error is tried again by the next tick
                    self.store.mark_job(f"fotmob-history:{self.now().date().isoformat()}", self.now(), f"{len(past)} partite storiche da leggere")
        except Exception as e:  # noqa: BLE001 - an optional source: a bug or a changed page is a log line, never a failed run
            if transient_db_error(e):
                raise
            st.errors.append(f"FotMob: errore imprevisto ({type(e).__name__}: {e})")
        return st

