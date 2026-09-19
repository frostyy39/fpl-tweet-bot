"""Atomic Captain-only persistence port; physical storage and IAM intentionally unspecified."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from fpl_bot.captain_handoff import CaptainAssignment, ProjectionHandoff
from fpl_bot.captain_state import (
    AcquisitionAttempt,
    Generation,
    GenerationStatus,
    Mutation,
    PostingAttempt,
    PostingRecord,
    PostingStatus,
    PostKey,
    RehearsalBinding,
    SessionHealthEvidence,
    TaskIntent,
    TaskKind,
    ValidatedCandidateRecord,
    VmUseLease,
)


class CaptainGenerationRepository(Protocol):
    """Every method is linearizable; compound writes are all-or-nothing.

    Times/IDs are caller supplied. Reads return immutable snapshots. StateConflict
    leaves all state unchanged. Implementations must never access Good Luck state.
    No operation is itself authority to call X or proof of caller authentication.
    """

    def plan(
        self,
        key: PostKey,
        assignment: CaptainAssignment,
        expected_current: UUID | None,
        now: datetime,
    ) -> Mutation[Generation]:
        """CAS current generation; atomically cancel old work and create three task intents."""
        ...

    def plan_rehearsal(
        self,
        key: PostKey,
        assignment: CaptainAssignment,
        binding: RehearsalBinding,
        expected_current: UUID | None,
        now: datetime,
    ) -> Mutation[Generation]:
        """Atomically create one generation and its permanent non-postable binding."""
        ...

    def generation(self, generation_id: UUID) -> Generation: ...
    def rehearsal(self, generation_id: UUID) -> RehearsalBinding | None: ...
    def current(self, key: PostKey) -> Generation | None: ...
    def transition(
        self,
        generation_id: UUID,
        expected: GenerationStatus,
        target: GenerationStatus,
        now: datetime,
    ) -> Mutation[Generation]: ...
    def claim_acquisition(
        self, generation_id: UUID, attempt_id: UUID, now: datetime
    ) -> Mutation[AcquisitionAttempt]: ...
    def acquisition(self, attempt_id: UUID) -> AcquisitionAttempt: ...
    def generation_acquisition(self, generation_id: UUID) -> AcquisitionAttempt | None: ...
    def accept(self, handoff: ProjectionHandoff, now: datetime) -> Mutation[AcquisitionAttempt]:
        """Atomically accept immutable payload, transition generation and enqueue publish intent."""
        ...


class CaptainPostingRepository(Protocol):
    """Event-keyed posting barrier; atomically checks generation/handoff state as well."""

    def posting(self, key: PostKey) -> PostingRecord: ...
    def candidate(self, generation_id: UUID) -> ValidatedCandidateRecord | None: ...
    def accept_candidate(
        self, candidate: ValidatedCandidateRecord, now: datetime
    ) -> Mutation[ValidatedCandidateRecord]: ...
    def claim_post(
        self, generation_id: UUID, claim_id: UUID, now: datetime
    ) -> Mutation[PostingAttempt]: ...
    def start_write(self, key: PostKey, claim_id: UUID, now: datetime) -> Mutation[PostingAttempt]:
        """Only applied=True authorizes proceeding to later guarded integration; never replay X."""
        ...

    def finish_post(
        self,
        key: PostKey,
        claim_id: UUID,
        outcome: PostingStatus,
        now: datetime,
        post_id: str | None = None,
    ) -> Mutation[PostingAttempt]: ...


class CaptainOutboxRepository(Protocol):
    """Intents are created transactionally by plan/accept, never by an unguarded put."""

    def pending_intents(self) -> tuple[TaskIntent, ...]: ...
    def task_intent(self, generation_id: UUID, kind: TaskKind) -> TaskIntent: ...
    def acknowledge_intent(self, generation_id: UUID, kind: TaskKind) -> Mutation[TaskIntent]:
        """Record external create/already-exists confirmation, NOT task execution success."""
        ...


class CaptainVmLeaseRepository(Protocol):
    """One exclusive, non-expiring use/stop fence for the dedicated worker VM."""

    def vm_use(self) -> VmUseLease | None: ...
    def acquire_vm(
        self, generation_id: UUID, lease_id: UUID, now: datetime
    ) -> Mutation[VmUseLease]: ...
    def begin_cleanup(self, lease_id: UUID, generation_id: UUID) -> Mutation[VmUseLease]:
        """Fence new owners BEFORE external stop. Retain fence across crash/uncertain stop."""
        ...

    def complete_cleanup(self, lease_id: UUID, generation_id: UUID) -> Mutation[VmUseLease]:
        """Release only after later controller independently confirms VM termination."""
        ...


class CaptainSessionHealthRepository(Protocol):
    """Append-only safe evidence, not browser inspection or posting authorization."""

    def record_session_health(
        self, generation_id: UUID, evidence: SessionHealthEvidence
    ) -> Mutation[SessionHealthEvidence]: ...
    def session_health(self, generation_id: UUID) -> tuple[SessionHealthEvidence, ...]: ...


class CaptainRepository(
    CaptainGenerationRepository,
    CaptainPostingRepository,
    CaptainOutboxRepository,
    CaptainVmLeaseRepository,
    CaptainSessionHealthRepository,
    Protocol,
):
    """Unified transactional contract; narrow consumer ports share the same atomic store."""
