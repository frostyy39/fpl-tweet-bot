"""Durable VM-operation contract and local reference ledger; no VM API calls."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import UUID

from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_state import Mutation, StateConflict, VmPhase, VmUseLease, identity


class VmAction(StrEnum):
    START = "start"
    STOP = "stop"


class OperationPhase(StrEnum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class DispatchStatus(StrEnum):
    RESERVED = "reserved"
    ACKNOWLEDGED = "acknowledged"
    AMBIGUOUS = "ambiguous"
    COMPLETED = "completed"
    FAILED = "failed"


class DispatchPending(StateConflict):
    """An unresolved opposite operation must settle before dispatch."""


@dataclass(frozen=True, slots=True)
class VmDispatchReservation:
    reservation_id: UUID
    request_id: UUID
    lease_id: UUID
    generation_id: UUID
    action: VmAction
    reserved_at: datetime
    updated_at: datetime
    status: DispatchStatus = DispatchStatus.RESERVED
    provider_operation_id: str | None = None
    instance_id: str | None = None
    instance_status: str | None = None
    failure_category: str | None = None

    def __post_init__(self):
        for value in (self.reservation_id, self.request_id, self.lease_id, self.generation_id):
            identity(value)
        require_utc(self.reserved_at)
        require_utc(self.updated_at)
        if self.updated_at < self.reserved_at or not isinstance(self.action, VmAction):
            raise StateConflict("invalid dispatch chronology/type")
        if not isinstance(self.status, DispatchStatus):
            raise StateConflict("invalid dispatch status")
        if self.provider_operation_id is not None and (
            not isinstance(self.provider_operation_id, str)
            or not self.provider_operation_id
            or len(self.provider_operation_id) > 256
        ):
            raise StateConflict("invalid dispatch provider identity")
        if self.instance_id is not None and (
            not isinstance(self.instance_id, str)
            or not self.instance_id
            or len(self.instance_id) > 64
        ):
            raise StateConflict("invalid dispatch instance identity")
        if self.instance_status is not None and (
            not isinstance(self.instance_status, str)
            or not self.instance_status
            or len(self.instance_status) > 32
        ):
            raise StateConflict("invalid dispatch instance status")
        if self.failure_category not in {
            None,
            "provider_rejected",
            "provider_ambiguous",
            "provider_failed",
        }:
            raise StateConflict("invalid dispatch failure category")
        if (
            self.status
            in {DispatchStatus.RESERVED, DispatchStatus.ACKNOWLEDGED, DispatchStatus.COMPLETED}
            and self.failure_category is not None
        ):
            raise StateConflict("inconsistent dispatch evidence")
        if self.status == DispatchStatus.ACKNOWLEDGED and self.provider_operation_id is None:
            raise StateConflict("acknowledged dispatch lacks provider identity")
        if (
            self.status == DispatchStatus.AMBIGUOUS
            and self.failure_category != "provider_ambiguous"
        ):
            raise StateConflict("ambiguous dispatch lacks classification")
        if self.status == DispatchStatus.FAILED and self.failure_category not in {
            "provider_rejected",
            "provider_failed",
        }:
            raise StateConflict("failed dispatch lacks classification")
        if self.status == DispatchStatus.COMPLETED:
            expected = "RUNNING" if self.action == VmAction.START else "TERMINATED"
            if self.instance_id is None or self.instance_status != expected:
                raise StateConflict("completed dispatch lacks terminal instance evidence")


@dataclass(frozen=True, slots=True)
class VmOperation:
    lease_id: UUID
    generation_id: UUID
    action: VmAction
    phase: OperationPhase = OperationPhase.REQUESTED
    terminated_confirmed: bool = False
    provider_operation_id: str | None = None
    dispatch_protocol: int = 1
    dispatch: VmDispatchReservation | None = None

    def __post_init__(self) -> None:
        identity(self.lease_id)
        identity(self.generation_id)
        if type(self.dispatch_protocol) is not int or self.dispatch_protocol not in {0, 1}:
            raise StateConflict("invalid dispatch protocol")
        if self.dispatch is not None:
            d = self.dispatch
            if not isinstance(d, VmDispatchReservation) or self.dispatch_protocol != 1:
                raise StateConflict("invalid dispatch record")
            if (d.lease_id, d.generation_id, d.action) != (
                self.lease_id,
                self.generation_id,
                self.action,
            ):
                raise StateConflict("dispatch operation mismatch")
            phases = {
                DispatchStatus.RESERVED: OperationPhase.REQUESTED,
                DispatchStatus.AMBIGUOUS: OperationPhase.REQUESTED,
                DispatchStatus.ACKNOWLEDGED: OperationPhase.IN_PROGRESS,
                DispatchStatus.COMPLETED: OperationPhase.COMPLETED,
                DispatchStatus.FAILED: OperationPhase.FAILED,
            }
            if (
                self.phase != phases[d.status]
                or self.provider_operation_id != d.provider_operation_id
            ):
                raise StateConflict("inconsistent operation/dispatch status")
        if (
            not isinstance(self.action, VmAction)
            or not isinstance(self.phase, OperationPhase)
            or type(self.terminated_confirmed) is not bool
            or (
                self.provider_operation_id is not None
                and (
                    not isinstance(self.provider_operation_id, str)
                    or not self.provider_operation_id
                    or len(self.provider_operation_id) > 256
                )
            )
        ):
            raise StateConflict("invalid VM operation record")
        if self.terminated_confirmed != (
            self.action == VmAction.STOP and self.phase == OperationPhase.COMPLETED
        ):
            raise StateConflict("inconsistent VM termination evidence")

    @property
    def identity(self) -> str:
        return f"captain-vm-{self.lease_id}-{self.action.value}"


class VmOperationRepository(Protocol):
    """Atomic per-lease ledger. STOP permanently seals the lease against later START.

    A durable implementation must retain operation identities after cleanup. An
    ambiguous provider response is NOT FAILED: retain REQUESTED/IN_PROGRESS and
    reconcile the same operation. No provider adapter is implemented here.
    """

    def request(self, lease: VmUseLease, action: VmAction) -> Mutation[VmOperation]: ...
    def get(self, lease_id: UUID, action: VmAction) -> VmOperation | None: ...
    def acknowledge(
        self, lease_id: UUID, action: VmAction, provider_operation_id: str | None = None
    ) -> Mutation[VmOperation]: ...
    def finish(
        self, lease_id: UUID, action: VmAction, *, succeeded: bool, terminated: bool = False
    ) -> Mutation[VmOperation]: ...
    def stop_is_settled(self, lease_id: UUID) -> bool: ...
    def reserve_dispatch(
        self,
        lease_id: UUID,
        generation_id: UUID,
        action: VmAction,
        reservation_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> Mutation[VmOperation]: ...
    def record_dispatch(
        self,
        lease_id: UUID,
        action: VmAction,
        reservation_id: UUID,
        status: DispatchStatus,
        now: datetime,
        provider_operation_id: str | None = None,
        instance_id: str | None = None,
        instance_status: str | None = None,
        failure_category: str | None = None,
        terminated: bool = False,
    ) -> Mutation[VmOperation]: ...


class InMemoryVmOperations:
    """Non-durable test implementation. Methods are linearizable and retry safe."""

    def __init__(self, repository=None) -> None:
        self._lock = RLock()
        self._repository = repository
        self._operations: dict[tuple[UUID, VmAction], VmOperation] = {}

    def reserve_dispatch(self, lease_id, generation_id, action, reservation_id, request_id, now):
        """Atomic ownership + opposite operation + reservation CAS; no expiry/takeover."""
        if self._repository is None:
            raise StateConflict("dispatch requires shared ownership repository")
        with self._repository._lock, self._lock:
            for value in (lease_id, generation_id, reservation_id, request_id):
                identity(value)
            require_utc(now)
            lease = self._repository.vm_use()
            if lease is None or (lease.lease_id, lease.generation_id) != (lease_id, generation_id):
                raise StateConflict("stale dispatch VM owner")
            old = self._require(lease_id, action)
            if old.generation_id != generation_id:
                raise StateConflict("dispatch generation mismatch")
            # Once reserved, the same logical operation must remain
            # reconcilable while cleanup sequencing changes around it.
            if old.dispatch is not None:
                return Mutation(old, False)
            if action == VmAction.START:
                self._repository._live(generation_id)
                if lease.phase != VmPhase.IN_USE or self.get(lease_id, VmAction.STOP) is not None:
                    raise StateConflict("start fenced by cleanup")
            elif lease.phase != VmPhase.STOPPING:
                raise StateConflict("stop requires cleanup fence")
            opposite = self.get(
                lease_id, VmAction.STOP if action == VmAction.START else VmAction.START
            )
            if opposite is not None and opposite.phase not in {
                OperationPhase.COMPLETED,
                OperationPhase.FAILED,
            }:
                raise DispatchPending("opposite VM operation remains unresolved")
            if old.dispatch_protocol != 1 or old.phase != OperationPhase.REQUESTED:
                raise StateConflict("legacy dispatch requires operator reconciliation")
            dispatch = VmDispatchReservation(
                reservation_id, request_id, lease_id, generation_id, action, now, now
            )
            result = replace(old, dispatch=dispatch)
            self._operations[lease_id, action] = result
            return Mutation(result, True)

    def record_dispatch(
        self,
        lease_id,
        action,
        reservation_id,
        status,
        now,
        provider_operation_id=None,
        instance_id=None,
        instance_status=None,
        failure_category=None,
        terminated=False,
    ):
        with self._lock:
            old = self._require(lease_id, action)
            d = old.dispatch
            if d is None or d.reservation_id != reservation_id:
                raise StateConflict("unknown dispatch reservation")
            require_utc(now)
            if now < d.updated_at:
                raise StateConflict("dispatch evidence precedes state")
            if d.status in {DispatchStatus.COMPLETED, DispatchStatus.FAILED}:
                if (
                    status != d.status
                    or provider_operation_id != d.provider_operation_id
                    or instance_id != d.instance_id
                    or instance_status != d.instance_status
                    or failure_category != d.failure_category
                    or terminated != old.terminated_confirmed
                ):
                    raise StateConflict("terminal dispatch evidence conflict")
                return Mutation(old, False)
            if (
                status == d.status
                and provider_operation_id == d.provider_operation_id
                and instance_id == d.instance_id
                and instance_status == d.instance_status
                and failure_category == d.failure_category
                and not terminated
            ):
                return Mutation(old, False)
            if d.status == DispatchStatus.ACKNOWLEDGED and status in {
                DispatchStatus.RESERVED,
                DispatchStatus.AMBIGUOUS,
            }:
                return Mutation(old, False)
            if status == DispatchStatus.RESERVED:
                raise StateConflict("dispatch reservation cannot be reset")
            if (
                d.provider_operation_id is not None
                and provider_operation_id != d.provider_operation_id
            ):
                raise StateConflict("dispatch provider identity conflict")
            phases = {
                DispatchStatus.ACKNOWLEDGED: OperationPhase.IN_PROGRESS,
                DispatchStatus.AMBIGUOUS: OperationPhase.REQUESTED,
                DispatchStatus.COMPLETED: OperationPhase.COMPLETED,
                DispatchStatus.FAILED: OperationPhase.FAILED,
            }
            if status not in phases:
                raise StateConflict("invalid dispatch outcome")
            if terminated != (action == VmAction.STOP and status == DispatchStatus.COMPLETED):
                raise StateConflict("dispatch lacks consistent termination evidence")
            result = replace(
                old,
                phase=phases[status],
                provider_operation_id=provider_operation_id,
                terminated_confirmed=terminated,
                dispatch=replace(
                    d,
                    status=status,
                    updated_at=now,
                    provider_operation_id=provider_operation_id,
                    instance_id=instance_id,
                    instance_status=instance_status,
                    failure_category=failure_category,
                ),
            )
            self._operations[lease_id, action] = result
            return Mutation(result, True)

    def request(self, lease: VmUseLease, action: VmAction) -> Mutation[VmOperation]:
        with self._lock:
            if not isinstance(lease, VmUseLease) or not isinstance(action, VmAction):
                raise StateConflict("invalid VM operation")
            key = (lease.lease_id, action)
            existing = self._operations.get(key)
            if existing is not None:
                if existing.generation_id != lease.generation_id:
                    raise StateConflict("VM operation identity conflict")
                return Mutation(existing, False)
            other = self._operations.get((lease.lease_id, VmAction.START))
            if other is not None and other.generation_id != lease.generation_id:
                raise StateConflict("VM lease identity conflict")
            if action == VmAction.START:
                if (
                    lease.phase != VmPhase.IN_USE
                    or (lease.lease_id, VmAction.STOP) in self._operations
                ):
                    raise StateConflict("VM start fenced by cleanup")
            elif lease.phase != VmPhase.STOPPING:
                raise StateConflict("VM stop requires cleanup fence")
            result = VmOperation(lease.lease_id, lease.generation_id, action)
            self._operations[key] = result
            return Mutation(result, True)

    def get(self, lease_id: UUID, action: VmAction) -> VmOperation | None:
        with self._lock:
            return self._operations.get((lease_id, action))

    def _require(self, lease_id: UUID, action: VmAction) -> VmOperation:
        identity(lease_id)
        if not isinstance(action, VmAction):
            raise StateConflict("invalid VM action")
        operation = self._operations.get((lease_id, action))
        if operation is None:
            raise StateConflict("unknown VM operation")
        return operation

    def _start_settled(self, lease_id: UUID) -> bool:
        start = self._operations.get((lease_id, VmAction.START))
        return start is None or start.phase in {OperationPhase.COMPLETED, OperationPhase.FAILED}

    def acknowledge(
        self, lease_id: UUID, action: VmAction, provider_operation_id: str | None = None
    ) -> Mutation[VmOperation]:
        with self._lock:
            old = self._require(lease_id, action)
            if old.phase != OperationPhase.REQUESTED:
                return Mutation(old, False)
            if action == VmAction.STOP and not self._start_settled(lease_id):
                raise StateConflict("VM start still outstanding")
            if provider_operation_id is not None and (
                not isinstance(provider_operation_id, str)
                or not provider_operation_id
                or len(provider_operation_id) > 256
            ):
                raise StateConflict("invalid provider operation identity")
            result = replace(
                old,
                phase=OperationPhase.IN_PROGRESS,
                provider_operation_id=provider_operation_id,
            )
            self._operations[lease_id, action] = result
            return Mutation(result, True)

    def finish(
        self, lease_id: UUID, action: VmAction, *, succeeded: bool, terminated: bool = False
    ) -> Mutation[VmOperation]:
        with self._lock:
            old = self._require(lease_id, action)
            if type(succeeded) is not bool or type(terminated) is not bool:
                raise StateConflict("invalid operation outcome")
            if terminated and (action != VmAction.STOP or not succeeded):
                raise StateConflict("unexpected termination evidence")
            if action == VmAction.STOP and succeeded and not terminated:
                raise StateConflict("stop completion requires termination evidence")
            phase = OperationPhase.COMPLETED if succeeded else OperationPhase.FAILED
            if old.phase == phase and old.terminated_confirmed == terminated:
                return Mutation(old, False)
            if old.phase != OperationPhase.IN_PROGRESS:
                raise StateConflict("VM operation is not in progress")
            result = replace(old, phase=phase, terminated_confirmed=terminated)
            self._operations[lease_id, action] = result
            return Mutation(result, True)

    def stop_is_settled(self, lease_id: UUID) -> bool:
        with self._lock:
            stop = self._operations.get((lease_id, VmAction.STOP))
            return bool(
                stop
                and stop.phase == OperationPhase.COMPLETED
                and stop.terminated_confirmed
                and self._start_settled(lease_id)
            )
