import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_repository import CaptainRepository
from fpl_bot.captain_state import (
    AttemptStatus,
    IntentStatus,
    PostKey,
    RefreshObservation,
    SessionExpiryKind,
    SessionHealthEvidence,
    StateConflict,
    TaskKind,
    VmPhase,
)
from fpl_bot.captain_state import (
    GenerationStatus as G,
)
from fpl_bot.captain_state import (
    PostingStatus as P,
)


@pytest.fixture(params=["memory", "firestore"])
def setup(request):
    if request.param == "firestore":
        from test_captain_firestore import adapters

        repository = adapters()[0]
    else:
        repository = InMemoryCaptainRepository()
    assignment = CaptainAssignment(
        UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(datetime(2026, 9, 18, 17, 30, tzinfo=UTC))
    )
    return repository, assignment, PostKey("12345", 5)


def release(repo, a, key, expected=None):
    w, t = a.timing.warmup_utc, a.timing.target_utc
    repo.plan(key, a, expected, w)
    repo.transition(a.generation_id, G.PLANNED, G.WARMING, w)
    repo.transition(a.generation_id, G.WARMING, G.READY, w)
    repo.transition(a.generation_id, G.READY, G.RELEASED, t)


def payload(a, attempt=3):
    return ProjectionHandoff(
        1,
        a,
        UUID(int=attempt),
        a.timing.target_utc,
        a.timing.target_utc + timedelta(seconds=30),
        (ProjectionRecord(1, "João Pedro", "CHE", Decimal("6.48")),),
        RowCounts(1, 1, 0, 1),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )


def accept(repo, a, key, expected=None, attempt=3):
    release(repo, a, key, expected)
    h = payload(a, attempt)
    repo.claim_acquisition(a.generation_id, h.attempt_id, h.acquisition_started_utc)
    repo.accept(h, h.acquisition_ended_utc)
    return h


def newer(a):
    return replace(
        a,
        assignment_id=UUID(int=11),
        generation_id=UUID(int=12),
        timing=CaptainTiming(a.timing.deadline_utc + timedelta(days=1)),
    )


def test_repository_port_and_immutable_snapshots(setup):
    repo, a, key = setup
    assert all(
        hasattr(repo, name)
        for port in CaptainRepository.__mro__
        for name in port.__dict__
        if not name.startswith("_")
    )
    result = repo.plan(key, a, None, a.timing.warmup_utc)
    assert result.applied
    with pytest.raises(FrozenInstanceError):
        result.record.version = 20
    assert repo.posting(key).attempts == ()
    assert repo.current(PostKey("777", 5)) is None


def test_plan_replay_conflict_cas_and_immutable_old_generation(setup):
    repo, a, key = setup
    now = a.timing.warmup_utc
    first = repo.plan(key, a, None, now)
    assert repo.plan(key, a, None, now).record == first.record
    assert not repo.plan(key, a, None, now).applied
    assert len(repo.pending_intents()) == 3
    with pytest.raises(StateConflict):
        repo.plan(key, replace(a, event_code="BGW5"), None, now)
    b = newer(a)
    with pytest.raises(StateConflict):
        repo.plan(key, b, None, now)
    assert repo.plan(key, b, a.generation_id, now).record.version == 2
    assert repo.generation(a.generation_id).status == G.CANCELLED_STALE
    assert not repo.plan(key, a, None, now).applied
    assert repo.current(key).assignment == b
    assert first.record.status == G.PLANNED
    with pytest.raises(StateConflict):
        repo.transition(a.generation_id, G.PLANNED, G.WARMING, now)
    with pytest.raises(StateConflict):
        repo.acknowledge_intent(a.generation_id, TaskKind.WARMUP)


@pytest.mark.parametrize("target", list(G))
def test_planned_transition_matrix(setup, target):
    repo, a, key = setup
    w = a.timing.warmup_utc
    repo.plan(key, a, None, w)
    now = a.timing.expiry_utc + timedelta(microseconds=1) if target == G.MISSED else w
    if target in {G.WARMING, G.FAILED, G.AUTHENTICATION_REQUIRED, G.MISSED}:
        assert repo.transition(a.generation_id, G.PLANNED, target, now).applied
        assert not repo.transition(a.generation_id, G.PLANNED, target, now).applied
    else:
        with pytest.raises(StateConflict):
            repo.transition(a.generation_id, G.PLANNED, target, now)
        assert repo.generation(a.generation_id).status == G.PLANNED


