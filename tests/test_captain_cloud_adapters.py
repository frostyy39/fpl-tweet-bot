from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded, NotFound, PermissionDenied

from fpl_bot.captain_cloud_tasks import (
    CaptainCloudTasksAdapter,
    CaptainCloudTasksConfig,
    CaptainTaskProviderError,
    task_payload,
)
from fpl_bot.captain_compute import (
    CaptainComputeAdapter,
    CaptainComputeError,
    CaptainComputeReconciler,
    ComputeTarget,
    ProviderResult,
)
from fpl_bot.captain_controller import TaskEnvelope
from fpl_bot.captain_handoff import CaptainAssignment
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_state import PostKey, StateConflict, TaskKind
from fpl_bot.captain_vm_operations import (
    DispatchStatus,
    InMemoryVmOperations,
    VmAction,
)

T = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)
ASSIGNMENT = CaptainAssignment(
    UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(T + timedelta(hours=2))
)
TASK = TaskEnvelope("12345", ASSIGNMENT, TaskKind.WARMUP, T - timedelta(minutes=15))


def config():
    return CaptainCloudTasksConfig(
        "captain-project",
        "europe-west2",
        "captain-orchestration",
        "https://captain.example.run.app",
        "tasks@captain-project.iam.gserviceaccount.com",
        "https://captain.example.run.app",
    )


class TasksClient:
    def __init__(self):
        self.created = None
        self.existing = None
        self.error = None

    def create_task(self, request, *, retry):
        assert retry is None
        self.created = request["task"]
        if self.error:
            raise self.error
        return SimpleNamespace(name=self.created.name)

    def get_task(self, request, *, retry):
        assert retry is None
        if self.existing is None:
            raise NotFound("missing")
        return self.existing


def test_cloud_task_is_deterministic_utc_oidc_and_captain_scoped():
    client = TasksClient()
    adapter = CaptainCloudTasksAdapter(config(), client)
    receipt = adapter.ensure(TASK)
    assert receipt.identity == TASK.identity and receipt.digest == TASK.digest
    assert client.created.name.endswith("/tasks/" + TASK.identity)
    assert client.created.schedule_time == TASK.scheduled_at
    assert client.created.http_request.url.endswith("/captain/tasks/warmup")
    assert client.created.http_request.oidc_token.audience == config().oidc_audience
    assert b'"kind":"warmup"' in task_payload(TASK)


def test_duplicate_task_requires_exact_provider_content():
    client = TasksClient()
    adapter = CaptainCloudTasksAdapter(config(), client)
    intended = adapter._api_task(TASK)
    client.error, client.existing = AlreadyExists("exists"), intended
    assert adapter.ensure(TASK).digest == TASK.digest
    client.existing.http_request.url = "https://captain.example.run.app/captain/tasks/release"
    with pytest.raises(ValueError, match="content conflict"):
        adapter.ensure(TASK)


def test_ambiguous_task_create_reconciles_or_remains_ambiguous():
    client = TasksClient()
    adapter = CaptainCloudTasksAdapter(config(), client)
    client.error, client.existing = DeadlineExceeded("lost"), adapter._api_task(TASK)
    assert adapter.ensure(TASK).identity == TASK.identity
    client.existing = None
    with pytest.raises(CaptainTaskProviderError) as error:
        adapter.ensure(TASK)
    assert error.value.category == "task_create_ambiguous"
    assert "lost" not in str(error.value)


def test_definite_task_rejection_is_typed_and_good_luck_target_rejected():
    client = TasksClient()
    client.error = PermissionDenied("secret detail")
    with pytest.raises(CaptainTaskProviderError) as error:
        CaptainCloudTasksAdapter(config(), client).ensure(TASK)
    assert error.value.category == "task_create_rejected"
    with pytest.raises(ValueError, match="Good Luck"):
        CaptainCloudTasksConfig(
            "captain-project",
            "europe-west2",
            "good-luck-queue",
            "https://captain.example.run.app",
            "tasks@captain-project.iam.gserviceaccount.com",
            "https://captain.example.run.app",
        )


class Instances:
    def __init__(self, status="TERMINATED"):
        self.status, self.calls, self.error = status, [], None

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return SimpleNamespace(name="captain-worker", id=42, status=self.status)

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(
            name="start-op",
            clientOperationId=kwargs["request_id"],
            operationType="start",
            targetLink="https://compute/v1/projects/captain-project/zones/europe-west2-b/instances/captain-worker",
        )

    def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(
            name="stop-op",
            clientOperationId=kwargs["request_id"],
            operationType="stop",
            targetLink="https://compute/v1/projects/captain-project/zones/europe-west2-b/instances/captain-worker",
        )


