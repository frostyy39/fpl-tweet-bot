"""Durable posting-state domain model and deterministic in-memory implementation."""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from fpl_bot.errors import FplBotError

EVENT_CODE_PATTERN = re.compile(r"(?:GW|BGW|DGW|BDGW)([1-9][0-9]*)\Z")
X_POST_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
MAX_ERROR_DETAIL_LENGTH = 2_000
SENSITIVE_ERROR_DETAIL_PATTERN = re.compile(
    r"(?:authorization\s*[\"']?\s*[:=]|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:access|refresh)[_-]?token\s*[\"']?\s*[:=]|"
    r"client[_-]?secret\s*[\"']?\s*[:=]|"
    r"cookie\s*[\"']?\s*[:=])",
    re.IGNORECASE,
)


class PostingStateError(FplBotError):
    """Base class for posting-state failures."""


class PostingStateValidationError(PostingStateError):
    """Raised when posting metadata or persisted state is malformed."""


class InvalidPostingStateTransition(PostingStateError):
    """Raised when an execution tries to complete a claim it does not own."""


class PostingStateConflictError(PostingStateError):
    """Raised when metadata conflicts with an event whose posting state is already claimed."""


class PostingStatus(StrEnum):
    CLAIMED = "posting_claimed"
    IN_PROGRESS = "posting_in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class EventPostingContext:
    """Event audit metadata that becomes immutable when posting is claimed."""

    event_id: int
    event_code: str | None
    official_deadline_utc: datetime
    scheduled_task_id: str | None = None
    scheduled_task_status: str | None = None
    preflight_status: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id <= 0
        ):
            raise PostingStateValidationError("FPL event ID must be a positive integer")
        if self.event_code is not None:
            match = (
                EVENT_CODE_PATTERN.fullmatch(self.event_code)
                if isinstance(self.event_code, str)
                else None
            )
            if match is None or int(match.group(1)) != self.event_id:
                raise PostingStateValidationError("Event code must match the FPL event ID")
        require_utc(self.official_deadline_utc, "Official deadline")
        _validate_optional_text(self.scheduled_task_id, "Scheduled task ID")
        _validate_optional_text(self.scheduled_task_status, "Scheduled task status")
        _validate_optional_text(self.preflight_status, "Preflight status")


