from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from uuid import UUID

import pytest

from fpl_bot.captain_firestore import (
    PREFIX,
    FirestoreCaptainRepository,
    FirestoreCaptainVmOperations,
)
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
from fpl_bot.captain_serialization import InvalidCaptainDocument, from_document, to_document
from fpl_bot.captain_state import GenerationStatus as G
from fpl_bot.captain_state import PostingStatus as P
from fpl_bot.captain_state import PostKey, SessionHealthEvidence, StateConflict, TaskKind
from fpl_bot.captain_vm_operations import VmAction

T = datetime(2026, 9, 18, 15, 30, 0, 123456, tzinfo=UTC)
KEY = PostKey("12345", 5)
ASSIGNMENT = CaptainAssignment(
    UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(T + timedelta(hours=2))
)


class Snapshot:
    def __init__(self, name, data):
        self.id = name.split("/")[-1]
        self.exists = data is not None
        self.data = deepcopy(data)

    def to_dict(self):
        return deepcopy(self.data)


class Reference:
    def __init__(self, name):
        self.name = name

    def get(self, *, transaction):
        assert not transaction.writes, "Firestore forbids reads after writes"
        transaction.reads.add(self.name)
        return Snapshot(self.name, transaction.snapshot.get(self.name))


class Collection:
    def __init__(self, name, filter=None):
        self.name, self.filter = name, filter

    def document(self, name):
        return Reference(f"{self.name}/{name}")

    def where(self, *, filter):
        return Collection(self.name, filter)

    def stream(self, *, transaction):
        assert not transaction.writes
        transaction.queries.add(self.name)
        for name, data in transaction.snapshot.items():
            if name.split("/")[0] != self.name:
                continue
            value = data
            for part in self.filter.field_path.split("."):
                value = value[part]
            if value == self.filter.value:
                transaction.reads.add(name)
                yield Snapshot(name, data)


class Transaction:
    def __init__(self, client):
        self.client = client
        with client.lock:
            self.snapshot = deepcopy(client.data)
            self.versions = dict(client.versions)
        self.reads, self.queries, self.writes = set(), set(), {}

    def set(self, reference, data):
        self.writes[reference.name] = deepcopy(data)

    def commit(self):
        with self.client.lock:
            names = self.reads | {n for n in self.client.data if n.split("/")[0] in self.queries}
            if any(self.versions.get(n, 0) != self.client.versions.get(n, 0) for n in names):
                return False
            for name, data in self.writes.items():
                self.client.data[name] = data
                self.client.versions[name] = self.client.versions.get(name, 0) + 1
            return True


class Client:
    project = "captain-test"
    _database_string = "projects/captain-test/databases/captain-choice"

    def __init__(self):
        self.data, self.versions = {}, {}
        self.lock = RLock()
        self.retry_once = False
        self.crash_before_commit = False
        self.crash_after_commit = False
        self.calls = 0

    def collection(self, name):
        assert name.startswith(PREFIX)
        return Collection(name)

    def transaction(self):
        return Transaction(self)

    def transactional(self, function):
        def run(transaction):
            for _ in range(20):
                self.calls += 1
                result = function(transaction)
                if self.crash_before_commit:
                    self.crash_before_commit = False
                    raise RuntimeError("simulated precommit crash")
                if self.retry_once:
                    self.retry_once = False
                    transaction = Transaction(self)
                    continue
                if transaction.commit():
                    if self.crash_after_commit:
                        self.crash_after_commit = False
                        raise RuntimeError("simulated lost acknowledgement")
                    return result
                transaction = Transaction(self)
            raise AssertionError("retry budget exhausted")

        return run


def adapters(client=None):
    client = client or Client()
    kwargs = dict(
        project=client.project,
        database="captain-choice",
        client=client,
        transactional_wrapper=client.transactional,
    )
    return FirestoreCaptainRepository(**kwargs), FirestoreCaptainVmOperations(**kwargs), client


def accepted(repo, assignment=ASSIGNMENT, attempt=None, at=T):
    attempt = attempt if attempt is not None else UUID(int=3)
    repo.plan(KEY, assignment, None, at)
    for old, new in [(G.PLANNED, G.WARMING), (G.WARMING, G.READY), (G.READY, G.RELEASED)]:
        repo.transition(assignment.generation_id, old, new, at)
    repo.claim_acquisition(assignment.generation_id, attempt, at)
    handoff = ProjectionHandoff(
        1,
        assignment,
        attempt,
        at,
        at,
        (ProjectionRecord(1, "João", "CHE", Decimal("7.3500")),),
        RowCounts(1, 1, 0, 1),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )
    repo.accept(handoff, at)
    return handoff


