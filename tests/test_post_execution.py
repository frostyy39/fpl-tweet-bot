import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from fpl_bot.classification import classify_fixtures, render_event_code
from fpl_bot.models import EventKind, EventReport, Fixture, FplEvent, Team
from fpl_bot.post_execution import (
    DeadlinePostExecutionCoordinator,
    DeadlinePostValidationError,
    PostingStatePersistenceError,
    UnclassifiedXBoundaryError,
    XOutcomePersistenceError,
    XPostSuccessPersistenceError,
)
from fpl_bot.posting_state import (
    ClaimDecision,
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingAuditRecord,
    PostingClaim,
    PostingStatus,
)
from fpl_bot.tweet import render_v1_tweet
from fpl_bot.x_api import (
    CreatedXPost,
    XApiClient,
    XHttpRequest,
    XHttpResponse,
)
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import (
    XAmbiguousWriteError,
    XApiResponseError,
    XConfigurationError,
    XIdentityMismatchError,
    XPermissionError,
    XTokenRefreshError,
)

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
CLAIMED_AT_UTC = datetime(2026, 8, 22, 10, 29, 58, tzinfo=UTC)
ATTEMPTED_AT_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
EXPECTED_USER_ID = "123456789"
X_POST_ID = "987654321"
ACCESS_TOKEN_PLACEHOLDER = "unit-test-token-placeholder"


