import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from fpl_bot.captain_controller import CaptainController, TaskConfirmation, TaskEnvelope
from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_state import (
    GenerationStatus,
    IntentStatus,
    PostingStatus,
    SessionExpiryKind,
    SessionHealthEvidence,
    StateConflict,
    TaskKind,
    VmPhase,
)
from fpl_bot.captain_vm_operations import (
    InMemoryVmOperations,
    OperationPhase,
    VmAction,
    VmOperation,
)
from fpl_bot.errors import DataValidationError


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class FakeClock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


class FakeFpl:
    def __init__(self, deadline):
        self.bootstrap = {
            "events": [{"id": 5, "name": "Gameweek 5", "deadline_time": deadline, "is_next": True}],
            "teams": [{"id": i, "name": f"Team {i}", "short_name": f"T{i}"} for i in range(1, 21)],
        }
        self.fixtures = {
            5: [{"id": i, "event": 5, "team_h": i, "team_a": i + 1} for i in range(1, 21, 2)]
        }
        self.reads = 0
        self.on_fixtures = lambda: None

    def fetch_bootstrap_static(self):
        self.reads += 1
        return deepcopy(self.bootstrap)

    def fetch_event_fixtures(self, event_id):
        self.on_fixtures()
        return deepcopy(self.fixtures[event_id])


class FakeScheduler:
    def __init__(self):
        self.tasks = {}
        self.calls = 0
        self.fail_call = None
        self.after_create = lambda: None

    def ensure(self, task):
        self.calls += 1
        previous = self.tasks.setdefault(task.identity, task)
        if previous != task:
            raise StateConflict("external payload conflict")
        self.after_create()
        if self.calls == self.fail_call:
            raise RuntimeError("simulated lost create response")
        return TaskConfirmation(task.identity, task.digest)


def build(deadline="2026-09-18T17:30:00Z"):
    d = utc(deadline)
    clock = FakeClock(d - timedelta(hours=2, minutes=15))
    repo, source, operations = (
        InMemoryCaptainRepository(),
        FakeFpl(deadline),
        InMemoryVmOperations(),
    )
    return CaptainController(repo, source, clock, operations), repo, source, clock, operations


def task(controller, generation, kind):
    return controller.envelope(
        controller.repository.task_intent(generation.assignment.generation_id, kind)
    )


def ready(controller):
    generation = controller.plan("12345")[0].generation
    controller.deliver(task(controller, generation, TaskKind.WARMUP))
    lease = controller.repository.vm_use()
    controller.vm_operations.acknowledge(lease.lease_id, VmAction.START)
    controller.vm_operations.finish(lease.lease_id, VmAction.START, succeeded=True)
    controller.confirm_runtime_ready(generation.assignment.generation_id)
    return generation


def accepted(controller, clock):
    generation = ready(controller)
    clock.value = generation.assignment.timing.target_utc
    controller.deliver(task(controller, generation, TaskKind.RELEASE))
    aid = UUID(int=40)
    controller.repository.claim_acquisition(generation.assignment.generation_id, aid, clock.value)
    h = ProjectionHandoff(
        1,
        generation.assignment,
        aid,
        clock.value,
        clock.value + timedelta(seconds=10),
        (ProjectionRecord(1, "João Pedro", "CHE", Decimal("6.48")),),
        RowCounts(1, 1, 0, 1),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )
    clock.value = h.acquisition_ended_utc
    controller.accept_handoff(h)
    return generation


@pytest.mark.parametrize(
    "deadline,target_day,warmup_day",
    [
        ("2026-09-18T17:30:00Z", "2026-09-18", "2026-09-18"),
        ("2026-12-18T17:30:00Z", "2026-12-18", "2026-12-18"),
        ("2026-09-19T00:00:00Z", "2026-09-18", "2026-09-18"),
        ("2026-09-19T01:05:00Z", "2026-09-19", "2026-09-18"),
        ("2026-03-29T03:05:00Z", "2026-03-29", "2026-03-29"),
        ("2026-10-25T03:05:00Z", "2026-10-25", "2026-10-25"),
    ],
)
def test_rolling_planning_calendar_and_idempotence(deadline, target_day, warmup_day):
    c, r, f, clock, _ = build(deadline)
    clock.value -= timedelta(days=2)
    first = c.plan("12345")[0]
    second = c.plan("12345")[0]
    assert first.created and not second.created
    assert first.generation == second.generation
    timing = first.generation.assignment.timing
    assert timing.target_london.date().isoformat() == target_day
    assert timing.warmup_london.date().isoformat() == warmup_day
    assert len(r.pending_intents()) == 3
    assert f.reads == 2
    assert {i.scheduled_at for i in r.pending_intents()} == {
        timing.warmup_utc,
        timing.target_utc,
        timing.expiry_utc,
    }