class Operations:
    def __init__(self, status="RUNNING", error=None):
        self.status, self.error = status, error

    def get(self, **kwargs):
        action = "start" if kwargs["operation"].startswith("start") else "stop"
        return SimpleNamespace(
            name=kwargs["operation"],
            status=self.status,
            error=self.error,
            clientOperationId=str(UUID(int=70)),
            operationType=action,
            targetLink="https://compute/v1/projects/captain-project/zones/europe-west2-b/instances/captain-worker",
            targetId="42",
        )

    def find_by_request_id(self, **kwargs):
        return ()


def compute(status="TERMINATED", op="RUNNING"):
    instances = Instances(status)
    return CaptainComputeAdapter(
        ComputeTarget("captain-project", "europe-west2-b", "captain-worker"),
        instances,
        Operations(op),
    ), instances


def test_compute_start_stop_and_already_terminal_are_idempotent():
    adapter, instances = compute()
    request_id = UUID(int=70)
    receipt = adapter.request(VmAction.START, request_id)
    assert receipt.result == ProviderResult.PENDING and receipt.operation_name == "start-op"
    assert instances.calls[-1][1] == {
        "project": "captain-project",
        "zone": "europe-west2-b",
        "instance": "captain-worker",
        "request_id": str(request_id),
    }
    adapter, instances = compute("RUNNING")
    assert adapter.request(VmAction.START, request_id).result == ProviderResult.COMPLETE
    assert not any(call[0] == "start" for call in instances.calls)
    adapter, _ = compute("TERMINATED")
    assert adapter.request(VmAction.STOP, request_id).result == ProviderResult.COMPLETE


def test_compute_reconciles_pending_failed_and_ambiguous():
    adapter, instances = compute("RUNNING", "RUNNING")
    request_id = UUID(int=70)
    assert (
        adapter.reconcile(VmAction.START, "start-op", request_id).result == ProviderResult.PENDING
    )
    adapter.zone_operations.status = "DONE"
    assert (
        adapter.reconcile(VmAction.START, "start-op", request_id).result == ProviderResult.COMPLETE
    )
    adapter.zone_operations.error = object()
    assert adapter.reconcile(VmAction.START, "start-op", request_id).result == ProviderResult.FAILED
    adapter.zone_operations.get = lambda **kwargs: (_ for _ in ()).throw(TimeoutError())
    assert (
        adapter.reconcile(VmAction.START, "start-op", request_id).result == ProviderResult.AMBIGUOUS
    )
    instances.error = PermissionDenied("detail")
    with pytest.raises(CaptainComputeError) as error:
        adapter.request(VmAction.STOP, request_id)
    assert error.value.category == "vm_stop_rejected"


class DispatchProvider:
    def __init__(self):
        self.requests, self.reconciliations = [], []
        self.request_result = ProviderResult.PENDING
        self.reconcile_result = ProviderResult.PENDING

    def request(self, action, request_id):
        self.requests.append((action, request_id))
        if self.request_result == ProviderResult.AMBIGUOUS:
            raise CaptainComputeError(f"vm_{action.value}_ambiguous")
        return SimpleNamespace(
            operation_name=f"{action.value}-op",
            result=self.request_result,
            instance=None,
        )

    def reconcile(self, action, operation_name, request_id):
        self.reconciliations.append((action, operation_name, request_id))
        evidence = (
            SimpleNamespace(
                instance_id="42", status="RUNNING" if action == VmAction.START else "TERMINATED"
            )
            if self.reconcile_result == ProviderResult.COMPLETE
            else None
        )
        return SimpleNamespace(
            operation_name=operation_name or f"{action.value}-op",
            result=self.reconcile_result,
            instance=evidence,
        )


def dispatch_setup():
    repo = InMemoryCaptainRepository()
    repo.plan(PostKey("12345", 5), ASSIGNMENT, None, T - timedelta(minutes=15))
    lease = repo.acquire_vm(
        ASSIGNMENT.generation_id, UUID(int=80), T - timedelta(minutes=15)
    ).record
    operations = InMemoryVmOperations(repo)
    operations.request(lease, VmAction.START)
    provider = DispatchProvider()

    def clock():
        return T - timedelta(minutes=14)

    return (
        repo,
        operations,
        provider,
        CaptainComputeReconciler(repo, operations, provider, clock),
        lease,
    )


