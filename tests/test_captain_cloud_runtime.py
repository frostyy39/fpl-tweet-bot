from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from fpl_bot.captain_cloud_runtime import NoPostConfig, NoPostDriver, compose
from fpl_bot.captain_compute import ProviderResult
from fpl_bot.captain_controller import CaptainController, TaskConfirmation
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_state import TaskKind
from fpl_bot.captain_vm_operations import InMemoryVmOperations, VmAction
from fpl_bot.captain_worker_cli import BoundedPollingClient

D = datetime(2026, 9, 18, 17, 30, tzinfo=UTC)
ORIGIN = "https://captain-controller-524790767721.europe-west1.run.app"
CONFIG = NoPostConfig(
    "fpl-frosty-bot-v1",
    "captain-state",
    ORIGIN,
    "captain-worker@fpl-frosty-bot-v1.iam.gserviceaccount.com",
    "captain-tasks@fpl-frosty-bot-v1.iam.gserviceaccount.com",
    "captain-planner@fpl-frosty-bot-v1.iam.gserviceaccount.com",
    "1",
)


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


class Source:
    def fetch_bootstrap_static(self):
        return deepcopy(
            {
                "events": [
                    {"id": 5, "name": "Gameweek 5", "deadline_time": D.isoformat(), "is_next": True}
                ],
                "teams": [
                    {"id": i, "name": f"Team {i}", "short_name": f"T{i}"} for i in range(1, 21)
                ],
            }
        )

    def fetch_event_fixtures(self, event_id):
        return [{"id": i, "event": 5, "team_h": i, "team_a": i + 1} for i in range(1, 21, 2)]


class Scheduler:
    def __init__(self):
        self.tasks = {}

    def ensure(self, task):
        self.tasks[task.identity] = task
        return TaskConfirmation(task.identity, task.digest)


class Authorizer:
    def authorize(self, header, audience):
        assert audience == ORIGIN
        return SimpleNamespace(email=header)


def test_tick_plans_and_persists_outbox_without_starting_before_warmup():
    repo = InMemoryCaptainRepository()
    scheduler = Scheduler()
    provider = SimpleNamespace(request=lambda *_: pytest.fail("early VM start"))
    app = compose(
        CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        Clock(D - timedelta(days=1)),
        scheduler,
        provider,
        Authorizer(),
    )
    client = app.test_client()
    for _ in range(2):
        response = client.post(
            "/captain/control/tick", headers={"Authorization": CONFIG.planner_email}
        )
        assert response.status_code == 200
        assert response.json["no_post"] is True
        assert response.json["vm"] == "idle"
    assert len(scheduler.tasks) == 3
    assert repo.vm_use() is None
    assert all(task.destination_user_id == "1" for task in scheduler.tasks.values())


def test_worker_cannot_invoke_planner_and_planner_cannot_get_assignment():
    repo = InMemoryCaptainRepository()
    app = compose(
        CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        Clock(D - timedelta(days=1)),
        Scheduler(),
        None,
        Authorizer(),
    )
    client = app.test_client()
    assert (
        client.post(
            "/captain/control/tick", headers={"Authorization": CONFIG.worker_email}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/captain/worker/assignment",
            headers={"Authorization": CONFIG.planner_email},
            json={"run_id": str(UUID(int=3))},
        ).status_code
        == 403
    )
    assert repo.pending_intents() == ()


def test_driver_reconciles_unresolved_start_before_stop_and_holds_cleanup_fence():
    repo = InMemoryCaptainRepository()
    operations = InMemoryVmOperations(repo)
    clock = Clock(D - timedelta(hours=2, minutes=15))
    controller = CaptainController(repo, Source(), clock, operations)
    generation = controller.plan("1")[0].generation
    gid = generation.assignment.generation_id
    controller.deliver(controller.envelope(repo.task_intent(gid, TaskKind.WARMUP)))
    lease = repo.vm_use()
    controller.request_cleanup(lease.lease_id, gid)
    calls = []
    compute = SimpleNamespace(
        reconcile=lambda lid, action: calls.append((lid, action)) or ProviderResult.PENDING,
        submit=lambda *_: pytest.fail("STOP before START settled"),
    )
    assert NoPostDriver(controller, Scheduler(), compute).advance() == "pending"
    assert calls == [(lease.lease_id, VmAction.START)]
    assert repo.vm_use().lease_id == lease.lease_id


def test_worker_pending_poll_is_bounded_and_cancellable_without_acquisition():
    clock = Clock(D - timedelta(hours=2, seconds=3))
    waits = []
    stop = SimpleNamespace(wait=lambda seconds: waits.append(seconds))
    work = SimpleNamespace(
        assignment=SimpleNamespace(
            timing=SimpleNamespace(expiry_utc=clock.now() + timedelta(seconds=4))
        )
    )
    client = BoundedPollingClient(SimpleNamespace(await_release=lambda *a, **k: None), clock, stop)
    assert (
        client.await_release(work, UUID(int=2), timeout_seconds=4, cancelled=lambda: False) is None
    )
    assert waits == [4]


@pytest.mark.parametrize(
    "project,database,origin",
    [
        ("other", "captain-state", ORIGIN),
        ("fpl-frosty-bot-v1", "(default)", ORIGIN),
        ("fpl-frosty-bot-v1", "captain-state", "https://fpl-bot.example"),
        (
            "fpl-frosty-bot-v1",
            "captain-state",
            "https://captain-controller-524790767721.europe-west2.run.app",
        ),
    ],
)
def test_no_post_deployment_rejects_unisolated_configuration(project, database, origin):
    with pytest.raises(ValueError):
        NoPostConfig(
            project,
            database,
            origin,
            CONFIG.worker_email,
            CONFIG.tasks_email,
            CONFIG.planner_email,
            "1",
        )