@dataclass(frozen=True, slots=True)
class PostingClaim:
    event_id: int
    claim_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id <= 0
        ):
            raise PostingStateValidationError("Claim event ID must be a positive integer")
        if not isinstance(self.claim_id, str) or not self.claim_id.strip():
            raise PostingStateValidationError("Claim ID must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    claim: PostingClaim | None
    existing_status: PostingStatus | None

    def __post_init__(self) -> None:
        if (self.claim is None) == (self.existing_status is None):
            raise PostingStateValidationError(
                "Claim decision must be either granted or denied with an existing state"
            )

    @property
    def granted(self) -> bool:
        return self.claim is not None


@dataclass(frozen=True, slots=True)
class PostingAuditRecord:
    context: EventPostingContext
    status: PostingStatus | None = None
    claim_id: str | None = field(default=None, repr=False)
    claimed_at_utc: datetime | None = None
    posting_attempted_at_utc: datetime | None = None
    x_post_id: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            if any(
                value is not None
                for value in (
                    self.claim_id,
                    self.claimed_at_utc,
                    self.posting_attempted_at_utc,
                    self.x_post_id,
                    self.error_detail,
                )
            ):
                raise PostingStateValidationError(
                    "Unclaimed event cannot contain posting claim or outcome fields"
                )
            return
        if self.context.event_code is None:
            raise PostingStateValidationError("Claimed posting state requires an event code")
        if not isinstance(self.status, PostingStatus):
            raise PostingStateValidationError("Posting status is invalid")
        if self.claim_id is None or self.claimed_at_utc is None:
            raise PostingStateValidationError("Claimed posting state requires claim metadata")
        PostingClaim(self.context.event_id, self.claim_id)
        require_utc(self.claimed_at_utc, "Claim timestamp")
        if self.posting_attempted_at_utc is not None:
            require_utc(self.posting_attempted_at_utc, "Posting attempt timestamp")
            if self.posting_attempted_at_utc < self.claimed_at_utc:
                raise PostingStateValidationError("Posting attempt cannot precede the claim")

        if self.status is PostingStatus.CLAIMED:
            if any(
                value is not None
                for value in (self.posting_attempted_at_utc, self.x_post_id, self.error_detail)
            ):
                raise PostingStateValidationError("Claimed state cannot contain an attempt outcome")
            return

        if self.posting_attempted_at_utc is None:
            raise PostingStateValidationError("Posting state requires an attempt timestamp")
        if self.status is PostingStatus.IN_PROGRESS:
            if self.x_post_id is not None or self.error_detail is not None:
                raise PostingStateValidationError("In-progress state cannot contain an outcome")
            return
        if self.status is PostingStatus.SUCCEEDED:
            if not isinstance(self.x_post_id, str) or not X_POST_ID_PATTERN.fullmatch(
                self.x_post_id
            ):
                raise PostingStateValidationError("Successful state requires a numeric X Post ID")
            if self.error_detail is not None:
                raise PostingStateValidationError("Successful state cannot contain an error")
            return

        if self.x_post_id is not None:
            raise PostingStateValidationError(
                "Failed or uncertain state cannot contain an X Post ID"
            )
        _validate_error_detail(self.error_detail)


class PostingStateStore(Protocol):
    def reconcile_unclaimed_event(
        self,
        context: EventPostingContext,
    ) -> PostingAuditRecord: ...

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ) -> ClaimDecision: ...

    def record_success(
        self,
        claim: PostingClaim,
        *,
        x_post_id: str,
    ) -> PostingAuditRecord: ...

    def mark_posting_attempt(
        self,
        claim: PostingClaim,
        *,
        posting_attempted_at_utc: datetime,
    ) -> PostingAuditRecord: ...

    def record_failure(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord: ...

    def record_uncertain(
        self,
        claim: PostingClaim,
        *,
        error_detail: str,
    ) -> PostingAuditRecord: ...

    def get_event(self, event_id: int) -> PostingAuditRecord | None: ...


class InMemoryPostingStateStore:
    """Thread-safe deterministic store for tests; production uses Firestore transactions."""

    def __init__(self, *, claim_id_factory: Callable[[], str] | None = None) -> None:
        self._claim_id_factory = claim_id_factory or _new_claim_id
        self._records: dict[int, PostingAuditRecord] = {}
        self._lock = threading.Lock()

    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        with self._lock:
            existing = self._records.get(context.event_id)
            if existing is None:
                record = PostingAuditRecord(context=context)
                self._records[context.event_id] = record
                return record
            if existing.status is not None:
                require_context_match(existing.context, context)
                return existing
            updated = replace(existing, context=context)
            self._records[context.event_id] = updated
            return updated

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ) -> ClaimDecision:
        require_utc(claimed_at_utc, "Claim timestamp")
        with self._lock:
            existing = self._records.get(context.event_id)
            if existing is not None:
                if existing.status is not None:
                    return ClaimDecision(claim=None, existing_status=existing.status)
                require_posting_identity_match(existing.context, context)
                context = existing.context
            claim = PostingClaim(context.event_id, self._claim_id_factory())
            self._records[context.event_id] = claimed_record(context, claim, claimed_at_utc)
            return ClaimDecision(claim=claim, existing_status=None)

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
        with self._lock:
            existing = self._records.get(claim.event_id)
            updated = attempted_record(
                existing,
                claim,
                posting_attempted_at_utc=posting_attempted_at_utc,
            )
            self._records[claim.event_id] = updated
            return updated

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
        with self._lock:
            return self._records.get(event_id)

    def _record_terminal(
        self,
        claim: PostingClaim,
        *,
        status: PostingStatus,
        x_post_id: str | None = None,
        error_detail: str | None = None,
    ) -> PostingAuditRecord:
        with self._lock:
            existing = self._records.get(claim.event_id)
            updated = terminal_record(
                existing,
                claim,
                status=status,
                x_post_id=x_post_id,
                error_detail=error_detail,
            )
            self._records[claim.event_id] = updated
            return updated


