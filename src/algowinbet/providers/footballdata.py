"""Adapter for football-data.co.uk CSV files (free for private use; download them manually, do not bot the site).

- Results file (e.g. E0.csv / I1.csv / a season file): finished matches + opening and closing odds.
- fixtures.csv: upcoming matches + current odds.
Timestamps are approximations (the CSV has no odds timestamps): opening odds ~ kickoff-48h, closing ~ kickoff-1h,
fixture odds = file modification time. Backtest leakage guarantees are therefore weaker than with a real snapshot feed.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain import Fixture, FixtureStatus, InformationEvent, MatchResult, OddsQuote

BOOK_PREFIXES = {"B365": "Bet365", "PS": "Pinnacle", "BW": "BetWin", "IW": "Interwetten", "WH": "WilliamHill", "VC": "BetVictor"}
DIV_NAMES = {"E0": "Premier League", "E1": "Championship", "SP1": "La Liga", "D1": "Bundesliga", "I1": "Serie A",
             "I2": "Serie B", "F1": "Ligue 1", "N1": "Eredivisie", "P1": "Primeira Liga"}


def _parse_dt(date: str, time: str | None) -> datetime:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            d = datetime.strptime(date.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"bad date {date}")
    hh, mm = (15, 0)
    if time and ":" in time:
        hh, mm = (int(x) for x in time.split(":")[:2])
    return d.replace(hour=hh, minute=mm, tzinfo=timezone.utc)


def _f(row: dict, key: str) -> float | None:
    v = row.get(key)
    try:
        x = float(v) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x and x > 1.0 else None


class FootballDataCSV:
    name = "football-data.co.uk"

    def __init__(self, paths: list[str | Path], names=None):
        self._canon = names.canon if names is not None else (lambda n: n)
        self._fixtures: dict[str, Fixture] = {}
        self._results: dict[str, MatchResult] = {}
        self._quotes: dict[str, list[OddsQuote]] = {}
        for p in paths:
            self._load(Path(p))

    def _load(self, path: Path) -> None:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if not row.get("HomeTeam") or not row.get("Date"):
                    continue
                ko = _parse_dt(row["Date"], row.get("Time"))
                div = row.get("Div", path.stem)
                comp = DIV_NAMES.get(div, div)
                home, away = self._canon(row["HomeTeam"]), self._canon(row["AwayTeam"])
                fid = f"fd-{div}-{ko:%Y%m%d}-{home}-{away}".replace(" ", "_")
                finished = row.get("FTHG") not in (None, "")
                self._fixtures[fid] = Fixture(
                    id=fid, competition=comp, home=home, away=away, kickoff=ko,
                    status=FixtureStatus.FINISHED if finished else FixtureStatus.SCHEDULED, provider=self.name,
                )
                if finished:
                    self._results[fid] = MatchResult(
                        fixture_id=fid, competition=comp, home=home, away=away, kickoff=ko,
                        home_goals=int(float(row["FTHG"])), away_goals=int(float(row["FTAG"])),
                    )
                qs: list[OddsQuote] = []
                for pre, book in BOOK_PREFIXES.items():
                    if finished:
                        qs += self._book_quotes(row, fid, pre, book, "", ko - timedelta(hours=48), "open")
                        qs += self._book_quotes(row, fid, pre, book, "C", ko - timedelta(hours=1), "close")
                    else:
                        qs += self._book_quotes(row, fid, pre, book, "", min(mtime, ko - timedelta(minutes=30)), "current")
                self._quotes[fid] = qs

    @staticmethod
    def _book_quotes(row, fid, pre, book, c, at, kind) -> list[OddsQuote]:
        out = []

        def add(code, sel, line, col):
            o = _f(row, col)
            if o:
                out.append(OddsQuote(fixture_id=fid, market_code=code, selection=sel, line=line, bookmaker=book,
                                     odds=o, observed_at=at, kind=kind, source_level="B"))

        add("MATCH_1X2", "HOME", None, f"{pre}{c}H")
        add("MATCH_1X2", "DRAW", None, f"{pre}{c}D")
        add("MATCH_1X2", "AWAY", None, f"{pre}{c}A")
        add("TOTAL_GOALS", "OVER", 2.5, f"{pre}{c}>2.5")
        add("TOTAL_GOALS", "UNDER", 2.5, f"{pre}{c}<2.5")
        return out

    # ---------------------------------------------------------------- adapter
    def list_competitions(self) -> list[str]:
        return sorted({f.competition for f in self._fixtures.values()})

    def list_fixtures(self, competitions, start, end) -> list[Fixture]:
        return sorted(
            (f for f in self._fixtures.values()
             if (not competitions or f.competition in competitions) and start <= f.kickoff <= end),
            key=lambda f: f.kickoff,
        )

    def list_history(self, competitions, until) -> list[MatchResult]:
        return sorted(
            (r for r in self._results.values()
             if (not competitions or r.competition in competitions) and r.available_at <= until),
            key=lambda r: r.kickoff,
        )

    def get_quotes(self, fixture_id: str) -> list[OddsQuote]:
        return list(self._quotes.get(fixture_id, []))

    def list_markets(self, fixture_id: str) -> set[str]:
        return {q.market_code for q in self._quotes.get(fixture_id, [])}

    def get_events(self, fixture_id: str) -> list[InformationEvent]:
        return []

    def result_of(self, fixture_id: str) -> MatchResult | None:
        return self._results.get(fixture_id)
