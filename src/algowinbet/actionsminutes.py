"""GitHub Actions minutes (private repo: 2,000 a month, every job billed in whole minutes).

  - free_seconds ...... seconds left in the minute this job is already paying for: spent on free price-path reads
  - record_run ........ adds this run's billed minutes to the month (api_usage 'actions-minutes')
  - sync_month ........ the daily run re-counts the month from GitHub's run list (probes and other workflows included)
  - level ............. 0 normal, 1 economy (projection over ECONOMY_AT: no runs only to keep data fresh), 2 minimum (over
                        MINIMUM_AT: only the morning run); read by the dashboard's tick route and by the job itself
The job start comes from the workflow (JOB_T0, written by its first step).
"""
from __future__ import annotations

import calendar
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone

SOURCE = "actions-minutes"
BUDGET = 2000
ECONOMY_AT = 1700  # projected month total
MINIMUM_AT = 1900  # used so far
SETUP_S = 3.0  # "Set up job" before the first step writes JOB_T0
TAIL_S = 10.0  # replica save and post steps after collect-auto
MARGIN_S = 6.0


def job_elapsed(clock=time.time) -> float | None:
    t0 = os.environ.get("JOB_T0")
    return clock() - float(t0) + SETUP_S if t0 else None


def billed_minutes(elapsed: float) -> int:
    return max(1, math.ceil((elapsed + TAIL_S) / 60.0))


def free_seconds(clock=time.time) -> float:
    """Seconds of the current billed minute still unused once the tail steps are counted (0 outside Actions)."""
    e = job_elapsed(clock)
    if e is None:
        return 0.0
    return max(0.0, billed_minutes(e) * 60.0 - TAIL_S - MARGIN_S - e)


def month_key(now: datetime) -> str:
    return f"M{now:%Y-%m}"


def record_run(store, now: datetime, clock=time.time) -> int:
    e = job_elapsed(clock)
    if e is None:
        return 0
    n = billed_minutes(e)
    store.add_usage(SOURCE, month_key(now), n)
    return n


def sync_month(store, now: datetime) -> int | None:
    """Whole-minute total of every finished run of this month (all workflows), from GitHub's run list. Needs GITHUB_TOKEN
    with actions: read; never lowers the stored count (runs in progress are added by record_run)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    first = now.replace(day=1).strftime("%Y-%m-%d")
    total, page = 0, 1
    while True:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/runs?created=%3E%3D{first}&per_page=100&page={page}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        runs = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("workflow_runs") or []
        for r in runs:
            if r.get("status") != "completed" or not r.get("run_started_at") or not r.get("updated_at"):
                continue
            d = (datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
                 - datetime.fromisoformat(r["run_started_at"].replace("Z", "+00:00"))).total_seconds()
            total += max(1, math.ceil(d / 60.0))
        if len(runs) < 100 or page >= 30:
            break
        page += 1
    store.set_usage_at_least(SOURCE, month_key(now), total)
    return total


def level(used: int, now: datetime) -> int:
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now.day - 1 + (now.hour + now.minute / 60) / 24) / days
    projected = used / max(elapsed, 1.0 / days)
    if used >= MINIMUM_AT:
        return 2
    return 1 if projected >= ECONOMY_AT else 0


def month_level(store, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    used = store.usage(SOURCE, month_key(now))
    return level(used, now), used
