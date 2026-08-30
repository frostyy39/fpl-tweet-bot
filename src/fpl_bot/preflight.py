"""Read-only live FPL preflight and stale-deadline task correction."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import DataValidationError, FplApiError, FplBotError
from fpl_bot.events import parse_events, to_london
from fpl_bot.posting_state import (
    EventPostingContext,
    PostingStateConflictError,
    PostingStateValidationError,
    PostingStatus,
    require_utc,
)
from fpl_bot.preflight_arming import (
    PreflightAuditStatus,
    PreflightAuditStore,
    PreflightTaskArmingResult,
    PreflightTaskArmingStatus,
)
from fpl_bot.service import FplDataSource
from fpl_bot.task_arming import TaskArmingResult, TaskArmingStatus


class PreflightError(FplBotError):
    """Base class for read-only preflight failures."""


class PreflightValidationError(PreflightError):
    """Raised before preflight activity for invalid input or time."""


class PreflightAuditPersistenceError(PreflightError):
    """Raised when preflight audit reconciliation cannot be confirmed."""


class PreflightStatus(StrEnum):
    OK = "preflight_ok"
    TOO_LATE = "preflight_too_late"
    STALE_REARMED = "preflight_stale_rearmed"
    STALE_OTHER_DAY = "preflight_stale_other_day"
    STALE_EVENT_MISSING = "preflight_stale_event_missing"
    OVERDUE_CHANGE = "preflight_overdue_change"
    POSTING_CLOSED = "preflight_posting_closed"


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: PreflightStatus
    instruction: ScheduledDeadlineInstruction
    authoritative_instruction: ScheduledDeadlineInstruction | None = None
    final_task_status: TaskArmingStatus | None = None
    preflight_task_status: PreflightTaskArmingStatus | None = None
    existing_posting_status: PostingStatus | None = None


class FinalTaskArmerBoundary(Protocol):
    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult: ...


class PreflightTaskArmerBoundary(Protocol):
    def arm(self, instruction: ScheduledDeadlineInstruction) -> PreflightTaskArmingResult: ...


Clock = Callable[[], datetime]


class DeadlinePreflight:
    """Validate live deadline identity and correct stale tasks without any posting path."""

    def __init__(
        self,
        fpl_source: FplDataSource,
        audit_store: PreflightAuditStore,
        final_task_armer: FinalTaskArmerBoundary,
        preflight_task_armer: PreflightTaskArmerBoundary,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._fpl_source = fpl_source
        self._audit_store = audit_store
        self._final_task_armer = final_task_armer
        self._preflight_task_armer = preflight_task_armer
        self._clock = clock or _utc_now

    def execute(self, instruction: ScheduledDeadlineInstruction) -> PreflightResult:
        if not isinstance(instruction, ScheduledDeadlineInstruction):
            raise PreflightValidationError("Preflight requires a ScheduledDeadlineInstruction")
        now_utc = self._validated_now()

        try:
            bootstrap = self._fpl_source.fetch_bootstrap_static()
            if not isinstance(bootstrap, Mapping) or "events" not in bootstrap:
                raise DataValidationError("FPL bootstrap response must contain events")
            events = parse_events(bootstrap["events"])
        except (FplApiError, DataValidationError):
            self._record_status(instruction, PreflightAuditStatus.FAILED_RETRYABLE)
            raise

        event = next(
            (item for item in events if item.event_id == instruction.expected_event_id),
            None,
        )
        if event is None:
            closed = self._record_status(
                instruction,
                PreflightAuditStatus.STALE_EVENT_MISSING,
            )
            return self._result_or_closed(
                PreflightStatus.STALE_EVENT_MISSING,
                instruction,
                closed,
            )

        if event.deadline_utc == instruction.expected_deadline_utc:
            if now_utc >= event.deadline_utc:
                closed = self._record_status(
                    instruction,
                    PreflightAuditStatus.SKIPPED_TOO_LATE,
                )
                return self._result_or_closed(
                    PreflightStatus.TOO_LATE,
                    instruction,
                    closed,
                )
            closed = self._record_status(instruction, PreflightAuditStatus.PASSED)
            return self._result_or_closed(PreflightStatus.OK, instruction, closed)

        authoritative = ScheduledDeadlineInstruction(event.event_id, event.deadline_utc)
        if to_london(event.deadline_utc).date() != to_london(now_utc).date():
            closed = self._record_status(
                authoritative,
                PreflightAuditStatus.STALE_OTHER_DAY,
            )
            return self._result_or_closed(
                PreflightStatus.STALE_OTHER_DAY,
                instruction,
                closed,
                authoritative_instruction=authoritative,
            )
        if event.deadline_utc <= now_utc:
            closed = self._record_status(
                authoritative,
                PreflightAuditStatus.OVERDUE_CHANGE,
            )
            return self._result_or_closed(
                PreflightStatus.OVERDUE_CHANGE,
                instruction,
                closed,
                authoritative_instruction=authoritative,
            )

        final_task = self._final_task_armer.arm(authoritative)
        if final_task.instruction is not authoritative:
            raise PreflightValidationError(
                "Final task armer did not preserve the authoritative instruction"
            )
        if final_task.status is TaskArmingStatus.POSTING_CLOSED:
            return self._closed_result(
                instruction,
                final_task.existing_posting_status,
                authoritative_instruction=authoritative,
                final_task_status=final_task.status,
            )
        if final_task.status is TaskArmingStatus.OVERDUE_SAME_DAY:
            closed = self._record_status(
                authoritative,
                PreflightAuditStatus.OVERDUE_CHANGE,
            )
            return self._result_or_closed(
                PreflightStatus.OVERDUE_CHANGE,
                instruction,
                closed,
                authoritative_instruction=authoritative,
                final_task_status=final_task.status,
            )
        if final_task.status not in {
            TaskArmingStatus.ARMED,
            TaskArmingStatus.ALREADY_ARMED,
            TaskArmingStatus.RECONCILED_ARMED,
        }:
            raise PreflightValidationError("Final task armer returned an invalid preflight result")

        preflight_task = self._preflight_task_armer.arm(authoritative)
        if preflight_task.instruction is not authoritative:
            raise PreflightValidationError(
                "Preflight task armer did not preserve the authoritative instruction"
            )
        if preflight_task.status is PreflightTaskArmingStatus.POSTING_CLOSED:
            return self._closed_result(
                instruction,
                preflight_task.existing_posting_status,
                authoritative_instruction=authoritative,
                final_task_status=final_task.status,
                preflight_task_status=preflight_task.status,
            )

        closed = self._record_status(authoritative, PreflightAuditStatus.STALE_REARMED)
        return self._result_or_closed(
            PreflightStatus.STALE_REARMED,
            instruction,
            closed,
            authoritative_instruction=authoritative,
            final_task_status=final_task.status,
            preflight_task_status=preflight_task.status,
        )

    def _validated_now(self) -> datetime:
        value = self._clock()
        try:
            require_utc(value, "Preflight time")
        except PostingStateValidationError as exc:
            raise PreflightValidationError(str(exc)) from None
        return value

    def _record_status(
        self,
        instruction: ScheduledDeadlineInstruction,
        status: PreflightAuditStatus,
    ) -> PostingStatus | None:
        try:
            existing = self._audit_store.get_event(instruction.expected_event_id)
            if existing is not None and existing.status is not None:
                return existing.status
            context = EventPostingContext(
                event_id=instruction.expected_event_id,
                event_code=existing.context.event_code if existing is not None else None,
                official_deadline_utc=instruction.expected_deadline_utc,
                scheduled_task_id=(
                    existing.context.scheduled_task_id if existing is not None else None
                ),
                scheduled_task_status=(
                    existing.context.scheduled_task_status if existing is not None else None
                ),
                preflight_status=status.value,
            )
            reconciled = self._audit_store.reconcile_unclaimed_event(context)
            return reconciled.status
        except PostingStateConflictError:
            try:
                raced = self._audit_store.get_event(instruction.expected_event_id)
            except Exception as exc:
                raise PreflightAuditPersistenceError(
                    "Preflight audit conflict could not be resolved"
                ) from exc
            if raced is not None and raced.status is not None:
                return raced.status
            raise PreflightAuditPersistenceError(
                "Preflight audit conflicted while posting remained unclaimed"
            ) from None
        except Exception as exc:
            raise PreflightAuditPersistenceError(
                "Preflight audit reconciliation could not be confirmed"
            ) from exc

    @staticmethod
    def _result_or_closed(
        status: PreflightStatus,
        instruction: ScheduledDeadlineInstruction,
        existing_posting_status: PostingStatus | None,
        **kwargs: object,
    ) -> PreflightResult:
        if existing_posting_status is not None:
            return DeadlinePreflight._closed_result(
                instruction,
                existing_posting_status,
                **kwargs,
            )
        return PreflightResult(status=status, instruction=instruction, **kwargs)

    @staticmethod
    def _closed_result(
        instruction: ScheduledDeadlineInstruction,
        existing_posting_status: PostingStatus | None,
        **kwargs: object,
    ) -> PreflightResult:
        if existing_posting_status is None:
            raise PreflightValidationError("Closed preflight result requires posting state")
        return PreflightResult(
            status=PreflightStatus.POSTING_CLOSED,
            instruction=instruction,
            existing_posting_status=existing_posting_status,
            **kwargs,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
