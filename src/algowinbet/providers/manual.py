"""Overlay provider: odds you type/paste yourself (e.g. read from your Sisal account) merged onto a base provider.

Sisal has no public API and automated scraping would breach its terms, so odds are entered by hand in a small JSON file:

{
  "bookmaker": "Sisal",
  "observed_at": "2025-04-20T11:30:00+00:00",          # optional, default = now
  "fixtures": [
    {"competition": "Serie A", "home": "Inter", "away": "Empoli", "kickoff": "2025-04-20T18:45:00+00:00",
     "markets": [
        {"market": "MATCH_1X2", "odds": {"HOME": 1.30, "DRAW": 5.0, "AWAY": 9.5}},
        {"market": "TOTAL_GOALS", "line": 2.5, "odds": {"OVER": 1.85, "UNDER": 1.95}}
     ]}
  ]
}
Team names must match the names used by the base provider (history source).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..domain import Fixture, FixtureStatus, OddsQuote


def load_manual_odds(path: str | Path) -> tuple[list[Fixture], dict[str, list[OddsQuote]]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    book = data.get("bookmaker", "Manual")
    default_at = datetime.fromisoformat(data["observed_at"]) if data.get("observed_at") else datetime.now(timezone.utc)
    fixtures, quotes = [], {}
    for f in data["fixtures"]:
        ko = datetime.fromisoformat(f["kickoff"])
        fid = f"manual-{f['competition']}-{ko:%Y%m%d}-{f['home']}-{f['away']}".replace(" ", "_")
        fixtures.append(Fixture(id=fid, competition=f["competition"], home=f["home"], away=f["away"], kickoff=ko,
                                status=FixtureStatus.SCHEDULED, provider="manual"))
        qs = []
        for m in f["markets"]:
            for sel, odds in m["odds"].items():
                qs.append(OddsQuote(fixture_id=fid, market_code=m["market"], selection=sel, line=m.get("line"),
                                    bookmaker=book, odds=float(odds), observed_at=default_at, kind="current",
                                    source_level="A"))
        quotes[fid] = qs
    return fixtures, quotes


class ManualOverlay:
    """Wraps a base provider: base fixtures/history/quotes + hand-entered fixtures/quotes."""

    def __init__(self, base, manual_path: str | Path):
        self.base = base
        self.name = f"{base.name}+manual"
        self._fx, self._q = load_manual_odds(manual_path)

    def list_competitions(self):
        return sorted(set(self.base.list_competitions()) | {f.competition for f in self._fx})

    def list_fixtures(self, competitions, start, end):
        mine = [f for f in self._fx if (not competitions or f.competition in competitions) and start <= f.kickoff <= end]
        return sorted(self.base.list_fixtures(competitions, start, end) + mine, key=lambda f: f.kickoff)

    def list_history(self, competitions, until):
        return self.base.list_history(competitions, until)

    def get_quotes(self, fixture_id):
        return self.base.get_quotes(fixture_id) + self._q.get(fixture_id, [])

    def list_markets(self, fixture_id):
        return {q.market_code for q in self.get_quotes(fixture_id)}

    def get_events(self, fixture_id):
        return self.base.get_events(fixture_id)

    def result_of(self, fixture_id):
        return getattr(self.base, "result_of", lambda _: None)(fixture_id)

    def __getattr__(self, item):  # players / lineups / news come from the wrapped provider
        if item in ("base", "_fx", "_q", "name"):
            raise AttributeError(item)
        return getattr(self.base, item)
