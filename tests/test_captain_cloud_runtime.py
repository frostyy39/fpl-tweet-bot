from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from fpl_bot.captain_cloud_runtime import NoPostConfig, NoPostDriver, compose
from fpl_bot.captain_compute import ProviderResult
from fpl_bot.captain_controller import CaptainController, TaskConfirmation
from fpl_bot.captain_http import DualPurposeWorkerService, WorkerControllerService
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_release_diagnostics import ReleaseRejectionCode
from fpl_bot.captain_serialization import from_document
from fpl_bot.captain_state import GenerationStatus, PostKey, StateConflict, TaskKind
from fpl_bot.captain_transport import parse_worker_assignment, worker_assignment_payload
from fpl_bot.captain_vm_operations import InMemoryVmOperations, VmAction
from fpl_bot.captain_worker import CaptainWorker, ReleaseGrant, WorkerStatus
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
PRODUCTION_ID = "1249335464571650048"
PRODUCTION_CONFIG = SimpleNamespace(**{**vars(CONFIG), "destination_user_id": PRODUCTION_ID})


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


def test_rehearsal_endpoint_creates_one_durable_non_postable_generation():
    now = D - timedelta(days=1)
    clock, repo, scheduler = Clock(now), InMemoryCaptainRepository(), Scheduler()
    app = compose(
        CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        clock,
        scheduler,
        SimpleNamespace(request=lambda *_: pytest.fail("task delivery did not run")),
        Authorizer(),
    )
    body = {
        "schema_version": 1,
        "rehearsal_id": str(UUID(int=700)),
        "release_utc": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
    }
    client = app.test_client()
    first = client.post(
        "/captain/control/rehearsal", json=body, headers={"Authorization": CONFIG.planner_email}
    )
    second = client.post(
        "/captain/control/rehearsal", json=body, headers={"Authorization": CONFIG.planner_email}
    )
    assert first.status_code == second.status_code == 200
    assert first.json["created"] is True and second.json["created"] is False
    assert first.json["postable"] is False
    assert first.json["purpose"] == "non_postable_rehearsal"
    gid = UUID(first.json["generation_id"])
    assert repo.rehearsal(gid).official_deadline_utc == D
    assert len(scheduler.tasks) == 3
    assert repo.posting(repo.generation(gid).key).attempts == ()


def test_production_composition_does_not_expose_rehearsal_by_default():
    class Router:
        destination_user_id = PRODUCTION_ID

        def ensure(self, *_):
            pytest.fail("no candidate exists")

    repo = InMemoryCaptainRepository()
    app = compose(
        PRODUCTION_CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        Clock(D - timedelta(days=1)),
        Scheduler(),
        SimpleNamespace(request=lambda *_: pytest.fail("VM operation")),
        Authorizer(),
        publisher_router=Router(),
    )
    response = app.test_client().post(
        "/captain/control/rehearsal",
        json={
            "schema_version": 1,
            "rehearsal_id": str(UUID(int=710)),
            "release_utc": (D - timedelta(days=1) + timedelta(minutes=10)).isoformat(),
        },
        headers={"Authorization": CONFIG.planner_email},
    )
    assert response.status_code == 403
    assert repo.current(PostKey("1", 5)) is None
    assert (
        app.test_client()
        .post(
            "/captain/control/inspect",
            json={"purpose": "non_postable_rehearsal"},
            headers={"Authorization": CONFIG.planner_email},
        )
        .status_code
        == 403
    )


