import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fpl_bot.checker_http_handler import (
    CHECKER_ACKNOWLEDGED_HTTP_STATUS,
    CHECKER_RETRYABLE_HTTP_STATUS,
    CheckerHttpResult,
    handle_checker_run,
)
from fpl_bot.cloud_tasks import serialize_instruction
from fpl_bot.deadline_checker import (
    DeadlineChecker,
    DeadlineCheckerResult,
    DeadlineCheckerStatus,
)
from fpl_bot.deadline_http_app import CHECKER_RUN_ROUTE, DEADLINE_TASK_ROUTE, create_app
from fpl_bot.deadline_planning import DeadlinePlanningDecision, DeadlinePlanningStatus
from fpl_bot.deadline_revalidation import (
    DeadlineExecutionRevalidator,
    ScheduledDeadlineInstruction,
)
from fpl_bot.post_execution import (
    DeadlinePostExecutionCoordinator,
    DeadlinePostExecutionResult,
)
from fpl_bot.posting_state import EventPostingContext, InMemoryPostingStateStore, PostingStatus
from fpl_bot.task_arming import TaskArmingResult, TaskArmingStatus
from fpl_bot.x_api import CreatedXPost

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
AFTER_DEADLINE_UTC = DEADLINE_UTC + timedelta(minutes=1)
POST_ID = "987654321"
TASK_NAME = "projects/test/locations/europe-west2/queues/fpl/tasks/deadline"
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"


