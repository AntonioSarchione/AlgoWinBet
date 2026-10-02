"""API-Football (api-sports.io, free plan): injuries, official lineups and player statistics (Fase 6-bis).

Free plan: 100 requests a day, 10 a minute; /status is not counted. Every response is saved raw in the snapshot store
(source "api-football") before anything reads it, like the other providers. The key comes only from the APIFOOTBALL_KEY
environment variable (a GitHub secret): never from the repo, never as an argument.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from ..snapshots import BudgetGuard, SnapshotStore
from .goalapi import Transport, urllib_transport

SOURCE = "api-football"
BASE_URL = "https://v3.football.api-sports.io"
FREE_ENDPOINTS = {"/status"}
MIN_INTERVAL_S = 6.5  # 10 requests a minute, with a margin


class ApiFootballError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class ApiFootballClient:
    def __init__(self, api_key: str | None = None, store: SnapshotStore | None = None, budget: BudgetGuard | None = None,
                 transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), base_url: str = BASE_URL):
        self._key = (api_key or os.environ.get("APIFOOTBALL_KEY") or "").strip().strip("\"'")
        if not self._key:
            raise ApiFootballError("chiave mancante: imposta APIFOOTBALL_KEY (mai nel repo, mai come argomento)")
        self.store, self.budget, self.transport = store, budget, transport or urllib_transport(timeout=30.0)
        self.sleep, self.clock, self.now, self.base = sleep, clock, now, base_url
        self._last = -1e9
        self.requests_sent = 0
        self.counted_sent = 0
        self.remaining: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"ApiFootballClient(requests_sent={self.requests_sent})"

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        """One request. Returns the JSON envelope ({get, parameters, errors, results, paging, response})."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.budget and endpoint not in FREE_ENDPOINTS:
            self.budget.check(1)  # raises BudgetExceeded before anything is sent
        wait = MIN_INTERVAL_S - (self.clock() - self._last)
        if wait > 0:
            self.sleep(wait)
        url = f"{self.base}{endpoint}" + (f"?{urllib.parse.urlencode(sorted(params.items()))}" if params else "")
        status, headers, body = self.transport(url, {"x-apisports-key": self._key, "Accept": "application/json"})
        self._last = self.clock()
        self.requests_sent += 1
        counted = endpoint not in FREE_ENDPOINTS and status == 200
        self.counted_sent += int(counted)
        self.remaining = {k: v for k, v in headers.items() if k.startswith("x-ratelimit")}
        if self.budget and counted:
            self.budget.add(1)
            try:  # the provider's own daily counter wins when it is higher (requests made elsewhere, errors billed)
                lim, left = int(headers["x-ratelimit-requests-limit"]), int(headers["x-ratelimit-requests-remaining"])
                self.budget.sync_from_headers(lim, left, "DAILY")
            except (KeyError, ValueError):
                pass
        if self.store:
            self.store.put_raw(SOURCE, endpoint, params, status, body.replace(self._key.encode(), b"***"), self.now(), cost=int(counted))
        if status in (401, 403):
            raise ApiFootballError(f"{status}: chiave non valida", status)
        if status != 200:
            raise ApiFootballError(f"{status} su {endpoint}: {body[:200]!r}", status)
        try:
            env = json.loads(body)
        except ValueError as e:
            raise ApiFootballError(f"risposta non JSON da {endpoint}", 200) from e
        errors = env.get("errors")
        if errors:  # the API answers 200 with an "errors" object for plan limits, wrong parameters, daily quota...
            raise ApiFootballError(f"errore API-Football su {endpoint}: {errors}", 200)
        return env

    def status(self) -> dict:
        """Free call: account, plan and today's request count."""
        r = self.get("/status").get("response") or {}
        req = r.get("requests") or {}
        sub = r.get("subscription") or {}
        return {"plan": sub.get("plan"), "active": sub.get("active"), "end": sub.get("end"),
                "today": req.get("current"), "limit_day": req.get("limit_day")}