def _dual_rehearsal(worker_offset_ms):
    now = D - timedelta(days=1)
    clock, repo, scheduler = Clock(now), InMemoryCaptainRepository(), Scheduler()

    class Router:
        destination_user_id = PRODUCTION_ID

        def ensure(self, *_):
            pytest.fail("rehearsal reached production publication router")

    app = compose(
        PRODUCTION_CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        clock,
        scheduler,
        SimpleNamespace(request=lambda *_: pytest.fail("no task delivery requested")),
        Authorizer(),
        publisher_router=Router(),
        rehearsal_enabled=True,
    )
    client = app.test_client()
    target = now + timedelta(minutes=10)
    response = client.post(
        "/captain/control/rehearsal",
        json={
            "schema_version": 1,
            "rehearsal_id": str(UUID(int=711)),
            "release_utc": target.isoformat().replace("+00:00", "Z"),
        },
        headers={"Authorization": CONFIG.planner_email},
    )
    assert response.status_code == 200 and response.json["postable"] is False
    gid = UUID(response.json["generation_id"])
    generation = repo.generation(gid)
    assert generation.key == PostKey("1", 5)
    assert repo.rehearsal(gid).purpose == "non_postable_rehearsal"
    diagnostic_view = client.post(
        "/captain/control/inspect",
        json={"purpose": "non_postable_rehearsal"},
        headers={"Authorization": CONFIG.planner_email},
    )
    production_view = client.post(
        "/captain/control/inspect",
        json={},
        headers={"Authorization": CONFIG.planner_email},
    )
    assert diagnostic_view.status_code == production_view.status_code == 200
    assert from_document(diagnostic_view.json["generation"]).assignment.generation_id == gid
    assert from_document(production_view.json["generation"]) is None
    repo.transition(gid, GenerationStatus.PLANNED, GenerationStatus.WARMING, now)
    repo.transition(gid, GenerationStatus.WARMING, GenerationStatus.READY, now)
    clock.value = target + timedelta(milliseconds=100)
    repo.transition(gid, GenerationStatus.READY, GenerationStatus.RELEASED, target)
    worker_clock = Clock(target + timedelta(milliseconds=worker_offset_ms))
    run_id = UUID(int=712)
    assignment_probe = client.post(
        "/captain/worker/assignment",
        json={"run_id": str(run_id)},
        headers={"Authorization": CONFIG.worker_email},
    )
    assert assignment_probe.status_code == 200, assignment_probe.json

    class Bridge:
        def obtain_assignment(self, requested_run_id):
            reply = client.post(
                "/captain/worker/assignment",
                json={"run_id": str(requested_run_id)},
                headers={"Authorization": CONFIG.worker_email},
            )
            assert reply.status_code == 200
            return parse_worker_assignment(reply.json)

        def await_release(self, work, requested_run_id, **_):
            reply = client.post(
                "/captain/worker/release",
                json={"run_id": str(requested_run_id), "work": worker_assignment_payload(work)},
                headers={"Authorization": CONFIG.worker_email},
            )
            assert reply.status_code == 200
            return ReleaseGrant(
                parse_worker_assignment(reply.json["work"]),
                UUID(reply.json["run_id"]),
                datetime.fromisoformat(reply.json["issued_at"].replace("Z", "+00:00")),
                datetime.fromisoformat(reply.json["server_sent_at"].replace("Z", "+00:00")),
            )

        def report_failure(self, work, requested_run_id, status, diagnostic=None):
            reply = client.post(
                "/captain/worker/failure",
                json={
                    "run_id": str(requested_run_id),
                    "work": worker_assignment_payload(work) if work else None,
                    "status": status.value,
                    "release_rejection": diagnostic.to_payload() if diagnostic else None,
                },
                headers={"Authorization": CONFIG.worker_email},
            )
            assert reply.status_code == 200

    acquired = []
    worker = CaptainWorker(
        Bridge(),
        SimpleNamespace(acquire=lambda event_id: acquired.append(event_id)),
        worker_clock,
        expected_user="captaintrial",
        current_user=lambda: "captaintrial",
        new_run_id=lambda: run_id,
    )
    return worker.run(), acquired, repo, gid, run_id


def test_production_composition_rehearsal_uses_real_release_predicates_and_central_audit():
    result, acquired, repo, gid, run_id = _dual_rehearsal(50)
    assert result.status == WorkerStatus.RELEASE_REJECTED, result.release_rejection
    assert result.release_rejection.code == ReleaseRejectionCode.ISSUED_IN_WORKER_FUTURE
    assert repo.release_failure(run_id).diagnostic == result.release_rejection
    assert acquired == []
    assert repo.candidate(gid) is None
    assert repo.posting(PostKey("1", 5)).attempts == ()
    assert repo.posting(PostKey(PRODUCTION_ID, 5)).attempts == ()


def test_production_composition_rehearsal_accepts_valid_release_without_posting():
    result, acquired, repo, gid, run_id = _dual_rehearsal(200)
    assert result.status == WorkerStatus.ACQUISITION_FAILED, result.release_rejection
    assert acquired == [5]
    assert repo.release_failure(run_id) is None
    assert repo.candidate(gid) is None
    assert repo.posting(PostKey(PRODUCTION_ID, 5)).attempts == ()


