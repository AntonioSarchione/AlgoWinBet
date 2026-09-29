"""Information cutoff enforcement (spec 8). Everything downstream sees ONLY what was observable at `cutoff`."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .domain import Fixture, InformationEvent, LineupSnapshot, MatchResult, OddsQuote, Player
from .information.news import RuleBasedParser


@dataclass
class MatchState:
    fixture: Fixture
    cutoff: datetime
    quotes: list[OddsQuote]
    events: list[InformationEvent] = field(default_factory=list)
    lineups: list[LineupSnapshot] = field(default_factory=list)

    @property
    def lineup_state(self) -> str:
        """none | probable | confirmed (confirmed only when BOTH teams have an official XI)."""
        f = self.fixture
        status = {t: {l.status for l in self.lineups if l.team == t and l.fixture_id == f.id} for t in (f.home, f.away)}
        if all("confirmed" in s for s in status.values()):
            return "confirmed"
        if any(s for s in status.values()):
            return "probable"
        kinds = {e.event_type for e in self.events if e.fixture_id in (None, f.id)}  # legacy: coarse events only
        if "LINEUP_CONFIRMED" in kinds:
            return "confirmed"
        if "LINEUP_PROBABLE" in kinds:
            return "probable"
        return "none"

    def latest_info_time(self) -> datetime | None:
        """Most recent lineup/status observation that can move probabilities."""
        times = [l.observed_at for l in self.lineups]
        times += [e.observed_at for e in self.events if e.event_type in ("PLAYER_STATUS", "LINEUP_CONFIRMED", "LINEUP_PROBABLE") and e.confidence >= 0.3]
        return max(times) if times else None


def build_state(provider, fixture: Fixture, cutoff: datetime, roster: list[Player] | None = None, parser=None) -> MatchState:
    quotes = [q for q in provider.get_quotes(fixture.id) if q.observed_at <= cutoff]
    events = [e for e in provider.get_events(fixture.id) if e.observed_at <= cutoff]
    news_fn = getattr(provider, "get_news_items", None)
    if news_fn and roster:
        parser = parser or RuleBasedParser()
        for item in news_fn(fixture.id):
            if item.observed_at <= cutoff:
                events += parser.parse(item, roster)
    lineups = [l for l in getattr(provider, "get_lineups", lambda _: [])(fixture.id) if l.observed_at <= cutoff]
    return MatchState(fixture=fixture, cutoff=cutoff, quotes=quotes, events=_dedupe(events), lineups=lineups)


def _dedupe(events: list[InformationEvent]) -> list[InformationEvent]:
    seen, out = set(), []
    for e in events:  # idempotent: same event id counted once (spec 26.3)
        if e.id not in seen:
            seen.add(e.id)
            out.append(e)
    return out


def history_at(provider, competition: str, cutoff: datetime) -> list[MatchResult]:
    """Results already known at cutoff (result availability = kickoff + 3h, enforced again here)."""
    return [r for r in provider.list_history([competition], cutoff) if r.available_at <= cutoff]
