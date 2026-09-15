"""Thread-safe reference repository for local contract tests, NOT durable production storage."""

from dataclasses import replace
from datetime import datetime
from functools import wraps
from threading import RLock
from uuid import UUID

from fpl_bot.captain_handoff import CaptainAssignment, ProjectionHandoff
from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_state import (
    AcquisitionAttempt,
    AttemptStatus,
    Generation,
    IntentStatus,
    Mutation,
    PostingAttempt,
    PostingRecord,
    PostKey,
    SessionHealthEvidence,
    StateConflict,
    TaskIntent,
    TaskKind,
    VmPhase,
    VmUseLease,
    identity,
)
from fpl_bot.captain_state import (
    GenerationStatus as G,
)
from fpl_bot.captain_state import (
    PostingStatus as P,
)


def atomic(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


def chronological(now: datetime, previous: datetime) -> None:
    require_utc(now)
    if now < previous:
        raise StateConflict("operation precedes recorded state")


class InMemoryCaptainRepository:
    """One instance models one Captain worker VM and its Captain-only state namespace."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._generations: dict[UUID, Generation] = {}
        self._current: dict[PostKey, UUID] = {}
        self._attempts: dict[UUID, AcquisitionAttempt] = {}
        self._generation_attempt: dict[UUID, UUID] = {}
        self._posts: dict[PostKey, PostingRecord] = {}
        self._intents: dict[tuple[UUID, TaskKind], TaskIntent] = {}
        self._vm: VmUseLease | None = None
        self._retired_vm: dict[UUID, VmUseLease] = {}
        self._health: dict[UUID, tuple[SessionHealthEvidence, ...]] = {}

    def _get(self, generation_id: UUID) -> Generation:
        try:
            return self._generations[generation_id]
        except KeyError:
            raise StateConflict("unknown generation") from None

    def _live(self, generation_id: UUID) -> Generation:
        generation = self._get(generation_id)
        if self._current[generation.key] != generation_id:
            raise StateConflict("stale generation")
        return generation

    def _set_status(self, generation: Generation, status: G, now: datetime) -> Generation:
        result = replace(generation, history=(*generation.history, (status, now)))
        self._generations[generation.assignment.generation_id] = result
        return result

    @atomic
    def plan(
        self,
        key: PostKey,
        assignment: CaptainAssignment,
        expected_current: UUID | None,
        now: datetime,
    ) -> Mutation[Generation]:
        require_utc(now)
        if not isinstance(key, PostKey) or not isinstance(assignment, CaptainAssignment):
            raise StateConflict("invalid generation contract")
        if key.event_id != assignment.event_id:
            raise StateConflict("event identity mismatch")
        gid = assignment.generation_id
        if gid in self._generations:
            old = self._generations[gid]
            if old.key != key or old.assignment != assignment:
                raise StateConflict("immutable generation conflict")
            return Mutation(old, False)
        if any(
            g.assignment.assignment_id == assignment.assignment_id
            for g in self._generations.values()
        ):
            raise StateConflict("assignment identity reused")
        current = self._current.get(key)
        if current != expected_current:
            raise StateConflict("generation compare-and-swap failed")
        previous = self._generations.get(current)
        if previous:
            chronological(now, previous.history[-1][1])
            self._set_status(previous, G.CANCELLED_STALE, now)
            aid = self._generation_attempt.get(current)
            if aid is not None and self._attempts[aid].status == AttemptStatus.ACQUIRING:
                self._attempts[aid] = replace(
                    self._attempts[aid], status=AttemptStatus.CANCELLED_STALE, updated_at=now
                )
            for intent_key, intent in tuple(self._intents.items()):
                if intent.generation_id == current:
                    self._intents[intent_key] = replace(intent, status=IntentStatus.CANCELLED)
        generation = Generation(
            key, assignment, previous.version + 1 if previous else 1, ((G.PLANNED, now),)
        )
        self._generations[gid] = generation
        self._current[key] = gid
        for kind, at in (
            (TaskKind.WARMUP, assignment.timing.warmup_utc),
            (TaskKind.RELEASE, assignment.timing.release_utc),
            (TaskKind.CLEANUP, assignment.timing.expiry_utc),
        ):
            self._intents[gid, kind] = TaskIntent(gid, kind, at)
        return Mutation(generation, True)

    @atomic
    def generation(self, generation_id: UUID) -> Generation:
        return self._get(generation_id)

    @atomic
    def current(self, key: PostKey) -> Generation | None:
        gid = self._current.get(key)
        return self._generations[gid] if gid is not None else None

    @atomic
    def transition(
        self, generation_id: UUID, expected: G, target: G, now: datetime
    ) -> Mutation[Generation]:
        generation = self._live(generation_id)
        chronological(now, generation.history[-1][1])
        if not isinstance(expected, G) or not isinstance(target, G):
            raise StateConflict("invalid generation state")
        normal = {G.PLANNED: G.WARMING, G.WARMING: G.READY, G.READY: G.RELEASED}
        failures = {G.FAILED, G.AUTHENTICATION_REQUIRED, G.MISSED}
        active = {G.PLANNED, G.WARMING, G.READY, G.RELEASED, G.ACQUIRING}
        legal = normal.get(expected) == target or (expected in active and target in failures)
        if not legal:
            raise StateConflict("illegal generation transition")
        if generation.status == target:
            return Mutation(generation, False)
        if generation.status != expected:
            raise StateConflict("generation state conflict")
        timing = generation.assignment.timing
        if target == G.MISSED:
            if now <= timing.expiry_utc:
                raise StateConflict("generation is not yet missed")
        elif target not in failures:
            if not timing.warmup_utc <= now <= timing.expiry_utc:
                raise StateConflict("outside generation activity window")
            if target == G.RELEASED and not timing.permits_new_attempt(now):
                raise StateConflict("release not permitted")
        result = self._set_status(generation, target, now)
        if target in failures:
            aid = self._generation_attempt.get(generation_id)
            if aid is not None:
                self._attempts[aid] = replace(
                    self._attempts[aid], status=AttemptStatus(target.value), updated_at=now
                )
        return Mutation(result, True)

    @atomic
    def claim_acquisition(
        self, generation_id: UUID, attempt_id: UUID, now: datetime
    ) -> Mutation[AcquisitionAttempt]:
        identity(attempt_id)
        generation = self._live(generation_id)
        chronological(now, generation.history[-1][1])
        if attempt_id in self._attempts:
            old = self._attempts[attempt_id]
            if old.generation_id != generation_id:
                raise StateConflict("attempt identity reused")
            return Mutation(old, False)
        if generation.status != G.RELEASED or generation_id in self._generation_attempt:
            raise StateConflict("acquisition already claimed or unavailable")
        if not generation.assignment.timing.permits_posting_acquisition(now):
            raise StateConflict("outside acquisition window")
        attempt = AcquisitionAttempt(attempt_id, generation_id, now, AttemptStatus.ACQUIRING, now)
        self._attempts[attempt_id] = attempt
        self._generation_attempt[generation_id] = attempt_id
        self._set_status(generation, G.ACQUIRING, now)
        return Mutation(attempt, True)

    @atomic
    def acquisition(self, attempt_id: UUID) -> AcquisitionAttempt:
        try:
            return self._attempts[attempt_id]
        except KeyError:
            raise StateConflict("unknown acquisition") from None

    @atomic
    def generation_acquisition(self, generation_id: UUID) -> AcquisitionAttempt | None:
        self._get(generation_id)
        aid = self._generation_attempt.get(generation_id)
        return self._attempts[aid] if aid is not None else None

    @atomic
    def accept(self, handoff: ProjectionHandoff, now: datetime) -> Mutation[AcquisitionAttempt]:
        if not isinstance(handoff, ProjectionHandoff):
            raise StateConflict("invalid handoff contract")
        gid = handoff.assignment.generation_id
        generation = self._live(gid)
        attempt = self.acquisition(handoff.attempt_id)
        chronological(now, attempt.updated_at)
        if attempt.generation_id != gid or generation.assignment != handoff.assignment:
            raise StateConflict("handoff assignment mismatch")
        if attempt.handoff is not None:
            if attempt.handoff.payload_digest != handoff.payload_digest:
                raise StateConflict("immutable payload conflict")
            return Mutation(attempt, False)
        if generation.status != G.ACQUIRING or attempt.status != AttemptStatus.ACQUIRING:
            raise StateConflict("acquisition not active")
        if handoff.acquisition_started_utc < attempt.claimed_at:
            raise StateConflict("acquisition predates claim")
        handoff.require_eligible(generation.assignment, now)
        result = replace(attempt, status=AttemptStatus.ACCEPTED, updated_at=now, handoff=handoff)
        self._attempts[attempt.attempt_id] = result
        self._set_status(generation, G.ACCEPTED, now)
        self._intents[gid, TaskKind.PUBLISH] = TaskIntent(gid, TaskKind.PUBLISH, now)
        return Mutation(result, True)

    @atomic
    def posting(self, key: PostKey) -> PostingRecord:
        return self._posts.get(key, PostingRecord(key))

    def _accepted(self, generation_id: UUID, now: datetime) -> tuple[Generation, ProjectionHandoff]:
        generation = self._live(generation_id)
        chronological(now, generation.history[-1][1])
        if generation.status != G.ACCEPTED:
            raise StateConflict("generation not accepted")
        handoff = self._attempts[self._generation_attempt[generation_id]].handoff
        handoff.require_eligible(generation.assignment, now)
        return generation, handoff

    @atomic
    def claim_post(
        self, generation_id: UUID, claim_id: UUID, now: datetime
    ) -> Mutation[PostingAttempt]:
        identity(claim_id)
        generation, handoff = self._accepted(generation_id, now)
        record = self.posting(generation.key)
        for prior_record in self._posts.values():
            for prior in prior_record.attempts:
                if prior.claim_id == claim_id:
                    if prior_record.key != generation.key or prior.generation_id != generation_id:
                        raise StateConflict("posting claim identity reused")
                    return Mutation(prior, False)
        if record.attempts and record.attempts[-1].status != P.FAILED_BEFORE_WRITE:
            raise StateConflict("event already claimed, uncertain or succeeded")
        if record.attempts:
            chronological(now, record.attempts[-1].history[-1][1])
        attempt = PostingAttempt(
            claim_id, generation_id, handoff.payload_digest, ((P.CLAIMED, now),)
        )
        self._posts[generation.key] = replace(record, attempts=(*record.attempts, attempt))
        return Mutation(attempt, True)

    def _post_attempt(self, key: PostKey, claim_id: UUID) -> tuple[PostingRecord, PostingAttempt]:
        record = self.posting(key)
        if not record.attempts or record.attempts[-1].claim_id != claim_id:
            raise StateConflict("posting claim is not current")
        return record, record.attempts[-1]

    def _set_post(self, record: PostingRecord, attempt: PostingAttempt) -> None:
        self._posts[record.key] = replace(record, attempts=(*record.attempts[:-1], attempt))

    @atomic
    def start_write(self, key: PostKey, claim_id: UUID, now: datetime) -> Mutation[PostingAttempt]:
        record, attempt = self._post_attempt(key, claim_id)
        chronological(now, attempt.history[-1][1])
        self._accepted(attempt.generation_id, now)
        if attempt.status == P.WRITE_STARTED:
            return Mutation(attempt, False)
        if attempt.status != P.CLAIMED:
            raise StateConflict("write cannot start")
        result = replace(attempt, history=(*attempt.history, (P.WRITE_STARTED, now)))
        self._set_post(record, result)
        return Mutation(result, True)

    @atomic
    def finish_post(
        self, key: PostKey, claim_id: UUID, outcome: P, now: datetime, post_id: str | None = None
    ) -> Mutation[PostingAttempt]:
        record, attempt = self._post_attempt(key, claim_id)
        chronological(now, attempt.history[-1][1])
        if outcome not in {P.FAILED_BEFORE_WRITE, P.UNCERTAIN, P.SUCCEEDED} or not isinstance(
            outcome, P
        ):
            raise StateConflict("invalid posting outcome")
        if outcome == P.SUCCEEDED:
            if (
                not isinstance(post_id, str)
                or not post_id.isascii()
                or not post_id.isdecimal()
                or post_id.startswith("0")
                or len(post_id) > 32
            ):
                raise StateConflict("invalid X post identity")
        elif post_id is not None:
            raise StateConflict("unexpected post identity")
        if attempt.status == outcome and attempt.post_id == post_id:
            return Mutation(attempt, False)
        legal = (attempt.status == P.CLAIMED and outcome == P.FAILED_BEFORE_WRITE) or (
            attempt.status in {P.WRITE_STARTED, P.UNCERTAIN}
            and outcome in {P.UNCERTAIN, P.SUCCEEDED}
        )
        if not legal:
            raise StateConflict("unsafe posting outcome transition")
        result = replace(attempt, history=(*attempt.history, (outcome, now)), post_id=post_id)
        self._set_post(record, result)
        return Mutation(result, True)

    @atomic
    def pending_intents(self) -> tuple[TaskIntent, ...]:
        return tuple(
            sorted(
                (i for i in self._intents.values() if i.status == IntentStatus.PENDING),
                key=lambda i: (i.scheduled_at, i.identity),
            )
        )

    @atomic
    def acknowledge_intent(self, generation_id: UUID, kind: TaskKind) -> Mutation[TaskIntent]:
        self._live(generation_id)
        if not isinstance(kind, TaskKind) or (generation_id, kind) not in self._intents:
            raise StateConflict("unknown task intent")
        intent = self._intents[generation_id, kind]
        if intent.status == IntentStatus.DISPATCHED:
            return Mutation(intent, False)
        if intent.status != IntentStatus.PENDING:
            raise StateConflict("intent is cancelled")
        result = replace(intent, status=IntentStatus.DISPATCHED)
        self._intents[generation_id, kind] = result
        return Mutation(result, True)

    @atomic
    def task_intent(self, generation_id: UUID, kind: TaskKind) -> TaskIntent:
        if not isinstance(kind, TaskKind) or (generation_id, kind) not in self._intents:
            raise StateConflict("unknown task intent")
        return self._intents[generation_id, kind]

    @atomic
    def vm_use(self) -> VmUseLease | None:
        return self._vm

    @atomic
    def acquire_vm(
        self, generation_id: UUID, lease_id: UUID, now: datetime
    ) -> Mutation[VmUseLease]:
        identity(lease_id)
        generation = self._live(generation_id)
        chronological(now, generation.history[-1][1])
        if generation.status not in {G.PLANNED, G.WARMING, G.READY, G.RELEASED, G.ACQUIRING}:
            raise StateConflict("generation cannot use VM")
        if (
            not generation.assignment.timing.warmup_utc
            <= now
            <= generation.assignment.timing.expiry_utc
        ):
            raise StateConflict("outside VM activity window")
        if self._vm is not None:
            if (
                self._vm.lease_id == lease_id
                and self._vm.generation_id == generation_id
                and self._vm.phase == VmPhase.IN_USE
            ):
                return Mutation(self._vm, False)
            raise StateConflict("VM already owned or stopping")
        if lease_id in self._retired_vm:
            raise StateConflict("retired VM lease")
        self._vm = VmUseLease(lease_id, generation_id, now)
        return Mutation(self._vm, True)

    @atomic
    def begin_cleanup(self, lease_id: UUID, generation_id: UUID) -> Mutation[VmUseLease]:
        if self._vm is None or (self._vm.lease_id, self._vm.generation_id) != (
            lease_id,
            generation_id,
        ):
            raise StateConflict("cleanup does not own VM")
        if self._vm.phase == VmPhase.STOPPING:
            return Mutation(self._vm, False)
        self._vm = replace(self._vm, phase=VmPhase.STOPPING)
        return Mutation(self._vm, True)

    @atomic
    def complete_cleanup(self, lease_id: UUID, generation_id: UUID) -> Mutation[VmUseLease]:
        retired = self._retired_vm.get(lease_id)
        if retired is not None:
            if retired.generation_id != generation_id:
                raise StateConflict("cleanup identity mismatch")
            return Mutation(retired, False)
        if (
            self._vm is None
            or (self._vm.lease_id, self._vm.generation_id) != (lease_id, generation_id)
            or self._vm.phase != VmPhase.STOPPING
        ):
            raise StateConflict("VM stop is not fenced")
        result = self._vm
        self._retired_vm[lease_id] = result
        self._vm = None
        return Mutation(result, True)

    @atomic
    def record_session_health(
        self, generation_id: UUID, evidence: SessionHealthEvidence
    ) -> Mutation[SessionHealthEvidence]:
        generation = self._live(generation_id)
        if not isinstance(evidence, SessionHealthEvidence):
            raise StateConflict("invalid session evidence contract")
        history = self._health.get(generation_id, ())
        for prior in history:
            if prior.observed_at == evidence.observed_at:
                if prior != evidence:
                    raise StateConflict("session observation conflict")
                return Mutation(prior, False)
        chronological(
            evidence.observed_at, history[-1].observed_at if history else generation.history[0][1]
        )
        self._health[generation_id] = (*history, evidence)
        return Mutation(evidence, True)

    @atomic
    def session_health(self, generation_id: UUID) -> tuple[SessionHealthEvidence, ...]:
        self._get(generation_id)
        return self._health.get(generation_id, ())
