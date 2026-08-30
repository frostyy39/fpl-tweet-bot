"""HTTP acknowledgement policy for one private deadline-checker invocation."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fpl_bot.deadline_checker import DeadlineCheckerResult, DeadlineCheckerStatus

CHECKER_ACKNOWLEDGED_HTTP_STATUS = 200
CHECKER_RETRYABLE_HTTP_STATUS = 503


class CheckerHttpResult(StrEnum):
    """Small non-sensitive result vocabulary returned to the future Scheduler caller."""

    NO_ACTION_NOT_TODAY = "no_action_not_today"
    TASK_ARMED = "task_armed"
    TASK_ALREADY_ARMED = "task_already_armed"
    TASK_RECONCILED_ARMED = "task_reconciled_armed"
    OVERDUE_EXECUTED = "overdue_executed"
    DUPLICATE = "duplicate"
    STALE = "stale"
    FAILED_CLOSED = "failed_closed"
    PREFLIGHT_FAILED = "preflight_failed"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class CheckerHttpResponse:
    """Framework-neutral status and deterministic response body."""

    status_code: int
    result: CheckerHttpResult

    def json_body(self) -> dict[str, str]:
        return {"result": self.result.value}


class DeadlineCheckerBoundary(Protocol):
    def run(self) -> DeadlineCheckerResult: ...


_ACKNOWLEDGED_RESULTS = {
    DeadlineCheckerStatus.NO_ACTION_NOT_TODAY: CheckerHttpResult.NO_ACTION_NOT_TODAY,
    DeadlineCheckerStatus.TASK_ARMED: CheckerHttpResult.TASK_ARMED,
    DeadlineCheckerStatus.TASK_ALREADY_ARMED: CheckerHttpResult.TASK_ALREADY_ARMED,
    DeadlineCheckerStatus.TASK_RECONCILED_ARMED: CheckerHttpResult.TASK_RECONCILED_ARMED,
    DeadlineCheckerStatus.ALREADY_HANDLED: CheckerHttpResult.DUPLICATE,
    DeadlineCheckerStatus.OVERDUE_EXECUTED: CheckerHttpResult.OVERDUE_EXECUTED,
    DeadlineCheckerStatus.OVERDUE_DUPLICATE: CheckerHttpResult.DUPLICATE,
    DeadlineCheckerStatus.STALE: CheckerHttpResult.STALE,
    DeadlineCheckerStatus.FAILED_CLOSED: CheckerHttpResult.FAILED_CLOSED,
}


def handle_checker_run(checker: DeadlineCheckerBoundary) -> CheckerHttpResponse:
    """Run the checker once and select acknowledgement versus later delivery retry."""
    try:
        outcome = checker.run()
    except Exception:
        return _retry()

    if outcome.preflight_failure_type is not None:
        return CheckerHttpResponse(
            CHECKER_RETRYABLE_HTTP_STATUS,
            CheckerHttpResult.PREFLIGHT_FAILED,
        )
    status = outcome.status
    if status is DeadlineCheckerStatus.RETRYABLE_FAILURE:
        return _retry()
    result = _ACKNOWLEDGED_RESULTS.get(status)
    if result is None:
        return _retry()
    return CheckerHttpResponse(CHECKER_ACKNOWLEDGED_HTTP_STATUS, result)


def _retry() -> CheckerHttpResponse:
    return CheckerHttpResponse(CHECKER_RETRYABLE_HTTP_STATUS, CheckerHttpResult.RETRYABLE)