def test_dual_purpose_assignment_fails_closed_when_both_purposes_are_live():
    now = D - timedelta(hours=2, minutes=20)
    repo, clock = InMemoryCaptainRepository(), Clock(now)
    scheduler = Scheduler()

    class Router:
        destination_user_id = PRODUCTION_ID

        def ensure(self, *_):
            pytest.fail("no candidate exists")

    app = compose(
        PRODUCTION_CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        clock,
        scheduler,
        SimpleNamespace(request=lambda *_: pytest.fail("VM operation")),
        Authorizer(),
        publisher_router=Router(),
        rehearsal_enabled=True,
    )
    client = app.test_client()
    response = client.post(
        "/captain/control/rehearsal",
        json={
            "schema_version": 1,
            "rehearsal_id": str(UUID(int=713)),
            "release_utc": (now + timedelta(minutes=10)).isoformat(),
        },
        headers={"Authorization": CONFIG.planner_email},
    )
    rehearsal_id = UUID(response.json["generation_id"])
    active_at = now + timedelta(minutes=10)
    repo.transition(rehearsal_id, GenerationStatus.PLANNED, GenerationStatus.WARMING, active_at)
    production = CaptainController(repo, Source(), clock, InMemoryVmOperations(repo))
    production_id = production.plan(PRODUCTION_ID)[0].generation.assignment.generation_id
    assert any(intent.generation_id == production_id for intent in repo.pending_intents())
    diagnostic_reconcile = client.post(
        "/captain/control/rehearsal/reconcile",
        json={},
        headers={"Authorization": CONFIG.planner_email},
    )
    assert diagnostic_reconcile.status_code == 200
    assert all(task.destination_user_id == "1" for task in scheduler.tasks.values())
    assert any(intent.generation_id == production_id for intent in repo.pending_intents())
    repo.transition(production_id, GenerationStatus.PLANNED, GenerationStatus.WARMING, active_at)
    clock.value = active_at
    assignment = client.post(
        "/captain/worker/assignment",
        json={"run_id": str(UUID(int=714))},
        headers={"Authorization": CONFIG.worker_email},
    )
    assert assignment.status_code == 204
    assert repo.posting(PostKey(PRODUCTION_ID, 5)).attempts == ()


def test_dual_purpose_rejects_mismatched_assignment_without_release_claim():
    now = D - timedelta(days=1)
    repo, clock = InMemoryCaptainRepository(), Clock(now)
    generation = (
        CaptainController(repo, Source(), clock, InMemoryVmOperations(repo))
        .plan(PRODUCTION_ID)[0]
        .generation
    )
    service = DualPurposeWorkerService(
        repo,
        WorkerControllerService(repo, Source(), clock, PRODUCTION_ID),
        WorkerControllerService(repo, Source(), clock, "1"),
    )
    run_id = UUID(int=715)
    wrong = replace(generation.assignment, assignment_id=UUID(int=716))
    from fpl_bot.captain_worker import WorkerAssignment

    with pytest.raises(StateConflict):
        service.release(WorkerAssignment(1, wrong, run_id), run_id)
    assert repo.generation_acquisition(generation.assignment.generation_id) is None


def test_diagnostic_reconcile_cannot_operate_a_production_vm_lease():
    clock = Clock(D - timedelta(hours=2, minutes=20))
    repo = InMemoryCaptainRepository()
    operations = InMemoryVmOperations(repo)
    controller = CaptainController(repo, Source(), clock, operations)
    production = controller.plan(PRODUCTION_ID)[0].generation
    clock.value += timedelta(minutes=10)
    repo.acquire_vm(production.assignment.generation_id, UUID(int=717), clock.value)
    driver = NoPostDriver(
        controller,
        Scheduler(),
        SimpleNamespace(submit=lambda *_: pytest.fail("cross-purpose Compute dispatch")),
    )
    with pytest.raises(StateConflict, match="another Captain purpose"):
        driver.advance("1")
    assert repo.vm_use().generation_id == production.assignment.generation_id


def test_rehearsal_reconcile_does_not_plan_or_mutate_posting_state():
    repo, scheduler = InMemoryCaptainRepository(), Scheduler()
    app = compose(
        CONFIG,
        repo,
        InMemoryVmOperations(repo),
        Source(),
        Clock(D - timedelta(days=1)),
        scheduler,
        SimpleNamespace(request=lambda *_: pytest.fail("no lease exists")),
        Authorizer(),
    )
    response = app.test_client().post(
        "/captain/control/reconcile", headers={"Authorization": CONFIG.planner_email}
    )
    assert response.status_code == 200
    assert response.json == {"no_post": True, "postable": False, "vm": "idle"}
    assert repo.pending_intents() == () and scheduler.tasks == {}


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
