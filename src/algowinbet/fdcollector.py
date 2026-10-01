"""football-data.co.uk season files, downloaded automatically (GitHub Actions) and linked to our GOAL calendar.

What a season CSV gives per finished match: half-time and full-time score, shots, shots on target, fouls, corners, cards,
xG (from 2026/27), and opening + closing prices of several books for 1X2, Over/Under 2.5 and Asian handicap. We keep
Pinnacle (until 2025/26), Betfair Exchange (every season: the sharp reference after Pinnacle left the files) and the market
average. Quotes are stored with the time they were observable, so a backtest never sees them early:
  opening ... the site collects them on Friday afternoons (weekend games) and Tuesday afternoons (midweek games)
  closing ... at kickoff (stored 5 minutes before)

Terms of the site: the data is free for private individuals, not for commercial or AI-training products. AlgoWinBet is a
private, non-commercial tool: it downloads each past season ONCE and the current season at most every few days, with an
identifying User-Agent and If-Modified-Since, and skips parsing when the file did not change.

Rows are linked to GOAL fixtures/results by date (+-36h) and team names learnt over the whole file (see _learn); a row
that matches nothing (or more than one match) is reported and never guessed.
"""
from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .collector import CollectStats
from .domain import MatchStat, OddsQuote, SelectionRef
from .markets import is_supported
from .names import TeamNames, normalize
from .snapshots import SnapshotStore

SOURCE = "football-data"
BASE = "https://football-data.co.uk/mmz4281/{season}/{div}.csv"
USER_AGENT = "AlgoWinBet/1.0 (private non-commercial use; few downloads a week)"
UK = ZoneInfo("Europe/London")
LINK_VERSION = 2  # bump when linking improves: files with unlinked rows are read again once
LINKED_ENOUGH = 0.9  # a past season is done once this share of its rows is linked to our matches
# column prefix -> bookmaker name stored in quotes
BOOKS = {"PS": "pinnacle", "BFE": "betfair-ex", "Avg": "market-avg"}
STATS = {"shots": ("HS", "AS"), "shots_on_target": ("HST", "AST"), "fouls": ("HF", "AF"), "corners": ("HC", "AC"),
         "yellow_cards": ("HY", "AY"), "red_cards": ("HR", "AR"), "expected_goals": ("HxG", "AxG")}


def season_code(day: datetime) -> str:
    """Seasons start in July: 2026-10-01 -> '2627'."""
    y = day.year if day.month >= 7 else day.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def seasons_back(day: datetime, previous: int) -> list[str]:
    first = int(season_code(day)[:2])
    return [f"{(first - k) % 100:02d}{(first - k + 1) % 100:02d}" for k in range(previous + 1)]