class RecordingStateStore(InMemoryPostingStateStore):
    def __init__(self, events: list[str], *, fail_on: str | None = None) -> None:
        super().__init__(claim_id_factory=lambda: "claim-1")
        self.events = events
        self.fail_on = fail_on
        self.claim_contexts: list[EventPostingContext] = []
        self.transition_claims: list[tuple[str, PostingClaim]] = []

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ) -> ClaimDecision:
        self.events.append("claim")
        self.claim_contexts.append(context)
        if self.fail_on == "claim":
            raise RuntimeError("simulated state failure")
        return super().claim_event(context, claimed_at_utc=claimed_at_utc)

    def mark_posting_attempt(
        self,
        claim: PostingClaim,
        *,
        posting_attempted_at_utc: datetime,
    ) -> PostingAuditRecord:
        self.events.append("mark_in_progress")
        self.transition_claims.append(("mark_in_progress", claim))
        if self.fail_on == "mark_in_progress":
            raise RuntimeError("simulated state failure")
        return super().mark_posting_attempt(
            claim,
            posting_attempted_at_utc=posting_attempted_at_utc,
        )

    def record_success(
        self,
        claim: PostingClaim,
        *,
        x_post_id: str,
    ) -> PostingAuditRecord:
        self.events.append("record_success")
        self.transition_claims.append(("record_success", claim))
        if self.fail_on == "record_success":
            raise RuntimeError("simulated state failure")
        return super().record_success(claim, x_post_id=x_post_id)

    def record_failure(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord:
        self.events.append("record_failure")
        self.transition_claims.append(("record_failure", claim))
        if self.fail_on == "record_failure":
            raise RuntimeError("simulated state failure")
        return super().record_failure(claim, error_detail=error_detail)

    def record_uncertain(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord:
        self.events.append("record_uncertain")
        self.transition_claims.append(("record_uncertain", claim))
        if self.fail_on == "record_uncertain":
            raise RuntimeError("simulated state failure")
        return super().record_uncertain(claim, error_detail=error_detail)


class RecordingXCreator:
    def __init__(
        self,
        events: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.events.append("x_create")
        self.messages.append(text)
        if self.error is not None:
            raise self.error
        return CreatedXPost(post_id=X_POST_ID, text=text)


class FailingTokenProvider:
    def get_valid_access_token(self) -> str:
        raise XTokenRefreshError("OAuth refresh failed without an X write")


class RecordingTransport:
    def __init__(
        self,
        events: list[str],
        outcomes: list[XHttpResponse | Exception],
    ) -> None:
        self.events = events
        self.outcomes = outcomes
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        assert timeout_seconds == 10.0
        self.requests.append(request)
        self.events.append("x_identity" if request.method == "GET" else "x_post")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def classification_for(kind: EventKind):
    teams = tuple(
        Team(team_id=team_id, name=f"Team {team_id}", short_name=f"T{team_id}")
        for team_id in range(1, 5)
    )
    fixture_pairs = {
        EventKind.GW: ((1, 2), (3, 4)),
        EventKind.BGW: ((1, 2),),
        EventKind.DGW: ((1, 2), (3, 4), (1, 3)),
        EventKind.BDGW: ((1, 2), (1, 3)),
    }[kind]
    fixtures = tuple(
        Fixture(
            fixture_id=index,
            event_id=EVENT_ID,
            home_team_id=home,
            away_team_id=away,
        )
        for index, (home, away) in enumerate(fixture_pairs, start=1)
    )
    classification = classify_fixtures(teams, fixtures)
    assert classification.kind is kind
    return classification


def event_report(kind: EventKind = EventKind.GW) -> EventReport:
    classification = classification_for(kind)
    event_code = render_event_code(EVENT_ID, classification.kind)
    return EventReport(
        event=FplEvent(
            event_id=EVENT_ID,
            name="Gameweek 3",
            deadline_utc=DEADLINE_UTC,
            is_current=False,
            is_next=True,
        ),
        deadline_london=DEADLINE_UTC,
        classification=classification,
        event_code=event_code,
        tweet=render_v1_tweet(event_code),
    )


def sequence_clock(*values: datetime) -> Callable[[], datetime]:
    iterator = iter(values)
    return lambda: next(iterator)


def coordinator(
    store: RecordingStateStore,
    x_client: RecordingXCreator | XApiClient,
) -> DeadlinePostExecutionCoordinator:
    return DeadlinePostExecutionCoordinator(
        store,
        x_client,
        clock=sequence_clock(CLAIMED_AT_UTC, ATTEMPTED_AT_UTC),
    )


def json_response(status_code: int, payload: object) -> XHttpResponse:
    return XHttpResponse(status_code=status_code, body=json.dumps(payload).encode())


def guarded_x_client(transport: RecordingTransport) -> XApiClient:
    return XApiClient(
        XPostingConfig(
            environment="test",
            posting_enabled=True,
            expected_user_id=EXPECTED_USER_ID,
            user_access_token=ACCESS_TOKEN_PLACEHOLDER,
        ),
        transport=transport,
    )


def assert_event_is_closed(
    store: RecordingStateStore,
    expected_status: PostingStatus,
) -> None:
    report = event_report()
    duplicate = store.claim_event(
        EventPostingContext(EVENT_ID, report.event_code, DEADLINE_UTC),
        claimed_at_utc=CLAIMED_AT_UTC,
    )
    assert duplicate.granted is False
    assert duplicate.existing_status is expected_status


def test_successful_execution_has_explicit_safe_order_and_one_x_post() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    report = event_report()
    transport = RecordingTransport(
        events,
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(201, {"data": {"id": X_POST_ID, "text": report.tweet}}),
        ],
    )

    result = coordinator(store, guarded_x_client(transport)).execute(report)

    assert events == [
        "claim",
        "mark_in_progress",
        "x_identity",
        "x_post",
        "record_success",
    ]
    assert [request.method for request in transport.requests] == ["GET", "POST"]
    assert result.posted is True
    assert result.x_post_id == X_POST_ID
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.SUCCEEDED
    assert persisted.x_post_id == X_POST_ID


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        (EventKind.GW, "GW3"),
        (EventKind.BGW, "BGW3"),
        (EventKind.DGW, "DGW3"),
        (EventKind.BDGW, "BDGW3"),
    ],
)
def test_existing_classification_and_renderer_flow_to_exact_tweet(
    kind: EventKind,
    expected_code: str,
) -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    x_client = RecordingXCreator(events)

    result = coordinator(store, x_client).execute(event_report(kind))

    expected_tweet = f"Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #{expected_code}"
    assert result.context.event_code == expected_code
    assert result.tweet == expected_tweet
    assert x_client.messages == [expected_tweet]


