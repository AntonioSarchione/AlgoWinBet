"""One scheduler tick: decide what is worth fetching NOW and do only that. Meant to run every ~15 minutes on a machine that
is not the user's PC (GitHub Actions + Turso). Every step is idempotent and budget-aware; a tick with nothing to do sends
zero requests.

Plan per tick (UTC):
  - GOAL fixtures ........ if the last successful sync is older than 20h            (~1 request/league/day)
  - GOAL results + stats . if the last successful sync is older than 20h            (~1 + 1 per finished match)
  - GOAL lineups ......... fixtures kicking off within the lineup window, until both XI are confirmed
  - GOAL backfill ........ once per league, one league per tick: multi-season results history (no manual CSV)
  - OddsPapi snapshot .... daily (next 72h has fixtures, last snapshot >20h ago) and once per kickoff slot
                           30-75 min before kickoff, i.e. after the official XI: the moment stale prices exist
  - OddsPapi closing ..... free /historical-odds after kickoff (closing line for CLV)
"""
from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .collector import CollectStats, GoalCollector
from .oddscollector import OddsCollector
from .snapshots import SnapshotProvider, SnapshotStore


@dataclass
class League:
    name: str
    goal: str | None = None       # GOAL league id (goal leagues "...")
    oddspapi: int | None = None   # OddsPapi tournamentId (odds tournaments "...")


@dataclass
class AutoConfig:
    goal_leagues: list[str] = field(default_factory=list)
    oddspapi_tournaments: list[str] = field(default_factory=list)
    bookmakers: list[str] = field(default_factory=lambda: ["sisal", "pinnacle"])
    leagues: list[League] = field(default_factory=list)
    goal_daily_limit: int = 1000
    goal_reserve: int = 50
    oddspapi_monthly_limit: int = 250
    oddspapi_reserve: int = 20
    lineup_window_min: int = 95
    fixtures_days: int = 14
    history_seasons: int = 2  # backfill: current season + 2 previous, never more
    prekick_min: tuple[int, int] = (30, 75)

    @classmethod
    def load(cls, path: str | Path) -> "AutoConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if "prekick_min" in raw:
            raw["prekick_min"] = tuple(raw["prekick_min"])
        cfg = cls(**{k: v for k, v in raw.items() if not k.startswith("_") and k != "leagues"})
        cfg.leagues = [League(**{k: v for k, v in l.items() if not k.startswith("_")}) for l in raw.get("leagues", [])]
        # ids saved once in the config: no lookup request is ever needed at run time
        cfg.goal_leagues = cfg.goal_leagues or [l.goal for l in cfg.leagues if l.goal]
        cfg.oddspapi_tournaments = cfg.oddspapi_tournaments or [str(l.oddspapi) for l in cfg.leagues if l.oddspapi]
        return cfg


def odds_allowed_today(store: SnapshotStore, cfg: AutoConfig, now: datetime, cost: int) -> bool:
    """Pace the monthly OddsPapi budget: today may spend at most twice the fair share of what is left (match days need more
    than empty days, and empty days spend nothing because no snapshot is planned without upcoming fixtures)."""
    used_today = store.usage("oddspapi", f"D{now:%Y-%m-%d}")
    left = cfg.oddspapi_monthly_limit - cfg.oddspapi_reserve - store.usage("oddspapi", f"M{now:%Y-%m}")
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    allowance = 2 * (left + used_today) / days_left
    return left >= cost and used_today + cost <= max(allowance, cost)


def _last_ok(store: SnapshotStore, source: str, endpoint_like: str) -> datetime | None:
    row = store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint LIKE ? AND status=200",
                           (source, endpoint_like)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _stale(last: datetime | None, now: datetime, age: timedelta) -> bool:
    return last is None or now - last >= age


def plan_tick(store: SnapshotStore, cfg: AutoConfig, now: datetime, last_odds: datetime | None, odds_cost: int | None = None) -> list[str]:
    """Pure decision (no network): which steps this tick should run."""
    steps: list[str] = []
    if cfg.goal_leagues:
        if _stale(_last_ok(store, "goal-api", "/leagues/%/fixtures"), now, timedelta(hours=20)):
            steps.append("fixtures")
        if _stale(_last_ok(store, "goal-api", "/leagues/%/results"), now, timedelta(hours=20)):
            steps += ["results", "stats"]
        steps.append("lineups")  # costs nothing when no fixture is inside the window
        if any(not store.job_done(f"backfill:goal:{lid}") for lid in cfg.goal_leagues):
            steps.append("backfill")  # one league per tick until every league has its multi-season history
    if cfg.oddspapi_tournaments:
        upcoming = SnapshotProvider(store).list_fixtures(None, now, now + timedelta(hours=24))  # analysis looks at the last 24h
        lo, hi = cfg.prekick_min
        prekick = [f for f in upcoming if timedelta(minutes=lo) <= f.kickoff - now <= timedelta(minutes=hi)]
        cost = odds_cost or len(cfg.bookmakers)
        due = (upcoming and _stale(last_odds, now, timedelta(hours=20))) or (prekick and _stale(last_odds, now, timedelta(minutes=hi - lo + 5)))
        if due and odds_allowed_today(store, cfg, now, cost):
            steps.append("odds")
        steps.append("closing")
    return steps


def run_tick(store: SnapshotStore, cfg: AutoConfig, goal: GoalCollector | None, odds: OddsCollector | None,
             now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), on_step: Callable[[CollectStats], None] | None = None,
             max_seconds: float | None = None, clock: Callable[[], float] = time.monotonic) -> list[CollectStats]:
    """Runs the planned steps in order. With max_seconds, no NEW step starts after that time (the CI job has a hard timeout;
    whatever is skipped is simply picked up by the next tick, every step being idempotent)."""
    t = now()
    t0 = clock()
    steps = plan_tick(store, cfg, t, odds.last_snapshot_at() if odds else None, odds.snapshot_cost() if odds else None)
    out: list[CollectStats] = []
    for s in steps:
        if max_seconds is not None and clock() - t0 > max_seconds:
            skipped = CollectStats(s)
            skipped.skipped.append("tempo del giro esaurito: rimandato al prossimo tick")
            out.append(skipped)
            if on_step:
                on_step(skipped)
            continue
        before = len(out)
        if s in ("fixtures", "results", "stats", "lineups", "backfill") and goal is None:
            continue
        if s in ("odds", "closing") and odds is None:
            continue
        if s == "fixtures":
            out.append(goal.sync_fixtures(cfg.fixtures_days))
        elif s == "results":
            out.append(goal.sync_results(goal.days_since_last_results()))
        elif s == "stats":
            out.append(goal.sync_stats(3))
        elif s == "lineups":
            out.append(goal.sync_lineups(cfg.lineup_window_min))
        elif s == "backfill":
            st = goal.backfill_next(cfg.history_seasons)
            if st:
                out.append(st)
        elif s == "odds":
            out.append(odds.sync_odds())
        elif s == "closing":
            out.append(odds.sync_closing())
        if on_step and len(out) > before:
            on_step(out[-1])
    return out