def test_durable_dispatch_replay_never_reissues_acknowledged_start():
    _, operations, provider, reconciler, lease = dispatch_setup()
    assert reconciler.submit(lease.lease_id, VmAction.START) == ProviderResult.PENDING
    assert reconciler.submit(lease.lease_id, VmAction.START) == ProviderResult.PENDING
    assert len(provider.requests) == 1
    operation = operations.get(lease.lease_id, VmAction.START)
    assert operation.dispatch.status == DispatchStatus.ACKNOWLEDGED
    assert provider.reconciliations[0][2] == provider.requests[0][1]


def test_reserved_crash_and_ambiguous_response_reconcile_without_reissue():
    _, operations, provider, reconciler, lease = dispatch_setup()
    reservation_id, request_id = reconciler._identities(lease.lease_id, VmAction.START)
    operations.reserve_dispatch(
        lease.lease_id,
        lease.generation_id,
        VmAction.START,
        reservation_id,
        request_id,
        T - timedelta(minutes=14),
    )
    assert reconciler.submit(lease.lease_id, VmAction.START) == ProviderResult.PENDING
    assert provider.requests == []
    assert provider.reconciliations == [(VmAction.START, None, request_id)]

    _, operations, provider, reconciler, lease = dispatch_setup()
    provider.request_result = ProviderResult.AMBIGUOUS
    assert reconciler.submit(lease.lease_id, VmAction.START) == ProviderResult.AMBIGUOUS
    assert (
        operations.get(lease.lease_id, VmAction.START).dispatch.status == DispatchStatus.AMBIGUOUS
    )
    reconciler.submit(lease.lease_id, VmAction.START)
    assert len(provider.requests) == 1


def test_unresolved_start_fences_stop_before_provider_call_then_allows_settled_stop():
    repo, operations, provider, reconciler, lease = dispatch_setup()
    reconciler.submit(lease.lease_id, VmAction.START)
    stopping = repo.begin_cleanup(lease.lease_id, lease.generation_id).record
    operations.request(stopping, VmAction.STOP)
    with pytest.raises(StateConflict):
        reconciler.submit(lease.lease_id, VmAction.STOP)
    assert all(action != VmAction.STOP for action, _ in provider.requests)

    provider.reconcile_result = ProviderResult.COMPLETE
    reconciler.reconcile(lease.lease_id, VmAction.START)
    assert reconciler.submit(lease.lease_id, VmAction.STOP) == ProviderResult.PENDING
    assert sum(action == VmAction.STOP for action, _ in provider.requests) == 1


def test_concurrent_duplicate_start_has_one_provider_dispatch_owner():
    _, operations, provider, reconciler, lease = dispatch_setup()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _: reconciler.submit(lease.lease_id, VmAction.START),
                range(2),
            )
        )
    assert results == (ProviderResult.PENDING, ProviderResult.PENDING)
    assert len(provider.requests) == 1
    operation = operations.get(lease.lease_id, VmAction.START)
    assert operation.dispatch.request_id == provider.requests[0][1]


def test_stop_completion_evidence_precedes_lease_release_and_stale_cleanup_is_fenced():
    repo, operations, provider, reconciler, lease = dispatch_setup()
    reconciler.submit(lease.lease_id, VmAction.START)
    provider.reconcile_result = ProviderResult.COMPLETE
    reconciler.reconcile(lease.lease_id, VmAction.START)
    stopping = repo.begin_cleanup(lease.lease_id, lease.generation_id).record
    operations.request(stopping, VmAction.STOP)
    reconciler.submit(lease.lease_id, VmAction.STOP)
    assert repo.vm_use() == stopping
    reconciler.reconcile(lease.lease_id, VmAction.STOP)
    assert operations.stop_is_settled(lease.lease_id)
    assert repo.vm_use() == stopping
    repo.complete_cleanup(lease.lease_id, lease.generation_id)
    newer = repo.acquire_vm(lease.generation_id, UUID(int=81), T).record
    before = len(provider.requests)
    with pytest.raises(StateConflict):
        reconciler.submit(lease.lease_id, VmAction.STOP)
    assert len(provider.requests) == before
    assert repo.vm_use() == newer


def test_compute_rejects_mismatched_client_operation_id():
    adapter, instances = compute()
    original = instances.start

    def mismatch(**kwargs):
        result = original(**kwargs)
        result.clientOperationId = str(UUID(int=999))
        return result

    instances.start = mismatch
    with pytest.raises(CaptainComputeError) as error:
        adapter.request(VmAction.START, UUID(int=70))
    assert error.value.category == "vm_start_ambiguous"


def test_missing_provider_evidence_after_reservation_stays_ambiguous_without_reissue():
    adapter, instances = compute()
    receipt = adapter.reconcile(VmAction.START, None, UUID(int=70))
    assert receipt.result == ProviderResult.AMBIGUOUS
    assert not any(action == "start" for action, _ in instances.calls)