def test_inconsistent_report_is_rejected_before_claim() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    x_client = RecordingXCreator(events)
    report = replace(event_report(), tweet="not the deterministic V1 tweet")

    with pytest.raises(DeadlinePostValidationError, match="inconsistent"):
        coordinator(store, x_client).execute(report)

    assert events == []
    assert x_client.messages == []


def test_duplicate_claim_causes_zero_x_requests() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    report = event_report()
    context = EventPostingContext(EVENT_ID, report.event_code, DEADLINE_UTC)
    first = store.claim_event(context, claimed_at_utc=CLAIMED_AT_UTC)
    assert first.granted is True
    events.clear()
    x_client = RecordingXCreator(events)

    result = coordinator(store, x_client).execute(report)

    assert result.posted is False
    assert result.existing_status is PostingStatus.CLAIMED
    assert events == ["claim"]
    assert x_client.messages == []


def test_failure_to_persist_in_progress_causes_zero_x_requests() -> None:
    events: list[str] = []
    store = RecordingStateStore(events, fail_on="mark_in_progress")
    x_client = RecordingXCreator(events)

    with pytest.raises(PostingStatePersistenceError, match="no X request"):
        coordinator(store, x_client).execute(event_report())

    assert events == ["claim", "mark_in_progress"]
    assert x_client.messages == []


def test_identity_mismatch_records_failure_without_create_post_request() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    transport = RecordingTransport(
        events,
        [json_response(200, {"data": {"id": "222222222", "username": "wrong_user"}})],
    )

    with pytest.raises(XIdentityMismatchError):
        coordinator(store, guarded_x_client(transport)).execute(event_report())

    assert events == [
        "claim",
        "mark_in_progress",
        "x_identity",
        "record_failure",
    ]
    assert [request.method for request in transport.requests] == ["GET"]
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.FAILED
    assert_event_is_closed(store, PostingStatus.FAILED)


def test_configuration_failure_records_failed_with_zero_x_requests() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    transport = RecordingTransport(events, [])
    x_client = XApiClient(
        XPostingConfig(
            environment="test",
            posting_enabled=False,
            expected_user_id=EXPECTED_USER_ID,
            user_access_token=ACCESS_TOKEN_PLACEHOLDER,
        ),
        transport=transport,
    )

    with pytest.raises(XConfigurationError):
        coordinator(store, x_client).execute(event_report())

    assert events == ["claim", "mark_in_progress", "record_failure"]
    assert transport.requests == []
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.FAILED
    assert_event_is_closed(store, PostingStatus.FAILED)


def test_token_refresh_failure_after_in_progress_is_definite_failed_with_zero_x_requests() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    transport = RecordingTransport(events, [])
    x_client = XApiClient(
        XPostingConfig(
            environment="test",
            posting_enabled=True,
            expected_user_id=EXPECTED_USER_ID,
        ),
        transport=transport,
        token_provider=FailingTokenProvider(),
    )

    with pytest.raises(XTokenRefreshError):
        coordinator(store, x_client).execute(event_report())

    assert events == ["claim", "mark_in_progress", "record_failure"]
    assert transport.requests == []
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.FAILED
    assert_event_is_closed(store, PostingStatus.FAILED)


def test_authenticated_user_failure_records_failed_with_zero_post_attempts() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    transport = RecordingTransport(
        events,
        [json_response(503, {"title": "temporarily unavailable"})],
    )

    with pytest.raises(XApiResponseError):
        coordinator(store, guarded_x_client(transport)).execute(event_report())

    assert events == [
        "claim",
        "mark_in_progress",
        "x_identity",
        "record_failure",
    ]
    assert [request.method for request in transport.requests] == ["GET"]
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.FAILED
    assert_event_is_closed(store, PostingStatus.FAILED)


def test_definite_x_rejection_records_failed_once_without_retry() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    report = event_report()
    transport = RecordingTransport(
        events,
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(403, {"title": "write permission required"}),
        ],
    )

    with pytest.raises(XPermissionError):
        coordinator(store, guarded_x_client(transport)).execute(report)

    assert events == [
        "claim",
        "mark_in_progress",
        "x_identity",
        "x_post",
        "record_failure",
    ]
    assert [request.method for request in transport.requests] == ["GET", "POST"]
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.FAILED
    assert persisted.error_detail == (
        "X operation was definitely rejected with HTTP 403 (XPermissionError); no automatic retry"
    )
    assert_event_is_closed(store, PostingStatus.FAILED)


