"""API-Football collector (Fase 6-bis): who is out, who starts, every squad. Free plan, 100 requests a day.

What the free plan allows (verified live 2026-10-02): no `season` parameter for the current season, dates only from
yesterday to tomorrow, no `ids` batch; one fixture per lineup request; squads by team. So, per tick:

  days ...... /fixtures?date=D for today and tomorrow, once each (1 request covers every league): links API-Football fixtures
              to our calendar and learns which API-Football team is which of our teams
  injuries .. /injuries?date=D for today and tomorrow (1 request each, every league), today's again after midday
  lineups ... /fixtures/lineups?fixture=ID from 55 minutes before kickoff until both XI are in (1 request per match)
  squads .... /players/squads?team=ID, refreshed every SQUAD_DAYS, teams playing soon first (roles of every player)
  finished .. for registry selections that need more than the final score: /fixtures?date=D again after the matches of D
              (half-time scores of every league, 1 request) and /fixtures/events?fixture=ID for first / last goal (1 each)

The 7 domestic leagues come first: cups and national teams only use what the leagues of the day leave over.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .collector import CollectStats
from .domain import LineupSnapshot, MatchStat, Player, Position
from .markets import needs_goal_order, needs_half_time
from .names import TeamNames, normalize
from .providers.apifootball import SOURCE, ApiFootballClient, ApiFootballError
from .snapshots import BudgetExceeded, SnapshotProvider, SnapshotStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS apif_teams(team_id INTEGER PRIMARY KEY, name TEXT, apif_name TEXT, squad_at TEXT);
CREATE TABLE IF NOT EXISTS player_status(id INTEGER PRIMARY KEY, source TEXT, fixture_id TEXT, team TEXT, player_id TEXT,
  player_name TEXT, status TEXT, reason TEXT, observed_at TEXT, UNIQUE(source, fixture_id, player_id, status, reason));
CREATE INDEX IF NOT EXISTS ix_status_fx ON player_status(fixture_id);
CREATE TABLE IF NOT EXISTS fixture_links(source TEXT, ext_id TEXT, fixture_id TEXT, linked_at TEXT, PRIMARY KEY(source, ext_id));
-- per player and match (API-Football /fixtures/players): shots, fouls and cards, which no other free source gives
CREATE TABLE IF NOT EXISTS player_match_stats(fixture_id TEXT, source TEXT, team TEXT, player_id TEXT, name TEXT, position TEXT,
  minutes INTEGER, substitute INTEGER, rating REAL, shots INTEGER, shots_on INTEGER, goals INTEGER, assists INTEGER, key_passes INTEGER,
  fouls_committed INTEGER, fouls_drawn INTEGER, yellow INTEGER, red INTEGER, observed_at TEXT, PRIMARY KEY(fixture_id, source, player_id));
"""

# Official XI from 60 minutes before kickoff, then every tick (15 minutes): before that the page shows our probable lineup
# (user's choice, 2026-10-06: it saves the API-Football requests of the early tries, mostly empty).
LINEUP_FROM = timedelta(minutes=60)
LINEUP_UNTIL = timedelta(minutes=10)  # still worth one try just after kickoff (late publications)
SQUAD_DAYS = 30
FINISHED_AFTER = timedelta(hours=2, minutes=15)  # from kickoff: half-time score and goal events are final
MAX_EVENTS = 10  # goal-order requests per tick
# Player match stats: finished matches of the last PLAYER_STATS_DAYS days, at most PLAYER_STATS_PER_TICK a tick (6.5 s each on
# the free plan), only with what the day leaves after the lineups still due today plus PLAYER_STATS_KEEP requests.
# The free plan lists fixtures only from yesterday to tomorrow, so only matches linked when they were played can be read:
# the history starts with the first matches after 2026-10-06 (no `last` or `season` parameter on the free plan).
PLAYER_STATS_DAYS = 10
PLAYER_STATS_PER_TICK = 6
PLAYER_STATS_KEEP = 10
INJURY_REFRESH = timedelta(hours=6)
POS = {"g": Position.GK, "goalkeeper": Position.GK, "d": Position.DEF, "defender": Position.DEF, "m": Position.MID,
       "midfielder": Position.MID, "f": Position.FWD, "attacker": Position.FWD, "forward": Position.FWD}
