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
    ComputeTarget,
    ProviderResult,
)
from fpl_bot.captain_controller import TaskEnvelope
from fpl_bot.captain_handoff import CaptainAssignment
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_state import TaskKind
from fpl_bot.captain_vm_operations import VmAction

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
        return SimpleNamespace(name="start-op")

    def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(name="stop-op")


class Operations:
    def __init__(self, status="RUNNING", error=None):
        self.status, self.error = status, error

    def get(self, **kwargs):
        return SimpleNamespace(status=self.status, error=self.error)


def compute(status="TERMINATED", op="RUNNING"):
    instances = Instances(status)
    return CaptainComputeAdapter(
        ComputeTarget("captain-project", "europe-west2-b", "captain-worker"),
        instances,
        Operations(op),
    ), instances


def test_compute_start_stop_and_already_terminal_are_idempotent():
    adapter, instances = compute()
    receipt = adapter.request(VmAction.START)
    assert receipt.result == ProviderResult.PENDING and receipt.operation_name == "start-op"
    assert instances.calls[-1][1] == {
        "project": "captain-project",
        "zone": "europe-west2-b",
        "instance": "captain-worker",
    }
    adapter, instances = compute("RUNNING")
    assert adapter.request(VmAction.START).result == ProviderResult.COMPLETE
    assert not any(call[0] == "start" for call in instances.calls)
    adapter, _ = compute("TERMINATED")
    assert adapter.request(VmAction.STOP).result == ProviderResult.COMPLETE


def test_compute_reconciles_pending_failed_and_ambiguous():
    adapter, instances = compute("RUNNING", "RUNNING")
    assert adapter.reconcile(VmAction.START, "op") == ProviderResult.PENDING
    adapter.zone_operations.status = "DONE"
    assert adapter.reconcile(VmAction.START, "op") == ProviderResult.COMPLETE
    adapter.zone_operations.error = object()
    assert adapter.reconcile(VmAction.START, "op") == ProviderResult.FAILED
    adapter.zone_operations.get = lambda **kwargs: (_ for _ in ()).throw(TimeoutError())
    assert adapter.reconcile(VmAction.START, "op") == ProviderResult.AMBIGUOUS
    instances.error = PermissionDenied("detail")
    with pytest.raises(CaptainComputeError) as error:
        adapter.request(VmAction.STOP)
    assert error.value.category == "vm_stop_rejected"