class FakeChecker:
    def __init__(self, outcome: DeadlineCheckerResult | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    def run(self) -> DeadlineCheckerResult:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeRevalidator:
    def __init__(self, result: DeadlinePostExecutionResult) -> None:
        self.result = result
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        self.instructions.append(instruction)
        return self.result


class FixedPlanner:
    def __init__(self, planned_instruction: ScheduledDeadlineInstruction) -> None:
        self.instruction = planned_instruction

    def plan(self) -> DeadlinePlanningDecision:
        return DeadlinePlanningDecision(
            DeadlinePlanningStatus.ELIGIBLE_TO_ARM,
            self.instruction,
        )


class OverdueArmer:
    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult:
        return TaskArmingResult(
            instruction=instruction,
            task_name=TASK_NAME,
            status=TaskArmingStatus.OVERDUE_SAME_DAY,
        )


class StaticFplSource:
    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        return {
            "events": [
                {
                    "id": EVENT_ID,
                    "name": "Gameweek 3",
                    "deadline_time": DEADLINE_UTC.isoformat().replace("+00:00", "Z"),
                    "is_current": False,
                    "is_next": False,
                }
            ],
            "teams": [
                {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
                for team_id in range(1, 21)
            ],
        }

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        assert event_id == EVENT_ID
        return [
            {"id": index, "event": EVENT_ID, "team_h": team_id, "team_a": team_id + 1}
            for index, team_id in enumerate(range(1, 21, 2), start=1)
        ]


class RecordingXCreator:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.messages.append(text)
        return CreatedXPost(post_id=POST_ID, text=text)


def instruction() -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, DEADLINE_UTC)


def context() -> EventPostingContext:
    return EventPostingContext(EVENT_ID, "GW3", DEADLINE_UTC)


def posted_result() -> DeadlinePostExecutionResult:
    return DeadlinePostExecutionResult(context(), EXPECTED_TWEET, x_post_id=POST_ID)


def checker_result(status: DeadlineCheckerStatus) -> DeadlineCheckerResult:
    if status is DeadlineCheckerStatus.NO_ACTION_NOT_TODAY:
        return DeadlineCheckerResult(status, AFTER_DEADLINE_UTC)
    if status in {DeadlineCheckerStatus.RETRYABLE_FAILURE, DeadlineCheckerStatus.FAILED_CLOSED}:
        return DeadlineCheckerResult(
            status,
            AFTER_DEADLINE_UTC,
            instruction(),
            failure_type="RedactedFailureType",
        )
    existing = (
        PostingStatus.SUCCEEDED
        if status
        in {
            DeadlineCheckerStatus.ALREADY_HANDLED,
            DeadlineCheckerStatus.OVERDUE_DUPLICATE,
        }
        else None
    )
    return DeadlineCheckerResult(
        status,
        AFTER_DEADLINE_UTC,
        instruction(),
        existing_posting_status=existing,
    )


@pytest.mark.parametrize(
    ("checker_status", "http_result"),
    [
        (DeadlineCheckerStatus.NO_ACTION_NOT_TODAY, CheckerHttpResult.NO_ACTION_NOT_TODAY),
        (DeadlineCheckerStatus.TASK_ARMED, CheckerHttpResult.TASK_ARMED),
        (DeadlineCheckerStatus.TASK_ALREADY_ARMED, CheckerHttpResult.TASK_ALREADY_ARMED),
        (
            DeadlineCheckerStatus.TASK_RECONCILED_ARMED,
            CheckerHttpResult.TASK_RECONCILED_ARMED,
        ),
        (DeadlineCheckerStatus.ALREADY_HANDLED, CheckerHttpResult.DUPLICATE),
        (DeadlineCheckerStatus.OVERDUE_EXECUTED, CheckerHttpResult.OVERDUE_EXECUTED),
        (DeadlineCheckerStatus.OVERDUE_DUPLICATE, CheckerHttpResult.DUPLICATE),
        (DeadlineCheckerStatus.STALE, CheckerHttpResult.STALE),
        (DeadlineCheckerStatus.FAILED_CLOSED, CheckerHttpResult.FAILED_CLOSED),
    ],
)
def test_terminal_checker_outcome_is_acknowledged(
    checker_status: DeadlineCheckerStatus,
    http_result: CheckerHttpResult,
) -> None:
    response = handle_checker_run(FakeChecker(checker_result(checker_status)))

    assert response.status_code == CHECKER_ACKNOWLEDGED_HTTP_STATUS
    assert response.json_body() == {"result": http_result.value}


def test_retryable_checker_outcome_returns_503() -> None:
    response = handle_checker_run(
        FakeChecker(checker_result(DeadlineCheckerStatus.RETRYABLE_FAILURE))
    )

    assert response.status_code == CHECKER_RETRYABLE_HTTP_STATUS
    assert response.json_body() == {"result": CheckerHttpResult.RETRYABLE.value}


def test_preflight_arming_failure_is_distinct_and_retryable_without_error_text() -> None:
    result = checker_result(DeadlineCheckerStatus.TASK_ARMED)
    result = DeadlineCheckerResult(
        result.status,
        result.checked_at_utc,
        result.instruction,
        preflight_failure_type="SensitiveProviderException",
    )

    response = handle_checker_run(FakeChecker(result))

    assert response.status_code == CHECKER_RETRYABLE_HTTP_STATUS
    assert response.json_body() == {"result": CheckerHttpResult.PREFLIGHT_FAILED.value}
    assert b"SensitiveProviderException" not in json.dumps(response.json_body()).encode()


def test_unknown_exception_is_retryable_and_response_is_redacted() -> None:
    secret = "Authorization: Bearer should-never-be-returned"

    response = handle_checker_run(FakeChecker(RuntimeError(secret)))

    assert response.status_code == CHECKER_RETRYABLE_HTTP_STATUS
    assert response.json_body() == {"result": CheckerHttpResult.RETRYABLE.value}
    assert secret not in json.dumps(response.json_body())


def test_post_checker_route_invokes_checker_exactly_once() -> None:
    checker = FakeChecker(checker_result(DeadlineCheckerStatus.NO_ACTION_NOT_TODAY))
    client = create_app(FakeRevalidator(posted_result()), checker=checker).test_client()

    response = client.post(CHECKER_RUN_ROUTE)

    assert response.status_code == CHECKER_ACKNOWLEDGED_HTTP_STATUS
    assert checker.calls == 1


def test_get_checker_route_does_not_invoke_checker() -> None:
    checker = FakeChecker(checker_result(DeadlineCheckerStatus.NO_ACTION_NOT_TODAY))
    client = create_app(FakeRevalidator(posted_result()), checker=checker).test_client()

    response = client.get(CHECKER_RUN_ROUTE)

    assert response.status_code == 405
    assert checker.calls == 0


def test_arbitrary_route_does_not_invoke_checker() -> None:
    checker = FakeChecker(checker_result(DeadlineCheckerStatus.NO_ACTION_NOT_TODAY))
    client = create_app(FakeRevalidator(posted_result()), checker=checker).test_client()

    response = client.post("/checker/other")

    assert response.status_code == 404
    assert checker.calls == 0


def test_request_data_cannot_override_checker_event_or_deadline() -> None:
    checker = FakeChecker(checker_result(DeadlineCheckerStatus.TASK_ARMED))
    client = create_app(FakeRevalidator(posted_result()), checker=checker).test_client()

    response = client.post(
        f"{CHECKER_RUN_ROUTE}?expected_event_id=999&expected_deadline_utc=2099-01-01T00:00:00Z",
        json={
            "expected_event_id": 999,
            "expected_deadline_utc": "2099-01-01T00:00:00Z",
            "event_code": "GW999",
            "tweet": "untrusted",
        },
    )

    assert response.status_code == CHECKER_ACKNOWLEDGED_HTTP_STATUS
    assert response.get_json() == {"result": CheckerHttpResult.TASK_ARMED.value}
    assert checker.calls == 1


def test_deadline_task_route_remains_available_in_shared_app() -> None:
    revalidator = FakeRevalidator(posted_result())
    checker = FakeChecker(checker_result(DeadlineCheckerStatus.NO_ACTION_NOT_TODAY))
    client = create_app(revalidator, checker=checker).test_client()

    response = client.post(
        DEADLINE_TASK_ROUTE,
        data=serialize_instruction(instruction()),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json() == {"result": "posted"}
    assert revalidator.instructions == [instruction()]
    assert checker.calls == 0


def test_repeated_checker_http_calls_use_durable_claim_and_create_one_x_post() -> None:
    planned = instruction()
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    x_creator = RecordingXCreator()
    coordinator = DeadlinePostExecutionCoordinator(
        store,
        x_creator,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    revalidator = DeadlineExecutionRevalidator(
        StaticFplSource(),
        store,
        coordinator,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    checker = DeadlineChecker(
        FixedPlanner(planned),
        OverdueArmer(),
        revalidator,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    client = create_app(revalidator, checker=checker).test_client()

    first = client.post(CHECKER_RUN_ROUTE)
    second = client.post(CHECKER_RUN_ROUTE)

    assert first.status_code == CHECKER_ACKNOWLEDGED_HTTP_STATUS
    assert first.get_json() == {"result": CheckerHttpResult.OVERDUE_EXECUTED.value}
    assert second.status_code == CHECKER_ACKNOWLEDGED_HTTP_STATUS
    assert second.get_json() == {"result": CheckerHttpResult.DUPLICATE.value}
    assert x_creator.messages == [EXPECTED_TWEET]
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED
