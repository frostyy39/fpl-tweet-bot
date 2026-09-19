"""Captain-only immutable state. No provider, browser or posting implementation."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from fpl_bot.captain_handoff import AuthenticationStatus, CaptainAssignment, ProjectionHandoff
from fpl_bot.captain_orchestration_timing import require_utc


class StateConflict(ValueError):
    """Rejected stale, conflicting or invalid state operation; no mutation occurred."""


def identity(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise StateConflict("invalid operation identity")


@dataclass(frozen=True, slots=True)
class PostKey:
    destination_user_id: str
    event_id: int
    post_type: str = "captain"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.destination_user_id, str)
            or not self.destination_user_id.isascii()
            or not self.destination_user_id.isdecimal()
            or self.destination_user_id.startswith("0")
            or len(self.destination_user_id) > 32
            or type(self.event_id) is not int
            or self.event_id < 1
            or self.post_type != "captain"
        ):
            raise StateConflict("invalid Captain posting key")


class GenerationStatus(StrEnum):
    PLANNED = "planned"
    WARMING = "warming"
    READY = "ready"
    RELEASED = "released"
    ACQUIRING = "acquiring"
    ACCEPTED = "accepted"
    CANCELLED_STALE = "cancelled_stale"
    AUTHENTICATION_REQUIRED = "authentication_required"
    FAILED = "failed"
    MISSED = "missed"


class AttemptStatus(StrEnum):
    ACQUIRING = "acquiring"
    ACCEPTED = "accepted"
    CANCELLED_STALE = "cancelled_stale"
    AUTHENTICATION_REQUIRED = "authentication_required"
    FAILED = "failed"
    MISSED = "missed"


@dataclass(frozen=True, slots=True)
class Generation:
    key: PostKey
    assignment: CaptainAssignment
    version: int
    history: tuple[tuple[GenerationStatus, datetime], ...]

    @property
    def status(self) -> GenerationStatus:
        return self.history[-1][0]


@dataclass(frozen=True, slots=True)
class AcquisitionAttempt:
    attempt_id: UUID
    generation_id: UUID
    claimed_at: datetime
    status: AttemptStatus
    updated_at: datetime
    handoff: ProjectionHandoff | None = None


class PostingStatus(StrEnum):
    CLAIMED = "claimed"
    FAILED_BEFORE_WRITE = "failed_before_write"
    WRITE_STARTED = "write_started"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class PostingAttempt:
    claim_id: UUID
    generation_id: UUID
    payload_digest: str
    history: tuple[tuple[PostingStatus, datetime], ...]
    post_id: str | None = None

    @property
    def status(self) -> PostingStatus:
        return self.history[-1][0]


@dataclass(frozen=True, slots=True)
class PostingRecord:
    key: PostKey
    attempts: tuple[PostingAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateSelectionEvidence:
    source_ordinal: int
    projection_rank: int
    official_id: int
    web_name: str
    projection: Decimal
    official_ownership: Decimal
    fixtures: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.source_ordinal) is not int
            or self.source_ordinal < 1
            or type(self.projection_rank) is not int
            or self.projection_rank < 1
            or type(self.official_id) is not int
            or self.official_id < 1
            or not isinstance(self.web_name, str)
            or not self.web_name.strip()
            or not isinstance(self.projection, Decimal)
            or not self.projection.is_finite()
            or not isinstance(self.official_ownership, Decimal)
            or not self.official_ownership.is_finite()
            or self.official_ownership < 0
            or type(self.fixtures) is not tuple
            or not self.fixtures
            or any(not isinstance(value, str) or not value for value in self.fixtures)
        ):
            raise StateConflict("invalid validated selection evidence")

    def payload(self) -> dict:
        return {
            "source_ordinal": self.source_ordinal,
            "projection_rank": self.projection_rank,
            "official_id": self.official_id,
            "web_name": self.web_name,
            "projection": str(self.projection),
            "official_ownership": str(self.official_ownership),
            "fixtures": list(self.fixtures),
        }


@dataclass(frozen=True, slots=True)
class ValidatedCandidateRecord:
    """Immutable cloud validation evidence; never permission to write by itself."""

    key: PostKey
    generation_id: UUID
    attempt_id: UUID
    handoff_digest: str
    event_code: str
    deadline_utc: datetime
    top_three: tuple[
        CandidateSelectionEvidence,
        CandidateSelectionEvidence,
        CandidateSelectionEvidence,
    ]
    differential: CandidateSelectionEvidence
    tweet: str
    weighted_length: int
    validated_at_utc: datetime
    candidate_digest: str

    def __post_init__(self) -> None:
        require_utc(self.deadline_utc)
        require_utc(self.validated_at_utc)
        identity(self.generation_id)
        identity(self.attempt_id)
        if (
            not isinstance(self.key, PostKey)
            or not isinstance(self.event_code, str)
            or not self.event_code
            or type(self.top_three) is not tuple
            or len(self.top_three) != 3
            or not all(isinstance(value, CandidateSelectionEvidence) for value in self.top_three)
            or not isinstance(self.differential, CandidateSelectionEvidence)
            or len({value.official_id for value in (*self.top_three, self.differential)}) != 4
            or not isinstance(self.tweet, str)
            or not self.tweet
            or type(self.weighted_length) is not int
            or not 1 <= self.weighted_length <= 280
            or not _digest(self.handoff_digest)
            or not _digest(self.candidate_digest)
            or self.candidate_digest != self.content_digest()
        ):
            raise StateConflict("invalid validated Captain candidate")

    def content_payload(self) -> dict:
        return candidate_content_payload(
            self.key,
            self.generation_id,
            self.attempt_id,
            self.handoff_digest,
            self.event_code,
            self.deadline_utc,
            self.top_three,
            self.differential,
            self.tweet,
            self.weighted_length,
        )

    def content_digest(self) -> str:
        canonical = json.dumps(
            self.content_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value.isascii()
        and all(character in "0123456789abcdef" for character in value)
    )


def candidate_content_payload(
    key: PostKey,
    generation_id: UUID,
    attempt_id: UUID,
    handoff_digest: str,
    event_code: str,
    deadline_utc: datetime,
    top_three: tuple[
        CandidateSelectionEvidence,
        CandidateSelectionEvidence,
        CandidateSelectionEvidence,
    ],
    differential: CandidateSelectionEvidence,
    tweet: str,
    weighted_length: int,
) -> dict:
    return {
        "schema_version": 1,
        "destination_user_id": key.destination_user_id,
        "post_type": key.post_type,
        "event_id": key.event_id,
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "handoff_digest": handoff_digest,
        "event_code": event_code,
        "deadline_utc": deadline_utc.isoformat(),
        "top_three": [value.payload() for value in top_three],
        "differential": differential.payload(),
        "tweet": tweet,
        "weighted_length": weighted_length,
    }


def candidate_content_digest(*args) -> str:
    canonical = json.dumps(
        candidate_content_payload(*args),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class TaskKind(StrEnum):
    WARMUP = "warmup"
    RELEASE = "release"
    PUBLISH = "publish"
    CLEANUP = "cleanup"


class IntentStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskIntent:
    generation_id: UUID
    kind: TaskKind
    scheduled_at: datetime
    status: IntentStatus = IntentStatus.PENDING

    @property
    def identity(self) -> str:
        return f"captain-{self.generation_id}-{self.kind.value}"


class VmPhase(StrEnum):
    IN_USE = "in_use"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class VmUseLease:
    lease_id: UUID
    generation_id: UUID
    acquired_at: datetime
    phase: VmPhase = VmPhase.IN_USE


class RefreshObservation(StrEnum):
    UNKNOWN = "unknown"
    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"


class SessionExpiryKind(StrEnum):
    UNKNOWN = "unknown"
    SESSION = "session"
    FIXED = "fixed"


@dataclass(frozen=True, slots=True)
class SessionHealthEvidence:
    """Allowlisted metadata only. Never a proof of future server-side validity."""

    observed_at: datetime
    authentication: AuthenticationStatus
    expiry_kind: SessionExpiryKind = SessionExpiryKind.UNKNOWN
    expires_at: datetime | None = None
    next_target: datetime | None = None
    refresh: RefreshObservation = RefreshObservation.UNKNOWN
    manual_reauthentication_required: bool = False

    def __post_init__(self) -> None:
        require_utc(self.observed_at)
        for value in (self.expires_at, self.next_target):
            if value is not None:
                require_utc(value)
        if (
            not isinstance(self.authentication, AuthenticationStatus)
            or not isinstance(self.expiry_kind, SessionExpiryKind)
            or not isinstance(self.refresh, RefreshObservation)
            or type(self.manual_reauthentication_required) is not bool
            or (self.expiry_kind == SessionExpiryKind.FIXED) != (self.expires_at is not None)
        ):
            raise StateConflict("invalid session evidence")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Mutation(Generic[T]):
    """applied=False is an observation/replay, NEVER authorization to repeat a side effect."""

    record: T
    applied: bool