@pytest.mark.parametrize("kind", ["GW", "BGW", "DGW", "BDGW"])
def test_reuses_official_fixture_classification(kind):
    c, _, f, _, _ = build()
    if kind in {"BGW", "BDGW"}:
        f.fixtures[5].pop()
    if kind in {"DGW", "BDGW"}:
        f.fixtures[5].append({"id": 50, "event": 5, "team_h": 1, "team_a": 3})
    assert c.plan("12345")[0].generation.assignment.event_code == f"{kind}5"


@pytest.mark.parametrize("fault", ["teams", "zero_fixtures", "chronology", "date"])
def test_invalid_official_data_fails_without_state(fault):
    c, r, f, _, _ = build()
    if fault == "teams":
        f.bootstrap["teams"].pop()
    elif fault == "zero_fixtures":
        f.fixtures[5] = []
    elif fault == "date":
        f.bootstrap["events"][0]["deadline_time"] = "bad"
    else:
        f.bootstrap["events"].append(
            {"id": 4, "name": "earlier", "deadline_time": "2026-09-18T17:00:00Z"}
        )
    with pytest.raises(DataValidationError):
        c.plan("12345")
    assert r.pending_intents() == ()


def test_changed_deadline_stales_generation_and_every_task():
    c, r, f, _, _ = build()
    old = c.plan("12345")[0].generation
    old_tasks = [c.envelope(i) for i in r.pending_intents()]
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    new = c.plan("12345")[0]
    assert new.created and new.generation.version == 2
    assert r.generation(old.assignment.generation_id).status == GenerationStatus.CANCELLED_STALE
    for delivery in old_tasks:
        assert (
            r.task_intent(old.assignment.generation_id, delivery.kind).status
            == IntentStatus.CANCELLED
        )
        with pytest.raises(StateConflict):
            c.deliver(delivery)
    assert not c.plan("12345")[0].created
    # Moving back to the original deadline must not resurrect its old generation UUID.
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-18T17:30:00Z"
    reverted = c.plan("12345")[0].generation
    assert reverted.assignment.generation_id != old.assignment.generation_id


def test_horizon_advances_to_next_captain_window_before_current_deadline():
    c, _, f, clock, _ = build()
    clock.value += timedelta(hours=1)
    f.bootstrap["events"].append({"id": 6, "name": "next", "deadline_time": "2026-09-25T17:30:00Z"})
    f.fixtures[6] = [{**row, "event": 6} for row in f.fixtures[5]]
    results = c.plan("12345")
    assert [p.generation.assignment.event_id for p in results] == [5, 6]
    assert results[0].generation.status == GenerationStatus.MISSED
    assert results[1].generation.status == GenerationStatus.PLANNED


def test_known_deadline_moved_into_past_is_also_superseded():
    c, r, f, _, _ = build()
    old = c.plan("12345")[0].generation
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-17T17:30:00Z"
    f.bootstrap["events"].append({"id": 6, "name": "next", "deadline_time": "2026-09-25T17:30:00Z"})
    f.fixtures[6] = [{**row, "event": 6} for row in f.fixtures[5]]
    results = c.plan("12345")
    assert r.generation(old.assignment.generation_id).status == GenerationStatus.CANCELLED_STALE
    assert results[0].generation.status == GenerationStatus.MISSED


def test_success_blocks_repost_across_deadline_change():
    c, r, f, clock, _ = build()
    g = accepted(c, clock)
    claim = UUID(int=41)
    r.claim_post(g.assignment.generation_id, claim, clock.value)
    r.start_write(g.key, claim, clock.value)
    r.finish_post(g.key, claim, PostingStatus.SUCCEEDED, clock.value, "987654")
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    new = c.plan("12345")[0]
    assert new.posting_suppressed
    assert c.deliver(task(c, new.generation, TaskKind.RELEASE)).status == "posting_suppressed"
    scheduler = FakeScheduler()
    c.reconcile_tasks(scheduler)
    assert all(t.kind == TaskKind.CLEANUP for t in scheduler.tasks.values())
    assert len(r.posting(g.key).attempts) == 1


