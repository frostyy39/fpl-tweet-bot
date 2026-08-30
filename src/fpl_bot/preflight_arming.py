"""Idempotent Cloud Tasks arming for the read-only five-minute preflight."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from fpl_bot.cloud_tasks import (
    CloudTaskBoundary,
    CloudTaskCreateAmbiguousError,
    CloudTaskCreateDisposition,
    CloudTaskCreateRejectedError,
    CloudTaskDefinition,
    CloudTaskDefinitionConflictError,
    CloudTaskNameReservedError,
    CloudTaskReconciliationError,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import FplBotError
from fpl_bot.posting_state import (
    EventPostingContext,
    PostingAuditRecord,
    PostingStateConflictError,
    PostingStateValidationError,
    PostingStatus,
    require_utc,
)


class PreflightTaskArmingError(FplBotError):
    """Base class for preflight task arming failures."""


class PreflightTaskArmingValidationError(PreflightTaskArmingError):
    """Raised before any audit or task-create operation for invalid input."""


class PreflightAuditPersistenceError(PreflightTaskArmingError):
    """Raised when preflight audit state cannot be confirmed before create."""


class PreflightOutcomeAuditPersistenceError(PreflightTaskArmingError):
    """Raised when the task outcome cannot be recorded durably."""

    def __init__(
        self,
        task_name: str,
        preflight_status: str,
        *,
        external_task_known: bool,
    ) -> None:
        super().__init__(
            "Preflight task outcome could not be durably recorded; reconcile only the same "
            "deterministic task name"
        )
        self.task_name = task_name
        self.preflight_status = preflight_status
        self.external_task_known = external_task_known


class PreflightAuditStatus(StrEnum):
    ARMING = "preflight_arming"
    SCHEDULED = "preflight_scheduled"
    ALREADY_SCHEDULED = "preflight_already_scheduled"
    RECONCILED_SCHEDULED = "preflight_reconciled_scheduled"
    SKIPPED_TOO_LATE = "preflight_skipped_too_late"
    CREATE_FAILED = "preflight_create_failed"
    CREATE_AMBIGUOUS = "preflight_create_ambiguous"
    TASK_NAME_RESERVED = "preflight_task_name_reserved"
    DEFINITION_CONFLICT = "preflight_definition_conflict"
    RECONCILIATION_REQUIRED = "preflight_reconciliation_required"
    PASSED = "preflight_passed"
    STALE_REARMED = "preflight_stale_rearmed"
    STALE_OTHER_DAY = "preflight_stale_other_day"
    STALE_EVENT_MISSING = "preflight_stale_event_missing"
    OVERDUE_CHANGE = "preflight_overdue_change"
    FAILED_RETRYABLE = "preflight_failed_retryable"


class PreflightTaskArmingStatus(StrEnum):
    SCHEDULED = "scheduled"
    ALREADY_SCHEDULED = "already_scheduled"
    RECONCILED_SCHEDULED = "reconciled_scheduled"
    TOO_LATE = "too_late"
    POSTING_CLOSED = "posting_closed"


@dataclass(frozen=True, slots=True)
class PreflightTaskArmingResult:
    instruction: ScheduledDeadlineInstruction
    task_name: str
    status: PreflightTaskArmingStatus
    existing_posting_status: PostingStatus | None = None

    def __post_init__(self) -> None:
        closed = self.status is PreflightTaskArmingStatus.POSTING_CLOSED
        if closed != (self.existing_posting_status is not None):
            raise PreflightTaskArmingValidationError(
                "Only a posting-closed preflight result may contain posting state"
            )


class PreflightAuditStore(Protocol):
    """Narrow audit-only state boundary; it deliberately exposes no posting claim methods."""

    def get_event(self, event_id: int) -> PostingAuditRecord | None: ...

    def reconcile_unclaimed_event(
        self,
        context: EventPostingContext,
    ) -> PostingAuditRecord: ...


Clock = Callable[[], datetime]


class PreflightTaskArmer:
    """Create at most one deterministic future preflight task without posting capability."""

    def __init__(
        self,
        audit_store: PreflightAuditStore,
        task_boundary: CloudTaskBoundary,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._audit_store = audit_store
        self._task_boundary = task_boundary
        self._clock = clock or _utc_now

    def arm(self, instruction: ScheduledDeadlineInstruction) -> PreflightTaskArmingResult:
        if not isinstance(instruction, ScheduledDeadlineInstruction):
            raise PreflightTaskArmingValidationError(
                "Preflight arming requires a ScheduledDeadlineInstruction"
            )
        now_utc = self._validated_now()
        definition = self._task_boundary.build_task(instruction)

        if now_utc >= definition.schedule_time_utc:
            closed = self._prepare_audit(
                instruction,
                definition,
                PreflightAuditStatus.SKIPPED_TOO_LATE,
            )
            return closed or PreflightTaskArmingResult(
                instruction=instruction,
                task_name=definition.task_name,
                status=PreflightTaskArmingStatus.TOO_LATE,
            )

        closed = self._prepare_audit(
            instruction,
            definition,
            PreflightAuditStatus.ARMING,
        )
        if closed is not None:
            return closed
        closed = self._check_before_create(instruction, definition)
        if closed is not None:
            return closed

        try:
            disposition = self._task_boundary.create_task(definition)
        except CloudTaskCreateRejectedError:
            self._persist_outcome(
                instruction,
                definition,
                PreflightAuditStatus.CREATE_FAILED,
                external_task_known=False,
            )
            raise
        except CloudTaskCreateAmbiguousError:
            self._persist_outcome(
                instruction,
                definition,
                PreflightAuditStatus.CREATE_AMBIGUOUS,
                external_task_known=False,
            )
            raise
        except CloudTaskNameReservedError:
            self._persist_outcome(
                instruction,
                definition,
                PreflightAuditStatus.TASK_NAME_RESERVED,
                external_task_known=False,
            )
            raise
        except CloudTaskDefinitionConflictError:
            self._persist_outcome(
                instruction,
                definition,
                PreflightAuditStatus.DEFINITION_CONFLICT,
                external_task_known=True,
            )
            raise
        except CloudTaskReconciliationError:
            self._persist_outcome(
                instruction,
                definition,
                PreflightAuditStatus.RECONCILIATION_REQUIRED,
                external_task_known=False,
            )
            raise

        if disposition is CloudTaskCreateDisposition.CREATED:
            audit_status = PreflightAuditStatus.SCHEDULED
            result_status = PreflightTaskArmingStatus.SCHEDULED
        elif disposition is CloudTaskCreateDisposition.ALREADY_EXISTS:
            audit_status = PreflightAuditStatus.ALREADY_SCHEDULED
            result_status = PreflightTaskArmingStatus.ALREADY_SCHEDULED
        elif disposition is CloudTaskCreateDisposition.RECONCILED:
            audit_status = PreflightAuditStatus.RECONCILED_SCHEDULED
            result_status = PreflightTaskArmingStatus.RECONCILED_SCHEDULED
        else:
            raise PreflightOutcomeAuditPersistenceError(
                definition.task_name,
                "unclassified_create_result",
                external_task_known=False,
            )

        self._persist_outcome(
            instruction,
            definition,
            audit_status,
            external_task_known=True,
        )
        return PreflightTaskArmingResult(
            instruction=instruction,
            task_name=definition.task_name,
            status=result_status,
        )

    def _validated_now(self) -> datetime:
        value = self._clock()
        try:
            require_utc(value, "Preflight arming time")
        except PostingStateValidationError as exc:
            raise PreflightTaskArmingValidationError(str(exc)) from None
        return value

    def _prepare_audit(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
        preflight_status: PreflightAuditStatus,
    ) -> PreflightTaskArmingResult | None:
        try:
            existing = self._audit_store.get_event(instruction.expected_event_id)
            if existing is not None and existing.status is not None:
                return _closed_result(instruction, definition, existing.status)
            context = _preflight_context(instruction, preflight_status, existing)
            reconciled = self._audit_store.reconcile_unclaimed_event(context)
        except PostingStateConflictError:
            return self._closed_after_conflict(instruction, definition)
        except Exception as exc:
            raise PreflightAuditPersistenceError(
                "Preflight audit could not be confirmed; create_task was not called"
            ) from exc
        if reconciled.status is not None:
            return _closed_result(instruction, definition, reconciled.status)
        return None

    def _check_before_create(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
    ) -> PreflightTaskArmingResult | None:
        try:
            current = self._audit_store.get_event(instruction.expected_event_id)
        except Exception as exc:
            raise PreflightAuditPersistenceError(
                "Preflight audit could not be re-read; create_task was not called"
            ) from exc
        if current is None:
            raise PreflightAuditPersistenceError(
                "Preflight audit disappeared; create_task was not called"
            )
        if current.status is not None:
            return _closed_result(instruction, definition, current.status)
        if (
            current.context.official_deadline_utc != instruction.expected_deadline_utc
            or current.context.preflight_status != PreflightAuditStatus.ARMING.value
        ):
            raise PreflightAuditPersistenceError(
                "Preflight audit changed concurrently; create_task was not called"
            )
        return None

    def _closed_after_conflict(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
    ) -> PreflightTaskArmingResult:
        try:
            raced = self._audit_store.get_event(instruction.expected_event_id)
        except Exception as exc:
            raise PreflightAuditPersistenceError(
                "Preflight audit conflict could not be resolved; create_task was not called"
            ) from exc
        if raced is None or raced.status is None:
            raise PreflightAuditPersistenceError(
                "Preflight audit conflicted while unclaimed; create_task was not called"
            )
        return _closed_result(instruction, definition, raced.status)

    def _persist_outcome(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
        preflight_status: PreflightAuditStatus,
        *,
        external_task_known: bool,
    ) -> None:
        try:
            existing = self._audit_store.get_event(instruction.expected_event_id)
            if existing is None or existing.status is not None:
                raise PostingStateConflictError("Posting state closed during preflight arming")
            context = _preflight_context(instruction, preflight_status, existing)
            reconciled = self._audit_store.reconcile_unclaimed_event(context)
            if reconciled.status is not None:
                raise PostingStateConflictError("Posting state closed during preflight arming")
        except Exception as exc:
            raise PreflightOutcomeAuditPersistenceError(
                definition.task_name,
                preflight_status.value,
                external_task_known=external_task_known,
            ) from exc


def _preflight_context(
    instruction: ScheduledDeadlineInstruction,
    preflight_status: PreflightAuditStatus,
    existing: PostingAuditRecord | None,
) -> EventPostingContext:
    return EventPostingContext(
        event_id=instruction.expected_event_id,
        event_code=existing.context.event_code if existing is not None else None,
        official_deadline_utc=instruction.expected_deadline_utc,
        scheduled_task_id=existing.context.scheduled_task_id if existing is not None else None,
        scheduled_task_status=(
            existing.context.scheduled_task_status if existing is not None else None
        ),
        preflight_status=preflight_status.value,
    )


def _closed_result(
    instruction: ScheduledDeadlineInstruction,
    definition: CloudTaskDefinition,
    posting_status: PostingStatus,
) -> PreflightTaskArmingResult:
    return PreflightTaskArmingResult(
        instruction=instruction,
        task_name=definition.task_name,
        status=PreflightTaskArmingStatus.POSTING_CLOSED,
        existing_posting_status=posting_status,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