def test_full_trace_matches_reference_and_restart():
    durable, _, client = adapters()
    reference = InMemoryCaptainRepository()
    for repo in (durable, reference):
        handoff = accepted(repo)
        assert not repo.accept(handoff, T).applied
        repo.claim_post(ASSIGNMENT.generation_id, UUID(int=8), T)
        repo.start_write(KEY, UUID(int=8), T)
        repo.finish_post(KEY, UUID(int=8), P.UNCERTAIN, T)
        repo.finish_post(KEY, UUID(int=8), P.SUCCEEDED, T, "987654321")
        repo.record_session_health(
            ASSIGNMENT.generation_id, SessionHealthEvidence(T, AuthenticationStatus.AUTHENTICATED)
        )
    durable, _, _ = adapters(client)
    for name, args in [
        ("current", (KEY,)),
        ("posting", (KEY,)),
        ("acquisition", (UUID(int=3),)),
        ("pending_intents", ()),
        ("session_health", (ASSIGNMENT.generation_id,)),
    ]:
        assert getattr(durable, name)(*args) == getattr(reference, name)(*args)
    assert all(n.startswith(PREFIX) for n in client.data)
    assert durable.posting(KEY).attempts[0].post_id == "987654321"


def test_roundtrip_all_durable_records():
    repo, operations, client = adapters()
    repo.plan(KEY, ASSIGNMENT, None, T)
    lease = repo.acquire_vm(ASSIGNMENT.generation_id, UUID(int=20), T).record
    operations.request(lease, VmAction.START)
    operations.acknowledge(lease.lease_id, VmAction.START)
    operations.finish(lease.lease_id, VmAction.START, succeeded=True)
    lease = repo.begin_cleanup(lease.lease_id, lease.generation_id).record
    operations.request(lease, VmAction.STOP)
    operations.acknowledge(lease.lease_id, VmAction.STOP)
    operations.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True)
    repo.complete_cleanup(lease.lease_id, lease.generation_id)
    accepted(repo)
    repo.claim_post(ASSIGNMENT.generation_id, UUID(int=8), T)
    repo.record_session_health(
        ASSIGNMENT.generation_id, SessionHealthEvidence(T, AuthenticationStatus.AUTHENTICATED)
    )
    for data in client.data.values():
        assert to_document(from_document(data["entry"])) == data["entry"]
    handoff = repo.acquisition(UUID(int=3)).handoff
    assert handoff.records[0].projected_points.as_tuple() == Decimal("7.3500").as_tuple()
    assert handoff.acquisition_started_utc == T
    assert from_document(to_document(handoff)).payload_digest == handoff.payload_digest


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(secret="not-allowed"),
        lambda d: d["value"].update(record="UnknownClass"),
        lambda d: d["value"]["fields"].update(cookie_value="not-allowed"),
    ],
)
def test_malformed_document_rejected(mutation):
    data = to_document(SessionHealthEvidence(T, AuthenticationStatus.AUTHENTICATED))
    mutation(data)
    with pytest.raises(InvalidCaptainDocument):
        from_document(data)


def test_digest_timestamp_and_history_corruption_rejected():
    repo, _, _ = adapters()
    handoff = accepted(repo)
    data = to_document(handoff)
    data["value"]["digest"] = "0" * 64
    with pytest.raises(InvalidCaptainDocument):
        from_document(data)
    data = to_document(T)
    data["value"]["utc"] = "2026-09-18T16:30:00+01:00"
    with pytest.raises(InvalidCaptainDocument):
        from_document(data)
    bad = replace(repo.current(KEY), history=((G.ACCEPTED, T),))
    with pytest.raises(InvalidCaptainDocument):
        to_document(bad)


def compete(functions):
    def outcome(function):
        try:
            return function()
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(outcome, functions))


def test_concurrent_generation_replacement():
    repo, _, client = adapters()
    assert repo.plan(KEY, ASSIGNMENT, None, T).applied
    assert not repo.plan(KEY, ASSIGNMENT, None, T).applied
    a = replace(ASSIGNMENT, assignment_id=UUID(int=10), generation_id=UUID(int=11))
    b = replace(ASSIGNMENT, assignment_id=UUID(int=12), generation_id=UUID(int=13))
    other, _, _ = adapters(client)
    results = compete(
        [
            lambda: repo.plan(KEY, a, ASSIGNMENT.generation_id, T),
            lambda: other.plan(KEY, b, ASSIGNMENT.generation_id, T),
        ]
    )
    assert sum(r is not None for r in results) == 1
    assert repo.generation(ASSIGNMENT.generation_id).status == G.CANCELLED_STALE
    with pytest.raises(StateConflict):
        repo.acknowledge_intent(ASSIGNMENT.generation_id, TaskKind.RELEASE)