@pytest.mark.parametrize("failed_call", [1, 2, 3])
def test_partial_outbox_create_crash_and_duplicate_reconciliation(failed_call):
    c, r, _, _, _ = build()
    c.plan("12345")
    scheduler = FakeScheduler()
    scheduler.fail_call = failed_call
    with pytest.raises(RuntimeError):
        c.reconcile_tasks(scheduler)
    assert len(r.pending_intents()) == 4 - failed_call
    scheduler.fail_call = None
    c.reconcile_tasks(scheduler)
    assert len(scheduler.tasks) == 3
    assert not r.pending_intents()
    before = scheduler.calls
    assert c.reconcile_tasks(scheduler) == ()
    assert scheduler.calls == before


def test_task_identity_stable_and_wrong_confirmation_not_acknowledged():
    c, r, _, _, _ = build()
    g = c.plan("12345")[0].generation
    independent, _, _, _, _ = build()
    same = independent.plan("12345")[0].generation
    assert same.assignment == g.assignment
    envelope = task(c, g, TaskKind.RELEASE)
    assert envelope.identity == task(independent, same, TaskKind.RELEASE).identity
    assert envelope.digest == task(independent, same, TaskKind.RELEASE).digest

    class BadScheduler:
        def ensure(self, task):
            return TaskConfirmation(task.identity, "wrong")

    with pytest.raises(StateConflict):
        c.reconcile_tasks(BadScheduler())
    assert len(r.pending_intents()) == 3


def test_supersession_between_external_create_and_ack_is_safe():
    c, r, f, _, _ = build()
    old = c.plan("12345")[0].generation
    scheduler = FakeScheduler()

    def change():
        f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
        c.plan("12345")

    scheduler.after_create = change
    with pytest.raises(StateConflict):
        c.reconcile_tasks(scheduler)
    assert r.generation(old.assignment.generation_id).status == GenerationStatus.CANCELLED_STALE
    for envelope in scheduler.tasks.values():
        with pytest.raises(StateConflict):
            c.deliver(envelope)


def test_warmup_and_release_never_acquire_early():
    c, r, _, clock, ops = build()
    g = c.plan("12345")[0].generation
    clock.value -= timedelta(microseconds=1)
    assert c.deliver(task(c, g, TaskKind.WARMUP)).status == "early"
    assert r.vm_use() is None
    clock.value = g.assignment.timing.warmup_utc
    assert c.deliver(task(c, g, TaskKind.WARMUP)).status == "start_requested"
    lease = r.vm_use()
    assert ops.get(lease.lease_id, VmAction.START).phase == OperationPhase.REQUESTED
    assert c.deliver(task(c, g, TaskKind.RELEASE)).status == "early"
    assert r.generation_acquisition(g.assignment.generation_id) is None
    with pytest.raises(StateConflict):
        c.confirm_runtime_ready(g.assignment.generation_id)


@pytest.mark.parametrize(
    "seconds,status",
    [(-0.000001, "early"), (0, "released"), (300, "released"), (300.000001, "missed")],
)
def test_release_boundaries(seconds, status):
    c, r, _, clock, _ = build()
    g = ready(c)
    clock.value = g.assignment.timing.target_utc + timedelta(seconds=seconds)
    assert c.deliver(task(c, g, TaskKind.RELEASE)).status == status
    assert r.generation_acquisition(g.assignment.generation_id) is None
    if status == "missed":
        assert r.generation(g.assignment.generation_id).status == GenerationStatus.MISSED


def test_slow_fpl_fetch_crossing_l_is_rejected():
    c, r, f, clock, _ = build()
    g = ready(c)
    clock.value = g.assignment.timing.target_utc
    f.on_fixtures = lambda: setattr(
        clock, "value", g.assignment.timing.expiry_utc + timedelta(seconds=1)
    )
    assert c.deliver(task(c, g, TaskKind.RELEASE)).status == "missed"
    assert r.generation(g.assignment.generation_id).status == GenerationStatus.MISSED


def test_delivery_fresh_deadline_mismatch_fails_without_releasing():
    c, r, f, clock, _ = build()
    g = ready(c)
    clock.value = g.assignment.timing.target_utc
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    with pytest.raises(StateConflict):
        c.deliver(task(c, g, TaskKind.RELEASE))
    assert r.generation(g.assignment.generation_id).status == GenerationStatus.READY


