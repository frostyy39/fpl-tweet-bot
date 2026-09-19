"""Guarded Captain publisher. Validated candidates are evidence, never authority alone."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_state import PostingStatus, PostKey, StateConflict
from fpl_bot.captain_validation import (
    CandidateRejected,
    CaptainCandidateValidator,
    candidate_record,
)
from fpl_bot.x_api import XIdentityReader, XPostCreator
from fpl_bot.x_errors import XApiError, XIdentityMismatchError

FPLBOTTEST_USER_ID = "1732468005336907776"


class PublishStatus(StrEnum):
    DISABLED = "disabled"
    SUCCEEDED = "succeeded"
    ALREADY_SUCCEEDED = "already_succeeded"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class PublicationInstruction:
    generation_id: UUID
    attempt_id: UUID
    handoff_digest: str
    candidate_digest: str
    claim_id: UUID

    @classmethod
    def from_payload(cls, payload: object) -> "PublicationInstruction":
        if type(payload) is not dict or set(payload) != {
            "version",
            "generation_id",
            "attempt_id",
            "handoff_digest",
            "candidate_digest",
            "claim_id",
        }:
            raise StateConflict("malformed Captain publication instruction")
        if payload["version"] != 1:
            raise StateConflict("unsupported Captain publication instruction")
        try:
            result = cls(
                UUID(payload["generation_id"]),
                UUID(payload["attempt_id"]),
                payload["handoff_digest"],
                payload["candidate_digest"],
                UUID(payload["claim_id"]),
            )
        except (TypeError, ValueError):
            raise StateConflict("malformed Captain publication instruction") from None
        if any(
            value.int == 0 for value in (result.generation_id, result.attempt_id, result.claim_id)
        ):
            raise StateConflict("invalid Captain publication identity")
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
            for value in (result.handoff_digest, result.candidate_digest)
        ):
            raise StateConflict("invalid Captain publication digest")
        return result


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: PublishStatus
    event_id: int | None = None
    post_id: str | None = None


class PublisherClock(Protocol):
    def now(self): ...


class CaptainPublisher:
    """One fail-closed publication attempt with durable event-level fencing."""

    def __init__(
        self,
        repository,
        validator: CaptainCandidateValidator,
        identity: XIdentityReader,
        writer: XPostCreator,
        clock: PublisherClock,
        *,
        posting_enabled: bool,
    ) -> None:
        if type(posting_enabled) is not bool:
            raise ValueError("posting_enabled must be boolean")
        self.repository = repository
        self.validator = validator
        self.identity = identity
        self.writer = writer
        self.clock = clock
        self.posting_enabled = posting_enabled

    def _now(self):
        now = self.clock.now()
        require_utc(now)
        return now

    @staticmethod
    def _match_instruction(instruction, candidate, handoff) -> None:
        if (
            candidate is None
            or candidate.generation_id != instruction.generation_id
            or candidate.attempt_id != instruction.attempt_id
            or candidate.handoff_digest != instruction.handoff_digest
            or candidate.candidate_digest != instruction.candidate_digest
            or handoff is None
            or handoff.attempt_id != instruction.attempt_id
            or handoff.payload_digest != instruction.handoff_digest
        ):
            raise StateConflict("Captain publication evidence mismatch")

    @staticmethod
    def _match_fresh(stored, fresh) -> None:
        rebuilt = candidate_record(fresh)
        if (
            rebuilt.candidate_digest != stored.candidate_digest
            or rebuilt.content_payload() != stored.content_payload()
        ):
            raise StateConflict("fresh Captain candidate differs from accepted evidence")

    def publish(self, instruction: PublicationInstruction) -> PublishResult:
        if not isinstance(instruction, PublicationInstruction):
            raise StateConflict("invalid Captain publication instruction")
        # This server-side gate precedes repository, OAuth and X access.
        if not self.posting_enabled:
            return PublishResult(PublishStatus.DISABLED)

        generation = self.repository.generation(instruction.generation_id)
        key = PostKey(FPLBOTTEST_USER_ID, generation.key.event_id)
        if generation.key != key:
            raise StateConflict("Captain destination is not FPLBotTest")
        attempt = self.repository.generation_acquisition(instruction.generation_id)
        candidate = self.repository.candidate(instruction.generation_id)
        handoff = attempt.handoff if attempt is not None else None
        self._match_instruction(instruction, candidate, handoff)

        posting = self.repository.posting(key)
        if any(value.status == PostingStatus.SUCCEEDED for value in posting.attempts):
            succeeded = next(
                value for value in posting.attempts if value.status == PostingStatus.SUCCEEDED
            )
            return PublishResult(PublishStatus.ALREADY_SUCCEEDED, key.event_id, succeeded.post_id)

        try:
            fresh = self.validator.validate(key, handoff, allowed_claim_id=instruction.claim_id)
            self._match_fresh(candidate, fresh)
            claim = self.repository.claim_post(
                instruction.generation_id, instruction.claim_id, self._now()
            ).record
            if claim.status not in {PostingStatus.CLAIMED, PostingStatus.WRITE_STARTED}:
                raise StateConflict("Captain posting claim is not writable")

            # Re-fetch official data and state after the atomic claim. Only this
            # claim may pass the posting barrier during final validation.
            fresh = self.validator.validate(key, handoff, allowed_claim_id=instruction.claim_id)
            self._match_fresh(candidate, fresh)
            authenticated = self.identity.get_authenticated_user()
            if authenticated.user_id != FPLBOTTEST_USER_ID:
                raise XIdentityMismatchError("Captain publisher X identity mismatch")
            write = self.repository.start_write(key, instruction.claim_id, self._now())
            if not write.applied:
                return PublishResult(PublishStatus.REJECTED, key.event_id)
        except (CandidateRejected, StateConflict, XApiError):
            current = self.repository.posting(key)
            if (
                current.attempts
                and current.attempts[-1].claim_id == instruction.claim_id
                and current.attempts[-1].status == PostingStatus.CLAIMED
            ):
                self.repository.finish_post(
                    key,
                    instruction.claim_id,
                    PostingStatus.FAILED_BEFORE_WRITE,
                    self._now(),
                )
            return PublishResult(PublishStatus.REJECTED, key.event_id)

        try:
            created = self.writer.create_text_post(fresh.tweet)
        except Exception:
            # Once WRITE_STARTED is durable, even an apparently definite provider
            # error is retained as uncertain unless exact provider evidence later
            # proves the outcome. Never automatically retry.
            self.repository.finish_post(
                key, instruction.claim_id, PostingStatus.UNCERTAIN, self._now()
            )
            return PublishResult(PublishStatus.UNCERTAIN, key.event_id)

        self.repository.finish_post(
            key,
            instruction.claim_id,
            PostingStatus.SUCCEEDED,
            self._now(),
            created.post_id,
        )
        return PublishResult(PublishStatus.SUCCEEDED, key.event_id, created.post_id)

    def reconcile_exact_post(
        self, instruction: PublicationInstruction, post_id: str
    ) -> PublishResult:
        """Promote only externally proven exact evidence; discovery is deliberately separate."""

        generation = self.repository.generation(instruction.generation_id)
        key = PostKey(FPLBOTTEST_USER_ID, generation.key.event_id)
        result = self.repository.finish_post(
            key, instruction.claim_id, PostingStatus.SUCCEEDED, self._now(), post_id
        )
        return PublishResult(PublishStatus.SUCCEEDED, key.event_id, result.record.post_id)
