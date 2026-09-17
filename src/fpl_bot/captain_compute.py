"""Narrow Compute Engine adapter and durable operation reconciler."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID, uuid5

from google.api_core.exceptions import (
    FailedPrecondition,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)

from fpl_bot.captain_repository import CaptainVmLeaseRepository
from fpl_bot.captain_state import StateConflict
from fpl_bot.captain_vm_operations import (
    DispatchStatus,
    OperationPhase,
    VmAction,
    VmOperationRepository,
)

_DISPATCH_NAMESPACE = UUID("7f61c738-9bc6-4dc6-a9e3-f09e41ba5ffd")


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
    instance: InstanceEvidence | None = None


class CaptainComputeError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class CaptainComputeAdapter:
    """Fixed-target Compute API; requestId supplements durable dispatch fencing.

    Compute operation records are retained for a limited period. Absence of an
    operation with the stored clientOperationId is therefore not proof that a
    request was never sent: that state remains ambiguous and is not reissued.
    """

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

    def request(self, action: VmAction, request_id: UUID) -> ComputeOperationReceipt:
        if (
            not isinstance(action, VmAction)
            or not isinstance(request_id, UUID)
            or not request_id.int
        ):
            raise CaptainComputeError("invalid_vm_action")
        evidence = self.inspect()
        state = evidence.status.upper()
        if action == VmAction.START and state == "RUNNING":
            return ComputeOperationReceipt(None, ProviderResult.COMPLETE, evidence)
        if action == VmAction.STOP and state == "TERMINATED":
            return ComputeOperationReceipt(None, ProviderResult.COMPLETE, evidence)
        method = self.instances.start if action == VmAction.START else self.instances.stop
        try:
            op = method(
                project=self.target.project,
                zone=self.target.zone,
                instance=self.target.instance,
                request_id=str(request_id),
            )
        except (PermissionDenied, Unauthenticated, InvalidArgument, FailedPrecondition) as exc:
            raise CaptainComputeError(f"vm_{action.value}_rejected") from exc
        except Exception as exc:
            raise CaptainComputeError(f"vm_{action.value}_ambiguous") from exc
        name = getattr(op, "name", None)
        if (
            not isinstance(name, str)
            or not name
            or getattr(op, "clientOperationId", None) != str(request_id)
            or getattr(op, "operationType", None) != action.value
            or not str(getattr(op, "targetLink", "")).endswith(
                f"/zones/{self.target.zone}/instances/{self.target.instance}"
            )
        ):
            raise CaptainComputeError(f"vm_{action.value}_ambiguous")
        return ComputeOperationReceipt(name, ProviderResult.PENDING)

    def reconcile(
        self, action: VmAction, operation_name: str | None, request_id: UUID
    ) -> ComputeOperationReceipt:
        desired = "RUNNING" if action == VmAction.START else "TERMINATED"
        try:
            if operation_name:
                op = self.zone_operations.get(
                    project=self.target.project, zone=self.target.zone, operation=operation_name
                )
            else:
                matches = self.zone_operations.find_by_request_id(
                    project=self.target.project,
                    zone=self.target.zone,
                    request_id=str(request_id),
                )
                if len(matches) != 1:
                    return ComputeOperationReceipt(None, ProviderResult.AMBIGUOUS)
                op = matches[0]
        except Exception:
            return ComputeOperationReceipt(operation_name, ProviderResult.AMBIGUOUS)
        name = getattr(op, "name", None)
        if (
            not isinstance(name, str)
            or not name
            or getattr(op, "clientOperationId", None) != str(request_id)
            or getattr(op, "operationType", None) != action.value
            or not str(getattr(op, "targetLink", "")).endswith(
                f"/zones/{self.target.zone}/instances/{self.target.instance}"
            )
        ):
            return ComputeOperationReceipt(operation_name, ProviderResult.AMBIGUOUS)
        if str(getattr(op, "status", "")).upper() != "DONE":
            return ComputeOperationReceipt(name, ProviderResult.PENDING)
        if getattr(op, "error", None):
            return ComputeOperationReceipt(name, ProviderResult.FAILED)
        try:
            evidence = self.inspect()
        except CaptainComputeError:
            return ComputeOperationReceipt(name, ProviderResult.AMBIGUOUS)
        if str(getattr(op, "targetId", "")) != evidence.instance_id:
            return ComputeOperationReceipt(name, ProviderResult.AMBIGUOUS)
        result = (
            ProviderResult.COMPLETE
            if evidence.status.upper() == desired
            else ProviderResult.PENDING
        )
        return ComputeOperationReceipt(name, result, evidence)


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

    def start(self, request_id, **kwargs):
        response = self.session.post(
            self._url(**kwargs, suffix="/start"), params={"requestId": request_id}, timeout=10
        )
        if response.status_code >= 400:
            raise RuntimeError("compute start rejected")
        return SimpleNamespace(**response.json())

    def stop(self, request_id, **kwargs):
        response = self.session.post(
            self._url(**kwargs, suffix="/stop"), params={"requestId": request_id}, timeout=10
        )
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

    def find_by_request_id(self, *, project, zone, request_id):
        filter_value = quote(f'clientOperationId = "{request_id}"')
        url = (
            f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/operations"
            f"?filter={filter_value}&maxResults=2"
        )
        response = self.session.get(url, timeout=10)
        if response.status_code >= 400:
            raise RuntimeError("compute operation lookup failed")
        body = response.json()
        if body.get("nextPageToken"):
            raise RuntimeError("compute operation lookup ambiguous")
        return tuple(SimpleNamespace(**item) for item in body.get("items", ()))


class CaptainComputeReconciler:
    """External calls happen after durable request records and remain lease fenced."""

    def __init__(
        self,
        leases: CaptainVmLeaseRepository,
        operations: VmOperationRepository,
        provider,
        clock=lambda: datetime.now(UTC),
    ):
        self.leases, self.operations, self.provider = leases, operations, provider
        self.clock = clock

    @staticmethod
    def _identities(lease_id, action):
        logical = f"{lease_id}:{action.value}"
        return (
            uuid5(_DISPATCH_NAMESPACE, f"reservation:{logical}"),
            uuid5(_DISPATCH_NAMESPACE, f"compute-request:{logical}"),
        )

    def _record(self, operation, receipt):
        dispatch = operation.dispatch
        if dispatch is None:
            raise StateConflict("VM provider dispatch is not reserved")
        now = self.clock()
        evidence = receipt.instance
        common = {
            "provider_operation_id": receipt.operation_name,
            "instance_id": evidence.instance_id if evidence else None,
            "instance_status": evidence.status.upper() if evidence else None,
        }
        if receipt.result == ProviderResult.COMPLETE:
            status, failure = DispatchStatus.COMPLETED, None
        elif receipt.result == ProviderResult.PENDING:
            status, failure = DispatchStatus.ACKNOWLEDGED, None
        elif receipt.result == ProviderResult.FAILED:
            status, failure = DispatchStatus.FAILED, "provider_failed"
        else:
            status, failure = DispatchStatus.AMBIGUOUS, "provider_ambiguous"
        return self.operations.record_dispatch(
            operation.lease_id,
            operation.action,
            dispatch.reservation_id,
            status,
            now,
            failure_category=failure,
            terminated=operation.action == VmAction.STOP and status == DispatchStatus.COMPLETED,
            **common,
        ).record

    def submit(self, lease_id, action: VmAction) -> ProviderResult:
        operation = self.operations.get(lease_id, action)
        if operation is None:
            raise StateConflict("stale VM provider operation")
        reservation_id, request_id = self._identities(lease_id, action)
        reserved = self.operations.reserve_dispatch(
            lease_id,
            operation.generation_id,
            action,
            reservation_id,
            request_id,
            self.clock(),
        )
        operation = reserved.record
        if not reserved.applied:
            return self.reconcile(lease_id, action)
        try:
            receipt = self.provider.request(action, request_id)
        except CaptainComputeError as exc:
            ambiguous = exc.category.endswith("_ambiguous")
            self.operations.record_dispatch(
                lease_id,
                action,
                reservation_id,
                DispatchStatus.AMBIGUOUS if ambiguous else DispatchStatus.FAILED,
                self.clock(),
                failure_category="provider_ambiguous" if ambiguous else "provider_rejected",
            )
            return ProviderResult.AMBIGUOUS if ambiguous else ProviderResult.FAILED
        self._record(operation, receipt)
        return receipt.result

    def reconcile(self, lease_id, action: VmAction) -> ProviderResult:
        operation = self.operations.get(lease_id, action)
        if operation is None:
            raise StateConflict("unknown VM provider operation")
        if operation.phase == OperationPhase.COMPLETED:
            return ProviderResult.COMPLETE
        if operation.phase == OperationPhase.FAILED:
            return ProviderResult.FAILED
        dispatch = operation.dispatch
        if dispatch is None:
            raise StateConflict("legacy VM dispatch requires operator reconciliation")
        # This re-validates the exact current owner/fence before provider lookup.
        self.operations.reserve_dispatch(
            lease_id,
            operation.generation_id,
            action,
            dispatch.reservation_id,
            dispatch.request_id,
            self.clock(),
        )
        receipt = self.provider.reconcile(
            action, dispatch.provider_operation_id, dispatch.request_id
        )
        self._record(operation, receipt)
        return receipt.result