def test_publish_outbox_and_no_write_delivery():
    c, r, _, clock, _ = build()
    g = accepted(c, clock)
    publish = task(c, g, TaskKind.PUBLISH)
    assert c.deliver(publish).status == "publish_eligible_no_write"
    assert not r.posting(g.key).attempts
    assert c.deliver(publish).status == "publish_eligible_no_write"
    clock.value = g.assignment.timing.expiry_utc + timedelta(microseconds=1)
    assert c.deliver(publish).status == "missed"
    assert not r.posting(g.key).attempts


def test_stale_cleanup_and_async_stop_fence():
    c, r, f, clock, ops = build()
    g = ready(c)
    lease = r.vm_use()
    cleanup = task(c, g, TaskKind.CLEANUP)
    clock.value = g.assignment.timing.expiry_utc
    assert c.deliver(cleanup).status == "early"
    clock.value += timedelta(microseconds=1)
    assert c.deliver(cleanup).status == "cleanup_requested"
    assert r.vm_use().phase == VmPhase.STOPPING
    with pytest.raises(StateConflict):
        c.complete_cleanup(lease.lease_id, lease.generation_id)
    ops.acknowledge(lease.lease_id, VmAction.STOP)
    with pytest.raises(StateConflict):
        c.complete_cleanup(lease.lease_id, lease.generation_id)
    ops.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True)
    c.complete_cleanup(lease.lease_id, lease.generation_id)
    assert r.vm_use() is None
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    new = c.plan("12345")[0].generation
    clock.value = new.assignment.timing.warmup_utc
    c.deliver(task(c, new, TaskKind.WARMUP))
    with pytest.raises(StateConflict):
        c.deliver(cleanup)
    with pytest.raises(StateConflict):
        c.request_cleanup(lease.lease_id, lease.generation_id)
    assert r.vm_use().generation_id == new.assignment.generation_id


def test_outstanding_start_prevents_stop_ack_and_cleanup_release():
    c, r, _, clock, ops = build()
    g = c.plan("12345")[0].generation
    c.deliver(task(c, g, TaskKind.WARMUP))
    lease = r.vm_use()
    c.request_cleanup(lease.lease_id, lease.generation_id)
    assert ops.get(lease.lease_id, VmAction.STOP).phase == OperationPhase.REQUESTED
    with pytest.raises(StateConflict):
        ops.acknowledge(lease.lease_id, VmAction.STOP)
    with pytest.raises(StateConflict):
        c.complete_cleanup(lease.lease_id, lease.generation_id)
    ops.acknowledge(lease.lease_id, VmAction.START)
    ops.finish(lease.lease_id, VmAction.START, succeeded=False)
    ops.acknowledge(lease.lease_id, VmAction.STOP)
    ops.finish(lease.lease_id, VmAction.STOP, succeeded=False)
    assert not ops.stop_is_settled(lease.lease_id)
    assert r.vm_use().phase == VmPhase.STOPPING
    clock.value += timedelta(hours=1)
    with pytest.raises(StateConflict):
        c.complete_cleanup(lease.lease_id, lease.generation_id)


def test_crash_before_start_intent_and_after_stop_fence_recoverable():
    c, r, _, _, ops = build()
    g = c.plan("12345")[0].generation
    c.deliver(task(c, g, TaskKind.WARMUP))
    lease = r.vm_use()
    # Same delivery confirms the durable start; it does not create another operation.
    c.deliver(task(c, g, TaskKind.WARMUP))
    assert not ops.request(lease, VmAction.START).applied
    fenced = r.begin_cleanup(lease.lease_id, lease.generation_id).record
    c.request_cleanup(lease.lease_id, lease.generation_id)
    assert not ops.request(fenced, VmAction.STOP).applied


def test_optional_session_warning_creates_no_extra_browser_intent():
    c, r, _, clock, _ = build()
    g = c.plan("12345")[0].generation
    assert not c.plan("12345")[0].session_warning
    r.record_session_health(
        g.assignment.generation_id,
        SessionHealthEvidence(
            clock.value,
            AuthenticationStatus.AUTHENTICATED,
            SessionExpiryKind.FIXED,
            g.assignment.timing.target_utc,
            g.assignment.timing.target_utc,
        ),
    )
    assert c.plan("12345")[0].session_warning
    assert len(r.pending_intents()) == 3


def test_immutable_task_tampering_rejected():
    c, _, _, _, _ = build()
    g = c.plan("12345")[0].generation
    original = task(c, g, TaskKind.RELEASE)
    with pytest.raises(StateConflict):
        c.deliver(replace(original, destination_user_id="99999"))
    with pytest.raises(StateConflict):
        c.deliver(replace(original, scheduled_at=original.scheduled_at + timedelta(seconds=1)))
    with pytest.raises(ValueError):
        TaskEnvelope("12345", g.assignment, "release", original.scheduled_at)


