"""Quotes retention: the price table is most of the database (Turso free plan: 5 GB).

Every tick stores a full tournament snapshot, so a finished match carries the same price hundreds of times. Once a match is
over (and its closing prices are in), only a few points of each price series are ever read again:
- the opening price;
- the price in force at the checkpoints the collector already keeps from a price path (72..1 hours before kickoff);
- the price in force at the quality replay's decision time (2 hours before kickoff) and 24 hours before that (price move);
- the latest price before kickoff;
- every closing price (kind 'close'), never touched.
The kept points are real observations with their own timestamps, so the price in force at every one of those times is the
same before and after pruning (tests/test_retention.py checks it).
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta

from .oddscollector import CHECKPOINTS_H
from .quality import DECISION, MOVE

AFTER = timedelta(days=7)  # the closing-line fetch and the settlement are long done by then
JOB = "quotes-pruned"  # jobs row: "kickoff|fixture id" of the last match pruned (matches are pruned in that order)
MAX_VARS = 30000  # SQLite bound parameters per statement (limit 32766): one DELETE per match below it
CHECKPOINTS = sorted({timedelta(hours=h) for h in CHECKPOINTS_H} | {DECISION, DECISION + MOVE}, reverse=True)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso(t: datetime) -> str:
    from .snapshots import _iso as iso
    return iso(t)


def keep_ids(rows: list[tuple], kickoff: datetime) -> set[int]:
    """rows: (id, series key, observed_at) of one match, closing prices excluded. Ids worth keeping."""
    series: dict[tuple, list[tuple[datetime, int]]] = defaultdict(list)
    for rid, key, at in rows:
        series[key].append((_dt(at), rid))
    keep: set[int] = set()
    for pts in series.values():
        pts.sort()
        keep.add(pts[0][1])
        before = [p for p in pts if p[0] < kickoff]
        keep.add((before or pts)[-1][1])
        for cp in CHECKPOINTS:
            at = kickoff - cp
            inforce = [p for p in before if p[0] <= at]
            if inforce:
                keep.add(inforce[-1][1])
    return keep


def finished_matches(store, now: datetime, limit: int) -> list[tuple[str, datetime]]:
    """(fixture id, kickoff) of matches over for AFTER and not pruned yet, oldest first. Kickoff from the newest fixture row."""
    done = store.db.execute("SELECT detail FROM jobs WHERE name=?", (JOB,)).fetchone()
    ko0, _, fid0 = (done[0] if done and done[0] else "").partition("|")
    rows = store.db.execute(
        "SELECT f.fixture_id, f.kickoff FROM fixtures f JOIN (SELECT fixture_id, MAX(observed_at) AS t FROM fixtures GROUP BY fixture_id) l "
        "ON l.fixture_id = f.fixture_id AND l.t = f.observed_at WHERE (f.kickoff > ? OR (f.kickoff = ? AND f.fixture_id > ?)) AND f.kickoff < ? "
        "ORDER BY f.kickoff, f.fixture_id LIMIT ?", (ko0, ko0, fid0, _iso(now - AFTER), limit)).fetchall()
    seen, out = set(), []
    for fid, ko in rows:
        if fid not in seen:
            seen.add(fid)
            out.append((fid, _dt(ko)))
    return out


def prune_quotes(store, now: datetime, max_matches: int = 400, max_seconds: float = 120, dry_run: bool = False,
                 clock=time.monotonic) -> dict:
    """Prune the price series of finished matches. Returns {matches, before, deleted, last_kickoff}."""
    t0 = clock()
    out = {"matches": 0, "before": 0, "deleted": 0, "last_kickoff": None}
    for fid, ko in finished_matches(store, now, max_matches):
        if clock() - t0 > max_seconds:
            break
        rows = store.db.execute(
            "SELECT id, market_code, selection, line_key, bookmaker, source, observed_at FROM quotes WHERE fixture_id=? AND kind != 'close'",
            (fid,)).fetchall()
        keep = keep_ids([(r[0], tuple(r[1:6]), r[6]) for r in rows], ko)
        drop = [r[0] for r in rows if r[0] not in keep]
        out["matches"] += 1
        out["before"] += len(rows)
        out["deleted"] += len(drop)
        out["last_kickoff"] = ko.isoformat()
        if dry_run:
            continue
        if drop and len(keep) < MAX_VARS:
            # one round trip to the primary per match: everything of the match but the closing prices and the kept points
            ids = sorted(keep)
            store.db.execute(f"DELETE FROM quotes WHERE fixture_id=? AND kind != 'close' AND id NOT IN ({','.join('?' * len(ids))})",
                             [fid, *ids])
        else:
            for i in range(0, len(drop), MAX_VARS):
                part = drop[i:i + MAX_VARS]
                store.db.execute(f"DELETE FROM quotes WHERE id IN ({','.join('?' * len(part))})", part)
        store.db.execute("INSERT INTO jobs(name, done_at, detail) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET done_at=excluded.done_at, "
                         "detail=excluded.detail", (JOB, now.isoformat(), f"{_iso(ko)}|{fid}"))
        store.db.commit()
    return out
