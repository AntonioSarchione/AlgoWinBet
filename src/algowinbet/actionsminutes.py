"""GitHub Actions minutes. The repository is public since 2026-10-06: minutes are free and nothing is throttled on them any
more; the month's count is kept for information (dashboard Sistema page, health report).

  - free_seconds ...... seconds left in the minute this job is already paying for: spent on free price-path reads
  - record_run ........ adds this run's billed minutes to the month (api_usage 'actions-minutes')
  - sync_month ........ the daily run re-counts the month from GitHub's run list (probes and other workflows included)
  - month_used ........ the month's count so far
The job start comes from the workflow (JOB_T0, written by its first step).
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone

SOURCE = "actions-minutes"
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


def month_used(store, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return store.usage(SOURCE, month_key(now))


QUALITY_SOURCE = "quality-dispatch"  # api_usage period = model version: start attempts of its first quality replay
QUALITY_TRIES = 2


def dispatch_quality(store, version: str, now: datetime, post=None) -> str | None:
    """The morning run starts the quality replay at once when the model version has no meta-model yet: a version change
    switches the calibration off until the replay has fitted it again on the new model's probabilities. At most
    QUALITY_TRIES starts per version (one a morning).
    Needs GITHUB_TOKEN with actions: write (a workflow_dispatch event may be sent with it)."""
    from .meta import load_meta
    if not load_meta(store, version) and store.usage(QUALITY_SOURCE, version) < QUALITY_TRIES:
        key, why = version, f"per {version} (nessun meta-modello per questa versione)"
    elif now.weekday() == 0 and store.usage(QUALITY_SOURCE, f"W{now:%G-%V}") < 1 and not _quality_since(store, now.replace(hour=0, minute=0, second=0, microsecond=0)):
        # the weekly replay (and the weekly backup): GitHub's own schedule never fired it, the morning run is reliable
        key, why = f"W{now:%G-%V}", "settimanale"
    else:
        return None
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if post is None:
        if not token or not repo:
            return None

        def post(url: str) -> int:
            req = urllib.request.Request(url, data=json.dumps({"ref": "main"}).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            return urllib.request.urlopen(req, timeout=30).status
    status = post(f"https://api.github.com/repos/{repo}/actions/workflows/quality.yml/dispatches")
    store.add_usage(QUALITY_SOURCE, key, 1)
    if status == 204:
        return f"verifica qualità avviata {why}"
    return f"verifica qualità {why}: GitHub {status}"


def _quality_since(store, t: datetime) -> bool:
    try:
        return bool(store.db.execute("SELECT 1 FROM quality_runs WHERE created_at >= ? LIMIT 1", (t.isoformat(),)).fetchone())
    except Exception:  # noqa: BLE001 - no replay saved yet
        return False


def dispatch(workflow: str, inputs: dict | None = None, post=None) -> int | None:
    """Starts a workflow of this repository (workflow_dispatch on main): GitHub's status (204 = started), or None without
    GITHUB_TOKEN / GITHUB_REPOSITORY (a local run)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    body = {"ref": "main", **({"inputs": inputs} if inputs else {})}
    if post is None:
        if not token or not repo:
            return None

        def post(url: str) -> int:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            return urllib.request.urlopen(req, timeout=30).status
    return post(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches")