def test_provider_and_browser_modules_not_imported():
    for name in ("captain_controller", "captain_vm_operations"):
        tree = ast.parse((Path("src/fpl_bot") / f"{name}.py").read_text(encoding="utf-8"))
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not any(
            any(term in module for term in ("google", "api", "browser", "x_client", "service"))
            for module in imports
        )


def test_empty_future_horizon_still_reconciles_known_deadline_change():
    c, r, f, _, _ = build()
    old = c.plan("12345")[0].generation
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-17T17:30:00Z"
    result = c.plan("12345")[0]
    assert result.created and result.generation.status == GenerationStatus.MISSED
    assert r.generation(old.assignment.generation_id).status == GenerationStatus.CANCELLED_STALE
    assert c.plan("12345") == ()


def test_rolling_horizon_skips_multiple_expired_windows_and_delivery_uses_it():
    c, _, f, clock, _ = build("2026-09-18T16:00:00Z")
    clock.value = utc("2026-09-18T15:00:00Z")
    for eid, deadline in [(6, "2026-09-18T16:30:00Z"), (7, "2026-09-18T17:15:00Z")]:
        f.bootstrap["events"].append({"id": eid, "name": "later", "deadline_time": deadline})
        f.fixtures[eid] = [{**row, "event": eid} for row in f.fixtures[5]]
    results = c.plan("12345")
    assert [p.generation.assignment.event_id for p in results] == [5, 7]
    assert results[0].generation.status == GenerationStatus.MISSED
    assert c.deliver(task(c, results[1].generation, TaskKind.WARMUP)).status == "start_requested"


def test_concurrent_plan_during_classification_cannot_roll_back_newer_generation():
    c, r, f, clock, _ = build()
    old = c.plan("12345")[0].generation
    newer = replace(
        old.assignment,
        assignment_id=UUID(int=800),
        generation_id=UUID(int=801),
        timing=replace(
            old.assignment.timing,
            deadline_utc=old.assignment.timing.deadline_utc + timedelta(days=1),
        ),
    )
    f.on_fixtures = lambda: r.plan(old.key, newer, old.assignment.generation_id, clock.value)
    with pytest.raises(StateConflict, match="snapshot changed"):
        c.plan("12345")
    assert r.current(old.key).assignment == newer


def test_event_code_change_also_supersedes_generation():
    c, r, f, _, _ = build()
    old = c.plan("12345")[0].generation
    f.fixtures[5].pop()
    new = c.plan("12345")[0]
    assert new.created and new.generation.assignment.event_code == "BGW5"
    assert r.generation(old.assignment.generation_id).status == GenerationStatus.CANCELLED_STALE


def test_runtime_callback_revalidates_deadline():
    c, r, f, _, ops = build()
    g = ready(c)
    lease = r.vm_use()
    assert ops.get(lease.lease_id, VmAction.START).phase == OperationPhase.COMPLETED
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    with pytest.raises(StateConflict):
        c.confirm_runtime_ready(g.assignment.generation_id)


@pytest.mark.parametrize(
    "outcome",
    [
        PostingStatus.CLAIMED,
        PostingStatus.WRITE_STARTED,
        PostingStatus.UNCERTAIN,
        PostingStatus.SUCCEEDED,
    ],
)
def test_publish_rechecks_posting_barrier_after_fpl_call(outcome):
    c, r, f, clock, _ = build()
    g = accepted(c, clock)

    def claim_during_fetch():
        claim = UUID(int=900)
        r.claim_post(g.assignment.generation_id, claim, clock.value)
        if outcome != PostingStatus.CLAIMED:
            r.start_write(g.key, claim, clock.value)
        if outcome in {PostingStatus.UNCERTAIN, PostingStatus.SUCCEEDED}:
            r.finish_post(
                g.key,
                claim,
                outcome,
                clock.value,
                "76543" if outcome == PostingStatus.SUCCEEDED else None,
            )

    f.on_fixtures = claim_during_fetch
    assert c.deliver(task(c, g, TaskKind.PUBLISH)).status == "posting_suppressed"


@pytest.mark.parametrize("offset", [-1, 0, 10])
def test_late_warmup_never_creates_new_start(offset):
    c, r, _, clock, _ = build()
    g = c.plan("12345")[0].generation
    clock.value = g.assignment.timing.target_utc + timedelta(seconds=offset)
    status = c.deliver(task(c, g, TaskKind.WARMUP)).status
    assert status == ("start_requested" if offset < 0 else "warmup_window_closed")
    assert (r.vm_use() is None) == (offset >= 0)