def opening_time(kickoff: datetime) -> datetime:
    """When football-data captured the 'opening' prices: Friday afternoon for Fri-Mon games, Tuesday afternoon for Tue-Thu
    games (15:00 UK). Never later than 1h before kickoff."""
    local = kickoff.astimezone(UK)
    back = {4: 0, 5: 1, 6: 2, 0: 3, 1: 0, 2: 1, 3: 2}[local.weekday()]  # days back to that Friday / Tuesday
    at = (local - timedelta(days=back)).replace(hour=15, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return min(at, kickoff - timedelta(hours=1))


def _num(row: dict, key: str) -> float | None:
    v = (row.get(key) or "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _kickoff(row: dict) -> datetime | None:
    try:
        d = datetime.strptime(row["Date"].strip(), "%d/%m/%Y")
    except (KeyError, ValueError):
        try:
            d = datetime.strptime(row["Date"].strip(), "%d/%m/%y")
        except (KeyError, ValueError):
            return None
    hh, mm = 15, 0
    if ":" in (row.get("Time") or ""):
        hh, mm = (int(x) for x in row["Time"].split(":")[:2])
    return d.replace(hour=hh, minute=mm, tzinfo=UK).astimezone(timezone.utc)  # the site lists UK times


@dataclass
class FileReport:
    rows: int = 0
    linked: int = 0
    unmatched: list[str] = field(default_factory=list)


class FootballDataCollector:
    def __init__(self, store: SnapshotStore, divisions: dict[str, str], names: TeamNames | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 fetch: Callable[[str, dict], tuple[int, bytes]] | None = None):
        """divisions: football-data code -> our competition name, e.g. {'I1': 'Serie A'}."""
        self.store, self.divisions, self.names, self.now = store, divisions, names or TeamNames(), now
        self.fetch = fetch or self._http

    # ------------------------------------------------------------------ network
    @staticmethod
    def _http(url: str, headers: dict) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def _last_hash(self, endpoint: str) -> str | None:
        row = self.store.db.execute("SELECT hash FROM raw_requests WHERE source=? AND endpoint=? AND status=200 ORDER BY id DESC LIMIT 1",
                                    (SOURCE, endpoint)).fetchone()
        return row[0] if row else None

    def _checked_at(self, name: str) -> datetime | None:
        row = self.store.db.execute("SELECT done_at FROM jobs WHERE name=?", (name,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def _loaded(self, job: str) -> bool:
        """Loaded with the current linker, or with nothing left to link."""
        row = self.store.db.execute("SELECT detail FROM jobs WHERE name=?", (job,)).fetchone()
        if row is None:
            return False
        try:
            d = json.loads(row[0] or "{}")
        except ValueError:
            return False
        return d.get("link_version", 1) >= LINK_VERSION or not d.get("n_unmatched")

    # ------------------------------------------------------------------ sync
    def due(self, previous_seasons: int = 2, refresh_days: float = 3.0) -> list[tuple[str, str]]:
        """(season, division) files to download now: past seasons not loaded yet, the current season every refresh_days.
        A failed download is retried after refresh_days, not at every tick."""
        t = self.now()
        current = season_code(t)
        out = []
        for season in seasons_back(t, previous_seasons):
            for div in self.divisions:
                if season != current and self._loaded(f"dataset:{SOURCE}:{div}:{season}"):
                    continue
                checked = self._checked_at(f"dataset-check:{SOURCE}:{div}:{season}")
                if checked and t - checked < timedelta(days=refresh_days):  # also a past season that failed: retry later
                    continue
                out.append((season, div))
        return out

    def sync(self, previous_seasons: int = 2, refresh_days: float = 3.0, max_seconds: float | None = None,
             clock: Callable[[], float] | None = None) -> CollectStats:
        """Past seasons: downloaded once (a job marks them done). Current season: re-downloaded when older than refresh_days.
        With max_seconds, no new file starts after that time: the rest is picked up by the next tick."""
        import time
        clock = clock or time.monotonic
        st = CollectStats("datasets")
        t, t0 = self.now(), clock()
        current = season_code(t)
        todo = self.due(previous_seasons, refresh_days)
        for k, (season, div) in enumerate(todo):
            if max_seconds is not None and clock() - t0 > max_seconds:
                st.skipped.append(f"{len(todo) - k} file rimandati al prossimo giro (tempo)")
                break
            endpoint = f"/mmz4281/{season}/{div}.csv"
            job, check = f"dataset:{SOURCE}:{div}:{season}", f"dataset-check:{SOURCE}:{div}:{season}"
            checked = self._checked_at(check)
            old_hash = self._last_hash(endpoint)
            headers = {"If-Modified-Since": format_datetime(checked, usegmt=True)} if checked and old_hash else {}
            try:
                status, body = self.fetch(BASE.format(season=season, div=div), headers)
            except OSError as e:
                st.errors.append(f"{div} {season}: {e}")
                continue
            st.requests += 1
            self.store.mark_job(check, t)
            if status == 304:
                st.skipped.append(f"{div} {season}: invariato")
                continue
            if status != 200 or not body:
                st.errors.append(f"{div} {season}: HTTP {status}")
                continue
            raw_id = self.store.put_raw(SOURCE, endpoint, {}, 200, body, t)
            new_hash = self.store.db.execute("SELECT hash FROM raw_requests WHERE id=?", (raw_id,)).fetchone()[0]
            if old_hash == new_hash and self._loaded(job):
                st.skipped.append(f"{div} {season}: invariato")
                continue
            rep = self.load(body, div, raw_id, st)
            detail = json.dumps({"season": season, "div": div, "competition": self.divisions[div], "rows": rep.rows,
                                 "linked": rep.linked, "unmatched": rep.unmatched[:20], "n_unmatched": len(rep.unmatched),
                                 "link_version": LINK_VERSION},
                                ensure_ascii=False)
            self.store.mark_job(f"dataset-report:{SOURCE}:{div}:{season}", t, detail)
            if season == current or rep.linked >= LINKED_ENOUGH * rep.rows:
                self.store.mark_job(job, t, detail)
            else:  # a past season with many unlinked rows: our results may still be filling in, read it again later
                st.skipped.append(f"{div} {season}: abbinate {rep.linked}/{rep.rows}, riprovo tra {refresh_days:g} giorni")
            if rep.unmatched:
                st.skipped.append(f"{div} {season}: {len(rep.unmatched)} partite non abbinate, es. {', '.join(rep.unmatched[:3])}")
        return st

    # ------------------------------------------------------------------ parse + link
    def _calendar(self) -> dict[str, list[tuple[str, str, str, datetime]]]:
        """Our matches by UTC date, team names canonicalised: GOAL fixtures and results (results cover the backfill)."""
        rows = self.store.db.execute(
            "SELECT fixture_id, home, away, kickoff FROM results UNION "
            "SELECT fixture_id, home, away, MAX(kickoff) FROM fixtures GROUP BY fixture_id").fetchall()
        one: dict[str, tuple] = {}
        for fid, h, a, ko in rows:
            one.setdefault(fid, (fid, self.names.canon(h), self.names.canon(a), datetime.fromisoformat(ko)))  # once per match
        by_day: dict[str, list] = {}
        for x in one.values():
            by_day.setdefault(f"{x[3]:%Y-%m-%d}", []).append(x)
        return by_day

    @staticmethod
    def _near(cal: dict, ko: datetime) -> list[tuple[str, str, str, datetime]]:
        """Our matches within 36h: a club plays at most once in that window, whatever the competition."""
        return [x for d in (-2, -1, 0, 1, 2) for x in cal.get(f"{ko + timedelta(days=d):%Y-%m-%d}", [])
                if abs(x[3] - ko) <= timedelta(hours=36)]

    @staticmethod
    def _sim(a: str, b: str) -> float:
        import difflib
        na, nb = normalize(a), normalize(b)
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        if na in nb or nb in na:
            return 0.9
        return difflib.SequenceMatcher(None, na, nb).ratio()

    def _learn(self, rows: list[tuple[str, str, datetime, list]]) -> dict[str, str]:
        """Team name of the file -> our canonical name, learnt from the whole season file:
          1. same canonical name, or one clearly closest spelling among the clubs playing on those dates on that side;
          2. a known club pins its opponent: the only match of that club in the window names the other side ('Wolves'
             is whoever hosted or visited a known club on those dates). Accepted when the votes agree (80%+)."""
        import collections
        pools: dict[str, set[str]] = {}
        for home, away, _, near in rows:
            pools.setdefault(home, set()).update(x[1] for x in near)
            pools.setdefault(away, set()).update(x[2] for x in near)
        mapping: dict[str, str] = {}
        for name, pool in pools.items():
            c = self.names.canon(name)
            if c in pool:
                mapping[name] = c
                continue
            scored = sorted(((self._sim(c, p), p) for p in pool), reverse=True)
            if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.1):
                mapping[name] = scored[0][1]
        for _ in range(4):
            votes: dict[str, collections.Counter] = {}
            for home, away, _, near in rows:
                for known, other, ki, oi in ((home, away, 1, 2), (away, home, 2, 1)):
                    if known in mapping and other not in mapping:
                        hit = [x for x in near if x[ki] == mapping[known]]
                        if len(hit) == 1:
                            votes.setdefault(other, collections.Counter())[hit[0][oi]] += 1
            taken, added = set(mapping.values()), False
            for name, c in votes.items():
                best, n = c.most_common(1)[0]
                total = sum(c.values())
                if best in taken or n < 0.8 * total:
                    continue
                if n >= 2 or self._sim(name, best) >= 0.5:
                    mapping[name] = best
                    taken.add(best)
                    added = True
            if not added:
                break
        return mapping

    def load(self, body: bytes, div: str, raw_id: int | None, st: CollectStats) -> FileReport:
        rep = FileReport()
        cal = self._calendar()
        parsed = []
        for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace"))):
            if not row.get("HomeTeam") or not row.get("AwayTeam") or _num(row, "FTHG") is None:
                continue
            ko = _kickoff(row)
            if ko is None:
                continue
            parsed.append((row, ko, self._near(cal, ko)))
        mapping = self._learn([(r["HomeTeam"].strip(), r["AwayTeam"].strip(), ko, near) for r, ko, near in parsed])
        quotes: list[OddsQuote] = []
        stats: list[MatchStat] = []
        t = self.now()
        for row, ko, near in parsed:
            rep.rows += 1
            h, a = mapping.get(row["HomeTeam"].strip()), mapping.get(row["AwayTeam"].strip())
            hit = [x for x in near if x[1] == h and x[2] == a] if h and a else []
            # the same match stored under two GOAL ids (same clubs, same kickoff) gets the data on both
            if not hit or any(abs(x[3] - hit[0][3]) > timedelta(hours=3) for x in hit):
                rep.unmatched.append(f"{row['HomeTeam']}-{row['AwayTeam']} {ko:%d/%m/%Y}")
                continue
            rep.linked += 1
            for fid, _, _, kickoff in hit:
                stats += self._stats(row, fid, t)
                quotes += self._quotes(row, fid, kickoff)
        st.add("stats", self.store.save_stats(SOURCE, stats, raw_id))
        st.add("quotes", self.store.save_quotes(SOURCE, quotes, raw_id))
        return rep

    @staticmethod
    def _stats(row: dict, fid: str, t: datetime) -> list[MatchStat]:
        out = [MatchStat(fixture_id=fid, period="FT", stat="goals", home=_num(row, "FTHG"), away=_num(row, "FTAG"), observed_at=t)]
        if _num(row, "HTHG") is not None and _num(row, "HTAG") is not None:
            out.append(MatchStat(fixture_id=fid, period="1H", stat="goals", home=_num(row, "HTHG"), away=_num(row, "HTAG"), observed_at=t))
        for stat, (hk, ak) in STATS.items():
            h, a = _num(row, hk), _num(row, ak)
            if h is not None or a is not None:
                out.append(MatchStat(fixture_id=fid, period="FT", stat=stat, home=h, away=a, observed_at=t))
        return out

    @staticmethod
    def _quotes(row: dict, fid: str, kickoff: datetime) -> list[OddsQuote]:
        out: list[OddsQuote] = []
        ah = _num(row, "AHh")
        ahc = _num(row, "AHCh")
        for pre, book in BOOKS.items():
            short = "P" if pre == "PS" else pre  # Pinnacle's goal and handicap columns use 'P', its 1X2 columns 'PS'
            for closing in (False, True):
                c = "C" if closing else ""
                at, kind = (kickoff - timedelta(minutes=5), "close") if closing else (opening_time(kickoff), "open")
                cols = [("MATCH_1X2", "HOME", None, f"{pre}{c}H"), ("MATCH_1X2", "DRAW", None, f"{pre}{c}D"),
                        ("MATCH_1X2", "AWAY", None, f"{pre}{c}A"),
                        ("TOTAL_GOALS", "OVER", 2.5, f"{short}{c}>2.5"), ("TOTAL_GOALS", "UNDER", 2.5, f"{short}{c}<2.5")]
                line = ahc if closing else ah
                if line is not None:
                    cols += [("ASIAN_HANDICAP", "HOME", line, f"{short}{c}AHH"), ("ASIAN_HANDICAP", "AWAY", line, f"{short}{c}AHA")]
                for code, sel, ln, col in cols:
                    o = _num(row, col)
                    if o is None or o <= 1.0:
                        continue
                    if code == "ASIAN_HANDICAP" and not is_supported(SelectionRef(market_code=code, selection=sel, line=ln)):
                        continue  # whole and quarter lines refund stakes: not priced
                    out.append(OddsQuote(fixture_id=fid, market_code=code, selection=sel, line=ln, bookmaker=book, odds=o,
                                         observed_at=at, kind=kind, source_level="B"))
        return out
