from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from fpl_bot.posting_state import (
    EventPostingContext,
    InMemoryPostingStateStore,
    InvalidPostingStateTransition,
    PostingClaim,
    PostingStateConflictError,
    PostingStateValidationError,
    PostingStatus,
)

DEADLINE_UTC = datetime(2026, 9, 12, 10, 30, tzinfo=UTC)
CLAIMED_AT_UTC = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
ATTEMPTED_AT_UTC = datetime(2026, 9, 12, 10, 30, tzinfo=UTC)


def event_context(**overrides: object) -> EventPostingContext:
    values = {
        "event_id": 4,
        "event_code": "GW4",
        "official_deadline_utc": DEADLINE_UTC,
        "scheduled_task_id": None,
        "scheduled_task_status": None,
        "preflight_status": None,
    }
    values.update(overrides)
    return EventPostingContext(**values)  # type: ignore[arg-type]


def claimed_store() -> tuple[InMemoryPostingStateStore, PostingClaim]:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert decision.claim is not None
    return store, decision.claim


def attempted_store() -> tuple[InMemoryPostingStateStore, PostingClaim]:
    store, claim = claimed_store()
    store.mark_posting_attempt(claim, posting_attempted_at_utc=ATTEMPTED_AT_UTC)
    return store, claim


def test_first_execution_can_claim_unposted_event() -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")

    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert decision.granted is True
    assert decision.existing_status is None
    assert decision.claim == PostingClaim(event_id=4, claim_id="claim-1")
    record = store.get_event(4)
    assert record is not None
    assert record.status is PostingStatus.CLAIMED
    assert record.posting_attempted_at_utc is None


def test_duplicate_concurrent_claim_grants_exactly_one_execution() -> None:
    store = InMemoryPostingStateStore()
    store.reconcile_unclaimed_event(event_context())

    def claim_once(_: int) -> bool:
        return store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC).granted

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim_once, range(16)))

    assert results.count(True) == 1
    assert results.count(False) == 15


def test_preexisting_unclaimed_event_can_be_claimed() -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    unclaimed = store.reconcile_unclaimed_event(event_context())

    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert unclaimed.status is None
    assert unclaimed.claim_id is None
    assert decision.granted is True
    assert decision.claim == PostingClaim(event_id=4, claim_id="claim-1")


def test_unclaimed_scheduling_record_may_omit_event_code_until_live_revalidation() -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    scheduling_context = event_context(
        event_code=None,
        scheduled_task_id="fpl-deterministic-task",
        scheduled_task_status="armed",
    )

    unclaimed = store.reconcile_unclaimed_event(scheduling_context)

    assert unclaimed.status is None
    assert unclaimed.context.event_code is None
    with pytest.raises(PostingStateValidationError, match="requires an event code"):
        store.claim_event(scheduling_context, claimed_at_utc=CLAIMED_AT_UTC)
    with pytest.raises(PostingStateConflictError, match="reconcile before claiming"):
        store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    reconciled = store.reconcile_unclaimed_event(event_context())
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert reconciled.context.event_code == "GW4"
    assert decision.granted is True


def test_in_progress_event_cannot_be_claimed_by_another_execution() -> None:
    store, _ = attempted_store()

    duplicate = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert store.get_event(4) is not None
    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.IN_PROGRESS


def test_successful_event_cannot_be_claimed_again_even_if_deadline_changes() -> None:
    store, claim = attempted_store()
    store.record_success(
        claim,
        x_post_id="2094029998449393685",
    )
    changed_context = event_context(official_deadline_utc=DEADLINE_UTC + timedelta(hours=1))

    duplicate = store.claim_event(changed_context, claimed_at_utc=CLAIMED_AT_UTC)

    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.SUCCEEDED


def test_preclaim_deadline_and_audit_metadata_can_be_reconciled() -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    store.reconcile_unclaimed_event(event_context())
    changed = event_context(
        official_deadline_utc=DEADLINE_UTC + timedelta(hours=1),
        scheduled_task_id="future-task-4-v2",
        scheduled_task_status="reconciled",
        preflight_status="not_started",
    )

    updated = store.reconcile_unclaimed_event(changed)
    claim_context = event_context(official_deadline_utc=changed.official_deadline_utc)
    decision = store.claim_event(claim_context, claimed_at_utc=CLAIMED_AT_UTC)

    assert updated.status is None
    assert updated.context == changed
    assert decision.granted is True
    persisted = store.get_event(4)
    assert persisted is not None
    assert persisted.context == changed


def test_unclaimed_deadline_mismatch_requires_reconciliation_before_claim() -> None:
    store = InMemoryPostingStateStore()
    store.reconcile_unclaimed_event(event_context())
    changed = event_context(official_deadline_utc=DEADLINE_UTC + timedelta(hours=1))

    with pytest.raises(PostingStateConflictError, match="reconcile before claiming"):
        store.claim_event(changed, claimed_at_utc=CLAIMED_AT_UTC)

    persisted = store.get_event(4)
    assert persisted is not None
    assert persisted.status is None
    assert persisted.context.official_deadline_utc == DEADLINE_UTC


def test_postclaim_deadline_mismatch_fails_closed_without_mutation() -> None:
    store, _ = claimed_store()
    original = store.get_event(4)
    changed = event_context(official_deadline_utc=DEADLINE_UTC + timedelta(hours=1))

    duplicate = store.claim_event(changed, claimed_at_utc=CLAIMED_AT_UTC)
    with pytest.raises(PostingStateConflictError, match="failing closed"):
        store.reconcile_unclaimed_event(changed)

    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.CLAIMED
    assert store.get_event(4) == original


