import json
from datetime import UTC, datetime

import pytest

from fpl_bot.checker_http_handler import CheckerHttpResult
from fpl_bot.cloud_tasks import serialize_instruction
from fpl_bot.deadline_checker import DeadlineCheckerResult, DeadlineCheckerStatus
from fpl_bot.deadline_http_app import (
    CHECKER_RUN_ROUTE,
    DEADLINE_TASK_ROUTE,
    PREFLIGHT_TASK_ROUTE,
    create_app,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import FplApiError
from fpl_bot.post_execution import DeadlinePostExecutionResult
from fpl_bot.posting_state import EventPostingContext
from fpl_bot.preflight import PreflightResult, PreflightStatus
from fpl_bot.preflight_http_handler import (
    PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS,
    PREFLIGHT_RETRYABLE_HTTP_STATUS,
    PreflightHttpResult,
    handle_preflight_task,
)

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"


class FakePreflight:
    def __init__(self, outcome: PreflightResult | Exception) -> None:
        self.outcome = outcome
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def execute(self, instruction: ScheduledDeadlineInstruction) -> PreflightResult:
        self.instructions.append(instruction)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeRevalidator:
    def __init__(self) -> None:
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        self.instructions.append(instruction)
        return DeadlinePostExecutionResult(
            EventPostingContext(EVENT_ID, "GW3", DEADLINE_UTC),
            EXPECTED_TWEET,
            x_post_id="987654321",
        )


class FakeChecker:
    def __init__(self) -> None:
        self.calls = 0

    def run(self) -> DeadlineCheckerResult:
        self.calls += 1
        return DeadlineCheckerResult(
            DeadlineCheckerStatus.NO_ACTION_NOT_TODAY,
            DEADLINE_UTC,
        )


def instruction() -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, DEADLINE_UTC)


def outcome(status: PreflightStatus) -> PreflightResult:
    return PreflightResult(status, instruction())


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (PreflightStatus.OK, PreflightHttpResult.PREFLIGHT_OK),
        (PreflightStatus.TOO_LATE, PreflightHttpResult.PREFLIGHT_TOO_LATE),
        (PreflightStatus.STALE_REARMED, PreflightHttpResult.PREFLIGHT_STALE_REARMED),
        (PreflightStatus.STALE_OTHER_DAY, PreflightHttpResult.PREFLIGHT_STALE_OTHER_DAY),
        (PreflightStatus.STALE_EVENT_MISSING, PreflightHttpResult.PREFLIGHT_STALE),
        (PreflightStatus.OVERDUE_CHANGE, PreflightHttpResult.PREFLIGHT_TOO_LATE),
        (PreflightStatus.POSTING_CLOSED, PreflightHttpResult.PREFLIGHT_FAILED_CLOSED),
    ],
)
def test_terminal_preflight_outcomes_are_acknowledged(
    status: PreflightStatus,
    result: PreflightHttpResult,
) -> None:
    response = handle_preflight_task(
        serialize_instruction(instruction()),
        FakePreflight(outcome(status)),
    )

    assert response.status_code == PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS
    assert response.json_body() == {"result": result.value}


def test_temporary_fpl_failure_is_retryable() -> None:
    response = handle_preflight_task(
        serialize_instruction(instruction()),
        FakePreflight(FplApiError("temporary outage")),
    )

    assert response.status_code == PREFLIGHT_RETRYABLE_HTTP_STATUS
    assert response.json_body() == {"result": PreflightHttpResult.RETRYABLE.value}


def test_unknown_failure_is_retryable_without_returning_secret_text() -> None:
    secret = "Authorization: Bearer preflight-test-secret"
    response = handle_preflight_task(
        serialize_instruction(instruction()),
        FakePreflight(RuntimeError(secret)),
    )

    assert response.status_code == PREFLIGHT_RETRYABLE_HTTP_STATUS
    assert response.json_body() == {"result": PreflightHttpResult.RETRYABLE.value}
    assert secret not in json.dumps(response.json_body())


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"version":1,"expected_event_id":3}',
        b'{"version":1,"expected_event_id":0,"expected_deadline_utc":"2026-08-29T10:30:00Z"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"not-a-date"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-29T11:30:00+01:00"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-29T10:30:00Z","tweet":"bad"}',
    ],
)
def test_invalid_payload_is_terminal_without_preflight_activity(payload: bytes) -> None:
    preflight = FakePreflight(outcome(PreflightStatus.OK))

    response = handle_preflight_task(payload, preflight)

    assert response.status_code == PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS
    assert response.json_body() == {"result": PreflightHttpResult.INVALID_TASK_PAYLOAD.value}
    assert preflight.instructions == []


def test_post_preflight_route_preserves_instruction_and_invokes_once() -> None:
    preflight = FakePreflight(outcome(PreflightStatus.OK))
    client = create_app(FakeRevalidator(), preflight=preflight).test_client()

    response = client.post(
        PREFLIGHT_TASK_ROUTE,
        data=serialize_instruction(instruction()),
        content_type="application/json",
    )

    assert response.status_code == PREFLIGHT_ACKNOWLEDGED_HTTP_STATUS
    assert response.get_json() == {"result": PreflightHttpResult.PREFLIGHT_OK.value}
    assert preflight.instructions == [instruction()]


def test_get_or_unrelated_routes_do_not_invoke_preflight() -> None:
    preflight = FakePreflight(outcome(PreflightStatus.OK))
    client = create_app(FakeRevalidator(), preflight=preflight).test_client()

    assert client.get(PREFLIGHT_TASK_ROUTE).status_code == 405
    assert client.post("/tasks/other").status_code == 404
    assert preflight.instructions == []


def test_all_three_private_post_routes_coexist_without_cross_invocation() -> None:
    revalidator = FakeRevalidator()
    checker = FakeChecker()
    preflight = FakePreflight(outcome(PreflightStatus.OK))
    client = create_app(revalidator, checker=checker, preflight=preflight).test_client()

    preflight_response = client.post(
        PREFLIGHT_TASK_ROUTE,
        data=serialize_instruction(instruction()),
    )
    checker_response = client.post(CHECKER_RUN_ROUTE)
    deadline_response = client.post(
        DEADLINE_TASK_ROUTE,
        data=serialize_instruction(instruction()),
    )

    assert preflight_response.get_json() == {"result": PreflightHttpResult.PREFLIGHT_OK.value}
    assert checker_response.get_json() == {"result": CheckerHttpResult.NO_ACTION_NOT_TODAY.value}
    assert deadline_response.get_json() == {"result": "posted"}
    assert preflight.instructions == [instruction()]
    assert checker.calls == 1
    assert revalidator.instructions == [instruction()]
