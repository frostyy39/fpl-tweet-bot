import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fpl_bot.cloud_tasks import serialize_instruction
from fpl_bot.deadline_http_app import DEADLINE_TASK_ROUTE, create_app
from fpl_bot.deadline_revalidation import (
    DeadlineExecutionRevalidator,
    ScheduledDeadlineInstruction,
)
from fpl_bot.deadline_task_handler import (
    ACKNOWLEDGED_HTTP_STATUS,
    RETRYABLE_HTTP_STATUS,
    DeadlineTaskResult,
    handle_deadline_task,
)
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.post_execution import (
    DeadlinePostExecutionCoordinator,
    DeadlinePostExecutionResult,
    UnclassifiedXBoundaryError,
    XOutcomePersistenceError,
    XPostSuccessPersistenceError,
)
from fpl_bot.posting_state import (
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingAuditRecord,
    PostingClaim,
    PostingStatus,
)
from fpl_bot.x_api import CreatedXPost
from fpl_bot.x_errors import XAmbiguousWriteError, XRequestRejectedError

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
AFTER_DEADLINE_UTC = DEADLINE_UTC + timedelta(minutes=1)
POST_ID = "987654321"
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"


class FakeRevalidator:
    def __init__(self, outcome: DeadlinePostExecutionResult | Exception) -> None:
        self.outcome = outcome
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        self.instructions.append(instruction)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class StaticFplSource:
    def __init__(
        self,
        *,
        bootstrap: Mapping[str, Any] | Exception | None = None,
        fixtures: Sequence[Mapping[str, Any]] | Exception | None = None,
    ) -> None:
        self.bootstrap = bootstrap if bootstrap is not None else bootstrap_payload()
        self.fixtures = fixtures if fixtures is not None else fixture_payload()

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        if isinstance(self.bootstrap, Exception):
            raise self.bootstrap
        return self.bootstrap

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        if isinstance(self.fixtures, Exception):
            raise self.fixtures
        return self.fixtures


class ScriptedXCreator:
    def __init__(self, outcome: CreatedXPost | Exception | None = None) -> None:
        self.outcome = outcome or CreatedXPost(post_id=POST_ID, text=EXPECTED_TWEET)
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.messages.append(text)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class SuccessWriteFailingStore(InMemoryPostingStateStore):
    def record_success(
        self,
        claim: PostingClaim,
        *,
        x_post_id: str,
    ) -> PostingAuditRecord:
        raise RuntimeError("simulated Firestore failure")


class ClaimFailingStore(InMemoryPostingStateStore):
    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ):
        raise RuntimeError("simulated Firestore claim failure")


class ClaimThenRaiseStore(InMemoryPostingStateStore):
    def __init__(self) -> None:
        super().__init__(claim_id_factory=lambda: "claim-1")
        self.claim_calls = 0

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ):
        self.claim_calls += 1
        decision = super().claim_event(context, claimed_at_utc=claimed_at_utc)
        if self.claim_calls == 1:
            raise RuntimeError("simulated lost Firestore claim response")
        return decision


class AttemptWriteFailingStore(InMemoryPostingStateStore):
    def mark_posting_attempt(
        self,
        claim: PostingClaim,
        *,
        posting_attempted_at_utc: datetime,
    ) -> PostingAuditRecord:
        raise RuntimeError("simulated Firestore attempt failure")


class UnknownAfterProgressRevalidator:
    def __init__(
        self,
        delegate: DeadlineExecutionRevalidator,
        store: InMemoryPostingStateStore,
    ) -> None:
        self.delegate = delegate
        self.store = store
        self.first_delivery = True

    def execute(
        self,
        scheduled_instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        if self.first_delivery:
            self.first_delivery = False
            claim = self.store.claim_event(
                posting_context(),
                claimed_at_utc=DEADLINE_UTC,
            ).claim
            assert claim is not None
            self.store.mark_posting_attempt(
                claim,
                posting_attempted_at_utc=AFTER_DEADLINE_UTC,
            )
            raise RuntimeError("unknown exception after durable progress")
        return self.delegate.execute(scheduled_instruction)


def instruction() -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, DEADLINE_UTC)


