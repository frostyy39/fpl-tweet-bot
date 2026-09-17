"""Durable VM-operation contract and local reference ledger; no VM API calls."""

from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import UUID

from fpl_bot.captain_state import Mutation, StateConflict, VmPhase, VmUseLease, identity


class VmAction(StrEnum):
    START = "start"
    STOP = "stop"


class OperationPhase(StrEnum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class VmOperation:
    lease_id: UUID
    generation_id: UUID
    action: VmAction
    phase: OperationPhase = OperationPhase.REQUESTED
    terminated_confirmed: bool = False
    provider_operation_id: str | None = None

    def __post_init__(self) -> None:
        identity(self.lease_id)
        identity(self.generation_id)
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


class InMemoryVmOperations:
    """Non-durable test implementation. Methods are linearizable and retry safe."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._operations: dict[tuple[UUID, VmAction], VmOperation] = {}

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
