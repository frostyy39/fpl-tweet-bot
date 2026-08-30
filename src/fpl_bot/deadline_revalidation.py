"""Live FPL revalidation boundary for one scheduled deadline instruction."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fpl_bot.errors import DataValidationError, FplBotError
from fpl_bot.events import parse_events
from fpl_bot.models import EventReport, FplEvent
from fpl_bot.parsing import parse_fixtures, parse_teams
from fpl_bot.post_execution import DeadlinePostExecutionResult
from fpl_bot.posting_state import (
    EventPostingContext,
    PostingStateConflictError,
    PostingStateStore,
)
from fpl_bot.service import FplDataSource, build_event_report


class DeadlineRevalidationError(FplBotError):
    """Base class for scheduled deadline revalidation failures."""


class ScheduledInstructionValidationError(DeadlineRevalidationError):
    """Raised when immutable scheduled identity is malformed."""


class StaleDeadlineInstructionError(DeadlineRevalidationError):
    """Raised when live FPL identity no longer matches the scheduled instruction."""


class EarlyDeadlineExecutionError(DeadlineRevalidationError):
    """Raised when execution is attempted before the live official FPL deadline."""


@dataclass(frozen=True, slots=True)
class ScheduledDeadlineInstruction:
    """Minimum immutable identity carried by future scheduled work."""

    expected_event_id: int
    expected_deadline_utc: datetime

    def __post_init__(self) -> None:
        if (
            isinstance(self.expected_event_id, bool)
            or not isinstance(self.expected_event_id, int)
            or self.expected_event_id <= 0
        ):
            raise ScheduledInstructionValidationError(
                "Expected FPL event ID must be a positive integer"
            )
        _require_utc(self.expected_deadline_utc, "Expected FPL deadline")


class PostExecutionBoundary(Protocol):
    def execute(self, report: EventReport) -> DeadlinePostExecutionResult: ...


Clock = Callable[[], datetime]


class DeadlineExecutionRevalidator:
    """Rebuild live FPL truth before allowing any posting-side effect."""

    def __init__(
        self,
        fpl_source: FplDataSource,
        state_store: PostingStateStore,
        post_executor: PostExecutionBoundary,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._fpl_source = fpl_source
        self._state_store = state_store
        self._post_executor = post_executor
        self._clock = clock or _utc_now

    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        if not isinstance(instruction, ScheduledDeadlineInstruction):
            raise ScheduledInstructionValidationError(
                "Execution requires a ScheduledDeadlineInstruction"
            )

        bootstrap = self._fpl_source.fetch_bootstrap_static()
        if (
            not isinstance(bootstrap, Mapping)
            or "events" not in bootstrap
            or "teams" not in bootstrap
        ):
            raise DataValidationError("FPL bootstrap response must contain events and teams")

        events = parse_events(bootstrap["events"])
        event = _find_expected_event(events, instruction.expected_event_id)
        if event.deadline_utc != instruction.expected_deadline_utc:
            raise StaleDeadlineInstructionError(
                f"FPL event {instruction.expected_event_id} official deadline changed; "
                "scheduled instruction is stale"
            )

        now_utc = self._clock()
        _require_utc(now_utc, "Current execution time")
        if now_utc < event.deadline_utc:
            raise EarlyDeadlineExecutionError(
                f"FPL event {event.event_id} cannot execute before its official deadline"
            )

        teams = parse_teams(bootstrap["teams"])
        fixture_payload = self._fpl_source.fetch_event_fixtures(event.event_id)
        fixtures = parse_fixtures(list(fixture_payload), expected_event_id=event.event_id)
        report = build_event_report(event, teams, fixtures)

        context = _fresh_posting_context(report)
        existing = self._state_store.get_event(event.event_id)
        if existing is not None and existing.status is not None:
            return self._post_executor.execute(report)
        if existing is not None:
            context = replace(
                context,
                scheduled_task_id=existing.context.scheduled_task_id,
                scheduled_task_status=existing.context.scheduled_task_status,
                preflight_status=existing.context.preflight_status,
            )
        try:
            self._state_store.reconcile_unclaimed_event(context)
        except PostingStateConflictError:
            raced = self._state_store.get_event(event.event_id)
            if raced is None or raced.status is None:
                raise
        return self._post_executor.execute(report)


def _find_expected_event(events: tuple[FplEvent, ...], expected_event_id: int) -> FplEvent:
    event = next((item for item in events if item.event_id == expected_event_id), None)
    if event is None:
        raise StaleDeadlineInstructionError(
            f"Expected FPL event {expected_event_id} is absent from live FPL data"
        )
    return event


def _fresh_posting_context(report: EventReport) -> EventPostingContext:
    return EventPostingContext(
        event_id=report.event.event_id,
        event_code=report.event_code,
        official_deadline_utc=report.event.deadline_utc,
    )


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ScheduledInstructionValidationError(f"{label} must be timezone-aware UTC")


def _utc_now() -> datetime:
    return datetime.now(UTC)
