"""Who is likely to start, given only what was observable at the cutoff (spec 8, 9.3).

Priority: confirmed official XI > probable XI (+ later news) > prior start rates adjusted by player-status news.
Rumours shift probabilities in proportion to source reliability; they never flip a player to certain-out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain import InformationEvent, LineupSnapshot, Player, Position
from .sources import reliability

NEWS_MAX_AGE = timedelta(days=30)  # team-level news without a fixture link expires


@dataclass
class TeamAvailability:
    team: str
    p_start: dict[str, float]
    source: str  # confirmed | probable | prior
    notes: list[str] = field(default_factory=list)
    lineup: LineupSnapshot | None = None
    key_absences: list[Player] = field(default_factory=list)


def base_rates(roster: list[Player], learned: dict[str, float] | None = None) -> dict[str, float]:
    learned = learned or {}
    dflt = 11.0 / max(len(roster), 11)
    return {p.id: (p.start_rate if p.start_rate is not None else learned.get(p.id, dflt)) for p in roster}


def _latest_status(events: list[InformationEvent], team: str, ids: set[str], cutoff: datetime,
                   fixture_id: str | None, after: datetime | None = None) -> dict[str, InformationEvent]:
    latest: dict[str, InformationEvent] = {}
    for e in events:
        if e.event_type != "PLAYER_STATUS" or e.player not in ids or e.observed_at > cutoff or e.confidence <= 0:
            continue
        if after is not None and e.observed_at <= after:
            continue
        if e.fixture_id is None and cutoff - e.observed_at > NEWS_MAX_AGE:
            continue
        if e.fixture_id is not None and e.fixture_id != fixture_id:
            continue
        cur = latest.get(e.player)
        if cur is None or (e.observed_at, reliability(e.source_level)) > (cur.observed_at, reliability(cur.source_level)):
            latest[e.player] = e
    return latest


def _redistribute(roster: list[Player], base: dict[str, float], p: dict[str, float], out: set[str]) -> dict[str, float]:
    """Keep the expected number of starters per position: absent players' minutes go to teammates in the same position."""
    for pos in Position:
        grp = [x for x in roster if x.position == pos]
        target = sum(base[x.id] for x in grp)
        for _ in range(3):
            cur = sum(p[x.id] for x in grp)
            free = [x for x in grp if x.id not in out and p[x.id] < 0.995]
            if cur >= target - 1e-6 or not free:
                break
            room = sum(1 - p[x.id] for x in free)
            fill = min(target - cur, room)
            weights = [max(base[x.id], 0.05) * (1 - p[x.id]) for x in free]
            tw = sum(weights)
            for x, w in zip(free, weights):
                p[x.id] = min(1.0, p[x.id] + fill * w / tw)
    return p


def build_availability(team: str, roster: list[Player], base: dict[str, float], events: list[InformationEvent],
                       lineups: list[LineupSnapshot], cutoff: datetime, fixture_id: str) -> TeamAvailability:
    ids = {p.id for p in roster}
    by_id = {p.id: p for p in roster}
    # a lineup is usable only if its player ids belong to the roster (sources number players differently)
    mine = [l for l in lineups if l.team == team and l.fixture_id == fixture_id and l.observed_at <= cutoff
            and len(set(l.starters) & ids) >= 7]
    confirmed = sorted((l for l in mine if l.status == "confirmed"), key=lambda l: l.observed_at)
    probable = sorted((l for l in mine if l.status == "probable"), key=lambda l: l.observed_at)
    notes: list[str] = []

    if confirmed:
        lu = confirmed[-1]
        p = {i: (1.0 if i in set(lu.starters) else 0.0) for i in ids}
        notes.append(f"{team}: formazione ufficiale ({lu.formation or 'modulo n/d'}) osservata {lu.observed_at:%d/%m %H:%M}")
        return _finish(team, roster, base, p, "confirmed", notes, lu)

    if probable:
        lu = probable[-1]
        rel = reliability(lu.source_level)
        starters = set(lu.starters)
        p = {i: (0.75 + 0.15 * rel if i in starters else base[i] * 0.25) for i in ids}
        for i, e in _latest_status(events, team, ids, cutoff, fixture_id, after=lu.observed_at).items():
            st = e.payload.get("status")
            if st in ("OUT", "SUSPENDED"):
                p[i] *= 1 - e.confidence
                notes.append(f"{by_id[i].name}: {st} dopo la probabile ({e.source_level}, conf {e.confidence:.2f})")
        notes.append(f"{team}: probabile formazione osservata {lu.observed_at:%d/%m %H:%M} (fonte {lu.source_level})")
        return _finish(team, roster, base, p, "probable", notes, lu)

    p = dict(base)
    out: set[str] = set()
    for i, e in _latest_status(events, team, ids, cutoff, fixture_id).items():
        st, c = e.payload.get("status"), e.confidence
        if st in ("OUT", "SUSPENDED"):
            p[i] = base[i] * (1 - c)
            if c >= 0.5:
                out.add(i)
        elif st == "DOUBTFUL":
            p[i] = base[i] * (1 - 0.5 * c)
        else:  # AVAILABLE: back at usual rate
            p[i] = base[i]
        notes.append(f"{by_id[i].name}: {st} (fonte {e.source_level}, conf {c:.2f}{', ipotesi' if e.payload.get('hedged') else ''})")
    p = _redistribute(roster, base, p, out)
    return _finish(team, roster, base, p, "prior", notes, None)


def _finish(team, roster, base, p, source, notes, lu) -> TeamAvailability:
    key = sorted((x for x in roster if base[x.id] >= 0.6 and p[x.id] <= 0.2), key=lambda x: -base[x.id])
    return TeamAvailability(team=team, p_start=p, source=source, notes=notes, lineup=lu, key_absences=key)