def valid_payload() -> bytes:
    return serialize_instruction(instruction())


def posting_context() -> EventPostingContext:
    return EventPostingContext(EVENT_ID, "GW3", DEADLINE_UTC)


def successful_result() -> DeadlinePostExecutionResult:
    return DeadlinePostExecutionResult(posting_context(), EXPECTED_TWEET, x_post_id=POST_ID)


def duplicate_result(status: PostingStatus) -> DeadlinePostExecutionResult:
    return DeadlinePostExecutionResult(
        posting_context(),
        EXPECTED_TWEET,
        existing_status=status,
    )


def bootstrap_payload(*, deadline: datetime = DEADLINE_UTC) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": EVENT_ID,
                "name": "Gameweek 3",
                "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
                "is_current": False,
                "is_next": False,
            }
        ],
        "teams": [
            {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
            for team_id in range(1, 21)
        ],
    }


def fixture_payload() -> list[dict[str, int]]:
    return [
        {"id": index, "event": EVENT_ID, "team_h": team_id, "team_a": team_id + 1}
        for index, team_id in enumerate(range(1, 21, 2), start=1)
    ]


def live_revalidator(
    *,
    source: StaticFplSource | None = None,
    store: InMemoryPostingStateStore | None = None,
    x_creator: ScriptedXCreator | None = None,
    now: datetime = AFTER_DEADLINE_UTC,
) -> tuple[DeadlineExecutionRevalidator, InMemoryPostingStateStore, ScriptedXCreator]:
    actual_store = store or InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    actual_x = x_creator or ScriptedXCreator()
    coordinator = DeadlinePostExecutionCoordinator(
        actual_store,
        actual_x,
        clock=lambda: now,
    )
    revalidator = DeadlineExecutionRevalidator(
        source or StaticFplSource(),
        actual_store,
        coordinator,
        clock=lambda: now,
    )
    return revalidator, actual_store, actual_x


def assert_response(response, status_code: int, result: DeadlineTaskResult) -> None:
    assert response.status_code == status_code
    assert response.json_body() == {"result": result.value}


def test_valid_payload_success_is_acknowledged_without_returning_post_id() -> None:
    revalidator = FakeRevalidator(successful_result())

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.POSTED)
    assert revalidator.instructions == [instruction()]
    assert POST_ID not in json.dumps(response.json_body())


@pytest.mark.parametrize("status", list(PostingStatus))
def test_every_existing_posting_state_is_an_acknowledged_duplicate(status: PostingStatus) -> None:
    response = handle_deadline_task(valid_payload(), FakeRevalidator(duplicate_result(status)))

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)


def test_stale_instruction_is_terminally_acknowledged() -> None:
    source = StaticFplSource(
        bootstrap=bootstrap_payload(deadline=DEADLINE_UTC + timedelta(minutes=30))
    )
    revalidator, store, x_creator = live_revalidator(source=source)

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.STALE)
    assert store.get_event(EVENT_ID) is None
    assert x_creator.messages == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"version":1,"expected_event_id":3}',
        b'{"version":2,"expected_event_id":3,"expected_deadline_utc":"2026-08-22T10:30:00Z"}',
        b'{"version":1,"expected_event_id":true,"expected_deadline_utc":"2026-08-22T10:30:00Z"}',
        b'{"version":1,"expected_event_id":0,"expected_deadline_utc":"2026-08-22T10:30:00Z"}',
        b'{"version":1,"expected_event_id":-1,"expected_deadline_utc":"2026-08-22T10:30:00Z"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":123}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"not-a-date"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-22T10:30:00"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-22T11:30:00+01:00"}',
        b'{"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-22T10:30:00Z","event_code":"GW3"}',
        b'{"version":1,"version":1,"expected_event_id":3,"expected_deadline_utc":"2026-08-22T10:30:00Z"}',
    ],
)
def test_invalid_payload_is_acknowledged_without_calling_revalidator(payload: bytes) -> None:
    revalidator = FakeRevalidator(successful_result())

    response = handle_deadline_task(payload, revalidator)

    assert_response(
        response,
        ACKNOWLEDGED_HTTP_STATUS,
        DeadlineTaskResult.INVALID_TASK_PAYLOAD,
    )
    assert revalidator.instructions == []


