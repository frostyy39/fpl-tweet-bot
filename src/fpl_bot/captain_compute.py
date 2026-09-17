"""Narrow Compute Engine adapter and durable operation reconciler."""

import re
from dataclasses import dataclass
from enum import StrEnum
from types import SimpleNamespace

from google.api_core.exceptions import (
    FailedPrecondition,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)

from fpl_bot.captain_repository import CaptainVmLeaseRepository
from fpl_bot.captain_state import StateConflict, VmPhase
from fpl_bot.captain_vm_operations import OperationPhase, VmAction, VmOperationRepository


class ProviderResult(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ComputeTarget:
    project: str
    zone: str
    instance: str

    def __post_init__(self):
        pattern = r"[a-z][a-z0-9-]{1,62}"
        if not all(
            re.fullmatch(pattern, value) for value in (self.project, self.zone, self.instance)
        ):
            raise ValueError("invalid fixed Compute target")
        if "good-luck" in self.instance.casefold():
            raise ValueError("Captain adapter cannot target Good Luck VM")


@dataclass(frozen=True, slots=True)
class InstanceEvidence:
    name: str
    instance_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ComputeOperationReceipt:
    operation_name: str | None
    result: ProviderResult


class CaptainComputeError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class CaptainComputeAdapter:
    """The injected clients expose get/start/stop and operation get; target is immutable."""

    def __init__(self, target: ComputeTarget, instances, zone_operations):
        self.target, self.instances, self.zone_operations = target, instances, zone_operations

    @classmethod
    def from_default_credentials(cls, target: ComputeTarget):
        """Construct narrow REST clients lazily; tests inject fakes instead."""
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise CaptainComputeError("compute_client_unavailable") from exc
        credentials, _ = google.auth.default(
            scopes=("https://www.googleapis.com/auth/cloud-platform",)
        )
        session = AuthorizedSession(credentials)
        return cls(target, _RestInstances(session), _RestZoneOperations(session))

    def inspect(self) -> InstanceEvidence:
        try:
            item = self.instances.get(
                project=self.target.project, zone=self.target.zone, instance=self.target.instance
            )
        except (PermissionDenied, Unauthenticated, InvalidArgument, NotFound) as exc:
            raise CaptainComputeError("vm_inspect_rejected") from exc
        except Exception as exc:
            raise CaptainComputeError("vm_inspect_ambiguous") from exc
        if getattr(item, "name", None) != self.target.instance:
            raise CaptainComputeError("vm_identity_mismatch")
        return InstanceEvidence(item.name, str(getattr(item, "id", "")), str(item.status))

    def request(self, action: VmAction) -> ComputeOperationReceipt:
        if not isinstance(action, VmAction):
            raise CaptainComputeError("invalid_vm_action")
        state = self.inspect().status.upper()
        if action == VmAction.START and state == "RUNNING":
            return ComputeOperationReceipt(None, ProviderResult.COMPLETE)
        if action == VmAction.STOP and state == "TERMINATED":
            return ComputeOperationReceipt(None, ProviderResult.COMPLETE)
        method = self.instances.start if action == VmAction.START else self.instances.stop
        try:
            op = method(
                project=self.target.project, zone=self.target.zone, instance=self.target.instance
            )
        except (PermissionDenied, Unauthenticated, InvalidArgument, FailedPrecondition) as exc:
            raise CaptainComputeError(f"vm_{action.value}_rejected") from exc
        except Exception as exc:
            raise CaptainComputeError(f"vm_{action.value}_ambiguous") from exc
        name = getattr(op, "name", None)
        if not isinstance(name, str) or not name:
            raise CaptainComputeError(f"vm_{action.value}_ambiguous")
        return ComputeOperationReceipt(name, ProviderResult.PENDING)

    def reconcile(self, action: VmAction, operation_name: str | None) -> ProviderResult:
        desired = "RUNNING" if action == VmAction.START else "TERMINATED"
        if operation_name:
            try:
                op = self.zone_operations.get(
                    project=self.target.project, zone=self.target.zone, operation=operation_name
                )
            except Exception:
                return ProviderResult.AMBIGUOUS
            if str(getattr(op, "status", "")).upper() != "DONE":
                return ProviderResult.PENDING
            if getattr(op, "error", None):
                return ProviderResult.FAILED
        return (
            ProviderResult.COMPLETE
            if self.inspect().status.upper() == desired
            else ProviderResult.PENDING
        )


class _RestInstances:
    def __init__(self, session):
        self.session = session

    def _url(self, project, zone, instance, suffix=""):
        return (
            f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/"
            f"{instance}{suffix}"
        )

    def get(self, **kwargs):
        response = self.session.get(self._url(**kwargs), timeout=10)
        if response.status_code >= 400:
            raise NotFound("compute instance unavailable")
        return SimpleNamespace(**response.json())

    def start(self, **kwargs):
        response = self.session.post(self._url(**kwargs, suffix="/start"), timeout=10)
        if response.status_code >= 400:
            raise RuntimeError("compute start rejected")
        return SimpleNamespace(**response.json())

    def stop(self, **kwargs):
        response = self.session.post(self._url(**kwargs, suffix="/stop"), timeout=10)
        if response.status_code >= 400:
            raise RuntimeError("compute stop rejected")
        return SimpleNamespace(**response.json())


class _RestZoneOperations:
    def __init__(self, session):
        self.session = session

    def get(self, *, project, zone, operation):
        url = (
            f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/operations/"
            f"{operation}"
        )
        response = self.session.get(url, timeout=10)
        if response.status_code >= 400:
            raise RuntimeError("compute operation lookup failed")
        return SimpleNamespace(**response.json())


class CaptainComputeReconciler:
    """External calls happen after durable request records and remain lease fenced."""

    def __init__(
        self, leases: CaptainVmLeaseRepository, operations: VmOperationRepository, provider
    ):
        self.leases, self.operations, self.provider = leases, operations, provider

    def submit(self, lease_id, action: VmAction) -> ProviderResult:
        lease = self.leases.vm_use()
        operation = self.operations.get(lease_id, action)
        if lease is None or operation is None or lease.lease_id != lease_id:
            raise StateConflict("stale VM provider operation")
        if lease.generation_id != operation.generation_id:
            raise StateConflict("VM operation generation mismatch")
        if action == VmAction.STOP and lease.phase != VmPhase.STOPPING:
            raise StateConflict("stop is not cleanup fenced")
        receipt = self.provider.request(action)
        self.operations.acknowledge(lease_id, action, receipt.operation_name)
        if receipt.result == ProviderResult.COMPLETE:
            self.operations.finish(
                lease_id, action, succeeded=True, terminated=action == VmAction.STOP
            )
        return receipt.result

    def reconcile(self, lease_id, action: VmAction) -> ProviderResult:
        operation = self.operations.get(lease_id, action)
        if operation is None:
            raise StateConflict("unknown VM provider operation")
        if operation.phase == OperationPhase.COMPLETED:
            return ProviderResult.COMPLETE
        if operation.phase == OperationPhase.FAILED:
            return ProviderResult.FAILED
        result = self.provider.reconcile(action, operation.provider_operation_id)
        if operation.phase == OperationPhase.REQUESTED and result == ProviderResult.COMPLETE:
            operation = self.operations.acknowledge(lease_id, action).record
        if result in {ProviderResult.COMPLETE, ProviderResult.FAILED}:
            self.operations.finish(
                lease_id,
                action,
                succeeded=result == ProviderResult.COMPLETE,
                terminated=action == VmAction.STOP and result == ProviderResult.COMPLETE,
            )
        return result