def test_success_records_x_post_id_and_attempt_timestamp() -> None:
    store, claim = claimed_store()
    in_progress = store.mark_posting_attempt(
        claim,
        posting_attempted_at_utc=ATTEMPTED_AT_UTC,
    )

    record = store.record_success(
        claim,
        x_post_id="2094029998449393685",
    )

    assert in_progress.status is PostingStatus.IN_PROGRESS
    assert in_progress.posting_attempted_at_utc == ATTEMPTED_AT_UTC
    assert record.status is PostingStatus.SUCCEEDED
    assert record.x_post_id == "2094029998449393685"
    assert record.posting_attempted_at_utc == ATTEMPTED_AT_UTC
    assert record.error_detail is None


def test_definite_failure_is_auditable_and_remains_closed() -> None:
    store, claim = attempted_store()

    record = store.record_failure(
        claim,
        error_detail="X rejected the request with HTTP 403",
    )
    duplicate = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert record.status is PostingStatus.FAILED
    assert record.error_detail == "X rejected the request with HTTP 403"
    assert record.x_post_id is None
    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.FAILED


def test_error_detail_is_normalized_before_persistence() -> None:
    store, claim = attempted_store()

    record = store.record_failure(
        claim,
        error_detail="X rejected the request\n  with HTTP 403",
    )

    assert record.error_detail == "X rejected the request with HTTP 403"


@pytest.mark.parametrize(
    "error_detail",
    [
        "Authorization: Bearer unit-test-secret-value",
        "refresh_token=unit-test-secret-value",
        "client_secret: unit-test-secret-value",
        "cookie=session=unit-test-secret-value",
    ],
)
def test_error_detail_rejects_credential_like_material(error_detail: str) -> None:
    store, claim = attempted_store()

    with pytest.raises(PostingStateValidationError, match="credential material"):
        store.record_failure(claim, error_detail=error_detail)


def test_error_detail_is_bounded_for_audit_storage() -> None:
    store, claim = attempted_store()

    with pytest.raises(PostingStateValidationError, match="must not exceed 2000"):
        store.record_uncertain(claim, error_detail="x" * 2_001)


def test_uncertain_write_is_auditable_and_fails_closed() -> None:
    store, claim = attempted_store()

    record = store.record_uncertain(
        claim,
        error_detail="Connection ended after the write may have reached X",
    )
    duplicate = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert record.status is PostingStatus.UNCERTAIN
    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.UNCERTAIN


def test_event_metadata_and_future_placeholders_are_retained() -> None:
    context = event_context(
        event_code="DGW4",
        scheduled_task_id="future-task-4",
        scheduled_task_status="placeholder",
        preflight_status="not_started",
    )
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")

    store.claim_event(context, claimed_at_utc=CLAIMED_AT_UTC)

    record = store.get_event(4)
    assert record is not None
    assert record.context == context
    assert record.context.official_deadline_utc == DEADLINE_UTC
    assert record.context.event_code == "DGW4"


def test_wrong_claim_cannot_complete_event() -> None:
    store, _ = claimed_store()

    with pytest.raises(InvalidPostingStateTransition, match="does not own"):
        store.mark_posting_attempt(
            PostingClaim(event_id=4, claim_id="wrong-claim"),
            posting_attempted_at_utc=ATTEMPTED_AT_UTC,
        )


def test_terminal_state_cannot_transition_again() -> None:
    store, claim = attempted_store()
    store.record_failure(
        claim,
        error_detail="definite rejection",
    )

    with pytest.raises(InvalidPostingStateTransition, match="state failed"):
        store.record_success(
            claim,
            x_post_id="123456789",
        )


@pytest.mark.parametrize(
    "invalid_deadline",
    [
        datetime(2026, 9, 12, 10, 30),
        datetime(2026, 9, 12, 11, 30, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_event_deadline_requires_timezone_aware_utc(invalid_deadline: datetime) -> None:
    with pytest.raises(PostingStateValidationError, match="timezone-aware UTC"):
        event_context(official_deadline_utc=invalid_deadline)


def test_claim_and_attempt_timestamps_require_utc() -> None:
    store = InMemoryPostingStateStore()

    with pytest.raises(PostingStateValidationError, match="timezone-aware UTC"):
        store.claim_event(event_context(), claimed_at_utc=datetime(2026, 9, 12, 9, 0))

    store, claim = claimed_store()
    with pytest.raises(PostingStateValidationError, match="timezone-aware UTC"):
        store.mark_posting_attempt(
            claim,
            posting_attempted_at_utc=datetime(2026, 9, 12, 10, 30),
        )


def test_invalid_event_code_and_success_payload_are_rejected() -> None:
    with pytest.raises(PostingStateValidationError, match="match the FPL event ID"):
        event_context(event_code="GW5")

    store, claim = attempted_store()
    with pytest.raises(PostingStateValidationError, match="numeric X Post ID"):
        store.record_success(
            claim,
            x_post_id="not-numeric",
        )


def test_success_is_rejected_until_attempt_timestamp_is_persisted() -> None:
    store, claim = claimed_store()

    with pytest.raises(InvalidPostingStateTransition, match="state posting_claimed"):
        store.record_success(claim, x_post_id="123456789")
