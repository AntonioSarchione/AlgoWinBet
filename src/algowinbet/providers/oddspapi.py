"""OddsPapi v4 adapter (https://oddspapi.io): Italian bookmaker prices (Sisal, Snai...) plus Pinnacle as sharp reference.

Free plan: 250 requests/month, and OddsPapi counts a request even when it answers with an error (except 401/403/429). So:
  - every billable call is checked against a monthly BudgetGuard BEFORE it is sent;
  - metadata (/markets, /bookmakers, /tournaments) is read from the snapshot store when fetched recently;
  - /account and /historical-odds are free (per the OddsPapi docs, to be confirmed on the live account) and are not charged.
The API key travels as the `apiKey` query parameter: it is added only at send time and never stored or printed.

Payload shapes follow the v4 documentation examples (also used by the author's oddspapi-mcp). They are NOT yet verified against
live responses: mappers are tolerant and count what they cannot map, and raw payloads are kept for re-mapping.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..domain import Fixture, OddsQuote
from ..names import TeamNames
from ..snapshots import BudgetGuard, SnapshotStore
from .goalapi import MappingReport, Transport, parse_dt, urllib_transport

BASE_URL = "https://api.oddspapi.io/v4"
SOURCE = "oddspapi"
MONTHLY_LIMIT = 250
FREE_ENDPOINTS = {"/account", "/historical-odds"}
COOLDOWN = {"/account": 1.0, "/sports": 1.0, "/bookmakers": 1.0, "/markets": 1.0, "/tournaments": 1.0, "/fixtures": 2.0,
            "/odds": 0.5, "/odds-by-tournaments": 1.0, "/historical-odds": 5.0}
METADATA_TTL = {"/markets": timedelta(days=7), "/bookmakers": timedelta(days=7), "/tournaments": timedelta(days=1)}


class OddsPapiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class OddsPapiClient:
    def __init__(self, api_key: str | None = None, store: SnapshotStore | None = None, budget: BudgetGuard | None = None,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), clock: Callable[[], float] = time.monotonic,
                 base_url: str = BASE_URL):
        self._key = (api_key or os.environ.get("ODDSPAPI_API_KEY") or os.environ.get("ODDSPAPI_KEY") or "").strip().strip("\"'")
        if not self._key:
            raise OddsPapiError("chiave mancante: imposta ODDSPAPI_API_KEY (mai nel repo, mai come argomento)")
        self.store, self.budget, self.transport = store, budget, transport or urllib_transport(timeout=30.0)
        self.sleep, self.now, self.clock, self.base = sleep, now, clock, base_url
        self._last_call: dict[str, float] = {}
        self.requests_sent = 0
        self.billable_sent = 0

    def __repr__(self) -> str:
        return f"OddsPapiClient(base={self.base!r}, requests_sent={self.requests_sent})"

    def get(self, endpoint: str, params: dict[str, Any] | None = None, use_store_cache: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        ttl = METADATA_TTL.get(endpoint)
        if ttl and use_store_cache and self.store:
            hit = self._cached(endpoint, params, ttl)
            if hit is not None:
                return hit
        billable = endpoint not in FREE_ENDPOINTS
        if billable and self.budget:
            self.budget.check(1)  # raises BudgetExceeded before anything is sent
        wait = COOLDOWN.get(endpoint, 1.0) - (self.clock() - self._last_call.get(endpoint, -1e9))
        if wait > 0:
            self.sleep(wait)
        query = urllib.parse.urlencode(sorted({**params, "apiKey": self._key}.items()))
        try:
            status, _, body = self.transport(f"{self.base}{endpoint}?{query}", {"Accept": "application/json", "User-Agent": "AlgoWinBet/0.3"})
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            status, body = 0, str(e).replace(self._key, "***").encode()
        self._last_call[endpoint] = self.clock()
        self.requests_sent += 1
        fetched = self.now()
        counted = billable and status not in (0, 401, 403, 429)  # rejected before the endpoint: not billed by OddsPapi
        if counted:
            self.billable_sent += 1
            if self.budget:
                self.budget.add(1)
        if self.store:
            self.store.put_raw(SOURCE, endpoint, params, status, body, fetched, cost=1 if counted else 0)
        if status == 429:
            raise OddsPapiError(f"429 da OddsPapi (quota mensile o cooldown): {body[:200]!r}", 429)
        if status in (401, 403):
            raise OddsPapiError(f"{status}: chiave non valida o endpoint non incluso nel piano", status)
        if status != 200:
            raise OddsPapiError(f"{status} su {endpoint}: {body[:200]!r}", status)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise OddsPapiError(f"risposta non JSON da {endpoint}", 200) from e
        return {"data": data, "_fetched_at": fetched}

    def _cached(self, endpoint: str, params: dict, ttl: timedelta) -> dict | None:
        row = self.store.db.execute(
            "SELECT id, fetched_at FROM raw_requests WHERE source=? AND endpoint=? AND params=? AND status=200 ORDER BY id DESC LIMIT 1",
            (SOURCE, endpoint, json.dumps(params, sort_keys=True))).fetchone()
        if not row or self.now() - datetime.fromisoformat(row[1]) > ttl:
            return None
        return {"data": json.loads(self.store.raw_body(row[0])), "_fetched_at": datetime.fromisoformat(row[1]), "_cached": True}

    def account(self) -> dict:
        """Free call: plan limits and usage. Syncs the local monthly counter with the provider's own."""
        acc = self.get("/account")["data"]
        subs = acc.get("subscriptions") or []
        cur = next((s for s in subs if s.get("subscription_id") == acc.get("current_subscription_id")), subs[0] if subs else {})
        limit, count = cur.get("request_limit"), cur.get("request_count")
        if self.budget and isinstance(limit, int) and isinstance(count, int):
            self.budget.sync_from_headers(limit, limit - count, "MONTHLY")
        return {"request_limit": limit, "request_count": count, "valid_until": cur.get("valid_until"),
                "bookmakers": sorted(cur.get("bookmakers") or {}), "sport_ids": cur.get("sport_ids")}

    def resolve_bookmakers(self, wanted: list[str]) -> list[str]:
        """'sisal' -> the OddsPapi slug; prefers the Italian (.it) variant when a brand has several."""
        books = self.get("/bookmakers")["data"]
        slugs = {b["slug"].lower(): b["slug"] for b in books if b.get("slug")}
        out: list[str] = []
        for w in wanted:
            lw = w.strip().lower()
            if lw in slugs:
                out.append(slugs[lw])
                continue
            matches = [b["slug"] for b in books if lw in b["slug"].lower() or lw in str(b.get("bookmakerName") or "").lower()]
            it = [s for s in matches if s.lower().endswith((".it", "-it"))]
            if len(matches) == 1 or len(it) == 1:
                out.append(matches[0] if len(matches) == 1 else it[0])
            elif not matches:
                raise OddsPapiError(f"bookmaker '{w}' non trovato su OddsPapi")
            else:
                raise OddsPapiError(f"'{w}' ambiguo: {', '.join(sorted(matches)[:10])}. Usa lo slug esatto.")
        return list(dict.fromkeys(out))


