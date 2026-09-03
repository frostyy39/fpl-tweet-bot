"""Pure same-day planning for the next authoritative FPL deadline."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import (
    DataValidationError,
    DeadlineEventSelectionError,
    DeadlineTimezoneError,
    FplBootstrapValidationError,
    MultipleSameDayEventsError,
    NoSuitableEventError,
)
from fpl_bot.events import parse_events, select_next_event, to_london
from fpl_bot.models import FplEvent
from fpl_bot.service import FplDataSource


class DeadlinePlanningStatus(StrEnum):
    """Non-error outcomes of the London calendar-day decision."""

    ELIGIBLE_TO_ARM = "eligible_to_arm"
    NOT_CURRENT_LONDON_DAY = "not_current_london_day"


@dataclass(frozen=True, slots=True)
class DeadlinePlanningDecision:
    """Immutable arm/no-arm result; invalid authoritative data raises a typed error."""

    status: DeadlinePlanningStatus
    instruction: ScheduledDeadlineInstruction | None = None

    def __post_init__(self) -> None:
        if self.status is DeadlinePlanningStatus.ELIGIBLE_TO_ARM:
            if self.instruction is None:
                raise ValueError("An eligible planning decision requires an instruction")
        elif self.instruction is not None:
            raise ValueError("A no-arm planning decision cannot contain an instruction")

    @property
    def should_arm(self) -> bool:
        return self.status is DeadlinePlanningStatus.ELIGIBLE_TO_ARM


@dataclass(frozen=True, slots=True)
class DeadlinePlanningObservation:
    """Read-only result shared by the production planner and diagnostic probe."""

    event: FplEvent
    observed_at_utc: datetime

    @property
    def deadline_london(self) -> datetime:
        return _to_london(self.event.deadline_utc)

    @property
    def is_current_london_day(self) -> bool:
        return _to_london(self.observed_at_utc).date() == self.deadline_london.date()


Clock = Callable[[], datetime]


class DeadlinePlanner:
    """Select the live authoritative event and decide whether it is London-today."""

    def __init__(
        self,
        fpl_source: FplDataSource,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._fpl_source = fpl_source
        self._clock = clock or _utc_now

    def plan(self) -> DeadlinePlanningDecision:
        observation = self.observe()
        return decide_london_deadline_day(observation.event, observation.observed_at_utc)

    def observe(self) -> DeadlinePlanningObservation:
        """Fetch and select exactly the event needed by planning, without arming anything."""

        bootstrap = self._fpl_source.fetch_bootstrap_static()
        if not isinstance(bootstrap, Mapping) or "events" not in bootstrap:
            raise FplBootstrapValidationError("FPL bootstrap response must contain events")

        try:
            events = parse_events(bootstrap["events"])
        except DataValidationError as exc:
            raise FplBootstrapValidationError(str(exc)) from exc
        now_utc = _normalize_utc(self._clock(), "Current planning time")
        event = _select_planning_event(events, now_utc)
        return DeadlinePlanningObservation(event=event, observed_at_utc=now_utc)


def decide_london_deadline_day(
    event: FplEvent,
    now_utc: datetime,
) -> DeadlinePlanningDecision:
    """Apply only the London date rule after authoritative event selection."""
    normalized_now = _normalize_utc(now_utc, "Current planning time")
    _require_utc(event.deadline_utc, "Official FPL deadline")

    if _to_london(normalized_now).date() != _to_london(event.deadline_utc).date():
        return DeadlinePlanningDecision(status=DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY)

    return DeadlinePlanningDecision(
        status=DeadlinePlanningStatus.ELIGIBLE_TO_ARM,
        instruction=ScheduledDeadlineInstruction(
            expected_event_id=event.event_id,
            expected_deadline_utc=event.deadline_utc,
        ),
    )


def _select_planning_event(
    events: Sequence[FplEvent],
    now_utc: datetime,
) -> FplEvent:
    london_today = _to_london(now_utc).date()
    today_events = tuple(
        event for event in events if _to_london(event.deadline_utc).date() == london_today
    )
    if len(today_events) > 1:
        raise MultipleSameDayEventsError(
            "FPL exposes multiple event deadlines on the current Europe/London day"
        )
    if today_events:
        return today_events[0]
    try:
        return select_next_event(events, now=now_utc)
    except NoSuitableEventError:
        raise
    except DataValidationError as exc:
        raise DeadlineEventSelectionError(str(exc)) from exc


def _normalize_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DeadlineTimezoneError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DeadlineTimezoneError(f"{label} must be timezone-aware UTC")


def _to_london(value: datetime) -> datetime:
    try:
        return to_london(value)
    except (OverflowError, ValueError) as exc:
        raise DeadlineTimezoneError("London deadline conversion failed") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC)
