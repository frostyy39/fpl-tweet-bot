"""Immutable Captain publisher instruction contract with no X or provider dependency."""

from dataclasses import dataclass
from uuid import UUID

from fpl_bot.captain_state import StateConflict


@dataclass(frozen=True, slots=True)
class PublicationInstruction:
    generation_id: UUID
    attempt_id: UUID
    handoff_digest: str
    candidate_digest: str
    claim_id: UUID

    def to_payload(self) -> dict[str, str | int]:
        return {
            "version": 1,
            "generation_id": str(self.generation_id),
            "attempt_id": str(self.attempt_id),
            "handoff_digest": self.handoff_digest,
            "candidate_digest": self.candidate_digest,
            "claim_id": str(self.claim_id),
        }

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