# ------------------------------------------------------------------- mapping
class OddsPapiMapper:
    """Converts OddsPapi fixtures/odds into our fixtures (matched to the GOAL calendar) and OddsQuote rows."""

    def __init__(self, names: TeamNames | None = None, markets: list[dict] | None = None):
        self.names = names or TeamNames()
        self.report = MappingReport()
        self.markets: dict[int, dict] = {}
        for m in markets or []:
            try:
                self.markets[int(m["marketId"])] = {**m, "_out": {int(o["outcomeId"]): str(o.get("outcomeName") or "") for o in m.get("outcomes") or []}}
            except (KeyError, TypeError, ValueError):
                self.report.gap("markets: riga senza marketId/outcomeId")

    # -- fixtures
    def match_fixture(self, row: dict, calendar: list[Fixture], tolerance: timedelta = timedelta(hours=3)) -> Fixture | None:
        home = self.names.canon(str(row.get("participant1Name") or ""))
        away = self.names.canon(str(row.get("participant2Name") or ""))
        ko = parse_dt(row.get("startTime"))
        if not home or not away or ko is None:
            self.report.gap("fixture oddspapi: squadre o startTime mancanti")
            return None
        hits = [f for f in calendar if f.home == home and f.away == away and abs(f.kickoff - ko) <= tolerance]
        if len(hits) != 1:
            self.report.gap(f"fixture oddspapi senza corrispondenza nel calendario: {home}-{away} {ko:%Y-%m-%d %H:%M}")
            return None
        self.report.good("fixture link")
        return hits[0]

    # -- markets
    def _market(self, mid: int) -> tuple[str, float | None] | None:
        """(market_code, line) for full-time goal markets; None for anything else (counted as unmapped)."""
        if mid == 101:
            return "MATCH_1X2", None
        if mid == 104:
            return "BTTS", None
        m = self.markets.get(mid)
        if not m:
            return None
        name = str(m.get("marketName") or "").lower()
        mtype = str(m.get("marketType") or "").lower()
        period = str(m.get("period") or "").lower()
        if period and period not in ("fulltime", "full time", "ft", "result", "match", "regular"):
            return None
        if m.get("playerProp"):
            return None
        if mtype in ("1x2",) or name in ("1x2", "full time result", "match result"):
            return "MATCH_1X2", None
        if mtype in ("totals", "over/under", "overunder") or re.search(r"over.?/?.?under|total goals", name):
            if "corner" in name or "card" in name or "team" in name:
                return None
            line = m.get("handicap")
            try:
                return "TOTAL_GOALS", float(line)
            except (TypeError, ValueError):
                mm = re.search(r"([0-9]+\.[05])", name)
                return ("TOTAL_GOALS", float(mm.group(1))) if mm else None
        if mtype in ("btts", "both teams to score") or re.search(r"both teams|goal/no ?goal|btts", name):
            return "BTTS", None
        if mtype in ("double chance", "doublechance") or "double chance" in name:
            return "DOUBLE_CHANCE", None
        return None

    def _selection(self, code: str, mid: int, oid: int, fixture: Fixture) -> str | None:
        label = self.markets.get(mid, {}).get("_out", {}).get(oid, "")
        t = label.strip().lower()
        if code == "MATCH_1X2":
            if t in ("1", "home") or self.names.canon(label) == fixture.home:
                return "HOME"
            if t in ("x", "draw"):
                return "DRAW"
            if t in ("2", "away") or self.names.canon(label) == fixture.away:
                return "AWAY"
            if not label and mid == 101:  # documented ids when /markets has not been loaded
                return {101: "HOME", 102: "DRAW", 103: "AWAY"}.get(oid)
        elif code == "TOTAL_GOALS":
            if t.startswith("over"):
                return "OVER"
            if t.startswith("under"):
                return "UNDER"
        elif code == "BTTS":
            if t in ("yes", "gg", "goal"):
                return "YES"
            if t in ("no", "ng", "nogoal", "no goal"):
                return "NO"
        elif code == "DOUBLE_CHANCE":
            return {"1x": "1X", "12": "12", "x2": "X2", "home/draw": "1X", "home/away": "12", "draw/away": "X2"}.get(t)
        return None

    def odds(self, payload: dict, fixture: Fixture, observed_at: datetime, kind: str = "current") -> list[OddsQuote]:
        """payload: one fixture object with bookmakerOdds{slug:{markets{mid:{outcomes{oid:{players{'0':{price,active,...}}}}}}}}."""
        out: list[OddsQuote] = []
        for book, bdata in (payload.get("bookmakerOdds") or {}).items():
            if not isinstance(bdata, dict) or bdata.get("suspended"):
                continue
            for mid_s, mdata in (bdata.get("markets") or {}).items():
                try:
                    mid = int(mid_s)
                except ValueError:
                    continue
                mk = self._market(mid)
                if mk is None:
                    self.report.market(str(self.markets.get(mid, {}).get("marketName") or f"market {mid}"))
                    continue
                if mdata.get("marketActive") is False:
                    continue
                for oid_s, odata in (mdata.get("outcomes") or {}).items():
                    sel = self._selection(mk[0], mid, int(oid_s), fixture)
                    for pdata in (odata.get("players") or {}).values():
                        if not isinstance(pdata, dict) or pdata.get("active") is False:
                            continue
                        price = pdata.get("price")
                        if sel is None or not isinstance(price, (int, float)) or price <= 1.0:
                            self.report.gap(f"odds: esito non mappato (market {mid}, outcome {oid_s})")
                            continue
                        self.report.good("quote")
                        out.append(OddsQuote(fixture_id=fixture.id, market_code=mk[0], selection=sel, line=mk[1], bookmaker=book,
                                             odds=float(price), observed_at=observed_at, kind=kind, source_level="B"))
        return out

    def history(self, payload: dict, fixture: Fixture) -> list[OddsQuote]:
        """/historical-odds: every price change with its own timestamp. Points after kickoff are dropped (in-play); the last
        pre-kickoff point of each selection is marked kind='close' (closing line, for CLV)."""
        out: list[OddsQuote] = []
        for book, bdata in (payload.get("bookmakers") or {}).items():
            for mid_s, mdata in ((bdata or {}).get("markets") or {}).items():
                mk = self._market(int(mid_s))
                if mk is None:
                    self.report.market(f"market {mid_s}")
                    continue
                for oid_s, odata in (mdata.get("outcomes") or {}).items():
                    sel = self._selection(mk[0], int(mid_s), int(oid_s), fixture)
                    for series in (odata.get("players") or {}).values():
                        pts = []
                        for p in series if isinstance(series, list) else []:
                            at, price = parse_dt(p.get("createdAt")), p.get("price")
                            if at and isinstance(price, (int, float)) and price > 1.0 and at < fixture.kickoff:
                                pts.append((at, float(price)))
                        if sel is None or not pts:
                            if sel is None:
                                self.report.gap(f"storico: esito non mappato (market {mid_s}, outcome {oid_s})")
                            continue
                        pts.sort()
                        for i, (at, price) in enumerate(pts):
                            out.append(OddsQuote(fixture_id=fixture.id, market_code=mk[0], selection=sel, line=mk[1], bookmaker=book,
                                                 odds=price, observed_at=at, kind="close" if i == len(pts) - 1 else "current",
                                                 source_level="B"))
                        self.report.good("storico")
        return out
