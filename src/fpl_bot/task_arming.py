"""Application orchestration for idempotent same-day Cloud Task arming."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

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
    PostingStateStore,
    PostingStateValidationError,
    PostingStatus,
    require_utc,
)


class TaskArmingError(FplBotError):
    """Base class for arming orchestration failures."""


class TaskArmingValidationError(TaskArmingError):
    """Raised before any audit or Cloud Tasks operation for invalid input/time."""


class TaskArmingAuditPersistenceError(TaskArmingError):
    """Raised when pre-create audit state cannot be confirmed."""


class TaskOutcomeAuditPersistenceError(TaskArmingError):
    """Raised after a Cloud Tasks outcome whose final audit cannot be confirmed."""

    def __init__(
        self,
        task_name: str,
        task_status: str,
        *,
        external_task_known: bool,
    ) -> None:
        super().__init__(
            "Cloud Task outcome could not be durably recorded; reconcile the same deterministic "
            "task name and never create a differently named task"
        )
        self.task_name = task_name
        self.task_status = task_status
        self.external_task_known = external_task_known


class ScheduledTaskAuditStatus(StrEnum):
    ARMING = "arming"
    ARMED = "armed"
    ALREADY_ARMED = "already_armed"
    RECONCILED_ARMED = "reconciled_armed"
    OVERDUE_SAME_DAY = "overdue_same_day"
    CREATE_FAILED = "create_failed"
    CREATE_AMBIGUOUS = "create_ambiguous"
    TASK_NAME_RESERVED = "task_name_reserved"
    DEFINITION_CONFLICT = "definition_conflict"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class TaskArmingStatus(StrEnum):
    ARMED = "armed"
    ALREADY_ARMED = "already_armed"
    RECONCILED_ARMED = "reconciled_armed"
    OVERDUE_SAME_DAY = "overdue_same_day"
    POSTING_CLOSED = "posting_closed"


@dataclass(frozen=True, slots=True)
class TaskArmingResult:
    instruction: ScheduledDeadlineInstruction
    task_name: str
    status: TaskArmingStatus
    existing_posting_status: PostingStatus | None = None

    def __post_init__(self) -> None:
        closed = self.status is TaskArmingStatus.POSTING_CLOSED
        if closed != (self.existing_posting_status is not None):
            raise TaskArmingValidationError(
                "Only a posting-closed arming result may contain posting state"
            )


Clock = Callable[[], datetime]


class DeadlineTaskArmer:
    """Audit and create at most one deterministic task for a future deadline."""

    def __init__(
        self,
        state_store: PostingStateStore,
        task_boundary: CloudTaskBoundary,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._state_store = state_store
        self._task_boundary = task_boundary
        self._clock = clock or _utc_now

    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult:
        if not isinstance(instruction, ScheduledDeadlineInstruction):
            raise TaskArmingValidationError("Task arming requires a ScheduledDeadlineInstruction")
        now_utc = self._validated_now()
        definition = self._task_boundary.build_task(instruction)

        if now_utc >= instruction.expected_deadline_utc:
            closed = self._prepare_audit(
                instruction,
                definition,
                ScheduledTaskAuditStatus.OVERDUE_SAME_DAY,
            )
            return closed or TaskArmingResult(
                instruction=instruction,
                task_name=definition.task_name,
                status=TaskArmingStatus.OVERDUE_SAME_DAY,
            )

        closed = self._prepare_audit(
            instruction,
            definition,
            ScheduledTaskAuditStatus.ARMING,
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
                ScheduledTaskAuditStatus.CREATE_FAILED,
                external_task_known=False,
            )
            raise
        except CloudTaskCreateAmbiguousError:
            self._persist_outcome(
                instruction,
                definition,
                ScheduledTaskAuditStatus.CREATE_AMBIGUOUS,
                external_task_known=False,
            )
            raise
        except CloudTaskNameReservedError:
            self._persist_outcome(
                instruction,
                definition,
                ScheduledTaskAuditStatus.TASK_NAME_RESERVED,
                external_task_known=False,
            )
            raise
        except CloudTaskDefinitionConflictError:
            self._persist_outcome(
                instruction,
                definition,
                ScheduledTaskAuditStatus.DEFINITION_CONFLICT,
                external_task_known=True,
            )
            raise
        except CloudTaskReconciliationError:
            self._persist_outcome(
                instruction,
                definition,
                ScheduledTaskAuditStatus.RECONCILIATION_REQUIRED,
                external_task_known=False,
            )
            raise

        if disposition is CloudTaskCreateDisposition.CREATED:
            audit_status = ScheduledTaskAuditStatus.ARMED
            result_status = TaskArmingStatus.ARMED
        elif disposition is CloudTaskCreateDisposition.ALREADY_EXISTS:
            audit_status = ScheduledTaskAuditStatus.ALREADY_ARMED
            result_status = TaskArmingStatus.ALREADY_ARMED
        elif disposition is CloudTaskCreateDisposition.RECONCILED:
            audit_status = ScheduledTaskAuditStatus.RECONCILED_ARMED
            result_status = TaskArmingStatus.RECONCILED_ARMED
        else:
            raise TaskOutcomeAuditPersistenceError(
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
        return TaskArmingResult(
            instruction=instruction,
            task_name=definition.task_name,
            status=result_status,
        )

    def _validated_now(self) -> datetime:
        value = self._clock()
        try:
            require_utc(value, "Task arming time")
        except PostingStateValidationError as exc:
            raise TaskArmingValidationError(str(exc)) from None
        return value

    def _prepare_audit(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
        task_status: ScheduledTaskAuditStatus,
    ) -> TaskArmingResult | None:
        try:
            existing = self._state_store.get_event(instruction.expected_event_id)
            if existing is not None and existing.status is not None:
                return _closed_result(instruction, definition, existing.status)
            context = _scheduling_context(instruction, definition, task_status, existing)
            reconciled = self._state_store.reconcile_unclaimed_event(context)
        except PostingStateConflictError:
            return self._closed_after_conflict(instruction, definition)
        except Exception as exc:
            raise TaskArmingAuditPersistenceError(
                "Task arming audit could not be confirmed; create_task was not called"
            ) from exc
        if reconciled.status is not None:
            return _closed_result(instruction, definition, reconciled.status)
        return None

    def _check_before_create(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
    ) -> TaskArmingResult | None:
        try:
            current = self._state_store.get_event(instruction.expected_event_id)
        except Exception as exc:
            raise TaskArmingAuditPersistenceError(
                "Task arming audit could not be re-read; create_task was not called"
            ) from exc
        if current is None:
            raise TaskArmingAuditPersistenceError(
                "Task arming audit disappeared; create_task was not called"
            )
        if current.status is not None:
            return _closed_result(instruction, definition, current.status)
        if (
            current.context.official_deadline_utc != instruction.expected_deadline_utc
            or current.context.scheduled_task_id != definition.task_id
            or current.context.scheduled_task_status != ScheduledTaskAuditStatus.ARMING.value
        ):
            raise TaskArmingAuditPersistenceError(
                "Task arming audit changed concurrently; create_task was not called"
            )
        return None

    def _closed_after_conflict(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
    ) -> TaskArmingResult:
        try:
            raced = self._state_store.get_event(instruction.expected_event_id)
        except Exception as exc:
            raise TaskArmingAuditPersistenceError(
                "Task arming audit conflict could not be resolved; create_task was not called"
            ) from exc
        if raced is None or raced.status is None:
            raise TaskArmingAuditPersistenceError(
                "Task arming audit conflicted while unclaimed; create_task was not called"
            )
        return _closed_result(instruction, definition, raced.status)

    def _persist_outcome(
        self,
        instruction: ScheduledDeadlineInstruction,
        definition: CloudTaskDefinition,
        task_status: ScheduledTaskAuditStatus,
        *,
        external_task_known: bool,
    ) -> None:
        try:
            existing = self._state_store.get_event(instruction.expected_event_id)
            if existing is None or existing.status is not None:
                raise PostingStateConflictError("Posting state closed during task arming")
            context = _scheduling_context(instruction, definition, task_status, existing)
            reconciled = self._state_store.reconcile_unclaimed_event(context)
            if reconciled.status is not None:
                raise PostingStateConflictError("Posting state closed during task arming")
        except Exception as exc:
            raise TaskOutcomeAuditPersistenceError(
                definition.task_name,
                task_status.value,
                external_task_known=external_task_known,
            ) from exc


def _scheduling_context(
    instruction: ScheduledDeadlineInstruction,
    definition: CloudTaskDefinition,
    task_status: ScheduledTaskAuditStatus,
    existing: PostingAuditRecord | None,
) -> EventPostingContext:
    return EventPostingContext(
        event_id=instruction.expected_event_id,
        event_code=existing.context.event_code if existing is not None else None,
        official_deadline_utc=instruction.expected_deadline_utc,
        scheduled_task_id=definition.task_id,
        scheduled_task_status=task_status.value,
        preflight_status=existing.context.preflight_status if existing is not None else None,
    )


def _closed_result(
    instruction: ScheduledDeadlineInstruction,
    definition: CloudTaskDefinition,
    posting_status: PostingStatus,
) -> TaskArmingResult:
    return TaskArmingResult(
        instruction=instruction,
        task_name=definition.task_name,
        status=TaskArmingStatus.POSTING_CLOSED,
        existing_posting_status=posting_status,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
