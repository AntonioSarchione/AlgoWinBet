"""Provider adapter contract (spec 6.1). The rest of the app only sees canonical objects."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain import Fixture, InformationEvent, MatchResult, OddsQuote


class ProviderAdapter(Protocol):
    name: str

    def list_competitions(self) -> list[str]: ...

    def list_fixtures(
        self, competitions: list[str] | None, start: datetime, end: datetime
    ) -> list[Fixture]: ...

    def list_history(self, competitions: list[str] | None, until: datetime) -> list[MatchResult]: ...

    def get_quotes(self, fixture_id: str) -> list[OddsQuote]:
        """ALL quote snapshots for the fixture (every observed_at). Cutoff filtering happens in the state builder."""
        ...

    def list_markets(self, fixture_id: str) -> set[str]: ...

    def get_events(self, fixture_id: str) -> list[InformationEvent]:
        """News / lineup events for the fixture (all observed_at)."""
        ...