def test_acquisition_contention_and_handoff_conflict():
    repo, _, _ = adapters()
    handoff = accepted(repo)
    with pytest.raises(StateConflict):
        repo.claim_acquisition(ASSIGNMENT.generation_id, UUID(int=4), T)
    with pytest.raises(StateConflict):
        repo.accept(
            replace(handoff, records=(replace(handoff.records[0], projected_points=Decimal("8")),)),
            T,
        )
    assert not repo.accept(handoff, T).applied


@pytest.mark.parametrize("outcome", [P.WRITE_STARTED, P.UNCERTAIN, P.SUCCEEDED])
def test_posting_barrier_survives_generation_change(outcome):
    repo, _, client = adapters()
    accepted(repo)
    repo.claim_post(ASSIGNMENT.generation_id, UUID(int=8), T)
    repo.start_write(KEY, UUID(int=8), T)
    if outcome != P.WRITE_STARTED:
        repo.finish_post(KEY, UUID(int=8), outcome, T, "123" if outcome == P.SUCCEEDED else None)
    new = replace(
        ASSIGNMENT,
        assignment_id=UUID(int=11),
        generation_id=UUID(int=12),
        timing=CaptainTiming(ASSIGNMENT.timing.deadline_utc + timedelta(seconds=1)),
    )
    repo.plan(KEY, new, ASSIGNMENT.generation_id, T)
    at = T + timedelta(seconds=1)
    accepted(repo, new, UUID(int=13), at)
    repo, _, _ = adapters(client)
    with pytest.raises(StateConflict):
        repo.claim_post(new.generation_id, UUID(int=14), at)


def test_outbox_retries_and_crash_boundaries():
    repo, _, client = adapters()
    client.crash_before_commit = True
    with pytest.raises(RuntimeError):
        repo.plan(KEY, ASSIGNMENT, None, T)
    assert not client.data
    client.retry_once = True
    client.crash_after_commit = True
    with pytest.raises(RuntimeError):
        repo.plan(KEY, ASSIGNMENT, None, T)
    assert len(repo.pending_intents()) == 3
    assert not repo.plan(KEY, ASSIGNMENT, None, T).applied
    # External task creation would occur here, outside transactions.
    assert repo.acknowledge_intent(ASSIGNMENT.generation_id, TaskKind.WARMUP).applied
    assert not repo.acknowledge_intent(ASSIGNMENT.generation_id, TaskKind.WARMUP).applied
    assert len(repo.pending_intents()) == 2


def test_vm_fence_and_operations_survive_restart():
    repo, ops, client = adapters()
    repo.plan(KEY, ASSIGNMENT, None, T)
    lease = repo.acquire_vm(ASSIGNMENT.generation_id, UUID(int=20), T).record
    with pytest.raises(StateConflict):
        repo.acquire_vm(ASSIGNMENT.generation_id, UUID(int=21), T)
    ops.request(lease, VmAction.START)
    stopping = repo.begin_cleanup(lease.lease_id, lease.generation_id).record
    ops.request(stopping, VmAction.STOP)
    with pytest.raises(StateConflict):
        ops.acknowledge(lease.lease_id, VmAction.STOP)
    ops.acknowledge(lease.lease_id, VmAction.START)
    ops.finish(lease.lease_id, VmAction.START, succeeded=True)
    ops.acknowledge(lease.lease_id, VmAction.STOP)
    repo, ops, _ = adapters(client)
    assert repo.vm_use() == stopping
    assert not ops.stop_is_settled(lease.lease_id)
    ops.finish(lease.lease_id, VmAction.STOP, succeeded=True, terminated=True)
    assert ops.stop_is_settled(lease.lease_id)
    repo.complete_cleanup(lease.lease_id, lease.generation_id)
    newer = repo.acquire_vm(ASSIGNMENT.generation_id, UUID(int=21), T).record
    assert not repo.complete_cleanup(lease.lease_id, lease.generation_id).applied
    assert repo.vm_use() == newer
    with pytest.raises(StateConflict):
        repo.begin_cleanup(lease.lease_id, lease.generation_id)


def test_explicit_database_client_binding():
    with pytest.raises(ValueError):
        FirestoreCaptainRepository(project="wrong", database="captain-choice", client=Client())
    with pytest.raises(ValueError):
        FirestoreCaptainRepository(project="captain-test", database="", client=Client())