def test_early_delivery_is_retryable() -> None:
    revalidator, store, x_creator = live_revalidator(now=DEADLINE_UTC - timedelta(microseconds=1))

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert store.get_event(EVENT_ID) is None
    assert x_creator.messages == []


@pytest.mark.parametrize(
    "error",
    [FplApiError("temporarily unavailable"), DataValidationError("malformed live data")],
)
def test_live_fpl_failures_are_retryable(error: Exception) -> None:
    response = handle_deadline_task(valid_payload(), FakeRevalidator(error))

    assert_response(response, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)


def test_claim_persistence_failure_that_can_progress_is_retryable() -> None:
    store = ClaimFailingStore(claim_id_factory=lambda: "claim-1")
    revalidator, _, x_creator = live_revalidator(store=store)

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert store.get_event(EVENT_ID).status is None
    assert x_creator.messages == []


def test_ambiguous_claim_commit_retries_then_collapses_to_duplicate_without_x() -> None:
    store = ClaimThenRaiseStore()
    revalidator, _, x_creator = live_revalidator(store=store)

    first = handle_deadline_task(valid_payload(), revalidator)
    assert store.get_event(EVENT_ID).status is PostingStatus.CLAIMED
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert x_creator.messages == []
    assert store.claim_calls == 2


def test_post_claim_persistence_failure_is_terminally_acknowledged() -> None:
    store = AttemptWriteFailingStore(claim_id_factory=lambda: "claim-1")
    revalidator, _, x_creator = live_revalidator(store=store)

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.FAILED_CLOSED)
    assert store.get_event(EVENT_ID).status is PostingStatus.CLAIMED
    assert x_creator.messages == []


@pytest.mark.parametrize(
    "error",
    [
        XRequestRejectedError("definitely rejected", 403),
        XAmbiguousWriteError("possibly posted"),
        XPostSuccessPersistenceError(POST_ID),
        XOutcomePersistenceError(PostingStatus.UNCERTAIN, "XAmbiguousWriteError"),
        UnclassifiedXBoundaryError("closed in posting_in_progress"),
    ],
)
def test_known_closed_x_outcomes_are_terminally_acknowledged(error: Exception) -> None:
    response = handle_deadline_task(valid_payload(), FakeRevalidator(error))

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.FAILED_CLOSED)


def test_unknown_exception_is_conservatively_retryable_and_redacted() -> None:
    secret = "Authorization: Bearer highly-sensitive-test-value"

    response = handle_deadline_task(valid_payload(), FakeRevalidator(RuntimeError(secret)))

    assert_response(response, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert secret not in json.dumps(response.json_body())


def test_unknown_exception_after_durable_progress_retries_into_duplicate_without_x() -> None:
    delegate, store, x_creator = live_revalidator()
    revalidator = UnknownAfterProgressRevalidator(delegate, store)

    first = handle_deadline_task(valid_payload(), revalidator)
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert store.get_event(EVENT_ID).status is PostingStatus.IN_PROGRESS
    assert x_creator.messages == []


def test_flask_route_preserves_instruction_and_ignores_delivery_headers() -> None:
    revalidator = FakeRevalidator(successful_result())
    client = create_app(revalidator).test_client()

    response = client.post(
        DEADLINE_TASK_ROUTE,
        data=valid_payload(),
        content_type="application/json",
        headers={
            "Authorization": "Bearer never-return-this-value",
            "X-CloudTasks-TaskName": "untrusted-observability-only",
        },
    )

    assert response.status_code == ACKNOWLEDGED_HTTP_STATUS
    assert response.get_json() == {"result": DeadlineTaskResult.POSTED.value}
    assert revalidator.instructions == [instruction()]
    assert b"never-return-this-value" not in response.data


def test_only_post_deadline_route_and_method_are_exposed() -> None:
    client = create_app(FakeRevalidator(successful_result())).test_client()

    assert client.get(DEADLINE_TASK_ROUTE).status_code == 405
    assert client.post("/other", data=valid_payload()).status_code == 404


def test_lost_success_response_redelivery_is_duplicate_with_zero_second_x_write() -> None:
    revalidator, store, x_creator = live_revalidator()

    first = handle_deadline_task(valid_payload(), revalidator)
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.POSTED)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert x_creator.messages == [EXPECTED_TWEET]
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED


