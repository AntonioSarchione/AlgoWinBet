"""FotMob player ids -> GOAL player ids (the ids of our lineups, players table and models). No request.

The link is made match by match: in a match both sources hold, the FotMob players of a team (fotmob_player_stats, everyone who
played) are named against the GOAL XI and bench of that team (lineups, names from the players table): same full name, else the
same surname (with the first initial when the surname is shared), else every word of the shorter name inside the longer one,
always unique on both sides. Each match is one vote; a FotMob id is linked to the GOAL id that took at least LINK_SHARE of its
votes. A player FotMob only lists as absent (never played in a match we read) is named against the GOAL players of that team in
our lineups within ABSENT_WINDOW of the absence. A doubtful name is left out: better a player without a link than an absence
given to the wrong person.

The links live in their own table (fotmob_player_links): nothing reads them yet (the absences are measured before any model
uses them).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .information.news import norm
from .snapshots import SnapshotStore

LINK_SHARE = 0.8
ABSENT_WINDOW = timedelta(days=60)
GOAL_SOURCE = "goal-api"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fotmob_player_links(fotmob_id TEXT PRIMARY KEY, goal_id TEXT, name TEXT, votes INTEGER, agree INTEGER,
  how TEXT, linked_at TEXT);
"""


def parts(name: str | None) -> list[str]:
    return norm(name or "").replace(".", " ").replace("-", " ").replace("'", " ").split()


def pick(name: str | None, cands: dict[str, list[str]]) -> str | None:
    """The one candidate (GOAL id -> name words) this name points to, or None."""
    n = parts(name)
    if not n:
        return None
    full = [g for g, p in cands.items() if p == n]
    if len(full) == 1:
        return full[0]
    if full:
        return None
    sur = [g for g, p in cands.items() if p and p[-1] == n[-1]]
    if len(sur) > 1 and len(n) > 1:
        sur = [g for g in sur if cands[g][0][:1] == n[0][:1]]
    if len(sur) == 1:
        return sur[0]
    if sur:
        return None
    inside = [g for g, p in cands.items() if p and (set(n) <= set(p) or set(p) <= set(n))]
    return inside[0] if len(inside) == 1 else None


def pair(names: dict[str, str], cands: dict[str, list[str]]) -> dict[str, str]:
    """FotMob id -> GOAL id for the players of one team in one match; a GOAL id two FotMob names point to is dropped."""
    got = {f: g for f, g in ((f, pick(n, cands)) for f, n in names.items()) if g}
    dup = {g for g, k in Counter(got.values()).items() if k > 1}
    return {f: g for f, g in got.items() if g not in dup}


@dataclass
class LinkReport:
    fotmob_players: int = 0
    linked: int = 0
    by_absence: int = 0
    doubtful: int = 0
    starters_checked: int = 0
    starters_same: int = 0
    absences: dict[str, list[int]] = field(default_factory=dict)  # competition -> [rows, linked, rows of a team with a GOAL XI, linked]

    def lines(self) -> list[str]:
        out = [f"giocatori FotMob: {self.fotmob_players} · collegati a GOAL {self.linked} ({self.by_absence} solo dalle assenze) · "
               f"dubbi lasciati fuori {self.doubtful}"]
        if self.starters_checked:
            out.append(f"controllo: titolari FotMob collegati che sono titolari anche nella formazione GOAL "
                       f"{100 * self.starters_same / self.starters_checked:.1f}% ({self.starters_checked})")
        out.append("assenze FotMob con il giocatore collegato, per competizione (tutte · solo partite con la formazione GOAL della squadra):")
        for comp, (n, ok, nx, okx) in sorted(self.absences.items()):
            out.append(f"  {comp}: {ok}/{n} ({100 * ok / max(n, 1):.0f}%) · {okx}/{nx} ({100 * okx / max(nx, 1):.0f}%)")
        return out