def claimed_record(
    context: EventPostingContext,
    claim: PostingClaim,
    claimed_at_utc: datetime,
) -> PostingAuditRecord:
    return PostingAuditRecord(
        context=context,
        status=PostingStatus.CLAIMED,
        claim_id=claim.claim_id,
        claimed_at_utc=claimed_at_utc,
    )


def terminal_record(
    existing: PostingAuditRecord | None,
    claim: PostingClaim,
    *,
    status: PostingStatus,
    x_post_id: str | None = None,
    error_detail: str | None = None,
) -> PostingAuditRecord:
    if existing is None:
        raise InvalidPostingStateTransition("Cannot complete a claim for a missing event")
    if existing.status is not PostingStatus.IN_PROGRESS:
        state = existing.status.value if existing.status is not None else "unclaimed"
        raise InvalidPostingStateTransition(f"Cannot complete event from state {state}")
    if existing.claim_id != claim.claim_id:
        raise InvalidPostingStateTransition("Claim ID does not own this event")
    if status not in {PostingStatus.SUCCEEDED, PostingStatus.FAILED, PostingStatus.UNCERTAIN}:
        raise InvalidPostingStateTransition("Claim can transition only to a terminal state")
    if status in {PostingStatus.FAILED, PostingStatus.UNCERTAIN}:
        error_detail = sanitize_error_detail(error_detail)
    return replace(
        existing,
        status=status,
        x_post_id=x_post_id,
        error_detail=error_detail,
    )


def attempted_record(
    existing: PostingAuditRecord | None,
    claim: PostingClaim,
    *,
    posting_attempted_at_utc: datetime,
) -> PostingAuditRecord:
    if existing is None:
        raise InvalidPostingStateTransition("Cannot start an attempt for a missing event")
    if existing.status is not PostingStatus.CLAIMED:
        state = existing.status.value if existing.status is not None else "unclaimed"
        raise InvalidPostingStateTransition(f"Cannot start an attempt from state {state}")
    if existing.claim_id != claim.claim_id:
        raise InvalidPostingStateTransition("Claim ID does not own this event")
    return replace(
        existing,
        status=PostingStatus.IN_PROGRESS,
        posting_attempted_at_utc=posting_attempted_at_utc,
    )


def require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PostingStateValidationError(f"{label} must be timezone-aware UTC")


def require_context_match(
    persisted: EventPostingContext,
    proposed: EventPostingContext,
) -> None:
    if persisted != proposed:
        raise PostingStateConflictError(
            "Event metadata differs from the persisted posting operation; failing closed"
        )


def require_posting_identity_match(
    persisted: EventPostingContext,
    proposed: EventPostingContext,
) -> None:
    if (
        persisted.event_id != proposed.event_id
        or persisted.event_code != proposed.event_code
        or persisted.official_deadline_utc != proposed.official_deadline_utc
    ):
        raise PostingStateConflictError(
            "Event code or official deadline differs from the unclaimed event record; "
            "reconcile before claiming"
        )


def _validate_optional_text(value: str | None, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise PostingStateValidationError(f"{label} must be a non-empty string when supplied")


def _validate_error_detail(value: str | None) -> None:
    sanitized = sanitize_error_detail(value)
    if value != sanitized:
        raise PostingStateValidationError("Persisted error details must already be sanitized")


def sanitize_error_detail(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostingStateValidationError("Failed or uncertain state requires error details")
    sanitized = " ".join(value.split())
    if len(sanitized) > MAX_ERROR_DETAIL_LENGTH:
        raise PostingStateValidationError(
            f"Error details must not exceed {MAX_ERROR_DETAIL_LENGTH} characters"
        )
    if SENSITIVE_ERROR_DETAIL_PATTERN.search(sanitized):
        raise PostingStateValidationError(
            "Error details appear to contain authentication or credential material"
        )
    return sanitized


def _new_claim_id() -> str:
    return uuid4().hex