def test_redelivery_of_in_progress_event_is_duplicate_with_zero_x_writes() -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    claim = store.claim_event(
        posting_context(),
        claimed_at_utc=DEADLINE_UTC,
    ).claim
    assert claim is not None
    store.mark_posting_attempt(claim, posting_attempted_at_utc=AFTER_DEADLINE_UTC)
    x_creator = ScriptedXCreator()
    revalidator, _, _ = live_revalidator(store=store, x_creator=x_creator)

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert x_creator.messages == []
    assert store.get_event(EVENT_ID).status is PostingStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("x_error", "expected_status"),
    [
        (XRequestRejectedError("rejected", 403), PostingStatus.FAILED),
        (XAmbiguousWriteError("ambiguous"), PostingStatus.UNCERTAIN),
    ],
)
def test_durable_x_terminal_outcome_is_acknowledged_and_never_retried(
    x_error: Exception,
    expected_status: PostingStatus,
) -> None:
    x_creator = ScriptedXCreator(x_error)
    revalidator, store, _ = live_revalidator(x_creator=x_creator)

    first = handle_deadline_task(valid_payload(), revalidator)
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.FAILED_CLOSED)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert len(x_creator.messages) == 1
    assert store.get_event(EVENT_ID).status is expected_status


def test_success_persistence_failure_is_acknowledged_and_never_posts_twice() -> None:
    store = SuccessWriteFailingStore(claim_id_factory=lambda: "claim-1")
    revalidator, _, x_creator = live_revalidator(store=store)

    first = handle_deadline_task(valid_payload(), revalidator)
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.FAILED_CLOSED)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert len(x_creator.messages) == 1
    assert store.get_event(EVENT_ID).status is PostingStatus.IN_PROGRESS


def test_unclassified_x_failure_is_acknowledged_and_never_posts_twice() -> None:
    x_creator = ScriptedXCreator(RuntimeError("unexpected boundary bug"))
    revalidator, store, _ = live_revalidator(x_creator=x_creator)

    first = handle_deadline_task(valid_payload(), revalidator)
    second = handle_deadline_task(valid_payload(), revalidator)

    assert_response(first, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.FAILED_CLOSED)
    assert_response(second, ACKNOWLEDGED_HTTP_STATUS, DeadlineTaskResult.DUPLICATE)
    assert len(x_creator.messages) == 1
    assert store.get_event(EVENT_ID).status is PostingStatus.IN_PROGRESS


@pytest.mark.parametrize("failure_stage", ["bootstrap", "fixtures"])
def test_temporary_fpl_failure_before_claim_is_retryable_with_zero_x_activity(
    failure_stage: str,
) -> None:
    error = FplApiError(f"temporary {failure_stage} failure")
    source = StaticFplSource(
        bootstrap=error if failure_stage == "bootstrap" else None,
        fixtures=error if failure_stage == "fixtures" else None,
    )
    revalidator, store, x_creator = live_revalidator(source=source)

    response = handle_deadline_task(valid_payload(), revalidator)

    assert_response(response, RETRYABLE_HTTP_STATUS, DeadlineTaskResult.RETRYABLE)
    assert store.get_event(EVENT_ID) is None
    assert x_creator.messages == []
