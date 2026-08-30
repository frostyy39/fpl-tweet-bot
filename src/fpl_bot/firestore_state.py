"""Firestore adapter for durable event-level posting claims and audit state."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from fpl_bot.posting_state import (
    ClaimDecision,
    EventPostingContext,
    InvalidPostingStateTransition,
    PostingAuditRecord,
    PostingClaim,
    PostingStateStore,
    PostingStateValidationError,
    PostingStatus,
    attempted_record,
    claimed_record,
    require_context_match,
    require_posting_identity_match,
    require_utc,
    terminal_record,
)

DEFAULT_COLLECTION = "fpl_event_posts"
SCHEMA_VERSION = 1


class _Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> Mapping[str, Any] | None: ...


class _DocumentReference(Protocol):
    def get(self, *, transaction: Any | None = None) -> _Snapshot: ...


class _CollectionReference(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class _Transaction(Protocol):
    def create(self, reference: _DocumentReference, data: Mapping[str, Any]) -> None: ...

    def update(self, reference: _DocumentReference, fields: Mapping[str, Any]) -> None: ...


class FirestoreClient(Protocol):
    def collection(self, collection_name: str) -> _CollectionReference: ...

    def transaction(self) -> _Transaction: ...


TransactionalWrapper = Callable[[Callable[..., Any]], Callable[..., Any]]


class FirestorePostingStateStore(PostingStateStore):
    """One Firestore document per immutable FPL event ID."""

    def __init__(
        self,
        client: FirestoreClient | None = None,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        claim_id_factory: Callable[[], str] | None = None,
        transactional_wrapper: TransactionalWrapper | None = None,
    ) -> None:
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise ValueError("collection_name must be a non-empty string")
        self._client = client if client is not None else _default_firestore_client()
        self._collection = self._client.collection(collection_name)
        self._claim_id_factory = claim_id_factory or _new_claim_id
        self._transactional = transactional_wrapper or _default_transactional_wrapper()

    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        reference = self._collection.document(str(context.event_id))

        def operation(transaction: _Transaction) -> PostingAuditRecord:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                record = PostingAuditRecord(context=context)
                transaction.create(reference, record_to_document(record))
                return record
            existing = record_from_document(snapshot.to_dict(), context.event_id)
            if existing.status is not None:
                require_context_match(existing.context, context)
                return existing
            updated = PostingAuditRecord(context=context)
            transaction.update(reference, context_fields(context))
            return updated

        return self._transactional(operation)(self._client.transaction())

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ) -> ClaimDecision:
        require_utc(claimed_at_utc, "Claim timestamp")
        reference = self._collection.document(str(context.event_id))
        proposed_claim = PostingClaim(context.event_id, self._claim_id_factory())

        def operation(transaction: _Transaction) -> ClaimDecision:
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                existing = record_from_document(snapshot.to_dict(), context.event_id)
                if existing.status is not None:
                    return ClaimDecision(claim=None, existing_status=existing.status)
                require_posting_identity_match(existing.context, context)
                record = claimed_record(existing.context, proposed_claim, claimed_at_utc)
                transaction.update(reference, claim_fields(record))
                return ClaimDecision(claim=proposed_claim, existing_status=None)
            record = claimed_record(context, proposed_claim, claimed_at_utc)
            transaction.create(reference, record_to_document(record))
            return ClaimDecision(claim=proposed_claim, existing_status=None)

        return self._transactional(operation)(self._client.transaction())

    def record_success(
        self,
        claim: PostingClaim,
        *,
        x_post_id: str,
    ) -> PostingAuditRecord:
        return self._record_terminal(
            claim,
            status=PostingStatus.SUCCEEDED,
            x_post_id=x_post_id,
        )

    def mark_posting_attempt(
        self,
        claim: PostingClaim,
        *,
        posting_attempted_at_utc: datetime,
    ) -> PostingAuditRecord:
        reference = self._collection.document(str(claim.event_id))

        def operation(transaction: _Transaction) -> PostingAuditRecord:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise InvalidPostingStateTransition("Cannot start an attempt for a missing event")
            existing = record_from_document(snapshot.to_dict(), claim.event_id)
            updated = attempted_record(
                existing,
                claim,
                posting_attempted_at_utc=posting_attempted_at_utc,
            )
            transaction.update(
                reference,
                {
                    "posting_status": updated.status.value,
                    "posting_attempted_at_utc": updated.posting_attempted_at_utc,
                },
            )
            return updated

        return self._transactional(operation)(self._client.transaction())

    def record_failure(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord:
        return self._record_terminal(
            claim,
            status=PostingStatus.FAILED,
            error_detail=error_detail,
        )

    def record_uncertain(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord:
        return self._record_terminal(
            claim,
            status=PostingStatus.UNCERTAIN,
            error_detail=error_detail,
        )

    def get_event(self, event_id: int) -> PostingAuditRecord | None:
        reference = self._collection.document(str(event_id))
        snapshot = reference.get()
        if not snapshot.exists:
            return None
        return record_from_document(snapshot.to_dict(), event_id)

    def _record_terminal(
        self,
        claim: PostingClaim,
        *,
        status: PostingStatus,
        x_post_id: str | None = None,
        error_detail: str | None = None,
    ) -> PostingAuditRecord:
        reference = self._collection.document(str(claim.event_id))

        def operation(transaction: _Transaction) -> PostingAuditRecord:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise InvalidPostingStateTransition("Cannot complete a claim for a missing event")
            existing = record_from_document(snapshot.to_dict(), claim.event_id)
            updated = terminal_record(
                existing,
                claim,
                status=status,
                x_post_id=x_post_id,
                error_detail=error_detail,
            )
            transaction.update(reference, terminal_fields(updated))
            return updated

        return self._transactional(operation)(self._client.transaction())


def record_to_document(record: PostingAuditRecord) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "fpl_event_id": record.context.event_id,
        "event_code": record.context.event_code,
        "official_deadline_utc": record.context.official_deadline_utc,
        "scheduled_task_id": record.context.scheduled_task_id,
        "scheduled_task_status": record.context.scheduled_task_status,
        "preflight_status": record.context.preflight_status,
        "posting_status": record.status.value if record.status is not None else None,
        "claim_id": record.claim_id,
        "claimed_at_utc": record.claimed_at_utc,
        "posting_attempted_at_utc": record.posting_attempted_at_utc,
        "x_post_id": record.x_post_id,
        "error_detail": record.error_detail,
    }


def context_fields(context: EventPostingContext) -> dict[str, Any]:
    return {
        "event_code": context.event_code,
        "official_deadline_utc": context.official_deadline_utc,
        "scheduled_task_id": context.scheduled_task_id,
        "scheduled_task_status": context.scheduled_task_status,
        "preflight_status": context.preflight_status,
    }


def claim_fields(record: PostingAuditRecord) -> dict[str, Any]:
    if record.status is not PostingStatus.CLAIMED:
        raise PostingStateValidationError("Claim fields require claimed posting state")
    return {
        "posting_status": record.status.value,
        "claim_id": record.claim_id,
        "claimed_at_utc": record.claimed_at_utc,
    }


def terminal_fields(record: PostingAuditRecord) -> dict[str, Any]:
    return {
        "posting_status": record.status.value,
        "posting_attempted_at_utc": record.posting_attempted_at_utc,
        "x_post_id": record.x_post_id,
        "error_detail": record.error_detail,
    }


def record_from_document(
    raw_document: Mapping[str, Any] | None,
    expected_event_id: int,
) -> PostingAuditRecord:
    if not isinstance(raw_document, Mapping):
        raise PostingStateValidationError("Firestore posting document must be an object")
    if raw_document.get("schema_version") != SCHEMA_VERSION:
        raise PostingStateValidationError("Firestore posting document has an unsupported schema")
    event_id = raw_document.get("fpl_event_id")
    if event_id != expected_event_id:
        raise PostingStateValidationError("Firestore document ID and FPL event ID do not match")
    try:
        context = EventPostingContext(
            event_id=event_id,
            event_code=raw_document["event_code"],
            official_deadline_utc=raw_document["official_deadline_utc"],
            scheduled_task_id=raw_document.get("scheduled_task_id"),
            scheduled_task_status=raw_document.get("scheduled_task_status"),
            preflight_status=raw_document.get("preflight_status"),
        )
        raw_status = raw_document.get("posting_status")
        status = PostingStatus(raw_status) if raw_status is not None else None
        return PostingAuditRecord(
            context=context,
            status=status,
            claim_id=raw_document.get("claim_id"),
            claimed_at_utc=raw_document.get("claimed_at_utc"),
            posting_attempted_at_utc=raw_document.get("posting_attempted_at_utc"),
            x_post_id=raw_document.get("x_post_id"),
            error_detail=raw_document.get("error_detail"),
        )
    except (KeyError, TypeError, ValueError):
        raise PostingStateValidationError("Firestore posting document is malformed") from None


def _default_firestore_client() -> FirestoreClient:
    from google.cloud import firestore_v1

    return firestore_v1.Client()


def _default_transactional_wrapper() -> TransactionalWrapper:
    from google.cloud.firestore_v1.transaction import transactional

    return transactional


def _new_claim_id() -> str:
    from uuid import uuid4

    return uuid4().hex
