from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fpl_bot.firestore_state import (
    DEFAULT_COLLECTION,
    FirestorePostingStateStore,
    record_from_document,
)
from fpl_bot.posting_state import (
    EventPostingContext,
    InvalidPostingStateTransition,
    PostingStateConflictError,
    PostingStateValidationError,
    PostingStatus,
)

DEADLINE_UTC = datetime(2026, 9, 12, 10, 30, tzinfo=UTC)
CLAIMED_AT_UTC = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
ATTEMPTED_AT_UTC = datetime(2026, 9, 12, 10, 30, tzinfo=UTC)


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> Mapping[str, Any] | None:
        return None if self._data is None else dict(self._data)


class FakeDocumentReference:
    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        self.data: dict[str, Any] | None = None
        self.transaction_reads = 0

    def get(self, *, transaction: Any | None = None) -> FakeSnapshot:
        if transaction is not None:
            self.transaction_reads += 1
        return FakeSnapshot(self.data)


class FakeCollectionReference:
    def __init__(self) -> None:
        self.documents: dict[str, FakeDocumentReference] = {}

    def document(self, document_id: str) -> FakeDocumentReference:
        return self.documents.setdefault(document_id, FakeDocumentReference(document_id))


class FakeTransaction:
    def __init__(self) -> None:
        self.create_calls: list[tuple[FakeDocumentReference, Mapping[str, Any]]] = []
        self.update_calls: list[tuple[FakeDocumentReference, Mapping[str, Any]]] = []

    def create(
        self,
        reference: FakeDocumentReference,
        data: Mapping[str, Any],
    ) -> None:
        assert reference.data is None
        self.create_calls.append((reference, data))
        reference.data = dict(data)

    def update(
        self,
        reference: FakeDocumentReference,
        fields: Mapping[str, Any],
    ) -> None:
        assert reference.data is not None
        self.update_calls.append((reference, fields))
        reference.data.update(fields)


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.collection_name: str | None = None
        self.collection_reference = FakeCollectionReference()
        self.transactions: list[FakeTransaction] = []

    def collection(self, collection_name: str) -> FakeCollectionReference:
        self.collection_name = collection_name
        return self.collection_reference

    def transaction(self) -> FakeTransaction:
        transaction = FakeTransaction()
        self.transactions.append(transaction)
        return transaction


class RecordingTransactionalWrapper:
    def __init__(self) -> None:
        self.invocations = 0

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(transaction: FakeTransaction) -> Any:
            self.invocations += 1
            return function(transaction)

        return wrapped


def event_context() -> EventPostingContext:
    return EventPostingContext(
        event_id=4,
        event_code="GW4",
        official_deadline_utc=DEADLINE_UTC,
    )


def firestore_store() -> tuple[
    FirestorePostingStateStore,
    FakeFirestoreClient,
    RecordingTransactionalWrapper,
]:
    client = FakeFirestoreClient()
    wrapper = RecordingTransactionalWrapper()
    store = FirestorePostingStateStore(
        client,
        claim_id_factory=lambda: "claim-1",
        transactional_wrapper=wrapper,
    )
    return store, client, wrapper


def test_firestore_claim_uses_event_id_document_and_transaction_create() -> None:
    store, client, wrapper = firestore_store()

    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert decision.granted is True
    assert client.collection_name == DEFAULT_COLLECTION
    reference = client.collection_reference.documents["4"]
    assert reference.document_id == "4"
    assert reference.transaction_reads == 1
    assert len(client.transactions[0].create_calls) == 1
    assert wrapper.invocations == 1
    assert reference.data is not None
    assert reference.data["fpl_event_id"] == 4
    assert reference.data["official_deadline_utc"] == DEADLINE_UTC
    assert reference.data["event_code"] == "GW4"
    assert reference.data["posting_status"] == "posting_claimed"


def test_firestore_existing_document_denies_duplicate_without_write() -> None:
    store, client, _ = firestore_store()
    first = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert first.claim is not None

    duplicate = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.CLAIMED
    assert client.transactions[1].create_calls == []
    assert client.transactions[1].update_calls == []


def test_firestore_preexisting_unclaimed_document_is_claimed_transactionally() -> None:
    store, client, wrapper = firestore_store()
    unclaimed = store.reconcile_unclaimed_event(event_context())

    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert unclaimed.status is None
    assert decision.granted is True
    assert wrapper.invocations == 2
    assert client.transactions[1].create_calls == []
    assert len(client.transactions[1].update_calls) == 1
    persisted = store.get_event(4)
    assert persisted is not None
    assert persisted.status is PostingStatus.CLAIMED


def test_firestore_unclaimed_scheduling_document_may_have_no_event_code() -> None:
    store, client, _ = firestore_store()
    context = EventPostingContext(
        event_id=4,
        event_code=None,
        official_deadline_utc=DEADLINE_UTC,
        scheduled_task_id="fpl-deterministic-task",
        scheduled_task_status="armed",
    )

    record = store.reconcile_unclaimed_event(context)

    assert record.status is None
    assert record.context.event_code is None
    persisted = client.collection_reference.documents["4"].data
    assert persisted is not None
    assert persisted["event_code"] is None


