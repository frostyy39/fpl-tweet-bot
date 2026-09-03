"""One-run orchestration for planning, exact-deadline arming, and overdue recovery."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from fpl_bot.deadline_planning import (
    DeadlinePlanningDecision,
    DeadlinePlanningStatus,
)
from fpl_bot.deadline_revalidation import (
    EarlyDeadlineExecutionError,
    ScheduledDeadlineInstruction,
    StaleDeadlineInstructionError,
)
from fpl_bot.errors import DataValidationError, FplApiError, FplBotError
from fpl_bot.fpl_diagnostics import FplFailureDiagnostic, diagnose_fpl_failure
from fpl_bot.post_execution import (
    DeadlinePostExecutionResult,
    PostingStatePersistenceError,
    PostingStatePersistenceStage,
    UnclassifiedXBoundaryError,
    XOutcomePersistenceError,
    XPostSuccessPersistenceError,
)
from fpl_bot.posting_state import PostingStateValidationError, PostingStatus, require_utc
from fpl_bot.preflight_arming import PreflightTaskArmingResult, PreflightTaskArmingStatus
from fpl_bot.task_arming import TaskArmingResult, TaskArmingStatus
from fpl_bot.x_errors import XApiError


class DeadlineCheckerError(FplBotError):
    """Base class for deterministic checker contract failures."""


class DeadlineCheckerValidationError(DeadlineCheckerError):
    """Raised when checker time or an injected boundary result is invalid."""


class DeadlineCheckerStatus(StrEnum):
    NO_ACTION_NOT_TODAY = "no_action_not_today"
    TASK_ARMED = "task_armed"
    TASK_ALREADY_ARMED = "task_already_armed"
    TASK_RECONCILED_ARMED = "task_reconciled_armed"
    ALREADY_HANDLED = "already_handled"
    OVERDUE_EXECUTED = "overdue_executed"
    OVERDUE_DUPLICATE = "overdue_duplicate"
    STALE = "stale"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED_CLOSED = "failed_closed"


@dataclass(frozen=True, slots=True)
class DeadlineCheckerResult:
    """Small application-neutral result for one periodic-checker invocation."""

    status: DeadlineCheckerStatus
    checked_at_utc: datetime
    instruction: ScheduledDeadlineInstruction | None = None
    existing_posting_status: PostingStatus | None = None
    failure_type: str | None = None
    preflight_status: PreflightTaskArmingStatus | None = None
    preflight_failure_type: str | None = None
    failure_diagnostic: FplFailureDiagnostic | None = None

    def __post_init__(self) -> None:
        try:
            require_utc(self.checked_at_utc, "Checker time")
        except PostingStateValidationError as exc:
            raise DeadlineCheckerValidationError(str(exc)) from None
        if self.status is DeadlineCheckerStatus.NO_ACTION_NOT_TODAY:
            if self.instruction is not None:
                raise DeadlineCheckerValidationError(
                    "A not-today result cannot contain a scheduled instruction"
                )
        elif (
            self.status is not DeadlineCheckerStatus.RETRYABLE_FAILURE and self.instruction is None
        ):
            raise DeadlineCheckerValidationError(
                "This checker result requires the planned instruction"
            )
        if self.existing_posting_status is not None and self.status not in {
            DeadlineCheckerStatus.ALREADY_HANDLED,
            DeadlineCheckerStatus.OVERDUE_DUPLICATE,
        }:
            raise DeadlineCheckerValidationError(
                "Existing posting state belongs only to an already-handled result"
            )
        failure = self.status in {
            DeadlineCheckerStatus.RETRYABLE_FAILURE,
            DeadlineCheckerStatus.FAILED_CLOSED,
        }
        if failure != (self.failure_type is not None):
            raise DeadlineCheckerValidationError(
                "Only failure results require a non-secret failure type"
            )
        if self.failure_diagnostic is not None and not failure:
            raise DeadlineCheckerValidationError(
                "A checker diagnostic belongs only to a failure result"
            )
        final_task_success = self.status in {
            DeadlineCheckerStatus.TASK_ARMED,
            DeadlineCheckerStatus.TASK_ALREADY_ARMED,
            DeadlineCheckerStatus.TASK_RECONCILED_ARMED,
        }
        if (self.preflight_status is not None or self.preflight_failure_type is not None) and not (
            final_task_success
        ):
            raise DeadlineCheckerValidationError(
                "Preflight arming details require a successful final-task outcome"
            )
        if self.preflight_status is not None and self.preflight_failure_type is not None:
            raise DeadlineCheckerValidationError(
                "Preflight arming cannot be both successful and failed"
            )


class DeadlinePlannerBoundary(Protocol):
    def plan(self) -> DeadlinePlanningDecision: ...


class DeadlineTaskArmerBoundary(Protocol):
    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult: ...


class DeadlineExecutionBoundary(Protocol):
    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult: ...


class PreflightTaskArmerBoundary(Protocol):
    def arm(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> PreflightTaskArmingResult: ...


Clock = Callable[[], datetime]


class DeadlineChecker:
    """Perform one checker pass without internal retries or alternative write paths."""

    def __init__(
        self,
        planner: DeadlinePlannerBoundary,
        task_armer: DeadlineTaskArmerBoundary,
        execution_revalidator: DeadlineExecutionBoundary,
        *,
        preflight_task_armer: PreflightTaskArmerBoundary | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._planner = planner
        self._task_armer = task_armer
        self._execution_revalidator = execution_revalidator
        self._preflight_task_armer = preflight_task_armer
        self._clock = clock or _utc_now

    def run(self) -> DeadlineCheckerResult:
        checked_at_utc = self._validated_now()
        try:
            planning = self._planner.plan()
        except Exception as exc:
            return _failure_result(
                DeadlineCheckerStatus.RETRYABLE_FAILURE,
                checked_at_utc,
                failure=exc,
                diagnostic=diagnose_fpl_failure(exc),
            )

        if planning.status is DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY:
            if planning.instruction is not None:
                raise DeadlineCheckerValidationError(
                    "A not-today planning result must not contain an instruction"
                )
            return DeadlineCheckerResult(
                status=DeadlineCheckerStatus.NO_ACTION_NOT_TODAY,
                checked_at_utc=checked_at_utc,
            )
        if (
            planning.status is not DeadlinePlanningStatus.ELIGIBLE_TO_ARM
            or planning.instruction is None
        ):
            raise DeadlineCheckerValidationError("Planner returned an invalid checker decision")

        instruction = planning.instruction
        arming = self._task_armer.arm(instruction)
        if arming.instruction is not instruction:
            raise DeadlineCheckerValidationError(
                "Task armer did not preserve the planned scheduled instruction"
            )

        if arming.status is TaskArmingStatus.ARMED:
            return self._final_task_result(
                DeadlineCheckerStatus.TASK_ARMED,
                checked_at_utc,
                instruction,
            )
        if arming.status is TaskArmingStatus.ALREADY_ARMED:
            return self._final_task_result(
                DeadlineCheckerStatus.TASK_ALREADY_ARMED,
                checked_at_utc,
                instruction,
            )
        if arming.status is TaskArmingStatus.RECONCILED_ARMED:
            return self._final_task_result(
                DeadlineCheckerStatus.TASK_RECONCILED_ARMED,
                checked_at_utc,
                instruction,
            )
        if arming.status is TaskArmingStatus.POSTING_CLOSED:
            status = (
                DeadlineCheckerStatus.OVERDUE_DUPLICATE
                if checked_at_utc >= instruction.expected_deadline_utc
                else DeadlineCheckerStatus.ALREADY_HANDLED
            )
            return DeadlineCheckerResult(
                status=status,
                checked_at_utc=checked_at_utc,
                instruction=instruction,
                existing_posting_status=arming.existing_posting_status,
            )
        if arming.status is not TaskArmingStatus.OVERDUE_SAME_DAY:
            raise DeadlineCheckerValidationError("Task armer returned an unknown outcome")
        return self._execute_overdue(instruction, checked_at_utc)

    def _final_task_result(
        self,
        status: DeadlineCheckerStatus,
        checked_at_utc: datetime,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlineCheckerResult:
        if self._preflight_task_armer is None:
            return _success_result(status, checked_at_utc, instruction)
        try:
            preflight = self._preflight_task_armer.arm(instruction)
            if preflight.instruction is not instruction:
                raise DeadlineCheckerValidationError(
                    "Preflight armer did not preserve the planned scheduled instruction"
                )
        except Exception as exc:
            return DeadlineCheckerResult(
                status=status,
                checked_at_utc=checked_at_utc,
                instruction=instruction,
                preflight_failure_type=type(exc).__name__,
            )
        return DeadlineCheckerResult(
            status=status,
            checked_at_utc=checked_at_utc,
            instruction=instruction,
            preflight_status=preflight.status,
        )

    def _execute_overdue(
        self,
        instruction: ScheduledDeadlineInstruction,
        checked_at_utc: datetime,
    ) -> DeadlineCheckerResult:
        try:
            execution = self._execution_revalidator.execute(instruction)
        except StaleDeadlineInstructionError:
            return _success_result(DeadlineCheckerStatus.STALE, checked_at_utc, instruction)
        except (FplApiError, DataValidationError, EarlyDeadlineExecutionError) as exc:
            return _failure_result(
                DeadlineCheckerStatus.RETRYABLE_FAILURE,
                checked_at_utc,
                instruction=instruction,
                failure=exc,
            )
        except PostingStatePersistenceError as exc:
            status = (
                DeadlineCheckerStatus.RETRYABLE_FAILURE
                if exc.stage is PostingStatePersistenceStage.CLAIM_OUTCOME_UNCONFIRMED
                else DeadlineCheckerStatus.FAILED_CLOSED
            )
            return _failure_result(
                status,
                checked_at_utc,
                instruction=instruction,
                failure=exc,
            )
        except (
            XApiError,
            XPostSuccessPersistenceError,
            XOutcomePersistenceError,
            UnclassifiedXBoundaryError,
        ) as exc:
            return _failure_result(
                DeadlineCheckerStatus.FAILED_CLOSED,
                checked_at_utc,
                instruction=instruction,
                failure=exc,
            )
        except Exception as exc:
            return _failure_result(
                DeadlineCheckerStatus.RETRYABLE_FAILURE,
                checked_at_utc,
                instruction=instruction,
                failure=exc,
            )

        if execution.posted:
            return _success_result(
                DeadlineCheckerStatus.OVERDUE_EXECUTED,
                checked_at_utc,
                instruction,
            )
        if execution.existing_status is not None:
            return DeadlineCheckerResult(
                status=DeadlineCheckerStatus.OVERDUE_DUPLICATE,
                checked_at_utc=checked_at_utc,
                instruction=instruction,
                existing_posting_status=execution.existing_status,
            )
        raise DeadlineCheckerValidationError("Execution revalidator returned an invalid outcome")

    def _validated_now(self) -> datetime:
        value = self._clock()
        try:
            require_utc(value, "Checker time")
        except PostingStateValidationError as exc:
            raise DeadlineCheckerValidationError(str(exc)) from None
        return value


def _success_result(
    status: DeadlineCheckerStatus,
    checked_at_utc: datetime,
    instruction: ScheduledDeadlineInstruction,
) -> DeadlineCheckerResult:
    return DeadlineCheckerResult(
        status=status,
        checked_at_utc=checked_at_utc,
        instruction=instruction,
    )


def _failure_result(
    status: DeadlineCheckerStatus,
    checked_at_utc: datetime,
    *,
    failure: Exception,
    instruction: ScheduledDeadlineInstruction | None = None,
    diagnostic: FplFailureDiagnostic | None = None,
) -> DeadlineCheckerResult:
    return DeadlineCheckerResult(
        status=status,
        checked_at_utc=checked_at_utc,
        instruction=instruction,
        failure_type=type(failure).__name__,
        failure_diagnostic=diagnostic,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