def test_real_sdk_transaction_decorator_retries_without_duplicate_state():
    from google.api_core.exceptions import Aborted

    class SdkTransaction(Transaction):
        _read_only = False
        _max_attempts = 3
        _id = b"local-test-transaction"

        def _clean_up(self):
            pass

        def _begin(self, retry_id=None):
            Transaction.__init__(self, self.client)
            self.client.calls += 1

        def _commit(self):
            if self.client.retry_once:
                self.client.retry_once = False
                raise Aborted("simulated conflict")
            assert self.commit()

        def _rollback(self):
            self.writes.clear()

    client = Client()
    client.transaction = lambda: SdkTransaction(client)
    repo = FirestoreCaptainRepository(
        project=client.project, database="captain-choice", client=client
    )
    client.retry_once = True
    assert repo.plan(KEY, ASSIGNMENT, None, T).applied
    assert client.calls == 2
    assert repo.current(KEY).version == 1
    assert len(repo.pending_intents()) == 3
    assert not repo.plan(KEY, ASSIGNMENT, None, T).applied


def test_firestore_wire_encoding_no_nested_arrays_or_excessive_depth():
    from google.cloud.firestore_v1 import _helpers

    repo, _, client = adapters()
    accepted(repo)
    repo.claim_post(ASSIGNMENT.generation_id, UUID(int=8), T)
    repo.start_write(KEY, UUID(int=8), T)
    repo.finish_post(KEY, UUID(int=8), P.SUCCEEDED, T, "123")

    def depth(value):
        if isinstance(value, list):
            assert not any(isinstance(v, list) for v in value)
        children = (
            value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        )
        return 1 + max((depth(v) for v in children), default=0)

    for data in client.data.values():
        assert depth(data) <= 20
        assert _helpers.encode_dict(data)


def test_missing_current_referent_is_not_treated_as_unplanned():
    repo, _, client = adapters()
    repo.plan(KEY, ASSIGNMENT, None, T)
    name = next(n for n in client.data if n.startswith(PREFIX + "generations/"))
    del client.data[name]  # Simulated corrupted persistence, not a repository deletion.
    with pytest.raises(InvalidCaptainDocument):
        repo.current(KEY)


def test_sdk_client_factory_receives_explicit_database_without_rpc(monkeypatch):
    from google.cloud import firestore

    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return Client()

    monkeypatch.setattr(firestore, "Client", factory)
    FirestoreCaptainRepository(project="captain-test", database="captain-choice")
    assert calls == [{"project": "captain-test", "database": "captain-choice"}]


def test_changed_document_key_or_routing_is_rejected():
    repo, _, client = adapters()
    repo.plan(KEY, ASSIGNMENT, None, T)
    task = next(n for n in client.data if n.startswith(PREFIX + "intents/"))
    client.data[task]["routing"]["pending"] = False
    with pytest.raises(InvalidCaptainDocument):
        repo.task_intent(ASSIGNMENT.generation_id, TaskKind.WARMUP)


def test_unknown_decimal_and_size_fail_closed():
    with pytest.raises(InvalidCaptainDocument):
        from_document({"schema_version": 1, "value": {"decimal": "malformed"}})
    with pytest.raises(InvalidCaptainDocument):
        to_document("x" * 750_001)


def test_different_destinations_do_not_share_post_barrier():
    repo, _, _ = adapters()
    accepted(repo)
    repo.claim_post(ASSIGNMENT.generation_id, UUID(int=8), T)
    assert repo.posting(PostKey("99999", 5)).attempts == ()


def test_forced_overlapping_acquisition_transactions_have_one_winner():
    from threading import Barrier

    repo, _, client = adapters()
    repo.plan(KEY, ASSIGNMENT, None, T)
    for old, new in [(G.PLANNED, G.WARMING), (G.WARMING, G.READY), (G.READY, G.RELEASED)]:
        repo.transition(ASSIGNMENT.generation_id, old, new, T)
    barrier = Barrier(2)

    def overlapping_transaction():
        transaction = Transaction(client)  # Both start with the same durable snapshot.
        barrier.wait(timeout=10)
        return transaction

    client.transaction = overlapping_transaction
    before = client.calls
    results = compete(
        [
            lambda: repo.claim_acquisition(ASSIGNMENT.generation_id, UUID(int=30), T),
            lambda: repo.claim_acquisition(ASSIGNMENT.generation_id, UUID(int=31), T),
        ]
    )
    assert sum(r is not None and r.applied for r in results) == 1
    assert client.calls - before == 3  # Two original executions and one conflict retry.