def test_firestore_preclaim_deadline_metadata_can_be_reconciled() -> None:
    store, client, _ = firestore_store()
    store.reconcile_unclaimed_event(event_context())
    changed = EventPostingContext(
        event_id=4,
        event_code="GW4",
        official_deadline_utc=DEADLINE_UTC + timedelta(hours=1),
        scheduled_task_id="future-task-4-v2",
        scheduled_task_status="reconciled",
        preflight_status="not_started",
    )

    updated = store.reconcile_unclaimed_event(changed)
    claim_context = EventPostingContext(
        event_id=4,
        event_code="GW4",
        official_deadline_utc=changed.official_deadline_utc,
    )
    decision = store.claim_event(claim_context, claimed_at_utc=CLAIMED_AT_UTC)

    assert updated.status is None
    assert updated.context == changed
    assert len(client.transactions[1].update_calls) == 1
    persisted = store.get_event(4)
    assert persisted is not None
    assert decision.granted is True
    assert persisted.context == changed


def test_firestore_postclaim_deadline_mismatch_fails_closed() -> None:
    store, client, _ = firestore_store()
    store.reconcile_unclaimed_event(event_context())
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert decision.granted is True
    before = dict(client.collection_reference.documents["4"].data or {})
    changed = EventPostingContext(
        event_id=4,
        event_code="GW4",
        official_deadline_utc=DEADLINE_UTC + timedelta(hours=1),
    )

    duplicate = store.claim_event(changed, claimed_at_utc=CLAIMED_AT_UTC)
    with pytest.raises(PostingStateConflictError, match="failing closed"):
        store.reconcile_unclaimed_event(changed)

    assert duplicate.granted is False
    assert duplicate.existing_status is PostingStatus.CLAIMED
    assert client.collection_reference.documents["4"].data == before


def test_firestore_success_transition_is_transactional_and_persists_post_id() -> None:
    store, client, wrapper = firestore_store()
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert decision.claim is not None
    in_progress = store.mark_posting_attempt(
        decision.claim,
        posting_attempted_at_utc=ATTEMPTED_AT_UTC,
    )

    record = store.record_success(
        decision.claim,
        x_post_id="2094029998449393685",
    )

    assert in_progress.status is PostingStatus.IN_PROGRESS
    assert record.status is PostingStatus.SUCCEEDED
    assert wrapper.invocations == 3
    assert len(client.transactions[1].update_calls) == 1
    assert len(client.transactions[2].update_calls) == 1
    persisted = store.get_event(4)
    assert persisted is not None
    assert persisted.x_post_id == "2094029998449393685"
    assert persisted.posting_attempted_at_utc == ATTEMPTED_AT_UTC


@pytest.mark.parametrize(
    ("method_name", "expected_status"),
    [
        ("record_failure", PostingStatus.FAILED),
        ("record_uncertain", PostingStatus.UNCERTAIN),
    ],
)
def test_firestore_failure_outcomes_are_auditable_and_closed(
    method_name: str,
    expected_status: PostingStatus,
) -> None:
    store, _, _ = firestore_store()
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert decision.claim is not None
    store.mark_posting_attempt(
        decision.claim,
        posting_attempted_at_utc=ATTEMPTED_AT_UTC,
    )

    method = getattr(store, method_name)
    record = method(
        decision.claim,
        error_detail="safe diagnostic",
    )
    duplicate = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)

    assert record.status is expected_status
    assert record.error_detail == "safe diagnostic"
    assert duplicate.granted is False
    assert duplicate.existing_status is expected_status


def test_firestore_rejects_invalid_transition_without_update() -> None:
    store, client, _ = firestore_store()
    decision = store.claim_event(event_context(), claimed_at_utc=CLAIMED_AT_UTC)
    assert decision.claim is not None
    store.mark_posting_attempt(
        decision.claim,
        posting_attempted_at_utc=ATTEMPTED_AT_UTC,
    )
    store.record_success(
        decision.claim,
        x_post_id="123456789",
    )

    with pytest.raises(InvalidPostingStateTransition, match="state succeeded"):
        store.record_failure(
            decision.claim,
            error_detail="must not replace success",
        )

    assert client.transactions[-1].update_calls == []


def test_firestore_malformed_or_mismatched_document_fails_closed() -> None:
    with pytest.raises(PostingStateValidationError, match="unsupported schema"):
        record_from_document({"schema_version": 999}, expected_event_id=4)

    with pytest.raises(PostingStateValidationError, match="do not match"):
        record_from_document(
            {"schema_version": 1, "fpl_event_id": 5},
            expected_event_id=4,
        )


def test_firestore_malformed_value_is_not_exposed_by_validation_error() -> None:
    sensitive_value = "sensitive-persisted-value-placeholder"
    raw_document = {
        "schema_version": 1,
        "fpl_event_id": 4,
        "event_code": "GW4",
        "official_deadline_utc": DEADLINE_UTC,
        "posting_status": sensitive_value,
    }

    with pytest.raises(PostingStateValidationError) as error:
        record_from_document(raw_document, expected_event_id=4)

    assert str(error.value) == "Firestore posting document is malformed"
    assert sensitive_value not in str(error.value)
    assert error.value.__cause__ is None
