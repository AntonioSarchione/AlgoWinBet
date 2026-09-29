"""One scheduler tick: decide what is worth fetching NOW and do only that. Meant to run every ~15 minutes on a machine that
is not the user's PC (GitHub Actions + Turso). Every step is idempotent and budget-aware; a tick with nothing to do sends
zero requests.

Plan per tick (UTC):
  - GOAL fixtures ........ if the last successful sync is older than 20h            (~1 request/league/day)
  - GOAL results + stats . if the last successful sync is older than 20h            (~1 + 1 per finished match)
  - GOAL lineups ......... fixtures kicking off within the lineup window, until both XI are confirmed
  - OddsPapi snapshot .... daily (next 72h has fixtures, last snapshot >20h ago) and once per kickoff slot
                           30-75 min before kickoff, i.e. after the official XI: the moment stale prices exist
  - OddsPapi closing ..... free /historical-odds after kickoff (closing line for CLV)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .collector import CollectStats, GoalCollector
from .oddscollector import OddsCollector
from .snapshots import SnapshotProvider, SnapshotStore


@dataclass
class AutoConfig:
    goal_leagues: list[str] = field(default_factory=list)
    oddspapi_tournaments: list[str] = field(default_factory=list)
    bookmakers: list[str] = field(default_factory=lambda: ["sisal", "pinnacle", "snai"])
    goal_daily_limit: int = 1000
    goal_reserve: int = 50
    oddspapi_monthly_limit: int = 250
    oddspapi_reserve: int = 20
    lineup_window_min: int = 95
    fixtures_days: int = 14
    prekick_min: tuple[int, int] = (30, 75)

    @classmethod
    def load(cls, path: str | Path) -> "AutoConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if "prekick_min" in raw:
            raw["prekick_min"] = tuple(raw["prekick_min"])
        return cls(**{k: v for k, v in raw.items() if not k.startswith("_")})


def _last_ok(store: SnapshotStore, source: str, endpoint_like: str) -> datetime | None:
    row = store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint LIKE ? AND status=200",
                           (source, endpoint_like)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _stale(last: datetime | None, now: datetime, age: timedelta) -> bool:
    return last is None or now - last >= age


def plan_tick(store: SnapshotStore, cfg: AutoConfig, now: datetime, last_odds: datetime | None) -> list[str]:
    """Pure decision (no network): which steps this tick should run."""
    steps: list[str] = []
    if cfg.goal_leagues:
        if _stale(_last_ok(store, "goal-api", "/leagues/%/fixtures"), now, timedelta(hours=20)):
            steps.append("fixtures")
        if _stale(_last_ok(store, "goal-api", "/leagues/%/results"), now, timedelta(hours=20)):
            steps += ["results", "stats"]
        steps.append("lineups")  # costs nothing when no fixture is inside the window
    if cfg.oddspapi_tournaments:
        upcoming = SnapshotProvider(store).list_fixtures(None, now, now + timedelta(hours=72))
        lo, hi = cfg.prekick_min
        prekick = [f for f in upcoming if timedelta(minutes=lo) <= f.kickoff - now <= timedelta(minutes=hi)]
        if upcoming and _stale(last_odds, now, timedelta(hours=20)):
            steps.append("odds")
        elif prekick and _stale(last_odds, now, timedelta(minutes=hi - lo + 5)):
            steps.append("odds")
        steps.append("closing")
    return steps


def run_tick(store: SnapshotStore, cfg: AutoConfig, goal: GoalCollector | None, odds: OddsCollector | None,
             now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> list[CollectStats]:
    t = now()
    steps = plan_tick(store, cfg, t, odds.last_snapshot_at() if odds else None)
    out: list[CollectStats] = []
    for s in steps:
        if s in ("fixtures", "results", "stats", "lineups") and goal is None:
            continue
        if s in ("odds", "closing") and odds is None:
            continue
        if s == "fixtures":
            out.append(goal.sync_fixtures(cfg.fixtures_days))
        elif s == "results":
            out.append(goal.sync_results(3))
        elif s == "stats":
            out.append(goal.sync_stats(3))
        elif s == "lineups":
            out.append(goal.sync_lineups(cfg.lineup_window_min))
        elif s == "odds":
            out.append(odds.sync_odds())
        elif s == "closing":
            out.append(odds.sync_closing())
    return out