def test_lifecycle_and_post_intent_atomic_acceptance(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    g = repo.generation(a.generation_id)
    assert tuple(s for s, _ in g.history) == (
        G.PLANNED,
        G.WARMING,
        G.READY,
        G.RELEASED,
        G.ACQUIRING,
        G.ACCEPTED,
    )
    assert repo.acquisition(h.attempt_id).status == AttemptStatus.ACCEPTED
    assert {i.kind for i in repo.pending_intents()} == set(TaskKind)
    assert not repo.accept(h, a.timing.expiry_utc + timedelta(days=1)).applied
    assert repo.acquisition(h.attempt_id).handoff.canonical_bytes() == h.canonical_bytes()
    conflict = replace(h, records=(replace(h.records[0], projected_points=Decimal("7")),))
    with pytest.raises(StateConflict, match="payload conflict"):
        repo.accept(conflict, h.acquisition_ended_utc)
    assert repo.acquisition(h.attempt_id).handoff == h
    with pytest.raises(StateConflict):
        repo.transition(a.generation_id, G.ACCEPTED, G.FAILED, h.acquisition_ended_utc)


@pytest.mark.parametrize("failure", [G.FAILED, G.AUTHENTICATION_REQUIRED, G.MISSED])
def test_acquisition_failure_terminal_and_auditable(setup, failure):
    repo, a, key = setup
    release(repo, a, key)
    h = payload(a)
    repo.claim_acquisition(a.generation_id, h.attempt_id, a.timing.target_utc)
    now = a.timing.expiry_utc + timedelta(seconds=1) if failure == G.MISSED else a.timing.target_utc
    repo.transition(a.generation_id, G.ACQUIRING, failure, now)
    assert repo.acquisition(h.attempt_id).status.value == failure.value
    with pytest.raises(StateConflict):
        repo.claim_acquisition(a.generation_id, UUID(int=4), now)
    with pytest.raises(StateConflict):
        repo.accept(h, max(now, h.acquisition_ended_utc))


def test_concurrent_acquisition_claims_one_winner(setup):
    repo, a, key = setup
    release(repo, a, key)

    def claim(n):
        try:
            return repo.claim_acquisition(a.generation_id, UUID(int=n), a.timing.target_utc)
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(100, 132)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert not repo.claim_acquisition(
        a.generation_id, winners[0].record.attempt_id, a.timing.target_utc
    ).applied


@pytest.mark.parametrize("offset,allowed", [(0, True), (300, True), (300.000001, False)])
def test_acquisition_window(setup, offset, allowed):
    repo, a, key = setup
    release(repo, a, key)
    at = a.timing.target_utc + timedelta(seconds=offset)
    if allowed:
        assert repo.claim_acquisition(a.generation_id, UUID(int=3), at).applied
    else:
        with pytest.raises(StateConflict):
            repo.claim_acquisition(a.generation_id, UUID(int=3), at)


def test_early_release_and_missed_boundaries(setup):
    repo, a, key = setup
    w = a.timing.warmup_utc
    repo.plan(key, a, None, w)
    repo.transition(a.generation_id, G.PLANNED, G.WARMING, w)
    repo.transition(a.generation_id, G.WARMING, G.READY, w)
    with pytest.raises(StateConflict):
        repo.transition(
            a.generation_id, G.READY, G.RELEASED, a.timing.target_utc - timedelta(microseconds=1)
        )
    with pytest.raises(StateConflict):
        repo.transition(a.generation_id, G.READY, G.MISSED, a.timing.expiry_utc)
    assert repo.transition(
        a.generation_id, G.READY, G.MISSED, a.timing.expiry_utc + timedelta(microseconds=1)
    ).applied


@pytest.mark.parametrize(
    "bad", ["early", "late", "partial", "auth", "cleanup", "assignment", "claim"]
)
def test_handoff_rejection_is_atomic(setup, bad):
    repo, a, key = setup
    release(repo, a, key)
    h = payload(a)
    repo.claim_acquisition(a.generation_id, h.attempt_id, a.timing.target_utc)
    now = h.acquisition_ended_utc
    if bad == "early":
        h = replace(h, acquisition_started_utc=a.timing.target_utc - timedelta(seconds=1))
    elif bad == "late":
        now = a.timing.expiry_utc + timedelta(microseconds=1)
    elif bad == "partial":
        h = replace(h, completeness=CompletenessStatus.PARTIAL)
    elif bad == "auth":
        h = replace(h, authentication=AuthenticationStatus.REQUIRED)
    elif bad == "cleanup":
        h = replace(h, cleanup=CleanupStatus.UNCLEAN)
    elif bad == "assignment":
        h = replace(h, assignment=replace(a, event_code="BGW5"))
    else:
        h = replace(h, attempt_id=UUID(int=999))
    with pytest.raises(ValueError):
        repo.accept(h, now)
    assert repo.generation(a.generation_id).status == G.ACQUIRING
    assert all(i.kind != TaskKind.PUBLISH for i in repo.pending_intents())


def test_superseding_active_work_rejects_handoff_and_claim(setup):
    repo, a, key = setup
    release(repo, a, key)
    h = payload(a)
    repo.claim_acquisition(a.generation_id, h.attempt_id, a.timing.target_utc)
    repo.plan(key, newer(a), a.generation_id, h.acquisition_ended_utc)
    assert repo.acquisition(h.attempt_id).status == AttemptStatus.CANCELLED_STALE
    with pytest.raises(StateConflict):
        repo.accept(h, h.acquisition_ended_utc)
    with pytest.raises(StateConflict):
        repo.claim_acquisition(a.generation_id, h.attempt_id, h.acquisition_ended_utc)


def test_outbox_crash_reconciliation_and_duplicate_delivery(setup):
    repo, a, key = setup
    repo.plan(key, a, None, a.timing.warmup_utc)
    before = repo.pending_intents()
    # External create succeeded but acknowledgement crashed: intent remains discoverable.
    assert repo.pending_intents() == before
    for intent in before:
        assert repo.acknowledge_intent(a.generation_id, intent.kind).applied
        replay = repo.acknowledge_intent(a.generation_id, intent.kind)
        assert not replay.applied and replay.record.status == IntentStatus.DISPATCHED
    assert not repo.pending_intents()
    assert not repo.plan(key, a, None, a.timing.warmup_utc).applied
    assert not repo.pending_intents()


def test_crash_before_write_can_explicitly_fail_then_retry(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    now = h.acquisition_ended_utc
    first, second = UUID(int=20), UUID(int=21)
    assert repo.claim_post(a.generation_id, first, now).applied
    assert not repo.claim_post(a.generation_id, first, now).applied
    with pytest.raises(StateConflict):
        repo.claim_post(a.generation_id, second, now)
    assert repo.finish_post(key, first, P.FAILED_BEFORE_WRITE, now).applied
    assert not repo.finish_post(key, first, P.FAILED_BEFORE_WRITE, now).applied
    assert repo.claim_post(a.generation_id, second, now).applied
    with pytest.raises(StateConflict):
        repo.start_write(key, first, now)
    assert repo.start_write(key, second, now).applied
    assert not repo.start_write(key, second, now).applied
    assert len(repo.posting(key).attempts) == 2


@pytest.mark.parametrize("outcome", [P.WRITE_STARTED, P.UNCERTAIN, P.SUCCEEDED])
def test_post_barrier_survives_changed_deadline(setup, outcome):
    repo, a, key = setup
    h = accept(repo, a, key)
    now, claim = h.acquisition_ended_utc, UUID(int=20)
    repo.claim_post(a.generation_id, claim, now)
    repo.start_write(key, claim, now)
    if outcome != P.WRITE_STARTED:
        repo.finish_post(key, claim, outcome, now, "123456" if outcome == P.SUCCEEDED else None)
    b = newer(a)
    hb = accept(repo, b, key, a.generation_id, 13)
    with pytest.raises(StateConflict):
        repo.claim_post(b.generation_id, UUID(int=21), hb.acquisition_ended_utc)
    with pytest.raises(StateConflict):
        repo.finish_post(key, claim, P.FAILED_BEFORE_WRITE, hb.acquisition_ended_utc)
    assert repo.posting(key).attempts[-1].status == outcome


def test_concurrent_write_start_only_one_authorization(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim, now = UUID(int=20), h.acquisition_ended_utc
    repo.claim_post(a.generation_id, claim, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repo.start_write(key, claim, now), range(32)))
    assert sum(r.applied for r in results) == 1


def test_late_positive_response_preserved_even_for_obsolete_generation(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim, now = UUID(int=20), h.acquisition_ended_utc
    repo.claim_post(a.generation_id, claim, now)
    repo.start_write(key, claim, now)
    repo.finish_post(key, claim, P.UNCERTAIN, now)
    repo.plan(key, newer(a), a.generation_id, now)
    later = a.timing.expiry_utc + timedelta(minutes=20)
    assert repo.finish_post(key, claim, P.SUCCEEDED, later, "987654").applied
    assert not repo.finish_post(key, claim, P.SUCCEEDED, later, "987654").applied
    with pytest.raises(StateConflict):
        repo.finish_post(key, claim, P.SUCCEEDED, later, "999999")
    with pytest.raises(StateConflict):
        repo.finish_post(key, claim, P.UNCERTAIN, later)


def test_stale_claim_cannot_write_and_exact_l_boundary(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim = UUID(int=20)
    repo.claim_post(a.generation_id, claim, h.acquisition_ended_utc)
    with pytest.raises(ValueError):
        repo.start_write(key, claim, a.timing.expiry_utc + timedelta(microseconds=1))
    assert repo.start_write(key, claim, a.timing.expiry_utc).applied
    repo.plan(key, newer(a), a.generation_id, a.timing.expiry_utc)
    with pytest.raises(StateConflict):
        repo.start_write(key, claim, a.timing.expiry_utc)


def test_cleanup_fence_and_stale_replay_cannot_stop_new_owner(setup):
    repo, a, key = setup
    w, lease = a.timing.warmup_utc, UUID(int=50)
    repo.plan(key, a, None, w)
    assert repo.acquire_vm(a.generation_id, lease, w).applied
    assert not repo.acquire_vm(a.generation_id, lease, w).applied
    with pytest.raises(StateConflict):
        repo.complete_cleanup(lease, a.generation_id)
    b = newer(a)
    repo.plan(key, b, a.generation_id, w)
    with pytest.raises(StateConflict):
        repo.acquire_vm(b.generation_id, UUID(int=51), b.timing.warmup_utc)
    assert repo.begin_cleanup(lease, a.generation_id).record.phase == VmPhase.STOPPING
    assert not repo.begin_cleanup(lease, a.generation_id).applied
    with pytest.raises(StateConflict):
        repo.acquire_vm(b.generation_id, UUID(int=51), b.timing.warmup_utc)
    assert repo.complete_cleanup(lease, a.generation_id).applied
    assert repo.acquire_vm(b.generation_id, UUID(int=51), b.timing.warmup_utc).applied
    with pytest.raises(StateConflict):
        repo.begin_cleanup(lease, a.generation_id)
    assert not repo.complete_cleanup(lease, a.generation_id).applied
    assert not repo.acquire_vm(b.generation_id, UUID(int=51), b.timing.warmup_utc).applied


def test_safe_session_evidence_does_not_authorize_or_mutate_generation(setup):
    repo, a, key = setup
    now = a.timing.warmup_utc
    repo.plan(key, a, None, now)
    evidence = SessionHealthEvidence(
        now,
        AuthenticationStatus.AUTHENTICATED,
        SessionExpiryKind.FIXED,
        a.timing.deadline_utc,
        newer(a).timing.target_utc,
        RefreshObservation.OBSERVED,
        False,
    )
    assert repo.record_session_health(a.generation_id, evidence).applied
    assert not repo.record_session_health(a.generation_id, evidence).applied
    assert repo.session_health(a.generation_id) == (evidence,)
    assert repo.generation(a.generation_id).status == G.PLANNED
    with pytest.raises(StateConflict):
        repo.record_session_health(
            a.generation_id, replace(evidence, refresh=RefreshObservation.UNKNOWN)
        )
    with pytest.raises(TypeError):
        SessionHealthEvidence(now, AuthenticationStatus.AUTHENTICATED, cookie_value="secret")
    assert {f.name for f in fields(evidence)} == {
        "observed_at",
        "authentication",
        "expiry_kind",
        "expires_at",
        "next_target",
        "refresh",
        "manual_reauthentication_required",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expiry_kind": SessionExpiryKind.FIXED},
        {"expires_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"next_target": datetime(2026, 1, 1)},
        {"manual_reauthentication_required": "false"},
        {"authentication": "authenticated"},
        {"refresh": "cookie contents"},
    ],
)
def test_invalid_health_metadata(kwargs):
    args = {
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "authentication": AuthenticationStatus.NOT_CONFIRMED,
        **kwargs,
    }
    with pytest.raises(ValueError):
        SessionHealthEvidence(**args)


@pytest.mark.parametrize(
    "user,event,kind",
    [
        ("", 5, "captain"),
        ("0123", 5, "captain"),
        ("123", True, "captain"),
        ("123", 0, "captain"),
        ("123", 5, "good_luck"),
    ],
)
def test_invalid_post_keys(user, event, kind):
    with pytest.raises(StateConflict):
        PostKey(user, event, kind)


def test_state_modules_have_no_production_or_provider_imports():
    allowed = {
        "dataclasses",
        "datetime",
        "enum",
        "typing",
        "uuid",
        "functools",
        "threading",
        "fpl_bot.captain_handoff",
        "fpl_bot.captain_orchestration_timing",
        "fpl_bot.captain_state",
    }
    for name in ("captain_state", "captain_repository", "captain_memory_repository"):
        tree = ast.parse((Path("src/fpl_bot") / f"{name}.py").read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert imports <= allowed


def test_concurrent_plan_cas_one_winner(setup):
    repo, a, key = setup

    def plan(n):
        candidate = replace(a, assignment_id=UUID(int=n), generation_id=UUID(int=n + 100))
        try:
            return repo.plan(key, candidate, None, a.timing.warmup_utc)
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(plan, range(10, 42)))
    assert sum(r is not None for r in results) == 1
    assert len(repo.pending_intents()) == 3


def test_concurrent_handoff_and_post_claims(setup):
    repo, a, key = setup
    release(repo, a, key)
    h = payload(a)
    repo.claim_acquisition(a.generation_id, h.attempt_id, a.timing.target_utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repo.accept(h, h.acquisition_ended_utc), range(32)))
    assert sum(r.applied for r in results) == 1
    assert repo.generation_acquisition(a.generation_id).handoff == h
    assert (
        repo.task_intent(a.generation_id, TaskKind.PUBLISH).scheduled_at == h.acquisition_ended_utc
    )

    def claim(n):
        try:
            return repo.claim_post(a.generation_id, UUID(int=n), h.acquisition_ended_utc)
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(100, 132)))
    assert sum(r is not None for r in results) == 1
    assert len(repo.posting(key).attempts) == 1


def test_key_isolates_destinations_but_not_deadlines(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim = UUID(int=20)
    repo.claim_post(a.generation_id, claim, h.acquisition_ended_utc)
    repo.start_write(key, claim, h.acquisition_ended_utc)
    repo.finish_post(key, claim, P.SUCCEEDED, h.acquisition_ended_utc, "123456")
    other = replace(a, assignment_id=UUID(int=31), generation_id=UUID(int=32))
    other_key = PostKey("98765", 5)
    oh = accept(repo, other, other_key, attempt=33)
    assert repo.claim_post(other.generation_id, UUID(int=34), oh.acquisition_ended_utc).applied
    assert len(repo.posting(key).attempts) == 1


def test_stale_claim_before_write_cannot_start(setup):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim, now = UUID(int=20), h.acquisition_ended_utc
    repo.claim_post(a.generation_id, claim, now)
    repo.plan(key, newer(a), a.generation_id, now)
    with pytest.raises(StateConflict):
        repo.start_write(key, claim, now)
    assert repo.finish_post(key, claim, P.FAILED_BEFORE_WRITE, now).applied


@pytest.mark.parametrize("post_id", [None, "", "0", "abc", "123\n", 123, "9" * 33])
def test_malformed_success_id_cannot_unlock_barrier(setup, post_id):
    repo, a, key = setup
    h = accept(repo, a, key)
    claim, now = UUID(int=20), h.acquisition_ended_utc
    repo.claim_post(a.generation_id, claim, now)
    repo.start_write(key, claim, now)
    with pytest.raises(StateConflict):
        repo.finish_post(key, claim, P.SUCCEEDED, now, post_id)
    assert repo.posting(key).attempts[-1].status == P.WRITE_STARTED


def test_vm_lease_is_auditable_and_not_reused(setup):
    repo, a, key = setup
    w, lease = a.timing.warmup_utc, UUID(int=50)
    repo.plan(key, a, None, w)
    assert repo.vm_use() is None
    repo.acquire_vm(a.generation_id, lease, w)
    assert repo.vm_use().generation_id == a.generation_id
    with pytest.raises(StateConflict):
        repo.begin_cleanup(UUID(int=51), a.generation_id)
    with pytest.raises(StateConflict):
        repo.begin_cleanup(lease, UUID(int=99))
    repo.begin_cleanup(lease, a.generation_id)
    repo.complete_cleanup(lease, a.generation_id)
    assert repo.vm_use() is None
    with pytest.raises(StateConflict):
        repo.acquire_vm(a.generation_id, lease, w)


def test_reused_identities_and_invalid_time_leave_state_unchanged(setup):
    repo, a, key = setup
    now = a.timing.warmup_utc
    with pytest.raises(ValueError):
        repo.plan(key, a, None, now.replace(tzinfo=None))
    assert repo.current(key) is None
    repo.plan(key, a, None, now)
    b = replace(newer(a), assignment_id=a.assignment_id)
    with pytest.raises(StateConflict):
        repo.plan(key, b, a.generation_id, now)
    with pytest.raises(StateConflict):
        repo.plan(key, newer(a), a.generation_id, now - timedelta(seconds=1))
    assert repo.current(key).assignment == a
    assert repo.generation_acquisition(a.generation_id) is None


@pytest.mark.parametrize(
    "expected,target",
    [
        (G.WARMING, G.RELEASED),
        (G.READY, G.WARMING),
        (G.RELEASED, G.READY),
        (G.ACQUIRING, G.ACCEPTED),
        (G.FAILED, G.PLANNED),
        (G.MISSED, G.RELEASED),
        (G.AUTHENTICATION_REQUIRED, G.READY),
        (G.CANCELLED_STALE, G.PLANNED),
    ],
)
def test_no_unguarded_state_shortcuts(setup, expected, target):
    repo, a, key = setup
    release(repo, a, key)
    with pytest.raises(StateConflict):
        repo.transition(a.generation_id, expected, target, a.timing.target_utc)
    assert repo.generation(a.generation_id).status == G.RELEASED


def test_stale_and_conflicting_health_evidence_rejected(setup):
    repo, a, key = setup
    now = a.timing.warmup_utc
    repo.plan(key, a, None, now)
    evidence = SessionHealthEvidence(
        now, AuthenticationStatus.REQUIRED, manual_reauthentication_required=True
    )
    repo.record_session_health(a.generation_id, evidence)
    later = replace(evidence, observed_at=now + timedelta(seconds=10))
    repo.record_session_health(a.generation_id, later)
    with pytest.raises(StateConflict):
        repo.record_session_health(
            a.generation_id, replace(evidence, observed_at=now + timedelta(seconds=5))
        )
    repo.plan(key, newer(a), a.generation_id, now)
    with pytest.raises(StateConflict):
        repo.record_session_health(a.generation_id, evidence)
    assert repo.session_health(a.generation_id) == (evidence, later)
