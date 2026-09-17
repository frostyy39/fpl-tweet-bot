from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fpl_bot.captain_controller import CaptainController, TaskEnvelope
from fpl_bot.captain_http import (
    CallerIdentity,
    CaptainAuthConfig,
    WorkerControllerService,
    create_captain_app,
)
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_state import GenerationStatus, PostKey, TaskKind
from fpl_bot.captain_vm_operations import InMemoryVmOperations

D = datetime(2026, 9, 18, 17, 30, tzinfo=UTC)


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


class Source:
    def __init__(self):
        self.bootstrap = {
            "events": [
                {"id": 5, "name": "Gameweek 5", "deadline_time": D.isoformat(), "is_next": True}
            ],
            "teams": [{"id": i, "name": f"Team {i}", "short_name": f"T{i}"} for i in range(1, 21)],
        }
        self.fixtures = [
            {"id": i, "event": 5, "team_h": i, "team_a": i + 1} for i in range(1, 21, 2)
        ]

    def fetch_bootstrap_static(self):
        return deepcopy(self.bootstrap)

    def fetch_event_fixtures(self, event_id):
        return deepcopy(self.fixtures)


class Authorizer:
    def __init__(self):
        self.calls, self.allowed = [], True

    def authorize(self, header, audience):
        self.calls.append((header, audience))
        if not self.allowed:
            raise PermissionError("denied")
        email = (
            "worker@captain.invalid" if audience.endswith("/worker") else "tasks@captain.invalid"
        )
        return CallerIdentity("subject", email)


def setup():
    clock, source, repo = (
        Clock(D - timedelta(hours=2, minutes=15)),
        Source(),
        InMemoryCaptainRepository(),
    )
    controller = CaptainController(repo, source, clock, InMemoryVmOperations())
    generation = controller.plan("12345")[0].generation
    warmup = controller.envelope(
        repo.task_intent(generation.assignment.generation_id, TaskKind.WARMUP)
    )
    controller.deliver(warmup)
    service = WorkerControllerService(repo, source, clock, "12345")
    authorizer = Authorizer()
    auth = CaptainAuthConfig(
        "https://captain.invalid/worker",
        "https://captain.invalid/tasks",
        "worker@captain.invalid",
        "tasks@captain.invalid",
    )
    return (
        controller,
        service,
        authorizer,
        create_captain_app(service, controller, authorizer, auth),
        generation,
        clock,
    )


def test_unauthorized_worker_is_rejected_before_repository_read():
    controller, service, authorizer, app, generation, clock = setup()
    authorizer.allowed = False
    original = service.repository.current
    service.repository.current = lambda key: (_ for _ in ()).throw(AssertionError("mutation/read"))
    response = app.test_client().post(
        "/captain/worker/assignment",
        json={"run_id": str(UUID(int=9))},
        headers={"Authorization": "Bearer hidden"},
    )
    service.repository.current = original
    assert response.status_code == 403 and response.json == {"error": "unauthorized"}


def test_authenticated_assignment_and_release_identity_and_time_are_enforced():
    _, service, _, app, generation, clock = setup()
    headers = {"Authorization": "Bearer opaque"}
    response = app.test_client().post(
        "/captain/worker/assignment", json={"run_id": str(UUID(int=9))}, headers=headers
    )
    assert response.status_code == 200
    work = response.json
    assert work["attempt_id"] == str(UUID(int=9))
    pending = app.test_client().post(
        "/captain/worker/release", json={"run_id": str(UUID(int=9)), "work": work}, headers=headers
    )
    assert pending.status_code == 202
    work["attempt_id"] = str(UUID(int=8))
    mismatch = app.test_client().post(
        "/captain/worker/release", json={"run_id": str(UUID(int=9)), "work": work}, headers=headers
    )
    assert mismatch.status_code == 409


def test_task_handler_revalidates_early_delivery_and_has_no_x_surface():
    controller, _, _, app, generation, _ = setup()
    intent = controller.repository.task_intent(
        generation.assignment.generation_id, TaskKind.RELEASE
    )
    envelope = controller.envelope(intent)
    response = app.test_client().post(
        "/captain/tasks/release",
        json={
            "version": 1,
            "identity": envelope.identity,
            "digest": envelope.digest,
            "destination_user_id": envelope.destination_user_id,
            "assignment": envelope.assignment.to_payload(),
            "kind": envelope.kind.value,
            "scheduled_at": envelope.scheduled_at.isoformat().replace("+00:00", "Z"),
        },
        headers={"Authorization": "Bearer opaque"},
    )
    assert response.status_code == 200 and response.json["status"] == "early"
    assert generation.status == GenerationStatus.PLANNED
    assert all("post" not in rule.rule for rule in app.url_map.iter_rules())


def test_config_requires_separate_worker_and_task_identities():
    try:
        CaptainAuthConfig("a", "b", "same", "same")
    except ValueError as error:
        assert "distinct" in str(error)
    else:
        raise AssertionError("shared identity accepted")


def test_publish_candidate_handler_uses_envelope_assignment():
    controller, service, authorizer, _, generation, _ = setup()
    handoff = object()
    controller.deliver = lambda envelope: SimpleNamespace(status="publish_eligible_no_write")
    controller.repository.generation = lambda generation_id: generation
    controller.repository.generation_acquisition = lambda generation_id: SimpleNamespace(
        handoff=handoff
    )

    class Validator:
        def validate(self, key, received):
            assert key == generation.key and received is handoff
            return SimpleNamespace(
                key=PostKey("12345", 5),
                assignment=generation.assignment,
                weighted_length=204,
            )

    auth = CaptainAuthConfig(
        "https://captain.invalid/worker",
        "https://captain.invalid/tasks",
        "worker@captain.invalid",
        "tasks@captain.invalid",
    )
    app = create_captain_app(service, controller, authorizer, auth, candidate_validator=Validator())
    envelope = TaskEnvelope(
        "12345",
        generation.assignment,
        TaskKind.PUBLISH,
        generation.assignment.timing.target_utc,
    )
    response = app.test_client().post(
        "/captain/tasks/publish",
        json={
            "version": 1,
            "identity": envelope.identity,
            "digest": envelope.digest,
            "destination_user_id": envelope.destination_user_id,
            "assignment": envelope.assignment.to_payload(),
            "kind": envelope.kind.value,
            "scheduled_at": envelope.scheduled_at.isoformat().replace("+00:00", "Z"),
        },
        headers={"Authorization": "Bearer opaque"},
    )
    assert response.status_code == 200
    assert response.json == {
        "status": "validated_candidate",
        "event_id": 5,
        "event_code": "GW5",
        "weighted_length": 204,
    }
