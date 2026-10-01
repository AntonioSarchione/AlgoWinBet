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

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..domain import Fixture, OddsQuote, SelectionRef
from ..markets import is_supported, split_code
from ..names import TeamNames, normalize
from ..snapshots import BudgetGuard, SnapshotStore
from .goalapi import MappingReport, Transport, parse_dt, urllib_transport

BASE_URL = "https://api.oddspapi.io/v4"
SOURCE = "oddspapi"
MONTHLY_LIMIT = 250
FREE_ENDPOINTS = {"/account", "/historical-odds"}
COOLDOWN = {"/account": 1.0, "/sports": 1.0, "/bookmakers": 1.0, "/markets": 1.0, "/tournaments": 1.0, "/fixtures": 2.0,
            "/odds": 0.5, "/odds-by-tournaments": 1.0, "/historical-odds": 5.0}
METADATA_TTL = {"/markets": timedelta(days=7), "/bookmakers": timedelta(days=7), "/tournaments": timedelta(days=1),
                "/participants": timedelta(days=7)}


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

    def __init__(self, names: TeamNames | None = None, markets: list[dict] | None = None, participants: Any = None):
        self.names = names or TeamNames()
        self.report = MappingReport()
        # Fixtures carry only participant ids (v4 docs): names come from /participants, cached for a week.
        self.participants: dict[str, str] = {}
        rows = participants.items() if isinstance(participants, dict) else (participants or [])
        for r in rows:
            if isinstance(r, tuple):
                self.participants[str(r[0])] = str(r[1].get("participantName") if isinstance(r[1], dict) else r[1])
            elif isinstance(r, dict) and r.get("participantId") is not None:
                self.participants[str(r["participantId"])] = str(r.get("participantName") or r.get("name") or "")
        self.markets: dict[int, dict] = {}
        for m in markets or []:
            try:
                self.markets[int(m["marketId"])] = {**m, "_out": {int(o["outcomeId"]): str(o.get("outcomeName") or "") for o in m.get("outcomes") or []}}
            except (KeyError, TypeError, ValueError):
                self.report.gap("markets: riga senza marketId/outcomeId")

    # -- fixtures
    def _name(self, row: dict, side: int) -> str:
        return self.names.canon(str(row.get(f"participant{side}Name") or self.participants.get(str(row.get(f"participant{side}Id")), "")))

    @staticmethod
    def _sim(a: str, b: str) -> float:
        na, nb = normalize(a), normalize(b)
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        if na in nb or nb in na:
            return 0.9
        return difflib.SequenceMatcher(None, na, nb).ratio()

    def match_fixture(self, row: dict, calendar: list[Fixture], tolerance: timedelta = timedelta(hours=3)) -> Fixture | None:
        """Same kickoff (+-3h) and both team names similar. Exact alias match first, then fuzzy: across 9 leagues the two
        sources spell clubs differently ("Bayern Munich" / "Bayern Munchen") and aliases alone would never be complete."""
        home, away = self._name(row, 1), self._name(row, 2)
        ko = parse_dt(row.get("startTime"))
        if not home or not away or ko is None:
            self.report.gap("fixture oddspapi: nomi squadra (participants) o startTime mancanti")
            return None
        near = [f for f in calendar if abs(f.kickoff - ko) <= tolerance]
        exact = [f for f in near if f.home == home and f.away == away]
        if len(exact) == 1:
            self.report.good("fixture link")
            return exact[0]
        scored = sorted(((min(self._sim(home, f.home), self._sim(away, f.away)), f) for f in near), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.15):
            self.report.good("fixture link (nomi simili)")
            return scored[0][1]
        # Rescheduled match (TV picks, postponements): the two sources disagree on the date. Same two teams within a few days
        # and only one such fixture: it is the same match. The calendar (GOAL) keeps its own kickoff.
        moved = [f for f in calendar if abs(f.kickoff - ko) <= timedelta(days=4)
                 and self._sim(home, f.home) >= 0.95 and self._sim(away, f.away) >= 0.95]
        if len(moved) == 1:
            self.report.good("fixture link (orario diverso)")
            return moved[0]
        self.report.gap(f"fixture oddspapi senza corrispondenza: {home}-{away} {ko:%Y-%m-%d %H:%M}")
        return None

    # -- markets
    # OddsPapi v4 marketType -> our market code (full time only). Verified on the real /markets catalogue (2026-09-30).
    # A fixed line means the market is a special case of a line market (team to score = team over 0.5).
    TYPE_MAP: dict[str, tuple[str, float | None] | str] = {
        "1x2": "MATCH_1X2", "bothteamsscore": "BTTS", "doublechance": "DOUBLE_CHANCE", "totals": "TOTAL_GOALS",
        "teamtotals-team1": "TEAM_TOTAL_HOME", "teamtotals-team2": "TEAM_TOTAL_AWAY", "spreads": "ASIAN_HANDICAP",
        "spreads-european": "EURO_HANDICAP", "oddeven": "ODD_EVEN", "correctscore": "CORRECT_SCORE",
        "winningmargin": "WINNING_MARGIN", "exactscore": "TOTAL_EXACT", "exactscore-team1": "TEAM_EXACT_HOME",
        "exactscore-team2": "TEAM_EXACT_AWAY", "wintonil-team1": "WIN_TO_NIL_HOME", "wintonil-team2": "WIN_TO_NIL_AWAY",
        "toscore-team1": ("TEAM_TOTAL_HOME", 0.5), "toscore-team2": ("TEAM_TOTAL_AWAY", 0.5),
        "cleansheet-team1": ("TEAM_TOTAL_AWAY", 0.5), "cleansheet-team2": ("TEAM_TOTAL_HOME", 0.5),
        "drawnobet": "DRAW_NO_BET", "oddeven-team1": "TEAM_ODD_EVEN_HOME", "oddeven-team2": "TEAM_ODD_EVEN_AWAY",
        "firstgoal": "FIRST_GOAL", "lastgoal": "LAST_GOAL",
        # both halves (catalogue period: none)
        "halftime-fulltime": "HT_FT", "highestscoringh": "HIGHEST_HALF", "highestscoringh-team1": "HIGHEST_HALF_HOME",
        "highestscoringh-team2": "HIGHEST_HALF_AWAY", "toscoreinbh-team1": "SCORE_BOTH_HALVES_HOME",
        "toscoreinbh-team2": "SCORE_BOTH_HALVES_AWAY", "winbothh-team1": "WIN_BOTH_HALVES_HOME", "winbothh-team2": "WIN_BOTH_HALVES_AWAY",
        "wineitherh-team1": "WIN_EITHER_HALF_HOME", "wineitherh-team2": "WIN_EITHER_HALF_AWAY",
    }
    # catalogue period -> our suffix; markets over both halves carry no period. Corners, bookings and player props are other
    # marketTypes and never reach this table.
    PERIOD_SUFFIX = {"fulltime": "", "full time": "", "ft": "", "": "", "none": "", "p1": "@H1", "p2": "@H2"}
    WHOLE_MATCH_ONLY = {"FIRST_GOAL", "LAST_GOAL", "HT_FT", "HIGHEST_HALF", "HIGHEST_HALF_HOME", "HIGHEST_HALF_AWAY",
                        "SCORE_BOTH_HALVES_HOME", "SCORE_BOTH_HALVES_AWAY", "WIN_BOTH_HALVES_HOME", "WIN_BOTH_HALVES_AWAY",
                        "WIN_EITHER_HALF_HOME", "WIN_EITHER_HALF_AWAY"}
    LINE_CODES = {"TOTAL_GOALS", "TEAM_TOTAL_HOME", "TEAM_TOTAL_AWAY", "ASIAN_HANDICAP", "EURO_HANDICAP"}

    def _market(self, mid: int) -> tuple[str, float | None] | None:
        """(market_code, line) for full-time goal markets; None for anything else (counted as unmapped)."""
        if mid == 101:
            return "MATCH_1X2", None
        if mid == 104:
            return "BTTS", None
        m = self.markets.get(mid)
        if not m:
            return None
        mapped = self.TYPE_MAP.get(str(m.get("marketType") or "").lower())
        if mapped is not None:
            suffix = self.PERIOD_SUFFIX.get(str(m.get("period") or "").lower())
            base = mapped[0] if isinstance(mapped, tuple) else mapped
            if suffix is None or (suffix and base in self.WHOLE_MATCH_ONLY):
                return None  # unknown period (e.g. extra time) or a both-halves market listed per half
            if isinstance(mapped, tuple):
                return mapped[0] + suffix, mapped[1]
            if mapped in self.LINE_CODES:
                try:
                    return mapped + suffix, float(m.get("handicap"))
                except (TypeError, ValueError):
                    return None
            return mapped + suffix, None
        name = str(m.get("marketName") or "").lower()
        mtype = str(m.get("marketType") or "").lower()
        # The catalogue types every market: a type we do not map (totals-bookings, totals-corners, player props...) is NOT a goal
        # market, whatever its name says ("Bookings - Over Under Full Time" once passed for total goals). The name-based guess
        # below is only for rows without a type.
        if mtype:
            return None
        if re.search(r"booking|card|corner|player|shot|foul|offside|throw|goal ?kick|half", name):
            return None
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
        code = split_code(code)[0]
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
            return {"1x": "1X", "12": "12", "x2": "X2", "2x": "X2", "home/draw": "1X", "home/away": "12", "draw/away": "X2"}.get(t)
        elif code in ("TEAM_TOTAL_HOME", "TEAM_TOTAL_AWAY"):
            mtype = str(self.markets.get(mid, {}).get("marketType") or "").lower()
            if mtype.startswith("toscore"):
                return {"yes": "OVER", "no": "UNDER"}.get(t)
            if mtype.startswith("cleansheet"):
                return {"yes": "UNDER", "no": "OVER"}.get(t)
            return "OVER" if t.startswith("over") else "UNDER" if t.startswith("under") else None
        elif code in ("EURO_HANDICAP", "ASIAN_HANDICAP"):
            return {"1": "HOME", "x": "DRAW", "2": "AWAY"}.get(t)
        elif code in ("ODD_EVEN", "TEAM_ODD_EVEN_HOME", "TEAM_ODD_EVEN_AWAY"):
            return {"odd": "ODD", "even": "EVEN"}.get(t)
        elif code == "DRAW_NO_BET":
            return {"1": "HOME", "2": "AWAY", "home": "HOME", "away": "AWAY"}.get(t)
        elif code in ("FIRST_GOAL", "LAST_GOAL"):
            return {"1": "HOME", "2": "AWAY", "no goal": "NONE", "none": "NONE"}.get(t)
        elif code == "HT_FT":
            mm = re.fullmatch(r"([12x])\s*/\s*([12x])", t)
            return f"{mm.group(1)}/{mm.group(2)}".upper() if mm else None
        elif code.startswith("HIGHEST_HALF"):
            return {"1st": "1ST", "2nd": "2ND", "x": "EQUAL", "equal": "EQUAL"}.get(t)
        elif code.startswith(("SCORE_BOTH_HALVES", "WIN_BOTH_HALVES", "WIN_EITHER_HALF")):
            return {"yes": "YES", "no": "NO"}.get(t)
        elif code in ("WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"):
            return {"yes": "YES", "no": "NO"}.get(t)
        elif code == "CORRECT_SCORE":
            mm = re.fullmatch(r"(\d+)\s*[:-]\s*(\d+)", t)
            return f"{mm.group(1)}-{mm.group(2)}" if mm else None
        elif code in ("TOTAL_EXACT", "TEAM_EXACT_HOME", "TEAM_EXACT_AWAY"):
            return t if re.fullmatch(r"\d+\+?", t) else None
        elif code == "WINNING_MARGIN":
            if t in ("draw", "no goal", "draw (incl 0:0)"):
                return {"draw": "D", "no goal": "NG", "draw (incl 0:0)": "DI"}[t]
            mm = re.fullmatch(r"([12]) by (\d+\+?)", t)
            return f"{'H' if mm.group(1) == '1' else 'A'}{mm.group(2)}" if mm else None
        return None

    @staticmethod
    def _priceable(mk: tuple[str, float | None]) -> bool:
        """Line markets: only lines the score grid prices without refunds (x.5 totals/Asian, whole European)."""
        code, line = mk
        if line is None or split_code(code)[0] not in OddsPapiMapper.LINE_CODES:
            return True
        probe = {"EURO_HANDICAP": "HOME", "ASIAN_HANDICAP": "HOME"}.get(code, "OVER")
        return is_supported(SelectionRef(market_code=code, selection=probe, line=line))

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
                if not self._priceable(mk):
                    self.report.gap(f"linea non supportata: {mk[0]} {mk[1]}")
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

    def history(self, payload: dict, fixture: Fixture, closing: bool = True) -> list[OddsQuote]:
        """/historical-odds: every price change with its own timestamp. Points after kickoff are dropped (in-play); the last
        pre-kickoff point of each selection is marked kind='close' (closing line, for CLV). With closing=False (fixture not
        played yet) every point stays kind='current': the last one is simply the latest price."""
        out: list[OddsQuote] = []
        for book, bdata in (payload.get("bookmakers") or {}).items():
            for mid_s, mdata in ((bdata or {}).get("markets") or {}).items():
                mk = self._market(int(mid_s))
                if mk is None:
                    self.report.market(f"market {mid_s}")
                    continue
                if not self._priceable(mk):
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
                                                 odds=price, observed_at=at, kind="close" if closing and i == len(pts) - 1 else "current",
                                                 source_level="B"))
                        self.report.good("storico")
        return out