def link_players(store: SnapshotStore, now: datetime | None = None) -> LinkReport:
    """Rebuilds fotmob_player_links from everything stored (a full pass: a few seconds on the replica)."""
    now = now or datetime.now(timezone.utc)
    store.db.executescript(SCHEMA)
    rep = LinkReport()
    # GOAL side: who was in the XI or on the bench of each team in each match, and the names of those ids
    xi: dict[tuple[str, str], set[str]] = {}
    starters: dict[tuple[str, str], set[str]] = {}
    for fid, team, s, b in store.db.execute("SELECT fixture_id, team, starters, bench FROM lineups WHERE source = ? AND status = 'confirmed'",
                                            (GOAL_SOURCE,)).fetchall():
        st = [i for i in json.loads(s or "[]") if i.startswith("goal:")]
        xi.setdefault((fid, team), set()).update(st + [i for i in json.loads(b or "[]") if i.startswith("goal:")])
        starters.setdefault((fid, team), set()).update(st)
    names = {i: parts(n) for i, n in store.db.execute("SELECT id, name FROM players WHERE id LIKE 'goal:%'").fetchall()}
    kickoff = {f: datetime.fromisoformat(k) for f, k in store.db.execute("SELECT fixture_id, kickoff FROM results").fetchall()}

    # FotMob side, match by match
    played: dict[tuple[str, str], dict[str, str]] = {}
    fm_start: dict[tuple[str, str], set[str]] = {}
    fm_name: dict[str, str] = {}
    for fid, team, p, n, s in store.db.execute("SELECT fixture_id, team, player_id, name, starter FROM fotmob_player_stats").fetchall():
        played.setdefault((fid, team), {})[p] = n
        fm_name[p] = n
        if s:
            fm_start.setdefault((fid, team), set()).add(p)
    votes: dict[str, Counter] = {}
    for key, fm in played.items():
        cands = {g: names[g] for g in xi.get(key, ()) if g in names}
        if not cands:
            continue
        for f, g in pair(fm, cands).items():
            votes.setdefault(f, Counter())[g] += 1

    links: dict[str, tuple[str, int, int, str]] = {}
    for f, c in votes.items():
        g, k = c.most_common(1)[0]
        total = sum(c.values())
        if k / total >= LINK_SHARE:
            links[f] = (g, total, k, "partite")
        else:
            rep.doubtful += 1

    # players FotMob only lists as absent: the GOAL players of that team in our lineups near the absence
    by_team: dict[str, list[tuple[datetime, set[str]]]] = {}
    for (fid, team), ids in xi.items():
        if fid in kickoff:
            by_team.setdefault(team, []).append((kickoff[fid], ids))
    absences = store.db.execute("SELECT a.fixture_id, a.team, a.player_id, a.name, r.competition FROM fotmob_absences a "
                                "JOIN results r ON r.fixture_id = a.fixture_id").fetchall()
    av: dict[str, Counter] = {}
    for fid, team, p, n, _ in absences:
        fm_name.setdefault(p, n)
        if p in links or fid not in kickoff:
            continue
        near = set().union(*(ids for t, ids in by_team.get(team, []) if abs(t - kickoff[fid]) <= ABSENT_WINDOW), set())
        g = pick(n, {i: names[i] for i in near if i in names})
        if g:
            av.setdefault(p, Counter())[g] += 1
    taken = {v[0] for v in links.values()}
    for f, c in av.items():
        g, k = c.most_common(1)[0]
        if k / sum(c.values()) >= LINK_SHARE and g not in taken:
            links[f] = (g, sum(c.values()), k, "assenze")
            taken.add(g)
            rep.by_absence += 1
        else:
            rep.doubtful += 1

    rep.fotmob_players = len(fm_name)
    rep.linked = len(links)
    store.db.execute("DELETE FROM fotmob_player_links")
    store._bulk("INSERT OR REPLACE INTO fotmob_player_links(fotmob_id, goal_id, name, votes, agree, how, linked_at)",
                [(f, g, fm_name.get(f), v, k, how, now.isoformat()) for f, (g, v, k, how) in links.items()])

    # check: a linked FotMob starter should be a GOAL starter of the same match
    for key, fs in fm_start.items():
        gs = starters.get(key)
        if not gs:
            continue
        for f in fs:
            if f in links:
                rep.starters_checked += 1
                rep.starters_same += int(links[f][0] in gs)
    for fid, team, p, _, comp in absences:
        row = rep.absences.setdefault(comp, [0, 0, 0, 0])
        row[0] += 1
        row[1] += int(p in links)
        if (fid, team) in xi:
            row[2] += 1
            row[3] += int(p in links)
    return rep
