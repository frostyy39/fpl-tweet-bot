"""Cloud Tasks acknowledgement policy for the structurally read-only preflight."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fpl_bot.cloud_tasks import CloudTaskError, CloudTaskValidationError, parse_instruction_payload
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.preflight import PreflightAuditPersistenceError, PreflightResult, PreflightStatus
from fpl_bot.preflight_arming import PreflightTaskArmingError
from fpl_bot.task_arming import TaskArmingError

PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS = 200
PREFLIGHT_RETRYABLE_HTTP_STATUS = 503


class PreflightHttpResult(StrEnum):
    PREFLIGHT_OK = "preflight_ok"
    PREFLIGHT_STALE_REARMED = "preflight_stale_rearmed"
    PREFLIGHT_STALE_OTHER_DAY = "preflight_stale_other_day"
    PREFLIGHT_STALE = "preflight_stale"
    PREFLIGHT_TOO_LATE = "preflight_too_late"
    PREFLIGHT_FAILED_CLOSED = "preflight_failed_closed"
    INVALID_TASK_PAYLOAD = "invalid_task_payload"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class PreflightHttpResponse:
    status_code: int
    result: PreflightHttpResult

    def json_body(self) -> dict[str, str]:
        return {"result": self.result.value}


class PreflightBoundary(Protocol):
    def execute(self, instruction: ScheduledDeadlineInstruction) -> PreflightResult: ...


_ACKNOWLEDGED_RESULTS = {
    PreflightStatus.OK: PreflightHttpResult.PREFLIGHT_OK,
    PreflightStatus.TOO_LATE: PreflightHttpResult.PREFLIGHT_TOO_LATE,
    PreflightStatus.STALE_REARMED: PreflightHttpResult.PREFLIGHT_STALE_REARMED,
    PreflightStatus.STALE_OTHER_DAY: PreflightHttpResult.PREFLIGHT_STALE_OTHER_DAY,
    PreflightStatus.STALE_EVENT_MISSING: PreflightHttpResult.PREFLIGHT_STALE,
    PreflightStatus.OVERDUE_CHANGE: PreflightHttpResult.PREFLIGHT_TOO_LATE,
    PreflightStatus.POSTING_CLOSED: PreflightHttpResult.PREFLIGHT_FAILED_CLOSED,
}


def handle_preflight_task(
    body: bytes,
    preflight: PreflightBoundary,
) -> PreflightHttpResponse:
    try:
        instruction = parse_instruction_payload(body)
    except CloudTaskValidationError:
        return _acknowledge(PreflightHttpResult.INVALID_TASK_PAYLOAD)

    try:
        outcome = preflight.execute(instruction)
    except (
        FplApiError,
        DataValidationError,
        PreflightAuditPersistenceError,
        PreflightTaskArmingError,
        TaskArmingError,
        CloudTaskError,
    ):
        return _retry()
    except Exception:
        return _retry()

    result = _ACKNOWLEDGED_RESULTS.get(outcome.status)
    if result is None:
        return _retry()
    return _acknowledge(result)


def _acknowledge(result: PreflightHttpResult) -> PreflightHttpResponse:
    return PreflightHttpResponse(PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS, result)


def _retry() -> PreflightHttpResponse:
    return PreflightHttpResponse(PREFLIGHT_RETRYABLE_HTTP_STATUS, PreflightHttpResult.RETRYABLE)