_SUSPENDED = re.compile(r"suspend|red card|yellow card|ban", re.I)


def pid(api_id) -> str:
    return f"apif:{api_id}"


def status_of(kind: str, reason: str) -> str:
    """API-Football injury rows: type "Missing Fixture" (out) or "Questionable" (doubtful), with a free-text reason."""
    if (kind or "").lower().startswith("questionable"):
        return "DOUBTFUL"
    return "SUSPENDED" if _SUSPENDED.search(reason or "") else "OUT"


def _sim(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


@dataclass
class ApifLeague:
    api_id: int
    competition: str
    domestic: bool


class ApiFootballCollector:
    def __init__(self, client: ApiFootballClient, store: SnapshotStore, leagues: list[ApifLeague], names: TeamNames | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), squads_per_day: int = 20):
        self.client, self.store, self.now = client, store, now
        self.leagues = {l.api_id: l for l in leagues}
        self.names = names or TeamNames()
        self.squads_per_day = squads_per_day
        self.provider = SnapshotProvider(store)
        store.db.executescript(SCHEMA)
        if "detail" not in {r[1] for r in store.db.execute("PRAGMA table_info(lineups)").fetchall()}:
            # shirt number and pitch position ("row:col") of every player: the dashboard draws the formation on a pitch
            store.db.execute("ALTER TABLE lineups ADD COLUMN detail TEXT")
            store.db.commit()

    # ------------------------------------------------------------------ helpers
    def _last_fetch(self, endpoint: str, params: dict) -> datetime | None:
        row = self.store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint=? AND params=? AND status=200",
                                    (SOURCE, endpoint, json.dumps(params, sort_keys=True))).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def _fetches_today(self, endpoint: str) -> int:
        day = self.now().strftime("%Y-%m-%d")
        return self.store.db.execute("SELECT COUNT(*) FROM raw_requests WHERE source=? AND endpoint=? AND fetched_at >= ?",
                                     (SOURCE, endpoint, day)).fetchone()[0]

    def _links(self) -> dict[str, str]:
        """our fixture id -> API-Football fixture id"""
        return {fid: ext for ext, fid in self.store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall()}

    def _team_names(self) -> dict[int, str]:
        return {tid: name for tid, name in self.store.db.execute("SELECT team_id, name FROM apif_teams WHERE name IS NOT NULL").fetchall()}

    def _domestic(self, competition: str) -> bool:
        """Names may differ in spacing or case between the config and the calendar source."""
        return " ".join(competition.lower().split()) in {" ".join(l.competition.lower().split()) for l in self.leagues.values() if l.domestic}

    def _remaining(self) -> int | None:
        b = self.client.budget
        return None if b is None else b.remaining()["daily"]

    # ------------------------------------------------------------------ days
    def sync_days(self, st: CollectStats) -> None:
        """Today and tomorrow once each: link fixtures, learn team ids."""
        t = self.now()
        calendar = self.provider.list_fixtures(None, t - timedelta(days=1), t + timedelta(days=3))
        for d in (t.date(), (t + timedelta(days=1)).date()):
            params = {"date": d.isoformat()}
            last = self._last_fetch("/fixtures", params)
            if last is not None and t - last < timedelta(hours=30):
                continue
            rows = self.client.get("/fixtures", params).get("response") or []
            linked = 0
            teams: dict[int, tuple[str, str]] = {}
            refs: list[tuple[str, str]] = []
            for r in rows:
                lg = self.leagues.get(int(r["league"]["id"]))
                if not lg:
                    continue
                fx = self._match(r, [f for f in calendar if " ".join(f.competition.lower().split()) == " ".join(lg.competition.lower().split())]
                                 or calendar)
                if fx is None:
                    st.report.gap(f"api-football senza corrispondenza: {r['teams']['home']['name']}-{r['teams']['away']['name']}")
                    continue
                self.store.db.execute("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES(?,?,?,?)",
                                      (SOURCE, str(r["fixture"]["id"]), fx.id, t.isoformat()))
                teams[int(r["teams"]["home"]["id"])] = (fx.home, r["teams"]["home"]["name"])
                teams[int(r["teams"]["away"]["id"])] = (fx.away, r["teams"]["away"]["name"])
                if (r.get("fixture") or {}).get("referee"):
                    refs.append((fx.id, r["fixture"]["referee"]))
                linked += 1
            if refs:
                st.add("arbitri", self.store.save_referees(SOURCE, refs, t))
            for tid, (name, api_name) in teams.items():
                self.store.db.execute("INSERT INTO apif_teams(team_id, name, apif_name) VALUES(?,?,?) "
                                      "ON CONFLICT(team_id) DO UPDATE SET name=excluded.name, apif_name=excluded.apif_name", (tid, name, api_name))
            self.store.db.commit()
            st.add("fixture links", linked)

    def _match(self, r: dict, calendar):
        ko = datetime.fromisoformat(r["fixture"]["date"])
        home, away = self.names.canon(r["teams"]["home"]["name"]), self.names.canon(r["teams"]["away"]["name"])
        near = [f for f in calendar if abs(f.kickoff - ko) <= timedelta(hours=3)]
        exact = [f for f in near if f.home == home and f.away == away]
        if len(exact) == 1:
            return exact[0]
        scored = sorted(((min(_sim(home, f.home), _sim(away, f.away)), f) for f in near), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.15):
            return scored[0][1]
        return None

    # ------------------------------------------------------------------ injuries
    def sync_injuries(self, st: CollectStats) -> None:
        t = self.now()
        rev = {ext: fid for fid, ext in self._links().items()}
        names = self._team_names()
        for d in (t.date(), (t + timedelta(days=1)).date()):
            params = {"date": d.isoformat()}
            last = self._last_fetch("/injuries", params)
            if last is not None and (last.date() == t.date() if d != t.date() else t - last < INJURY_REFRESH):
                continue
            if d == t.date() and not self._fixtures_on(d, rev):
                continue
            rows = self.client.get("/injuries", params).get("response") or []
            out = []
            for r in rows:
                fid = rev.get(str(r["fixture"]["id"]))
                team = names.get(int(r["team"]["id"]))
                if not fid or not team:
                    continue
                p = r["player"]
                out.append((SOURCE, fid, team, pid(p["id"]), p.get("name"), status_of(p.get("type"), p.get("reason")),
                            p.get("reason"), t.isoformat()))
            # the list is complete for its date: a player no longer listed is available again, so the date's rows are replaced
            on_day = [fid for fid in rev.values() if fid in self._kickoff_dates() and self._kickoff_dates()[fid] == d]
            if on_day:
                self.store.db.execute(f"DELETE FROM player_status WHERE source=? AND fixture_id IN ({','.join('?' * len(on_day))})",
                                      [SOURCE, *on_day])
            st.add("player status", self.store._bulk("INSERT OR IGNORE INTO player_status(source,fixture_id,team,player_id,player_name,"
                                                     "status,reason,observed_at)", out))

    def _kickoff_dates(self) -> dict:
        if not hasattr(self, "_ko"):
            t = self.now()
            self._ko = {f.id: f.kickoff.date() for f in self.provider.list_fixtures(None, t - timedelta(days=1), t + timedelta(days=3))}
        return self._ko

    def _fixtures_on(self, d, rev) -> bool:
        return any(f.kickoff.date() == d for f in self.provider.list_fixtures(None, self.now(), self.now() + timedelta(days=1))
                   if f.id in set(rev.values()))

    # ------------------------------------------------------------------ lineups
    def due_lineups(self) -> list[tuple[str, str, bool]]:
        """(our fixture id, API-Football id, domestic) inside the lineup window without both confirmed XI from API-Football."""
        t = self.now()
        links = self._links()
        have = {}
        for fid, team in self.store.db.execute("SELECT fixture_id, team FROM lineups WHERE source=? AND status='confirmed'", (SOURCE,)).fetchall():
            have.setdefault(fid, set()).add(team)
        out = []
        for f in self.provider.list_fixtures(None, t - LINEUP_UNTIL, t + LINEUP_FROM):
            if f.id in links and not {f.home, f.away} <= have.get(f.id, set()):  # linked = one of our leagues
                out.append((f.id, links[f.id], self._domestic(f.competition), f.kickoff))
        out.sort(key=lambda x: (not x[2], x[3]))  # domestic leagues first, then by kickoff
        return [(a, b, c) for a, b, c, _ in out]

    def sync_lineups(self, st: CollectStats, reserve_for_leagues: int = 0) -> None:
        due = self.due_lineups()
        names = self._team_names()
        for fid, ext, domestic in due:
            if not domestic:
                left = self._remaining()
                if left is not None and left - reserve_for_leagues < 1:
                    st.skipped.append(f"{fid}: coppa/nazionale rimandata (budget per i campionati)")
                    continue
            env = self.client.get("/fixtures/lineups", {"fixture": ext})
            if domestic:
                reserve_for_leagues = max(0, reserve_for_leagues - 1)
            lus, players = self._lineups(env.get("response") or [], fid, names)
            if players:
                self._save_players(players, keep_position=True)
            st.add("lineups", self.store.save_lineups(SOURCE, lus))
            self._save_detail(env.get("response") or [], fid, names)

    def _lineups(self, rows: list[dict], fid: str, names: dict[int, str]) -> tuple[list[LineupSnapshot], list[Player]]:
        t = self.now()
        lus, players = [], []
        for r in rows:
            team = names.get(int(r["team"]["id"]))
            xi = [x["player"] for x in r.get("startXI") or []]
            if not team or len(xi) != 11:
                continue
            bench = [x["player"] for x in r.get("substitutes") or []]
            for p in xi + bench:
                pos = POS.get(str(p.get("pos") or "").lower())
                if pos and p.get("id"):
                    players.append(Player(id=pid(p["id"]), name=p.get("name") or str(p["id"]), team=team, position=pos))
            lus.append(LineupSnapshot(fixture_id=fid, team=team, status="confirmed", formation=r.get("formation"),
                                      starters=[pid(p["id"]) for p in xi if p.get("id")], bench=[pid(p["id"]) for p in bench if p.get("id")],
                                      published_at=t, observed_at=t, source_level="A"))
        return lus, players

    def _save_detail(self, rows: list[dict], fid: str, names: dict[int, str]) -> None:
        for r in rows:
            team = names.get(int(r["team"]["id"]))
            if not team:
                continue
            detail = {pid(x["player"]["id"]): {"n": x["player"].get("number"), "g": x["player"].get("grid")}
                      for x in (r.get("startXI") or []) + (r.get("substitutes") or []) if x["player"].get("id")}
            self.store.db.execute("UPDATE lineups SET detail=? WHERE fixture_id=? AND team=? AND source=? AND detail IS NULL",
                                  (json.dumps(detail), fid, team, SOURCE))
        self.store.db.commit()

    def backfill_detail(self) -> int:
        """Lineups saved before the detail column existed: numbers and positions from the stored raw responses (no request)."""
        todo = self.store.db.execute("SELECT DISTINCT fixture_id FROM lineups WHERE source=? AND detail IS NULL", (SOURCE,)).fetchall()
        links, names, n = self._links(), self._team_names(), 0
        for (fid,) in todo:
            ext = links.get(fid)
            row = ext and self.store.db.execute(
                "SELECT id FROM raw_requests WHERE source=? AND endpoint='/fixtures/lineups' AND params=? AND status=200 ORDER BY id DESC LIMIT 1",
                (SOURCE, json.dumps({"fixture": ext}, sort_keys=True))).fetchone()
            if row:
                self._save_detail(json.loads(self.store.raw_body(row[0])).get("response") or [], fid, names)
                n += 1
        return n

    # ------------------------------------------------------------------ squads
    def sync_squads(self, st: CollectStats, reserve_for_leagues: int = 0) -> None:
        t = self.now()
        allowed = self.squads_per_day - self._fetches_today("/players/squads")
        if allowed <= 0:
            return
        soon = {team for f in self.provider.list_fixtures(None, t, t + timedelta(days=3)) for team in (f.home, f.away)}
        dom_teams = {team for f in self.provider.list_fixtures(None, t - timedelta(days=60), t + timedelta(days=14))
                     if self._domestic(f.competition) for team in (f.home, f.away)}
        rows = self.store.db.execute("SELECT team_id, name, squad_at FROM apif_teams WHERE name IS NOT NULL").fetchall()
        stale = [(tid, name) for tid, name, at in rows if at is None or t - datetime.fromisoformat(at) > timedelta(days=SQUAD_DAYS)]
        stale.sort(key=lambda x: (x[1] not in soon, x[1] not in dom_teams))
        for tid, name in stale[:allowed]:
            left = self._remaining()
            if left is not None and left - reserve_for_leagues < 1:
                st.skipped.append("rose rimandate: budget del giorno tenuto per le formazioni")
                break
            resp = self.client.get("/players/squads", {"team": tid}).get("response") or []
            players = [Player(id=pid(p["id"]), name=p.get("name") or str(p["id"]), team=name, position=POS[str(p.get("position")).lower()])
                       for s in resp for p in s.get("players") or [] if str(p.get("position")).lower() in POS and p.get("id")]
            self._save_players(players, keep_position=False)
            self.store.db.execute("UPDATE apif_teams SET squad_at=? WHERE team_id=?", (t.isoformat(), tid))
            self.store.db.commit()
            st.add("players", len(players))

    def _save_players(self, players: list[Player], keep_position: bool) -> None:
        """Squads give the official role; a lineup only adds players a squad does not list yet (new signings, youth)."""
        if keep_position:
            known = {r[0] for r in self.store.db.execute(
                f"SELECT id FROM players WHERE id IN ({','.join('?' * len(players))})", [p.id for p in players]).fetchall()}
            players = [p for p in players if p.id not in known]
        if players:
            self.store.save_players(SOURCE, list({p.id: p for p in players}.values()), self.now())

    # ------------------------------------------------------------------ tick
    def run(self, max_seconds: float = 120.0, clock: Callable[[], float] = time.monotonic) -> CollectStats:
        """Everything due now, cheapest decisions first. Lineups are never starved: every other step keeps back what the
        domestic lineups still due today may need."""
        st = CollectStats("api-football")
        t0 = clock()
        before = self.client.requests_sent
        try:
            self.backfill_detail()
            self.sync_days(st)
            reserve = 2 * self._league_matches_left_today()  # up to 2 tries each for the league lineups still to come today
            self.sync_lineups(st, reserve_for_leagues=reserve)
            reserve = 2 * self._league_matches_left_today()
            self.sync_injuries(st)
            self.sync_finished(st)
            if clock() - t0 < max_seconds:
                self.sync_player_stats(st, reserve_for_leagues=reserve)
            if clock() - t0 < max_seconds:
                self.sync_squads(st, reserve_for_leagues=reserve)
        except BudgetExceeded as e:
            st.stopped_by_budget = True
            st.errors.append(str(e))
        except ApiFootballError as e:
            st.errors.append(str(e))
        except Exception as e:  # a bug here must never cost the rest of the tick (odds, history, publication)
            st.errors.append(f"errore interno api-football: {type(e).__name__}: {e}")
        st.requests = self.client.requests_sent - before
        return st

    # ------------------------------------------------------------------ finished matches (registry)
    def _pending_detail(self) -> tuple[dict, set]:
        """Registry selections still open (or closed as not settleable) of matches that ended since yesterday and need the
        half-time score or the goal order: ({date: last kickoff + FINISHED_AFTER} to read again, {fixture ids for events})."""
        t = self.now()
        since = datetime.combine(t.date() - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        try:
            rows = self.store.db.execute(
                "SELECT DISTINCT fixture_id, sel_key, kickoff FROM paper_legs WHERE (result IS NULL OR result = 'non valutabile') "
                "AND kickoff >= ? AND kickoff <= ?", (since.isoformat(), (t - FINISHED_AFTER).isoformat())).fetchall()
        except Exception:  # registry not created yet (first publication pending)
            return {}, set()
        dates: dict = {}
        events: set = set()
        for fid, key, ko in rows:
            code = key.split("|", 1)[0]
            k = datetime.fromisoformat(ko)
            if needs_half_time(code) and "goals" not in self.store.stats_of(fid, "1H"):
                dates[k.date()] = max(dates.get(k.date(), k), k)
            if needs_goal_order(code) and "first_goal_minute" not in self.store.stats_of(fid, "FT"):
                events.add(fid)
        return {d: k + FINISHED_AFTER for d, k in dates.items()}, events

    def sync_finished(self, st: CollectStats) -> None:
        dates, events = self._pending_detail()
        stats: list[MatchStat] = []
        refs: list[tuple[str, str]] = []
        for d, ready in sorted(dates.items()):
            params = {"date": d.isoformat()}
            last = self._last_fetch("/fixtures", params)
            if last is not None and last >= ready:
                continue  # already read after these matches ended: no half-time score there
            day0 = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
            calendar = self.provider.list_fixtures(None, day0 - timedelta(hours=6), day0 + timedelta(hours=30))
            links = {ext: fid for fid, ext in self._links().items()}
            for r in self.client.get("/fixtures", params).get("response") or []:
                if not self.leagues.get(int(r["league"]["id"])):
                    continue
                fid = links.get(str(r["fixture"]["id"]))
                if fid is None:
                    fx = self._match(r, calendar)
                    if fx is None:
                        continue
                    fid = fx.id
                    self.store.db.execute("INSERT OR REPLACE INTO fixture_links(source, ext_id, fixture_id, linked_at) VALUES(?,?,?,?)",
                                          (SOURCE, str(r["fixture"]["id"]), fid, self.now().isoformat()))
                if (r.get("fixture") or {}).get("referee"):
                    refs.append((fid, r["fixture"]["referee"]))
                ht = (r.get("score") or {}).get("halftime") or {}
                done = str(((r.get("fixture") or {}).get("status") or {}).get("short") or "") in ("FT", "AET", "PEN")
                if done and ht.get("home") is not None and ht.get("away") is not None:
                    stats.append(MatchStat(fixture_id=fid, period="1H", stat="goals", home=float(ht["home"]), away=float(ht["away"]),
                                           observed_at=self.now()))
        links = self._links()
        names = self._team_names()
        for fid in sorted(events)[:MAX_EVENTS]:
            ext, r = links.get(fid), self.provider.result_of(fid)
            if not ext or r is None:
                continue
            got = self._goal_minutes(self.client.get("/fixtures/events", {"fixture": ext}).get("response") or [], names, r)
            if got:
                first, last = got
                stats.append(MatchStat(fixture_id=fid, period="FT", stat="first_goal_minute", home=first[0], away=first[1], observed_at=self.now()))
                stats.append(MatchStat(fixture_id=fid, period="FT", stat="last_goal_minute", home=last[0], away=last[1], observed_at=self.now()))
        if refs:
            st.add("arbitri", self.store.save_referees(SOURCE, refs, self.now()))
        if stats:
            st.add("dettagli partite finite", self.store.save_stats(SOURCE, stats))
        self.store.db.commit()

    # ------------------------------------------------------------------ player match stats
    def player_stats_due(self) -> list[tuple[str, str]]:
        """(our fixture id, API-Football id) of linked matches finished in the last PLAYER_STATS_DAYS days with a stored result
        and no player stats yet, domestic leagues first, newest first."""
        t = self.now()
        have = {r[0] for r in self.store.db.execute("SELECT DISTINCT fixture_id FROM player_match_stats WHERE source=?", (SOURCE,)).fetchall()}
        given_up = {r[0][len("no-player-stats:"):] for r in self.store.db.execute("SELECT name FROM jobs WHERE name LIKE 'no-player-stats:%'").fetchall()}
        links = self._links()
        out = []
        for f in self.provider.list_fixtures(None, t - timedelta(days=PLAYER_STATS_DAYS), t - FINISHED_AFTER):
            if f.id in links and f.id not in have and f.id not in given_up and self.provider.result_of(f.id) is not None:
                out.append((not self._domestic(f.competition), -f.kickoff.timestamp(), f.id, links[f.id]))
        return [(fid, ext) for _, _, fid, ext in sorted(out)]

    def sync_player_stats(self, st: CollectStats, reserve_for_leagues: int = 0) -> None:
        names = self._team_names()
        for fid, ext in self.player_stats_due()[:PLAYER_STATS_PER_TICK]:
            left = self._remaining()
            if left is not None and left - reserve_for_leagues - PLAYER_STATS_KEEP < 1:
                st.skipped.append("statistiche giocatori rimandate: budget del giorno tenuto per formazioni e riserva")
                break
            resp = self.client.get("/fixtures/players", {"fixture": ext}).get("response") or []
            rows = self._player_rows(resp, fid, names)
            if rows:
                st.add("statistiche giocatori", self.store._bulk(
                    "INSERT OR REPLACE INTO player_match_stats(fixture_id,source,team,player_id,name,position,minutes,substitute,rating,shots,"
                    "shots_on,goals,assists,key_passes,fouls_committed,fouls_drawn,yellow,red,observed_at)", rows))
            elif self.now() - (self.provider.result_of(fid).kickoff if self.provider.result_of(fid) else self.now()) > timedelta(days=2):
                self.store.mark_job(f"no-player-stats:{fid}", self.now(), "API-Football senza statistiche giocatori")
        self.store.db.commit()

    def _player_rows(self, resp: list[dict], fid: str, names: dict[int, str]) -> list[tuple]:
        def n(x):
            try:
                return int(x) if x is not None else None
            except (TypeError, ValueError):
                return None
        out, at = [], self.now().isoformat()
        for side in resp:
            team = names.get(int((side.get("team") or {}).get("id") or 0))
            if not team:
                continue
            for p in side.get("players") or []:
                info, stats = p.get("player") or {}, (p.get("statistics") or [{}])[0]
                g, sh, gl = stats.get("games") or {}, stats.get("shots") or {}, stats.get("goals") or {}
                fo, cd, ps = stats.get("fouls") or {}, stats.get("cards") or {}, stats.get("passes") or {}
                if not info.get("id") or not g.get("minutes"):
                    continue  # unused substitutes: no minutes, nothing to count
                try:
                    rating = float(g["rating"]) if g.get("rating") else None
                except (TypeError, ValueError):
                    rating = None
                out.append((fid, SOURCE, team, pid(info["id"]), info.get("name"), g.get("position"), n(g.get("minutes")), int(bool(g.get("substitute"))),
                            rating, n(sh.get("total")) or 0, n(sh.get("on")) or 0, n(gl.get("total")) or 0, n(gl.get("assists")) or 0,
                            n(ps.get("key")) or 0, n(fo.get("committed")) or 0, n(fo.get("drawn")) or 0, n(cd.get("yellow")) or 0,
                            n(cd.get("red")) or 0, at))
        return out

    @staticmethod
    def _goal_minutes(rows: list[dict], names: dict[int, str], r) -> tuple[tuple, tuple] | None:
        """(first goal minute home, away), (last goal minute home, away) from the goal events (minute + stoppage/100, so 45+2
        comes before 46). Own goals: the side whose count then matches the final score (both readings are tried); None when
        neither matches or a team is unknown."""
        goals = []
        for e in rows:
            if str(e.get("type") or "").lower() != "goal" or "missed" in str(e.get("detail") or "").lower():
                continue
            tm = e.get("time") or {}
            minute = float(tm.get("elapsed") or 0) + float(tm.get("extra") or 0) / 100
            team = names.get(int((e.get("team") or {}).get("id") or 0))
            if team not in (r.home, r.away):
                return None
            goals.append((minute, team == r.home, "own" in str(e.get("detail") or "").lower()))
        for own_flips in (False, True):
            home = [m for m, h, own in goals if h != (own and own_flips)]
            away = [m for m, h, own in goals if h == (own and own_flips)]
            if len(home) == r.home_goals and len(away) == r.away_goals:
                first = (min(home) if home else None, min(away) if away else None)
                last = (max(home) if home else None, max(away) if away else None)
                return first, last
        return None

    def _league_matches_left_today(self) -> int:
        t = self.now()
        end = datetime.combine(t.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        done: dict[str, set] = {}
        for fid, team in self.store.db.execute("SELECT fixture_id, team FROM lineups WHERE source=? AND status='confirmed'", (SOURCE,)).fetchall():
            done.setdefault(fid, set()).add(team)
        return sum(1 for f in self.provider.list_fixtures(None, t, end)
                   if self._domestic(f.competition) and not {f.home, f.away} <= done.get(f.id, set()))