def test_reconcile_repairs_acquired_lease_without_start_record():
    c, r, _, clock, ops = build()
    g = c.plan("12345")[0].generation
    gid = g.assignment.generation_id
    r.transition(gid, GenerationStatus.PLANNED, GenerationStatus.WARMING, clock.value)
    lease = r.acquire_vm(gid, uuid5(gid, "vm-use"), clock.value).record
    assert ops.get(lease.lease_id, VmAction.START) is None
    assert c.reconcile_vm() == "start_recorded"
    assert c.reconcile_vm() == "start_recorded"
    assert ops.get(lease.lease_id, VmAction.START).phase == OperationPhase.REQUESTED


def test_reconcile_late_missing_start_seals_instead_of_booting():
    c, r, _, clock, ops = build()
    g = c.plan("12345")[0].generation
    lease = r.acquire_vm(g.assignment.generation_id, UUID(int=910), clock.value).record
    clock.value = g.assignment.timing.target_utc
    assert c.reconcile_vm() == "stop_pending"
    assert ops.get(lease.lease_id, VmAction.START) is None
    with pytest.raises(StateConflict):
        ops.request(lease, VmAction.START)


def test_cancelled_cleanup_intent_cannot_strand_the_old_vm_owner():
    c, r, f, _, ops = build()
    old = ready(c)
    lease = r.vm_use()
    f.bootstrap["events"][0]["deadline_time"] = "2026-09-19T17:30:00Z"
    c.plan("12345")
    assert (
        r.task_intent(old.assignment.generation_id, TaskKind.CLEANUP).status
        == IntentStatus.CANCELLED
    )
    assert c.reconcile_vm() == "stop_pending"
    assert r.vm_use().phase == VmPhase.STOPPING
    ops.acknowledge(lease.lease_id, VmAction.STOP)
    assert c.reconcile_vm() == "stop_pending"
    ops.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True)
    assert c.reconcile_vm() == "released"
    assert c.reconcile_vm() == "idle"


def test_accepted_handoff_causes_owned_vm_cleanup_without_waiting_until_l():
    c, r, _, clock, ops = build()
    accepted(c, clock)
    lease = r.vm_use()
    assert c.reconcile_vm() == "stop_pending"
    assert ops.get(lease.lease_id, VmAction.STOP).phase == OperationPhase.REQUESTED
    assert r.vm_use().phase == VmPhase.STOPPING


def test_operation_replays_and_conflicting_outcomes():
    c, r, _, _, ops = build()
    g = ready(c)
    lease = r.vm_use()
    assert not ops.acknowledge(lease.lease_id, VmAction.START).applied
    assert not ops.finish(lease.lease_id, VmAction.START, succeeded=True).applied
    with pytest.raises(StateConflict):
        ops.finish(lease.lease_id, VmAction.START, succeeded=False)
    with pytest.raises(StateConflict):
        ops.request(replace(lease, generation_id=UUID(int=999)), VmAction.START)
    c.request_cleanup(lease.lease_id, g.assignment.generation_id)
    ops.acknowledge(lease.lease_id, VmAction.STOP)
    with pytest.raises(StateConflict):
        ops.finish(lease.lease_id, VmAction.STOP, succeeded=True)
    with pytest.raises(StateConflict):
        ops.finish(lease.lease_id, VmAction.STOP, succeeded=False, terminated=True)
    assert not ops.stop_is_settled(lease.lease_id)
    ops.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True)
    assert not ops.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True).applied
    assert not ops.acknowledge(lease.lease_id, VmAction.STOP).applied
    c.complete_cleanup(lease.lease_id, g.assignment.generation_id)
    c.complete_cleanup(lease.lease_id, g.assignment.generation_id)
    assert r.vm_use() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_id": UUID(int=0)},
        {"action": "start"},
        {"phase": "completed"},
        {"terminated_confirmed": "true"},
        {"terminated_confirmed": True},
        {"action": VmAction.STOP, "phase": OperationPhase.COMPLETED},
    ],
)
def test_invalid_vm_operation_record(changes):
    args = {
        "lease_id": UUID(int=1),
        "generation_id": UUID(int=2),
        "action": VmAction.START,
        **changes,
    }
    with pytest.raises(StateConflict):
        VmOperation(**args)
