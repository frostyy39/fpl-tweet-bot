"""Immutable domain models for deterministic Captain recommendations."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from fpl_bot.models import FplEvent, FplPlayer, Team


@dataclass(frozen=True, slots=True)
class CaptainProjection:
    element_id: int
    projected_points: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.element_id, bool)
            or not isinstance(self.element_id, int)
            or self.element_id <= 0
        ):
            raise ValueError("projection element_id must be a positive integer")
        if not isinstance(self.projected_points, Decimal) or not self.projected_points.is_finite():
            raise ValueError("projected_points must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class CaptainFixture:
    opponent: Team
    is_home: bool


@dataclass(frozen=True, slots=True)
class CaptainSelection:
    player: FplPlayer
    team: Team
    projected_points: Decimal
    fixtures: tuple[CaptainFixture, ...]


@dataclass(frozen=True, slots=True)
class CaptainReport:
    event: FplEvent
    captain_target_utc: datetime
    captain_target_london: datetime
    event_code: str
    top_three: tuple[CaptainSelection, CaptainSelection, CaptainSelection]
    differential: CaptainSelection
    tweet: str = field(repr=False)
