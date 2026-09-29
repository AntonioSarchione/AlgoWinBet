"""Information cutoff enforcement (spec 8). Everything downstream sees ONLY what was observable at `cutoff`."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .domain import Fixture, InformationEvent, MatchResult, OddsQuote


@dataclass
class MatchState:
    fixture: Fixture
    cutoff: datetime
    quotes: list[OddsQuote]
    events: list[InformationEvent] = field(default_factory=list)

    @property
    def lineup_state(self) -> str:
        """none | probable | confirmed, from events observed by the cutoff."""
        kinds = {e.event_type for e in self.events if e.fixture_id in (None, self.fixture.id)}
        if "LINEUP_CONFIRMED" in kinds:
            return "confirmed"
        if "LINEUP_PROBABLE" in kinds:
            return "probable"
        return "none"


def build_state(provider, fixture: Fixture, cutoff: datetime) -> MatchState:
    quotes = [q for q in provider.get_quotes(fixture.id) if q.observed_at <= cutoff]
    events = [e for e in provider.get_events(fixture.id) if e.observed_at <= cutoff]
    return MatchState(fixture=fixture, cutoff=cutoff, quotes=quotes, events=events)


def history_at(provider, competition: str, cutoff: datetime) -> list[MatchResult]:
    """Results already known at cutoff (result availability = kickoff + 3h, enforced again here)."""
    return [r for r in provider.list_history([competition], cutoff) if r.available_at <= cutoff]
