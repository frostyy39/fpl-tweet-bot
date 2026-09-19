from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from fpl_bot.captain_controller import CaptainController, TaskEnvelope
from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_http import (
    CallerIdentity,
    CaptainAuthConfig,
    WorkerControllerService,
    create_captain_app,
)
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_state import GenerationStatus, PostKey, StateConflict, TaskKind
from fpl_bot.captain_vm_operations import InMemoryVmOperations
from fpl_bot.captain_worker import ReleaseGrant, WorkerAssignment

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


def test_rehearsal_assignment_release_and_handoff_require_fresh_official_binding():
    source, repo = Source(), InMemoryCaptainRepository()
    release_at = D - timedelta(days=1)
    clock = Clock(release_at - timedelta(minutes=10))
    controller = CaptainController(repo, source, clock, InMemoryVmOperations(repo))
    generation = controller.plan_rehearsal("1", UUID(int=700), release_at).generation
    controller.deliver(
        controller.envelope(repo.task_intent(generation.assignment.generation_id, TaskKind.WARMUP))
    )
    repo.transition(
        generation.assignment.generation_id,
        GenerationStatus.WARMING,
        GenerationStatus.READY,
        clock.value,
    )
    service = WorkerControllerService(repo, source, clock, "1")
    run_id = UUID(int=701)
    work = service.assignment(run_id)
    assert work.assignment == generation.assignment
    assert repo.rehearsal(generation.assignment.generation_id).official_deadline_utc == D

    clock.value = release_at
    controller.deliver(
        controller.envelope(repo.task_intent(generation.assignment.generation_id, TaskKind.RELEASE))
    )
    assert isinstance(service.release(work, run_id), ReleaseGrant)
    handoff = ProjectionHandoff(
        1,
        generation.assignment,
        run_id,
        release_at,
        release_at,
        (ProjectionRecord(1, "Player", "TAA", Decimal("5.0")),),
        RowCounts(1, 1, 0, 1),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )
    assert (
        service.handoff(handoff, handoff.payload_digest).generation_id
        == generation.assignment.generation_id
    )

    # Fresh authoritative drift blocks every later replay and cannot overwrite acceptance.
    source.bootstrap["events"][0]["deadline_time"] = (D + timedelta(hours=1)).isoformat()
    assert service.assignment(UUID(int=702)) is None
    assert service.release(work, run_id) is False
    with pytest.raises(StateConflict, match="fresh official FPL"):
        service.handoff(handoff, handoff.payload_digest)


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


def release_setup():
    controller, service, _, _, generation, clock = setup()
    clock.value = generation.assignment.timing.target_utc
    repo = controller.repository
    gid = generation.assignment.generation_id
    repo.transition(gid, GenerationStatus.WARMING, GenerationStatus.READY, clock.value)
    repo.transition(gid, GenerationStatus.READY, GenerationStatus.RELEASED, clock.value)
    return service, repo, clock, WorkerAssignment(1, generation.assignment, UUID(int=99))


@pytest.mark.parametrize("offset,expected", [(-1, None), (0, True), (300, True), (301, False)])
def test_release_fresh_authority_and_exact_window(offset, expected):
    service, repo, clock, work = release_setup()
    clock.value = work.assignment.timing.target_utc + timedelta(seconds=offset)
    result = service.release(work, work.attempt_id)
    if expected is True:
        assert isinstance(result, ReleaseGrant)
        assert repo.generation_acquisition(work.assignment.generation_id) is not None
    else:
        assert result is expected
        assert repo.generation_acquisition(work.assignment.generation_id) is None


@pytest.mark.parametrize("fault", ["deadline", "event", "fetch", "chronology"])
def test_authoritative_release_rejections_never_call_claim(fault):
    service, repo, _, work = release_setup()
    if fault == "deadline":
        service.source.bootstrap["events"][0]["deadline_time"] = (
            D + timedelta(hours=1)
        ).isoformat()
    elif fault == "event":
        service.source.bootstrap["events"][0]["id"] = 6
    elif fault == "fetch":
        service.source.fetch_bootstrap_static = lambda: (_ for _ in ()).throw(TimeoutError())
    else:
        service.source.bootstrap["events"].append(
            {
                "id": 4,
                "name": "Gameweek 4",
                "deadline_time": (D - timedelta(minutes=30)).isoformat(),
            }
        )
    repo.claim_acquisition = lambda *args: (_ for _ in ()).throw(AssertionError("claim called"))
    assert service.release(work, work.attempt_id) is False
    assert repo.generation_acquisition(work.assignment.generation_id) is None


def test_duplicate_release_preserves_one_atomic_attempt():
    service, repo, _, work = release_setup()
    first = service.release(work, work.attempt_id)
    assert service.release(work, work.attempt_id) == first
    other = replace(work, attempt_id=UUID(int=100))
    assert service.release(other, other.attempt_id) is False
    assert repo.generation_acquisition(work.assignment.generation_id).attempt_id == work.attempt_id


def test_release_at_target_waits_for_durable_release_task_without_claiming():
    controller, service, _, _, generation, clock = setup()
    clock.value = generation.assignment.timing.target_utc
    repo = controller.repository
    gid = generation.assignment.generation_id
    repo.transition(gid, GenerationStatus.WARMING, GenerationStatus.READY, clock.value)
    work = WorkerAssignment(1, generation.assignment, UUID(int=99))
    original = repo.claim_acquisition
    repo.claim_acquisition = lambda *args: (_ for _ in ()).throw(
        AssertionError("claim called before durable release")
    )

    assert service.release(work, work.attempt_id) is None
    assert repo.generation_acquisition(gid) is None

    repo.claim_acquisition = original
    repo.transition(gid, GenerationStatus.READY, GenerationStatus.RELEASED, clock.value)
    assert isinstance(service.release(work, work.attempt_id), ReleaseGrant)


@pytest.mark.parametrize("during_claim", [False, True])
def test_generation_supersession_fences_release_including_validation_claim_race(during_claim):
    service, repo, clock, work = release_setup()
    new = replace(
        work.assignment,
        assignment_id=UUID(int=101),
        generation_id=UUID(int=102),
        timing=CaptainTiming(D + timedelta(hours=1)),
    )
    original = repo.claim_acquisition

    def supersede():
        repo.plan(PostKey("12345", 5), new, work.assignment.generation_id, clock.value)

    if during_claim:

        def claim(*args):
            supersede()
            return original(*args)

        repo.claim_acquisition = claim
        with pytest.raises(StateConflict):
            service.release(work, work.attempt_id)
    else:
        supersede()
        assert service.release(work, work.attempt_id) is False
    assert repo.generation_acquisition(work.assignment.generation_id) is None
