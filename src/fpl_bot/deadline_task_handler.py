"""Cloud Tasks delivery acknowledgement boundary for deadline execution."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fpl_bot.cloud_tasks import CloudTaskValidationError, parse_instruction_payload
from fpl_bot.deadline_revalidation import (
    EarlyDeadlineExecutionError,
    ScheduledDeadlineInstruction,
    StaleDeadlineInstructionError,
)
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.post_execution import (
    DeadlinePostExecutionResult,
    PostingStatePersistenceError,
    PostingStatePersistenceStage,
    UnclassifiedXBoundaryError,
    XOutcomePersistenceError,
    XPostSuccessPersistenceError,
)
from fpl_bot.x_errors import XApiError

ACKNOWLEDGED_HTTP_STATUS = 200
RETRYABLE_HTTP_STATUS = 503


class DeadlineTaskResult(StrEnum):
    """Small non-sensitive result vocabulary returned to Cloud Tasks."""

    POSTED = "posted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    INVALID_TASK_PAYLOAD = "invalid_task_payload"
    FAILED_CLOSED = "failed_closed"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class DeadlineTaskHttpResponse:
    """Framework-neutral HTTP status and deterministic JSON response."""

    status_code: int
    result: DeadlineTaskResult

    def json_body(self) -> dict[str, str]:
        return {"result": self.result.value}


class DeadlineRevalidatorBoundary(Protocol):
    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult: ...


def handle_deadline_task(
    body: bytes,
    revalidator: DeadlineRevalidatorBoundary,
) -> DeadlineTaskHttpResponse:
    """Execute one authenticated delivery and choose acknowledgement versus retry."""
    try:
        instruction = parse_instruction_payload(body)
    except CloudTaskValidationError:
        return _acknowledge(DeadlineTaskResult.INVALID_TASK_PAYLOAD)

    try:
        result = revalidator.execute(instruction)
    except StaleDeadlineInstructionError:
        return _acknowledge(DeadlineTaskResult.STALE)
    except EarlyDeadlineExecutionError:
        return _retry()
    except (FplApiError, DataValidationError):
        return _retry()
    except PostingStatePersistenceError as exc:
        if exc.stage is PostingStatePersistenceStage.CLAIM_OUTCOME_UNCONFIRMED:
            return _retry()
        return _acknowledge(DeadlineTaskResult.FAILED_CLOSED)
    except (
        XApiError,
        XPostSuccessPersistenceError,
        XOutcomePersistenceError,
        UnclassifiedXBoundaryError,
    ):
        return _acknowledge(DeadlineTaskResult.FAILED_CLOSED)
    except Exception:
        return _retry()

    if result.posted:
        return _acknowledge(DeadlineTaskResult.POSTED)
    if result.existing_status is not None:
        return _acknowledge(DeadlineTaskResult.DUPLICATE)
    return _retry()


def _acknowledge(result: DeadlineTaskResult) -> DeadlineTaskHttpResponse:
    return DeadlineTaskHttpResponse(ACKNOWLEDGED_HTTP_STATUS, result)


def _retry() -> DeadlineTaskHttpResponse:
    return DeadlineTaskHttpResponse(RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