def test_ambiguous_x_outcome_records_uncertain_once_without_retry() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    report = event_report()
    transport = RecordingTransport(
        events,
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(503, {"title": "temporarily unavailable"}),
        ],
    )

    with pytest.raises(XAmbiguousWriteError):
        coordinator(store, guarded_x_client(transport)).execute(report)

    assert events == [
        "claim",
        "mark_in_progress",
        "x_identity",
        "x_post",
        "record_uncertain",
    ]
    assert [request.method for request in transport.requests] == ["GET", "POST"]
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.UNCERTAIN
    assert_event_is_closed(store, PostingStatus.UNCERTAIN)


def test_unclassified_boundary_failure_is_not_falsely_recorded_as_uncertain() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    x_client = RecordingXCreator(events, error=RuntimeError("unexpected boundary bug"))

    with pytest.raises(UnclassifiedXBoundaryError, match="cannot be safely classified"):
        coordinator(store, x_client).execute(event_report())

    assert events == ["claim", "mark_in_progress", "x_create"]
    assert len(x_client.messages) == 1
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.IN_PROGRESS
    assert_event_is_closed(store, PostingStatus.IN_PROGRESS)


def test_success_persistence_failure_retains_post_id_without_second_x_write() -> None:
    events: list[str] = []
    store = RecordingStateStore(events, fail_on="record_success")
    x_client = RecordingXCreator(events)

    with pytest.raises(XPostSuccessPersistenceError) as error:
        coordinator(store, x_client).execute(event_report())

    assert error.value.x_post_id == X_POST_ID
    assert "do not post again" in str(error.value)
    assert events == ["claim", "mark_in_progress", "x_create", "record_success"]
    assert len(x_client.messages) == 1
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("x_error", "fail_on", "expected_status", "record_event"),
    [
        (
            XPermissionError("safe rejection", 403),
            "record_failure",
            PostingStatus.FAILED,
            "record_failure",
        ),
        (
            XAmbiguousWriteError("safe ambiguous outcome"),
            "record_uncertain",
            PostingStatus.UNCERTAIN,
            "record_uncertain",
        ),
    ],
)
def test_x_outcome_persistence_failure_never_retries_post(
    x_error: Exception,
    fail_on: str,
    expected_status: PostingStatus,
    record_event: str,
) -> None:
    events: list[str] = []
    store = RecordingStateStore(events, fail_on=fail_on)
    x_client = RecordingXCreator(events, error=x_error)

    with pytest.raises(XOutcomePersistenceError) as error:
        coordinator(store, x_client).execute(event_report())

    assert error.value.terminal_status is expected_status
    assert events == ["claim", "mark_in_progress", "x_create", record_event]
    assert len(x_client.messages) == 1
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.IN_PROGRESS


def test_claim_identity_event_identity_and_utc_timestamps_are_preserved() -> None:
    events: list[str] = []
    store = RecordingStateStore(events)
    report = event_report(EventKind.BDGW)

    coordinator(store, RecordingXCreator(events)).execute(report)

    assert store.claim_contexts == [
        EventPostingContext(
            event_id=report.event.event_id,
            event_code=report.event_code,
            official_deadline_utc=report.event.deadline_utc,
        )
    ]
    assert [stage for stage, _ in store.transition_claims] == [
        "mark_in_progress",
        "record_success",
    ]
    assert {claim.claim_id for _, claim in store.transition_claims} == {"claim-1"}
    persisted = store.get_event(EVENT_ID)
    assert persisted is not None
    assert persisted.claimed_at_utc == CLAIMED_AT_UTC
    assert persisted.posting_attempted_at_utc == ATTEMPTED_AT_UTC
    assert persisted.claimed_at_utc.utcoffset() is not None
    assert persisted.claimed_at_utc.utcoffset().total_seconds() == 0
    assert persisted.posting_attempted_at_utc.utcoffset() is not None
    assert persisted.posting_attempted_at_utc.utcoffset().total_seconds() == 0
