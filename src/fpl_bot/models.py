"""Immutable domain models used by the deterministic core."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class FplEvent:
    event_id: int
    name: str
    deadline_utc: datetime
    is_current: bool
    is_next: bool


@dataclass(frozen=True, slots=True)
class Team:
    team_id: int
    name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: int
    event_id: int
    home_team_id: int
    away_team_id: int


class EventKind(StrEnum):
    GW = "GW"
    BGW = "BGW"
    DGW = "DGW"
    BDGW = "BDGW"


@dataclass(frozen=True, slots=True)
class TeamFixtureCount:
    team: Team
    fixture_count: int


@dataclass(frozen=True, slots=True)
class FixtureClassification:
    kind: EventKind
    team_counts: tuple[TeamFixtureCount, ...]

    @property
    def blank_teams(self) -> tuple[TeamFixtureCount, ...]:
        return tuple(item for item in self.team_counts if item.fixture_count == 0)

    @property
    def double_teams(self) -> tuple[TeamFixtureCount, ...]:
        return tuple(item for item in self.team_counts if item.fixture_count > 1)


@dataclass(frozen=True, slots=True)
class EventReport:
    event: FplEvent
    deadline_london: datetime
    classification: FixtureClassification
    event_code: str
    tweet: str
