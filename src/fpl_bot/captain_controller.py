"""Provider-independent Captain planning/delivery. Injected ports only; no live clients."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid5

from fpl_bot.captain_handoff import AuthenticationStatus, CaptainAssignment, ProjectionHandoff
from fpl_bot.captain_orchestration_timing import CaptainTiming, require_utc, utc_text
from fpl_bot.captain_repository import CaptainRepository
from fpl_bot.captain_state import (
    Generation,
    GenerationStatus,
    IntentStatus,
    PostingStatus,
    PostKey,
    StateConflict,
    TaskIntent,
    TaskKind,
    VmPhase,
)
from fpl_bot.captain_vm_operations import OperationPhase, VmAction, VmOperationRepository
from fpl_bot.classification import classify_fixtures, render_event_code
from fpl_bot.errors import DataValidationError, NoSuitableEventError
from fpl_bot.events import parse_events, select_next_event
from fpl_bot.models import FplEvent, Team
from fpl_bot.parsing import parse_fixtures, parse_teams

# Stable application namespace, not a secret or a cloud resource identity.
NAMESPACE = UUID("4b2645c2-d08d-4d01-9340-d4e9e1fb7e89")
ACTIVE = frozenset(
    {
        GenerationStatus.PLANNED,
        GenerationStatus.WARMING,
        GenerationStatus.READY,
        GenerationStatus.RELEASED,
        GenerationStatus.ACQUIRING,
    }
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class OfficialFplSource(Protocol):
    """Each call must fetch uncached authoritative data; tests supply fixtures only."""

    def fetch_bootstrap_static(self) -> Mapping: ...
    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping]: ...


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    destination_user_id: str
    assignment: CaptainAssignment
    kind: TaskKind
    scheduled_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.assignment, CaptainAssignment) or not isinstance(
            self.kind, TaskKind
        ):
            raise StateConflict("invalid Captain task contract")
        PostKey(self.destination_user_id, self.assignment.event_id)
        require_utc(self.scheduled_at)

    @property
    def identity(self) -> str:
        return f"captain-{self.assignment.generation_id}-{self.kind.value}"

    @property
    def digest(self) -> str:
        payload = {
            "destination_user_id": self.destination_user_id,
            "assignment": self.assignment.to_payload(),
            "kind": self.kind.value,
            "scheduled_at": utc_text(self.scheduled_at),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskConfirmation:
    identity: str
    digest: str


class TaskScheduler(Protocol):
    def ensure(self, task: TaskEnvelope) -> TaskConfirmation:
        """Create or confirm exact immutable task. Different payload under same name conflicts."""
        ...


@dataclass(frozen=True, slots=True)
class PlanResult:
    generation: Generation
    created: bool
    posting_suppressed: bool
    session_warning: bool


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    retry_at: datetime | None = None


class CaptainController:
    def __init__(
        self,
        repository: CaptainRepository,
        source: OfficialFplSource,
        clock: Clock,
        vm_operations: VmOperationRepository,
    ) -> None:
        self.repository = repository
        self.source = source
        self.clock = clock
        self.vm_operations = vm_operations

    def _now(self) -> datetime:
        now = self.clock.now()
        require_utc(now)
        return now

    def _snapshot(self) -> tuple[tuple[FplEvent, ...], tuple[Team, ...]]:
        payload = self.source.fetch_bootstrap_static()
        if not isinstance(payload, Mapping) or not {"events", "teams"} <= payload.keys():
            raise DataValidationError("missing Captain planning data")
        events, teams = parse_events(payload["events"]), parse_teams(payload["teams"])
        return events, teams

    def _code(self, event: FplEvent, teams: tuple[Team, ...]) -> str:
        fixtures = parse_fixtures(
            list(self.source.fetch_event_fixtures(event.event_id)), event.event_id
        )
        return render_event_code(event.event_id, classify_fixtures(teams, fixtures).kind)

    def _suppressed(self, key: PostKey) -> bool:
        attempts = self.repository.posting(key).attempts
        return bool(attempts and attempts[-1].status != PostingStatus.FAILED_BEFORE_WRITE)

    def _warning(self, generation: Generation) -> bool:
        evidence = self.repository.session_health(generation.assignment.generation_id)
        if not evidence:
            return False  # Unknown, not a claim that the session is healthy.
        last = evidence[-1]
        target = last.next_target or generation.assignment.timing.target_utc
        return (
            last.manual_reauthentication_required
            or last.authentication != AuthenticationStatus.AUTHENTICATED
            or (last.expires_at is not None and last.expires_at <= target)
        )

    def _next_window(self, events: tuple[FplEvent, ...], now: datetime) -> FplEvent | None:
        """Validate chronology before advancing beyond already-expired Captain windows."""
        remaining = events
        while remaining:
            try:
                event = select_next_event(remaining, now)
            except NoSuitableEventError:
                return None
            if now <= CaptainTiming(event.deadline_utc).expiry_utc:
                return event
            remaining = tuple(e for e in remaining if e.event_id != event.event_id)
        return None

    def plan(self, destination_user_id: str) -> tuple[PlanResult, ...]:
        """Event-based rolling horizon: nearest deadline, plus next live Captain window if missed.

        Also reconcile changed deadlines of already-known events in this bootstrap.
        No deadline-day filter and no default extra session-health tasks.
        """
        events, teams = self._snapshot()
        now = self._now()
        try:
            nearest = select_next_event(events, now)
        except NoSuitableEventError:
            nearest = None
        selected = {nearest.event_id: nearest} if nearest else {}
        following = self._next_window(events, now)
        if following:
            selected[following.event_id] = following
        observed = {}
        for event in events:
            old = self.repository.current(PostKey(destination_user_id, event.event_id))
            observed[event.event_id] = old
            if old and old.assignment.timing.deadline_utc != event.deadline_utc:
                selected[event.event_id] = event
        # Validate all selected classification inputs before changing any state.
        candidates = [
            (event, self._code(event, teams))
            for event in sorted(selected.values(), key=lambda e: (e.deadline_utc, e.event_id))
        ]
        results = []
        for event, code in candidates:
            key = PostKey(destination_user_id, event.event_id)
            # Retain the pre-classification CAS snapshot. Never silently replace a
            # concurrent planner's newer generation using this older FPL observation.
            old = observed[event.event_id]
            current = self.repository.current(key)
            previous = old.assignment.generation_id if old else None
            if (current.assignment.generation_id if current else None) != previous:
                raise StateConflict("planner snapshot changed; fetch FPL again")
            timing = CaptainTiming(event.deadline_utc)
            changed = (
                old is None or old.assignment.timing != timing or old.assignment.event_code != code
            )
            created = False
            if changed:
                seed = (
                    f"{key.destination_user_id}:{event.event_id}:{code}:"
                    f"{utc_text(event.deadline_utc)}:{previous}"
                )
                gid = uuid5(NAMESPACE, seed)
                assignment = CaptainAssignment(
                    uuid5(gid, "assignment"), gid, event.event_id, code, timing
                )
                mutation = self.repository.plan(key, assignment, previous, self._now())
                generation, created = mutation.record, mutation.applied
            else:
                generation = old
            now = self._now()
            if now > timing.expiry_utc and generation.status in ACTIVE:
                generation = self.repository.transition(
                    generation.assignment.generation_id,
                    generation.status,
                    GenerationStatus.MISSED,
                    now,
                ).record
            results.append(
                PlanResult(generation, created, self._suppressed(key), self._warning(generation))
            )
        return tuple(results)

    def envelope(self, intent: TaskIntent) -> TaskEnvelope:
        generation = self.repository.generation(intent.generation_id)
        return TaskEnvelope(
            generation.key.destination_user_id,
            generation.assignment,
            intent.kind,
            intent.scheduled_at,
        )

    def _current_task(self, task: TaskEnvelope) -> Generation:
        generation = self.repository.generation(task.assignment.generation_id)
        current = self.repository.current(generation.key)
        intent = self.repository.task_intent(task.assignment.generation_id, task.kind)
        if (
            current is None
            or current.assignment != task.assignment
            or self.envelope(intent) != task
            or intent.status == IntentStatus.CANCELLED
        ):
            raise StateConflict("stale or conflicting Captain task")
        return current

    def reconcile_tasks(self, scheduler: TaskScheduler) -> tuple[str, ...]:
        """Exceptions leave unacknowledged intents durable; retry confirms the same task."""
        confirmed = []
        for intent in self.repository.pending_intents():
            task = self.envelope(intent)
            generation = self._current_task(task)
            if task.kind != TaskKind.CLEANUP and (
                self._suppressed(generation.key)
                or generation.status not in ACTIVE | {GenerationStatus.ACCEPTED}
            ):
                continue
            receipt = scheduler.ensure(task)
            if receipt != TaskConfirmation(task.identity, task.digest):
                raise StateConflict("external task confirmation conflict")
            # A concurrently superseded task can exist externally; this check/ACK refuses it.
            self._current_task(task)
            self.repository.acknowledge_intent(intent.generation_id, intent.kind)
            confirmed.append(task.identity)
        return tuple(confirmed)

    def _fresh_match(self, generation: Generation) -> None:
        events, teams = self._snapshot()
        event = self._next_window(events, self._now())
        if (
            event is None
            or event.event_id != generation.assignment.event_id
            or event.deadline_utc != generation.assignment.timing.deadline_utc
            or self._code(event, teams) != generation.assignment.event_code
        ):
            raise StateConflict("fresh FPL does not match Captain assignment")

    def deliver(self, task: TaskEnvelope) -> DeliveryResult:
        """Guarded delivery; never launches acquisition or issues an X/VM request."""
        generation = self._current_task(task)
        timing, gid = generation.assignment.timing, generation.assignment.generation_id
        now = self._now()
        if task.kind == TaskKind.CLEANUP:
            # L is inclusive for posting; watchdog is allowed strictly after L.
            if now <= timing.expiry_utc:
                return DeliveryResult("early", timing.expiry_utc + timedelta(microseconds=1))
            lease = self.repository.vm_use()
            if lease is None or lease.generation_id != gid:
                return DeliveryResult("no_owned_vm")
            self.request_cleanup(lease.lease_id, gid)
            return DeliveryResult("cleanup_requested")
        if self._suppressed(generation.key):
            return DeliveryResult("posting_suppressed")
        if now > timing.expiry_utc:
            if generation.status in ACTIVE:
                self.repository.transition(gid, generation.status, GenerationStatus.MISSED, now)
            return DeliveryResult("missed")
        if generation.status not in ACTIVE | {GenerationStatus.ACCEPTED}:
            return DeliveryResult("terminal")
        self._fresh_match(generation)
        # Time/FPL calls may have consumed the window. Re-enter with no external effects.
        now = self._now()
        generation = self._current_task(task)
        if self._suppressed(generation.key):
            return DeliveryResult("posting_suppressed")
        if now > timing.expiry_utc:
            if generation.status in ACTIVE:
                self.repository.transition(gid, generation.status, GenerationStatus.MISSED, now)
            return DeliveryResult("missed")
        if task.kind == TaskKind.WARMUP:
            if now < timing.warmup_utc:
                return DeliveryResult("early", timing.warmup_utc)
            if now >= timing.target_utc:
                return DeliveryResult("warmup_window_closed")
            if generation.status == GenerationStatus.PLANNED:
                self.repository.transition(
                    gid, GenerationStatus.PLANNED, GenerationStatus.WARMING, now
                )
            elif generation.status != GenerationStatus.WARMING:
                return DeliveryResult("already_progressed")
            lease = self.repository.acquire_vm(gid, uuid5(gid, "vm-use"), now).record
            self.vm_operations.request(lease, VmAction.START)
            return DeliveryResult("start_requested")
        if now < timing.release_utc:
            return DeliveryResult("early", timing.release_utc)
        if task.kind == TaskKind.RELEASE:
            if generation.status == GenerationStatus.READY:
                self.repository.transition(
                    gid, GenerationStatus.READY, GenerationStatus.RELEASED, now
                )
                return DeliveryResult("released")
            return DeliveryResult(
                "already_released"
                if generation.status
                in {
                    GenerationStatus.RELEASED,
                    GenerationStatus.ACQUIRING,
                    GenerationStatus.ACCEPTED,
                }
                else "not_ready"
            )
        attempt = self.repository.generation_acquisition(gid)
        if attempt is None or attempt.handoff is None:
            raise StateConflict("publish lacks accepted handoff")
        attempt.handoff.require_eligible(generation.assignment, now)
        return DeliveryResult("publish_eligible_no_write")

    def confirm_runtime_ready(self, generation_id: UUID) -> None:
        """Future trusted runtime preflight callback; not a posting dataset or auth assertion."""
        lease = self.repository.vm_use()
        if lease is None or lease.generation_id != generation_id or lease.phase != VmPhase.IN_USE:
            raise StateConflict("runtime does not own VM")
        start = self.vm_operations.get(lease.lease_id, VmAction.START)
        if start is None or start.phase != OperationPhase.COMPLETED:
            raise StateConflict("VM start not complete")
        self._fresh_match(self.repository.generation(generation_id))
        self.repository.transition(
            generation_id, GenerationStatus.WARMING, GenerationStatus.READY, self._now()
        )

    def accept_handoff(self, handoff: ProjectionHandoff) -> None:
        generation = self.repository.generation(handoff.assignment.generation_id)
        self._fresh_match(generation)
        self.repository.accept(handoff, self._now())

    def request_cleanup(self, lease_id: UUID, generation_id: UUID) -> None:
        """Also repairs obsolete owners whose task intents were cancelled on replanning."""
        lease = self.repository.begin_cleanup(lease_id, generation_id).record
        self.vm_operations.request(lease, VmAction.STOP)

    def complete_cleanup(self, lease_id: UUID, generation_id: UUID) -> None:
        if not self.vm_operations.stop_is_settled(lease_id):
            raise StateConflict("external VM operations are not settled")
        self.repository.complete_cleanup(lease_id, generation_id)

    def reconcile_vm(self) -> str:
        """Repair durable lease/operation gaps only. Never submit an external VM operation.

        In particular, supersession cancels old task intents but cannot strand the
        old VM owner: the lease remains the durable recovery source of truth.
        """
        lease = self.repository.vm_use()
        if lease is None:
            return "idle"
        generation = self.repository.generation(lease.generation_id)
        current = self.repository.current(generation.key)
        start = self.vm_operations.get(lease.lease_id, VmAction.START)
        must_stop = (
            lease.phase == VmPhase.STOPPING
            or current is None
            or current.assignment.generation_id != lease.generation_id
            or generation.status not in ACTIVE
            or self._now() > generation.assignment.timing.expiry_utc
            or (start is not None and start.phase == OperationPhase.FAILED)
        )
        if must_stop:
            self.request_cleanup(lease.lease_id, lease.generation_id)
            if self.vm_operations.stop_is_settled(lease.lease_id):
                self.complete_cleanup(lease.lease_id, lease.generation_id)
                return "released"
            return "stop_pending"
        if start is None:
            # Recovery can occur after T, but must not start a new warmup then.
            if self._now() >= generation.assignment.timing.target_utc:
                self.request_cleanup(lease.lease_id, lease.generation_id)
                return "stop_pending"
            self.vm_operations.request(lease, VmAction.START)
        return "start_recorded"
