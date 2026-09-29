"""Overlay provider for information you copy from OFFICIAL sources (club / league sites) into a small JSON file:
rosters, official or probable XIs, and raw news text. Free, compliant (no scraping), and exactly what the spec calls a
Level-A source. Team names must match the names used by the base provider (history/odds source).

{
  "players": [ {"name": "Lautaro Martinez", "team": "Inter", "position": "FWD", "importance": 2.0, "start_rate": 0.85}, ... ],
  "lineups": [ {"team": "Inter", "date": "2025-04-20", "status": "confirmed", "formation": "3-5-2",
                "starters": ["Sommer", "Pavard", ...], "published_at": "2025-04-20T17:30:00+00:00"} ],
  "news":    [ {"source": "inter.it", "level": "A", "published_at": "2025-04-19T10:00:00+00:00",
                "team": "Inter", "text": "Martinez indisponibile per infortunio."} ]
}
importance (optional, default 1) scales the prior impact of a player: use >1 for key players (e.g. goals+assists per 90 relative
to a typical starter of that position). start_rate (optional) is the usual probability of starting.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain import LineupSnapshot, NewsItem, Player, Position
from ..information.news import parse_lineup_names


class ManualInfoOverlay:
    def __init__(self, base, path: str | Path):
        self.base = base
        self.name = f"{base.name}+info"
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        self.players = [Player(id=f"{p['team']}::{p['name']}", name=p["name"], team=p["team"], position=Position(p["position"]),
                               importance=float(p.get("importance", 1.0)), start_rate=p.get("start_rate"))
                        for p in data.get("players", [])]
        self._raw_lineups = data.get("lineups", [])
        self._now = now
        self._news = [NewsItem(source=n.get("source", "manual"), source_level=n.get("level", "C"),
                               published_at=datetime.fromisoformat(n.get("published_at", now.isoformat())),
                               observed_at=datetime.fromisoformat(n.get("observed_at", n.get("published_at", now.isoformat()))),
                               text=n["text"], team=n.get("team")) for n in data.get("news", [])]
        self._lineups: dict[str, list[LineupSnapshot]] = {}
        self.warnings: list[str] = []
        self._resolved = False

    def __getattr__(self, item):  # everything else (fixtures, quotes, history, ...) comes from the base provider
        if item in ("base", "players", "_raw_lineups", "_news", "_lineups", "_now", "warnings", "_resolved"):
            raise AttributeError(item)
        return getattr(self.base, item)

    def _team_roster(self, team: str) -> list[Player]:
        return [p for p in self.players if p.team == team]

    def _resolve_lineups(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        far = datetime(2100, 1, 1, tzinfo=timezone.utc)
        fixtures = self.base.list_fixtures(None, datetime(2000, 1, 1, tzinfo=timezone.utc), far)
        for lu in self._raw_lineups:
            day = datetime.fromisoformat(lu["date"]).replace(tzinfo=timezone.utc)
            fx = next((f for f in fixtures if lu["team"] in (f.home, f.away) and abs(f.kickoff - day) <= timedelta(days=1)), None)
            if fx is None:
                self.warnings.append(f"lineup {lu['team']} {lu['date']}: nessuna partita trovata nel provider")
                continue
            ids, bad = parse_lineup_names(",".join(lu["starters"]), self._team_roster(lu["team"]))
            if bad:
                self.warnings.append(f"lineup {lu['team']}: nomi non risolti {bad}")
            pub = datetime.fromisoformat(lu.get("published_at", self._now.isoformat()))
            self._lineups.setdefault(fx.id, []).append(LineupSnapshot(
                fixture_id=fx.id, team=lu["team"], status=lu.get("status", "confirmed"), starters=ids,
                formation=lu.get("formation"), published_at=pub,
                observed_at=datetime.fromisoformat(lu.get("observed_at", pub.isoformat())), source_level=lu.get("level", "A")))

    # ---------------------------------------------------------------- adapter
    def list_players(self, competition: str) -> list[Player]:
        teams = {r.home for r in self.base.list_history([competition], datetime(2100, 1, 1, tzinfo=timezone.utc))}
        teams |= {r.away for r in self.base.list_history([competition], datetime(2100, 1, 1, tzinfo=timezone.utc))}
        teams |= {t for f in self.base.list_fixtures([competition], datetime(2000, 1, 1, tzinfo=timezone.utc),
                                                    datetime(2100, 1, 1, tzinfo=timezone.utc)) for t in (f.home, f.away)}
        base_players = list(getattr(self.base, "list_players", lambda _c: [])(competition))
        have = {p.team for p in base_players}
        return base_players + [p for p in self.players if p.team in teams and p.team not in have]

    def get_lineups(self, fixture_id: str) -> list[LineupSnapshot]:
        self._resolve_lineups()
        return list(getattr(self.base, "get_lineups", lambda _f: [])(fixture_id)) + self._lineups.get(fixture_id, [])

    def get_news_items(self, fixture_id: str) -> list[NewsItem]:
        fx = next((f for f in self.base.list_fixtures(None, datetime(2000, 1, 1, tzinfo=timezone.utc),
                                                     datetime(2100, 1, 1, tzinfo=timezone.utc)) if f.id == fixture_id), None)
        mine = [n for n in self._news if fx and n.team in (fx.home, fx.away)]
        return list(getattr(self.base, "get_news_items", lambda _f: [])(fixture_id)) + mine
