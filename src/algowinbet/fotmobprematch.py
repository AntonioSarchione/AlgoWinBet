"""FotMob before kickoff: who is out or doubtful, and the official XI, from the match page of the coming matches.

The page of a coming match lists each team's unavailable players (injured, suspended, away with the national team) with the
expected return, "Doubtful" for a player who may still play (seen 2026-10-07, three days before kickoff, data by Enetpulse).
About an hour before kickoff the same page carries the official XI. API-Football's free plan answers no injuries for the
current season, so this is our only live source of absences.

Reads of each coming match (FotMob ticks, their own workflow, see .github/workflows/fotmob.yml):
  morning ...... first read once the match is within WINDOW;
  pre .......... again within PRE of kickoff (the list changes after the last training);
  xi ........... every XI_EVERY from XI_FROM before kickoff until the official XI is read (GOAL and API-Football have missed
                 it until 12 minutes before kickoff, 2026-10-05).
Absences go to player_status (source fotmob, the statuses of API-Football: OUT, SUSPENDED, DOUBTFUL, and AVAILABLE for a
player no longer listed), one row per change with the time it was first seen: the history of every status change, read by
the models like API-Football's rows. Players carry GOAL ids (fotmob_player_links, else the name within the team). The XI goes
to lineups (source fotmob) only when at least XI_MIN_KNOWN starters of each team have a GOAL id.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace

from .collector import CollectStats
from .domain import Fixture, FixtureStatus, LineupSnapshot
from .snapshots import SnapshotProvider, SnapshotStore

WINDOW = timedelta(hours=30)
PRE = timedelta(hours=3)
XI_FROM = timedelta(minutes=75)
XI_EVERY = timedelta(minutes=8)
PAGE_AGE = timedelta(hours=20)  # a stored current season page older than this is read again to link new coming matches
MAX_PAGES = 60  # match pages a tick, nearest kickoff first
XI_MIN_KNOWN = 10  # starters of a team with a GOAL id: fewer and the XI is left to GOAL (unknown ids would mislead the models)
REGULAR_SHEETS = 3  # a "regular" started one of the team's last 3 official XI: a change of his status is worth a new analysis
SOURCE = "fotmob"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fotmob_prematch(fixture_id TEXT PRIMARY KEY, ext_id TEXT, kickoff TEXT, reads INTEGER, last_read TEXT,
  absent INTEGER, xi_at TEXT);
CREATE TABLE IF NOT EXISTS player_status(id INTEGER PRIMARY KEY, source TEXT, fixture_id TEXT, team TEXT, player_id TEXT,
  player_name TEXT, status TEXT, reason TEXT, observed_at TEXT, UNIQUE(source, fixture_id, player_id, status, reason));
CREATE TABLE IF NOT EXISTS fotmob_player_links(fotmob_id TEXT PRIMARY KEY, goal_id TEXT, name TEXT, votes INTEGER, agree INTEGER,
  how TEXT, linked_at TEXT);
"""


def status_of(kind: str | None, expected: str | None) -> str:
    k = (kind or "").lower()
    if "suspen" in k:
        return "SUSPENDED"
    if "doubt" in (expected or "").lower():
        return "DOUBTFUL"
    return "OUT"


def _reads(store: SnapshotStore) -> dict[str, tuple]:
    store.db.executescript(SCHEMA)
    return {r[0]: r[1:] for r in store.db.execute("SELECT fixture_id, ext_id, last_read, xi_at FROM fotmob_prematch").fetchall()}


def due_reads(store: SnapshotStore, competitions: set[str], now: datetime) -> list[tuple[Fixture, str]]:
    """Coming matches of these competitions whose page is due now, nearest kickoff first, with the reason. No request."""
    reads = _reads(store)
    out = []
    for f in SnapshotProvider(store).list_fixtures(None, now, now + WINDOW):
        if f.competition not in competitions or f.status != FixtureStatus.SCHEDULED:
            continue
        ext, last, xi = reads.get(f.id, (None, None, None))
        last = datetime.fromisoformat(last) if last else None
        to_ko = f.kickoff - now
        if last is None:
            out.append((f, "mattina"))
        elif to_ko <= PRE and last < f.kickoff - PRE:
            out.append((f, "3 ore prima"))
        elif ext and to_ko <= XI_FROM and not xi and now - last >= XI_EVERY:
            out.append((f, "formazione"))
    return sorted(out, key=lambda x: x[0].kickoff)


