"""Canonical domain objects. Providers map into these; nothing else knows provider schemas."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class FixtureStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"
    LIVE = "LIVE"
    FINISHED = "FINISHED"
    UNKNOWN = "UNKNOWN"


class OpportunityStatus(str, Enum):
    STRONG = "STRONG"
    CANDIDATE = "CANDIDATE"
    WATCH = "WATCH"
    NEUTRAL = "NEUTRAL"
    AVOID = "AVOID"
    INVALID = "INVALID"


class Fixture(BaseModel):
    id: str
    competition: str
    home: str
    away: str
    kickoff: datetime
    status: FixtureStatus = FixtureStatus.SCHEDULED
    provider: str = "unknown"
    provider_event_id: str | None = None


class MatchResult(BaseModel):
    fixture_id: str
    competition: str
    home: str
    away: str
    kickoff: datetime
    home_goals: int
    away_goals: int
    neutral: bool = False  # played on neutral ground: no home advantage (international tournaments)

    @property
    def available_at(self) -> datetime:
        """Result is only known after the match ends (kickoff + 3h)."""
        from datetime import timedelta

        return self.kickoff + timedelta(hours=3)


class SelectionRef(BaseModel, frozen=True):
    market_code: str
    selection: str
    line: float | None = None

    @property
    def key(self) -> str:
        return f"{self.market_code}|{self.selection}|{'' if self.line is None else self.line}"

    def label(self) -> str:
        from .markets import describe

        return describe(self)


class MatchStat(BaseModel):
    """One team-level statistic of a match (corners, shots, cards...), per period: FT, 1H, 2H."""
    fixture_id: str
    period: str
    stat: str
    home: float | None = None
    away: float | None = None
    observed_at: datetime


class OddsQuote(BaseModel):
    fixture_id: str
    market_code: str
    selection: str
    line: float | None = None
    bookmaker: str
    odds: float = Field(gt=1.0)
    observed_at: datetime
    kind: str = "current"  # open | current | close
    source_level: str = "B"

    @property
    def ref(self) -> SelectionRef:
        return SelectionRef(market_code=self.market_code, selection=self.selection, line=self.line)


class InformationEvent(BaseModel):
    """Structured news/lineup event (spec 7.2/7.3). Milestone 1 stores and cutoff-filters
    them and lowers/raises data quality; feature impact is a later milestone."""

    id: str
    fixture_id: str | None = None
    team: str | None = None
    player: str | None = None
    event_type: str  # LINEUP_CONFIRMED | LINEUP_PROBABLE | PLAYER_STATUS | ...
    source_level: str = "A"
    published_at: datetime
    observed_at: datetime
    confidence: float = 1.0
    payload: dict[str, Any] = Field(default_factory=dict)


class Position(str, Enum):
    GK = "GK"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"


class Player(BaseModel):
    id: str
    name: str
    team: str
    position: Position
    importance: float = 1.0  # scales prior impact (1 = regular starter)
    start_rate: float | None = None  # usual probability of starting; learned from lineup history when absent


class LineupSnapshot(BaseModel):
    """Expected/probable/confirmed XI of one team for one fixture, as observed at `observed_at`."""

    fixture_id: str
    team: str
    status: str  # probable | confirmed
    starters: list[str]  # player ids
    bench: list[str] = Field(default_factory=list)
    formation: str | None = None
    published_at: datetime
    observed_at: datetime
    source_level: str = "A"


class HistoricalLineup(BaseModel):
    fixture_id: str
    team: str
    starters: list[str]


class NewsItem(BaseModel):
    """Raw text from a source, before parsing into structured InformationEvents."""

    source: str
    source_level: str = "C"
    published_at: datetime
    observed_at: datetime
    text: str
    team: str | None = None
    fixture_id: str | None = None
    url: str | None = None


class Opportunity(BaseModel):
    fixture_id: str
    competition: str
    home: str
    away: str
    kickoff: datetime
    ref: SelectionRef
    description: str
    odds: float
    bookmaker: str
    odds_observed_at: datetime
    n_books: int
    p_struct: float
    p_market: float | None
    p_ensemble_raw: float
    p_final: float
    p_low: float
    p_high: float
    fair_odds: float
    edge: float | None
    ev: float
    ev_lower: float
    uncertainty: float
    model_disagreement: float
    data_quality: float
    data_quality_parts: dict[str, float]
    status: OpportunityStatus
    score: float
    model_version: str
    calibration_version: str
    cutoff: datetime
    # lineup / information layer (spec 5, 8)
    p_struct_blind: float | None = None  # structural p ignoring lineups/availability
    lineup_delta_p: float = 0.0  # p_struct - p_struct_blind = information shock
    lineup_state: str = "none"  # none | probable | confirmed
    odds_stale: bool = False  # quote older than relevant information: verify current price
    lineup_notes: list[str] = Field(default_factory=list)


class Slip(BaseModel):
    legs: list[Opportunity]
    total_odds: float
    joint_probability: float
    fair_odds: float
    ev: float
    ev_lower: float
    uncertainty: float
    correlation_penalty: float
    model_disagreement: float
    objective: float
    stake: float = 0.0
    explanation: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> frozenset[tuple[str, str]]:
        return frozenset((o.fixture_id, o.ref.key) for o in self.legs)
