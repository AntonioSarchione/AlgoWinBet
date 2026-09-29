"""GOAL API client (https://api.goal-api.com/v1) + tolerant mappers into canonical objects.

VERIFIED from the official SDK reference (ENDPOINTS.md): base URL, Bearer auth, endpoint paths and query params, the
{success,data,pagination,source} envelope, error codes, rate-limit headers (X-RateLimit-Limit/Remaining/Reset/Type, Retry-After),
status enum, and the fixture fields fixtureId/status/homeScore/awayScore/homeTeam.name/awayTeam.name.

NOT VERIFIED: the field names of lineups, odds, players and most fixture fields (the SDK returns rows "provider-shaped").
Mappers therefore try several plausible key paths, never guess silently, and count everything they could not map in a
MappingReport. Every raw response is stored, so mappers can be corrected and re-run on real payloads without new requests.
Run `algowinbet goal probe <path>` once per endpoint to capture and inspect real shapes.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from ..domain import Fixture, FixtureStatus, LineupSnapshot, MatchResult, OddsQuote, Player, Position
from ..names import TeamNames
from ..snapshots import BudgetExceeded, BudgetGuard, SnapshotStore

BASE_URL = "https://api.goal-api.com/v1"
SOURCE = "goal-api"


# ------------------------------------------------------------------ errors
class GoalApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


class AuthError(GoalApiError): ...
class PlanUpgradeRequired(GoalApiError): ...
class NotFound(GoalApiError): ...
class ValidationFailed(GoalApiError): ...
class RateLimited(GoalApiError):
    def __init__(self, message: str, retry_after: float = 0.0, status: int = 429, code: str = "RATE_LIMIT_EXCEEDED", daily_exhausted: bool = False):
        super().__init__(message, status, code)
        self.retry_after, self.daily_exhausted = retry_after, daily_exhausted
class ServerError(GoalApiError): ...


Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


def urllib_transport(timeout: float = 20.0) -> Transport:
    def send(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    return send


# ------------------------------------------------------------------ client
class GoalApiClient:
    def __init__(self, api_key: str | None = None, store: SnapshotStore | None = None, budget: BudgetGuard | None = None,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep, max_retries: int = 3,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), base_url: str = BASE_URL):
        self._key = api_key or os.environ.get("GOALAPI_KEY", "")
        if not self._key:
            raise AuthError("chiave mancante: imposta la variabile d'ambiente GOALAPI_KEY (mai nel repo, mai come argomento)")
        self.store, self.budget, self.transport = store, budget, transport or urllib_transport()
        self.sleep, self.max_retries, self.now, self.base = sleep, max_retries, now, base_url
        self.last_rate: dict[str, Any] = {}
        self.requests_sent = 0

    def __repr__(self) -> str:  # never leak the key
        return f"GoalApiClient(base={self.base!r}, requests_sent={self.requests_sent})"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base}{path}" + (f"?{urllib.parse.urlencode(sorted(params.items()))}" if params else "")
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json", "User-Agent": "AlgoWinBet/0.3"}
        attempt = 0
        while True:
            if self.budget:
                self.budget.charge(1)  # raises BudgetExceeded before we send anything over the limit
            try:
                status, hdr, body = self.transport(url, headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                status, hdr, body = 0, {}, str(e).encode()
            self.requests_sent += 1
            fetched = self.now()
            self._track_rate(hdr)
            if self.store:
                self.store.put_raw(SOURCE, path, params, status, body, fetched)
            if status == 200:
                try:
                    env = json.loads(body)
                except ValueError as e:
                    raise ServerError(f"risposta non JSON da {path}", 200) from e
                if env.get("success") is False:
                    raise self._error(200, env)
                env["_fetched_at"] = fetched
                return env
            err = self._error(status, self._try_json(body))
            if isinstance(err, RateLimited):
                err.daily_exhausted = hdr.get("x-ratelimit-remaining") == "0" and (hdr.get("x-ratelimit-type", "").upper() == "DAILY"
                                                                                   or hdr.get("x-ratelimit-type", "").upper() == "MONTHLY")
                err.retry_after = float(hdr.get("retry-after", "1") or 1)
                if err.daily_exhausted or attempt >= self.max_retries:
                    raise err
                self.sleep(min(err.retry_after, 60.0))
            elif isinstance(err, ServerError) or status == 0:
                if attempt >= self.max_retries:
                    raise err
                self.sleep(min(2.0**attempt, 30.0))
            else:
                raise err
            attempt += 1

    @staticmethod
    def _try_json(body: bytes) -> dict:
        try:
            v = json.loads(body)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _error(status: int, env: dict) -> GoalApiError:
        code = str(env.get("code", ""))
        msg = str(env.get("message") or env.get("error") or f"HTTP {status}")
        if status == 401 or code in ("AUTH_FAILED", "INVALID_API_KEY"):
            return AuthError(msg, status, code)
        if status == 402 or code == "PLAN_UPGRADE_REQUIRED":
            return PlanUpgradeRequired(msg, status, code)
        if status == 429 or code in ("RATE_LIMIT_EXCEEDED", "BURST_LIMIT_EXCEEDED"):
            return RateLimited(msg, status=status, code=code)
        if status == 404:
            return NotFound(msg, status, code)
        if status in (400, 422) or code == "VALIDATION_ERROR":
            return ValidationFailed(msg, status, code)
        if status == 0 or status >= 500:
            return ServerError(msg, status, code)
        return GoalApiError(msg, status, code)

    def _track_rate(self, hdr: dict[str, str]) -> None:
        def num(k):
            try:
                return int(hdr[k])
            except (KeyError, ValueError):
                return None
        self.last_rate = {"limit": num("x-ratelimit-limit"), "remaining": num("x-ratelimit-remaining"),
                          "reset": num("x-ratelimit-reset"), "type": hdr.get("x-ratelimit-type")}
        if self.budget:
            self.budget.sync_from_headers(self.last_rate["limit"], self.last_rate["remaining"], self.last_rate["type"])

    def pages(self, path: str, params: dict[str, Any] | None = None, limit: int = 100, max_pages: int = 20) -> Iterator[dict]:
        """Yield rows across pages (limit/offset + pagination.hasMore). Each page costs one request."""
        params = dict(params or {})
        offset = 0
        for _ in range(max_pages):
            env = self.get(path, {**params, "limit": limit, "offset": offset})
            data = env.get("data") or []
            yield from (data if isinstance(data, list) else [data])
            pg = env.get("pagination") or {}
            if not pg.get("hasMore") or not data:
                return
            offset += limit


# ----------------------------------------------------------------- mappers
@dataclass
class MappingReport:
    ok: dict[str, int] = field(default_factory=dict)
    gaps: dict[str, int] = field(default_factory=dict)  # reason -> count
    unmapped_markets: dict[str, int] = field(default_factory=dict)

    def good(self, kind: str) -> None:
        self.ok[kind] = self.ok.get(kind, 0) + 1

    def gap(self, reason: str) -> None:
        self.gaps[reason] = self.gaps.get(reason, 0) + 1

    def market(self, name: str) -> None:
        self.unmapped_markets[name] = self.unmapped_markets.get(name, 0) + 1

    def merge(self, other: "MappingReport") -> None:
        for a, b in ((self.ok, other.ok), (self.gaps, other.gaps), (self.unmapped_markets, other.unmapped_markets)):
            for k, v in b.items():
                a[k] = a.get(k, 0) + v


def pick(d: Any, *paths: str) -> Any:
    """First non-empty value among dotted key paths."""
    for path in paths:
        cur = d
        for k in path.split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, "", []):
            return cur
    return None


def parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


_STATUS = {
    "SCHEDULED": FixtureStatus.SCHEDULED, "LIVE": FixtureStatus.LIVE, "HALF_TIME": FixtureStatus.LIVE,
    "FINISHED": FixtureStatus.FINISHED, "AFTER_ET": FixtureStatus.FINISHED, "AFTER_PEN": FixtureStatus.FINISHED,
    "AWARDED": FixtureStatus.FINISHED, "POSTPONED": FixtureStatus.POSTPONED, "CANCELLED": FixtureStatus.CANCELLED,
    "ABANDONED": FixtureStatus.CANCELLED, "SUSPENDED": FixtureStatus.POSTPONED,
}
_DT_KEYS = ("kickoff", "kickOff", "startTime", "startsAt", "matchDate", "matchDateTime", "utcDate", "scheduledAt", "dateTime", "date")


class GoalMapper:
    def __init__(self, names: TeamNames | None = None):
        self.names = names or TeamNames()
        self.report = MappingReport()

    def _team(self, row: dict, side: str) -> str | None:
        v = pick(row, f"{side}Team.name", f"{side}TeamName", f"{side}.name", f"{side}_team", f"{side}Team")
        return self.names.canon(str(v)) if isinstance(v, (str, int)) and str(v) else None

    def fixture(self, row: dict, default_competition: str | None = None) -> Fixture | None:
        fid = pick(row, "fixtureId", "id", "matchId", "matchApiId")
        home, away = self._team(row, "home"), self._team(row, "away")
        ko = next((parse_dt(pick(row, k)) for k in _DT_KEYS if pick(row, k) is not None and parse_dt(pick(row, k))), None)
        comp = pick(row, "league.name", "leagueName", "competition.name", "competition") or default_competition
        if fid is None or not home or not away or ko is None or not comp:
            self.report.gap(f"fixture: campi mancanti (id={fid is not None}, home={bool(home)}, away={bool(away)}, kickoff={ko is not None}, lega={bool(comp)})")
            return None
        raw_status = str(pick(row, "status", "matchStatus") or "SCHEDULED").upper()
        status = _STATUS.get(raw_status)
        if status is None:
            self.report.gap(f"fixture: status sconosciuto {raw_status}")
            status = FixtureStatus.UNKNOWN
        self.report.good("fixture")
        return Fixture(id=f"goal:{fid}", competition=self.names.canon(str(comp)), home=home, away=away, kickoff=ko, status=status,
                       provider=SOURCE, provider_event_id=str(fid))

    def result(self, row: dict, default_competition: str | None = None) -> MatchResult | None:
        f = self.fixture(row, default_competition)
        hs, as_ = pick(row, "homeScore", "homeTeamScore", "score.home", "score.fullTime.home"), pick(row, "awayScore", "awayTeamScore", "score.away", "score.fullTime.away")
        if f is None:
            return None
        if f.status != FixtureStatus.FINISHED:
            return None
        try:
            hg, ag = int(hs), int(as_)
        except (TypeError, ValueError):
            self.report.gap("result: punteggio mancante su partita FINISHED")
            return None
        self.report.good("result")
        return MatchResult(fixture_id=f.id, competition=f.competition, home=f.home, away=f.away, kickoff=f.kickoff, home_goals=hg, away_goals=ag)

    # ------------------------------------------------------------ lineups
    @staticmethod
    def _players_of(side: dict, *keys: str) -> list[dict | str]:
        for k in keys:
            v = pick(side, k)
            if isinstance(v, list) and v:
                return v
        return []

    def _player_id(self, p: dict | str, team: str) -> str | None:
        if isinstance(p, str):
            return f"{team}::{p}"
        name = pick(p, "name", "player.name", "playerName", "fullName")
        pid = pick(p, "playerId", "player.id", "id")
        if name is None and pid is None:
            return None
        return f"goal:{pid}" if pid is not None else f"{team}::{name}"

    def lineups(self, data: Any, fixture: Fixture, observed_at: datetime) -> list[LineupSnapshot]:
        """Expects data with a home-side and away-side object (keys home/homeTeam/...), each holding a starters list."""
        out: list[LineupSnapshot] = []
        if isinstance(data, list) and len(data) == 2:  # [home, away]
            sides = {"home": data[0], "away": data[1]}
        elif isinstance(data, dict):
            sides = {"home": pick(data, "home", "homeTeam", "homeLineup", "lineups.home"),
                     "away": pick(data, "away", "awayTeam", "awayLineup", "lineups.away")}
        else:
            sides = {}
        for side, team in (("home", fixture.home), ("away", fixture.away)):
            s = sides.get(side)
            if not isinstance(s, dict):
                self.report.gap(f"lineup: lato {side} non trovato")
                continue
            starters = self._players_of(s, "startXI", "startingXI", "starters", "startingLineup", "lineup", "players")
            ids = [i for i in (self._player_id(p, team) for p in starters) if i]
            if len(ids) != 11:
                self.report.gap(f"lineup: {len(ids)} titolari trovati (attesi 11)")
                continue
            bench = [i for i in (self._player_id(p, team) for p in self._players_of(s, "substitutes", "bench", "subs")) if i]
            status_raw = str(pick(s, "status", "lineupStatus", "confirmed") or "").lower()
            status = "confirmed" if status_raw in ("confirmed", "official", "true", "1") or pick(s, "isConfirmed") is True else "probable"
            self.report.good("lineup")
            out.append(LineupSnapshot(fixture_id=fixture.id, team=team, status=status, starters=ids, bench=bench,
                                      formation=pick(s, "formation"), published_at=parse_dt(pick(s, "publishedAt", "updatedAt")) or observed_at,
                                      observed_at=observed_at, source_level="B"))
        return out

    # --------------------------------------------------------------- odds
    _MARKETS = {"match winner": "MATCH_1X2", "1x2": "MATCH_1X2", "full time result": "MATCH_1X2", "match result": "MATCH_1X2",
                "fulltime result": "MATCH_1X2", "home/away": "MATCH_1X2",
                "goals over/under": "TOTAL_GOALS", "over/under": "TOTAL_GOALS", "total goals": "TOTAL_GOALS", "totals": "TOTAL_GOALS",
                "both teams score": "BTTS", "both teams to score": "BTTS", "btts": "BTTS", "goal/no goal": "BTTS",
                "double chance": "DOUBLE_CHANCE"}

    def _selection(self, code: str, label: str, fixture: Fixture) -> tuple[str, float | None] | None:
        t = label.strip().lower()
        if code == "MATCH_1X2":
            if t in ("home", "1") or self.names.canon(label) == fixture.home:
                return "HOME", None
            if t in ("draw", "x"):
                return "DRAW", None
            if t in ("away", "2") or self.names.canon(label) == fixture.away:
                return "AWAY", None
        elif code == "TOTAL_GOALS":
            m = re.match(r"(over|under)\s*([0-9]+(?:\.[0-9]+)?)", t)
            if m:
                return m.group(1).upper(), float(m.group(2))
        elif code == "BTTS":
            if t in ("yes", "gol", "goal", "si", "sì"):
                return "YES", None
            if t in ("no", "nogol", "no goal"):
                return "NO", None
        elif code == "DOUBLE_CHANCE":
            table = {"1x": "1X", "home/draw": "1X", "x2": "X2", "draw/away": "X2", "12": "12", "home/away": "12"}
            if t in table:
                return table[t], None
        return None

    def odds(self, data: Any, fixture: Fixture, observed_at: datetime) -> list[OddsQuote]:
        """Handles the common nestings: [{bookmaker, markets:[{name, outcomes:[{name, price, point?}]}]}] and flat rows."""
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        out: list[OddsQuote] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            for b in (pick(r, "bookmakers") or [r]):
                book = str(pick(b, "bookmaker.name", "bookmaker", "bookmakerName", "name", "title") or "unknown")
                for m in (pick(b, "markets", "bets") or []):
                    mname = str(pick(m, "name", "market", "key") or "")
                    code = self._MARKETS.get(mname.strip().lower())
                    if code is None:
                        self.report.market(mname or "?")
                        continue
                    for o in (pick(m, "outcomes", "values", "selections", "odds") or []):
                        label = str(pick(o, "name", "label", "selection", "value") or "")
                        price = pick(o, "price", "odd", "odds", "decimal")
                        if code == "TOTAL_GOALS" and pick(o, "point", "line", "handicap") is not None and not re.search(r"[0-9]", label):
                            label = f"{label} {pick(o, 'point', 'line', 'handicap')}"
                        sel = self._selection(code, label, fixture)
                        try:
                            price_f = float(price)
                        except (TypeError, ValueError):
                            price_f = 0.0
                        if sel is None or price_f <= 1.0:
                            self.report.gap(f"odds: selezione non mappata ({code} '{label}', prezzo {price})")
                            continue
                        self.report.good("quote")
                        out.append(OddsQuote(fixture_id=fixture.id, market_code=code, selection=sel[0], line=sel[1], bookmaker=book,
                                             odds=price_f, observed_at=observed_at, kind="current", source_level="B"))
        return out

    # ------------------------------------------------------------ players
    _POS = {"goalkeeper": Position.GK, "gk": Position.GK, "g": Position.GK, "goalkeepers": Position.GK,
            "defender": Position.DEF, "def": Position.DEF, "d": Position.DEF, "defenders": Position.DEF,
            "midfielder": Position.MID, "mid": Position.MID, "m": Position.MID, "midfielders": Position.MID,
            "attacker": Position.FWD, "forward": Position.FWD, "fwd": Position.FWD, "f": Position.FWD, "a": Position.FWD,
            "forwards": Position.FWD, "striker": Position.FWD}

    def player(self, row: dict, team: str, default_position: Position | None = None) -> Player | None:
        name = pick(row, "name", "fullName", "playerName")
        pid = pick(row, "playerId", "id")
        raw_pos = str(pick(row, "position", "positionName", "type") or "").strip().lower()
        pos = self._POS.get(raw_pos) or default_position
        if not name or pos is None:
            self.report.gap(f"player: nome/ruolo non mappati (name={bool(name)}, position='{raw_pos}')")
            return None
        self.report.good("player")
        return Player(id=f"goal:{pid}" if pid is not None else f"{team}::{name}", name=str(name), team=team, position=pos)


def shape_summary(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 6, out: list[str] | None = None) -> list[str]:
    """Key paths with types and a short example: what `goal probe` prints so mappers can be checked against real payloads."""
    out = [] if out is None else out
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.append(f"{path}: {type(v).__name__}" + (f"[{len(v)}]" if isinstance(v, list) else ""))
                shape_summary(v, path, depth + 1, max_depth, out)
            else:
                out.append(f"{path}: {type(v).__name__} = {str(v)[:40]!r}")
    elif isinstance(obj, list) and obj:
        shape_summary(obj[0], prefix + "[0]", depth + 1, max_depth, out)
    return out