@dataclass
class PrematchReport:
    pages: int = 0
    changes: int = 0
    relevant: list[str] = field(default_factory=list)  # what is worth a new analysis


class FotMobPrematch:
    def __init__(self, col):
        """col: the FotMobCollector of the tick (client, pauses and page failures are shared with it)."""
        self.col, self.store = col, col.store
        self.provider = SnapshotProvider(self.store)

    def _links(self, st: CollectStats, due: list[Fixture]) -> dict[str, str]:
        """our fixture id -> FotMob match id, linking the coming matches from the current season page (read again when older
        than PAGE_AGE and a due match is not linked yet)."""
        links = self.col._links()
        by_comp = {lg.competition: lg for lg in self.col.leagues}
        for comp in sorted({f.competition for f in due if f.id not in links}):
            lg = by_comp.get(comp)
            if lg is None or self.col._halted:
                continue
            todo = [SimpleNamespace(fixture_id=f.id, home=f.home, away=f.away, kickoff=f.kickoff) for f in due if f.competition == comp]
            last, stored = self.col._stored_pages(lg).get("current", (None, {}))
            matches = stored.get("matches") or []
            page = f"season:{lg.fotmob_id}:current"
            if (last is None or self.col.now() - last >= PAGE_AGE) and not self.col._skip(page):
                from .fotmobcollector import FotMobError
                try:
                    matches, _ = self.col._season_page(lg, None)
                    self.col._ok(page)
                    st.requests += 1
                except FotMobError as e:
                    self.col._fail(st, comp, e, page)
            self.col._link(st, lg, [m for m in matches if not m["status"]["finished"]], todo, report=False)
        return self.col._links()

    def _goal_ids(self) -> dict[str, str]:
        return dict(self.store.db.execute("SELECT fotmob_id, goal_id FROM fotmob_player_links").fetchall())

    def _gid(self, links: dict[str, str], team: str, p: dict) -> str | None:
        from .fotmobcollector import pid
        if p.get("id") is None:
            return None
        return links.get(pid(p["id"])) or self.provider.goal_id_by_name(team, str(p.get("name") or ""))

    def _regulars(self, team: str, before: datetime) -> set[str]:
        rows = self.store.db.execute("SELECT fixture_id, starters FROM lineups WHERE team = ? AND status = 'confirmed' AND observed_at < ? "
                                     "ORDER BY observed_at DESC LIMIT 12", (team, before.isoformat())).fetchall()
        out, seen = set(), []
        for fid, s in rows:
            if fid not in seen:
                seen.append(fid)
                if len(seen) > REGULAR_SHEETS:
                    break
                out.update(json.loads(s or "[]"))
        return out

    def run(self, st: CollectStats) -> PrematchReport:
        from .fotmobcollector import FotMobError, pid
        rep = PrematchReport()
        now = self.col.now()
        due = due_reads(self.store, {lg.competition for lg in self.col.leagues}, now)
        if not due:
            return rep
        fixtures = [f for f, _ in due]
        links = self._links(st, fixtures)
        ids = self._goal_ids()
        for f, why in due[:MAX_PAGES]:
            if self.col._halted:
                break
            ext = links.get(f.id)
            if ext is None:  # FotMob does not list this match: read again only at the next checkpoint
                self._mark(f, None, now, 0, None)
                continue
            page = f"match:{ext}"
            if self.col._skip(page):
                continue
            try:
                pp = self.col.client.page(f"/match/{ext}")
            except FotMobError as e:
                self.col._fail(st, f"{f.home}-{f.away} ({why})", e, page)
                continue
            self.col._ok(page)
            st.requests += 1
            rep.pages += 1
            lineup = (pp.get("content") or {}).get("lineup") or {}
            absent, xi = {}, []
            for key, team in (("homeTeam", f.home), ("awayTeam", f.away)):
                t = lineup.get(key) or {}
                for u in t.get("unavailable") or []:
                    v = u.get("unavailability") or {}
                    g = self._gid(ids, team, u) or (pid(u["id"]) if u.get("id") is not None else None)
                    if g:
                        reason = f"{v.get('type') or '?'}: {v.get('expectedReturn') or '-'}"
                        absent[g] = (team, u.get("name"), status_of(v.get("type"), v.get("expectedReturn")), reason)
                starters = t.get("starters") or []
                kind = str(lineup.get("lineupType") or "").lower()
                if len(starters) == 11 and "predict" not in kind and "unavailable" not in kind:
                    s_ids = [self._gid(ids, team, p) for p in starters]
                    bench = [g for g in (self._gid(ids, team, p) for p in t.get("subs") or []) if g]
                    if sum(1 for g in s_ids if g) >= XI_MIN_KNOWN:
                        xi.append(LineupSnapshot(fixture_id=f.id, team=team, status="confirmed", formation=t.get("formation"),
                                                 starters=[g or f"{team}::{p.get('name')}" for g, p in zip(s_ids, starters)],
                                                 bench=bench, published_at=now, observed_at=now))
            changed = self._save_status(f, absent, now)
            rep.changes += len(changed)
            regulars = self._regulars(f.home, now) | self._regulars(f.away, now)
            for g, (team, name, status) in changed.items():
                if g in regulars and status != "AVAILABLE":
                    rep.relevant.append(f"{name} ({team}) {status}")
            have = {l.team for l in self.provider.get_lineups(f.id) if l.status == "confirmed"}
            new_xi = [l for l in xi if l.team not in have]
            if len(xi) == 2:
                if new_xi:
                    st.add("formazioni FotMob", self.store.save_lineups(SOURCE, new_xi))
                    rep.relevant.append(f"formazioni ufficiali {f.home}-{f.away}")
            self._mark(f, ext, now, len(absent), now if len(xi) == 2 else None)
        if rep.changes:
            st.add("stati giocatori FotMob", rep.changes)
        self.store.db.commit()
        return rep

    def _save_status(self, f: Fixture, absent: dict[str, tuple], now: datetime) -> dict[str, tuple]:
        """One row per change: a new status, or AVAILABLE for a player no longer listed. Returns the changes."""
        latest: dict[str, tuple] = {}
        for p, team, name, s, reason, at in self.store.db.execute(
                "SELECT player_id, team, player_name, status, reason, observed_at FROM player_status WHERE source = ? AND fixture_id = ? "
                "ORDER BY observed_at", (SOURCE, f.id)).fetchall():
            latest[p] = (team, name, s, reason)
        rows, changed = [], {}
        for g, (team, name, s, reason) in absent.items():
            if latest.get(g, (None, None, None, None))[2:] != (s, reason):
                rows.append((SOURCE, f.id, team, g, name, s, reason, now.isoformat()))
                changed[g] = (team, name, s)
        for g, (team, name, s, _) in latest.items():
            if g not in absent and s != "AVAILABLE":
                rows.append((SOURCE, f.id, team, g, name, "AVAILABLE", "non più in elenco FotMob", now.isoformat()))
                changed[g] = (team, name, "AVAILABLE")
        for r in rows:  # a status seen before (OUT, AVAILABLE, OUT again) takes the time of its new sighting
            self.store.db.execute("INSERT INTO player_status(source, fixture_id, team, player_id, player_name, status, reason, observed_at) "
                                  "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source, fixture_id, player_id, status, reason) "
                                  "DO UPDATE SET observed_at = excluded.observed_at", r)
        return changed

    def _mark(self, f: Fixture, ext: str | None, now: datetime, absent: int, xi_at: datetime | None) -> None:
        self.store.db.execute(
            "INSERT INTO fotmob_prematch(fixture_id, ext_id, kickoff, reads, last_read, absent, xi_at) VALUES(?,?,?,1,?,?,?) "
            "ON CONFLICT(fixture_id) DO UPDATE SET ext_id = excluded.ext_id, kickoff = excluded.kickoff, reads = reads + 1, "
            "last_read = excluded.last_read, absent = excluded.absent, xi_at = COALESCE(fotmob_prematch.xi_at, excluded.xi_at)",
            (f.id, ext, f.kickoff.isoformat(), now.isoformat(), absent, xi_at.isoformat() if xi_at else None))
