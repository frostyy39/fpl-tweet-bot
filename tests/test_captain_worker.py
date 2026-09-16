from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from fpl_bot.captain_handoff import (
    AuthenticationStatus as Auth,
)
from fpl_bot.captain_handoff import (
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_session_health import SessionObservation, summarize_session
from fpl_bot.captain_state import RefreshObservation, SessionExpiryKind
from fpl_bot.captain_worker import (
    AcquiredDataset,
    CaptainWorker,
    HandoffReceipt,
    ReceiptStatus,
    ReleaseGrant,
    WorkerAssignment,
    WorkerStatus,
)
from fpl_bot.errors import CaptainReviewBrowserError

T = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)
WORK = WorkerAssignment(
    1,
    CaptainAssignment(UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(T + timedelta(hours=2))),
    UUID(int=3),
)
DATA = AcquiredDataset(
    (
        ProjectionRecord(1, "João Pedro", "CHE", Decimal("6.48")),
        ProjectionRecord(3, "Haaland", "MCI", Decimal("7.00")),
    ),
    RowCounts(3, 2, 1, 2),
    CompletenessStatus.COMPLETE,
    Auth.AUTHENTICATED,
    CleanupStatus.RELEASED,
)


class Clock:
    value = T

    def now(self):
        return self.value


class Acquisition:
    def __init__(self):
        self.calls = []
        self.data = DATA
        self.error = None

    def acquire(self, event_id):
        self.calls.append(event_id)
        if self.error:
            raise self.error
        return self.data


class Client:
    def __init__(self, clock):
        self.clock = clock
        self.work = WORK
        self.release = lambda work, run: ReleaseGrant(work, run, clock.now())
        self.polls = 0
        self.submissions = []
        self.failures = []
        self.receipt_status = ReceiptStatus.ACCEPTED
        self.error = None

    def obtain_assignment(self, run_id):
        return self.work

    def await_release(self, work, run_id, *, timeout_seconds, cancelled):
        assert 0 <= timeout_seconds <= 10
        self.polls += 1
        return self.release(work, run_id)

    def submit_handoff(self, handoff, digest, health):
        self.submissions.append((handoff, digest, health))
        if self.error:
            raise self.error
        return HandoffReceipt(
            handoff.assignment.generation_id, handoff.attempt_id, digest, self.receipt_status
        )

    def report_failure(self, work, run_id, status):
        self.failures.append(status)


@pytest.fixture
def setup():
    clock = Clock()
    client = Client(clock)
    acquisition = Acquisition()
    worker = CaptainWorker(
        client,
        acquisition,
        clock,
        expected_user="worker",
        current_user=lambda: "worker",
        new_run_id=lambda: UUID(int=9),
        max_release_checks=3,
    )
    return worker, client, acquisition, clock


@pytest.mark.parametrize(
    "assignment,status",
    [
        (None, WorkerStatus.NO_ASSIGNMENT),
        ({}, WorkerStatus.INVALID_ASSIGNMENT),
    ],
)
def test_no_or_malformed_assignment(setup, assignment, status):
    worker, client, acquisition, _ = setup
    client.work = assignment
    assert worker.run().status == status
    assert not acquisition.calls and not client.submissions


@pytest.mark.parametrize(
    "change",
    [
        dict(schema_version=2),
        dict(schema_version=True),
        dict(attempt_id=UUID(int=0)),
        dict(assignment={}),
        dict(next_target=datetime(2026, 1, 1)),
    ],
)
def test_invalid_assignment_contract(change):
    with pytest.raises(ValueError):
        replace(WORK, **change)


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-1, WorkerStatus.RELEASE_REJECTED),
        (0, WorkerStatus.SUCCEEDED),
        (300, WorkerStatus.SUCCEEDED),
        (300.000001, WorkerStatus.EXPIRED),
    ],
)
def test_exact_release_boundaries(setup, offset, expected):
    worker, client, acquisition, clock = setup
    clock.value = T + timedelta(seconds=offset)
    assert worker.run().status == expected
    assert len(acquisition.calls) == (expected == WorkerStatus.SUCCEEDED)


@pytest.mark.parametrize(
    "release",
    [
        lambda w, r: False,
        lambda w, r: ReleaseGrant(replace(w, attempt_id=UUID(int=20)), r, T),
        lambda w, r: ReleaseGrant(
            replace(w, assignment=replace(w.assignment, generation_id=UUID(int=20))), r, T
        ),
        lambda w, r: ReleaseGrant(w, UUID(int=20), T),
        lambda w, r: ReleaseGrant(w, r, T - timedelta(microseconds=1)),
        lambda w, r: ReleaseGrant(w, r, T + timedelta(seconds=1)),
    ],
)
def test_stale_revoked_or_mismatched_release(setup, release):
    worker, client, acquisition, _ = setup
    client.release = release
    assert worker.run().status == WorkerStatus.RELEASE_REJECTED
    assert not acquisition.calls and not client.submissions


def test_pending_release_bounded_and_cancellable(setup):
    worker, client, acquisition, _ = setup
    client.release = lambda w, r: None
    assert worker.run().status == WorkerStatus.WAIT_EXHAUSTED
    assert client.polls == 3 and not acquisition.calls


def test_wait_from_warmup_until_valid_release(setup):
    worker, client, acquisition, clock = setup
    clock.value = T - timedelta(minutes=1)

    def release(work, run):
        assert not acquisition.calls
        clock.value = T
        return ReleaseGrant(work, run, T)

    client.release = release
    assert worker.run().status == WorkerStatus.SUCCEEDED


def test_cancellation_during_wait(setup):
    worker, client, acquisition, _ = setup
    worker.cancelled = lambda: client.polls > 0
    client.release = lambda w, r: None
    assert worker.run().status == WorkerStatus.CANCELLED
    assert not acquisition.calls


def test_success_full_structured_handoff_and_no_repeat(setup):
    worker, client, acquisition, _ = setup
    result = worker.run()
    assert result.exit_code == 0
    handoff, digest, health = client.submissions[0]
    assert handoff.records == DATA.records
    assert [r.source_ordinal for r in handoff.records] == [1, 3]
    assert handoff.records[0].review_name == "João Pedro"
    assert handoff.assignment == WORK.assignment and handoff.attempt_id == WORK.attempt_id
    assert digest == handoff.payload_digest
    assert handoff.counts == DATA.counts
    assert health is None
    assert "tweet" not in handoff.to_payload()
    assert worker.run().status == WorkerStatus.RELEASE_REJECTED
    assert len(acquisition.calls) == len(client.submissions) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(completeness=CompletenessStatus.PARTIAL),
        dict(cleanup=CleanupStatus.UNCLEAN),
        dict(authentication=Auth.NOT_CONFIRMED),
        dict(records=()),
        dict(records=(DATA.records[0], DATA.records[0])),
        dict(records=tuple(reversed(DATA.records))),
        dict(records=(object(),)),
        dict(counts=RowCounts(4, 3, 1, 3)),
        dict(observations=(SessionObservation(T, Auth.NOT_CONFIRMED),)),
        dict(observations=(SessionObservation(T - timedelta(seconds=1), Auth.AUTHENTICATED),)),
    ],
)
def test_invalid_partial_acquisition_never_submitted(setup, changes):
    worker, client, acquisition, _ = setup
    acquisition.data = replace(DATA, **changes)
    assert worker.run().status == WorkerStatus.ACQUISITION_FAILED
    assert not client.submissions


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            CaptainReviewBrowserError("reauthentication_required"),
            WorkerStatus.AUTHENTICATION_REQUIRED,
        ),
        (CaptainReviewBrowserError("browser_profile_in_use"), WorkerStatus.ACQUISITION_FAILED),
        (RuntimeError("SECRET_SENTINEL"), WorkerStatus.ACQUISITION_FAILED),
    ],
)
def test_errors_are_sanitized_no_partial_handoff(setup, error, expected, capsys):
    worker, client, acquisition, _ = setup
    acquisition.error = error
    result = worker.run()
    assert result.status == expected
    assert "SECRET_SENTINEL" not in repr(result) + repr(client.failures)
    assert not client.submissions and capsys.readouterr().out == ""


def test_authentication_required_dataset(setup):
    worker, client, acquisition, _ = setup
    acquisition.data = replace(DATA, authentication=Auth.REQUIRED)
    assert worker.run().status == WorkerStatus.AUTHENTICATION_REQUIRED
    assert not client.submissions


@pytest.mark.parametrize(
    "status,expected",
    [
        (ReceiptStatus.ACCEPTED, WorkerStatus.SUCCEEDED),
        (ReceiptStatus.REPLAY, WorkerStatus.SUCCEEDED),
        (ReceiptStatus.REJECTED, WorkerStatus.HANDOFF_REJECTED),
    ],
)
def test_receipts_no_reacquisition(setup, status, expected):
    worker, client, acquisition, _ = setup
    client.receipt_status = status
    assert worker.run().status == expected
    assert len(client.submissions) == len(acquisition.calls) == 1


def test_lost_ack_preserves_exact_payload_without_retry(setup):
    worker, client, acquisition, _ = setup
    client.error = RuntimeError("SECRET_SENTINEL")
    result = worker.run()
    assert result.status == WorkerStatus.HANDOFF_UNCERTAIN
    assert result.handoff == client.submissions[0][0]
    assert len(client.submissions) == len(acquisition.calls) == 1


def test_wrong_receipt_identity(setup):
    worker, client, _, _ = setup
    client.submit_handoff = lambda *args: HandoffReceipt(
        UUID(int=99), WORK.attempt_id, "wrong", ReceiptStatus.ACCEPTED
    )
    assert worker.run().status == WorkerStatus.HANDOFF_REJECTED


def test_expected_account_mismatch(setup):
    worker, _, acquisition, _ = setup
    worker.current_user = lambda: "SYSTEM"
    assert worker.run().status == WorkerStatus.IDENTITY_MISMATCH
    assert not acquisition.calls


def test_controller_unavailable_does_not_acquire(setup):
    worker, client, acquisition, _ = setup

    def unavailable(run_id):
        raise RuntimeError("SECRET_SENTINEL")

    client.obtain_assignment = unavailable
    assert worker.run().status == WorkerStatus.CONTROLLER_UNAVAILABLE
    assert not acquisition.calls


def test_late_release_arrival_rejected(setup):
    worker, client, acquisition, clock = setup

    def release(work, run):
        clock.value = T + timedelta(minutes=5, microseconds=1)
        return ReleaseGrant(work, run, T)

    client.release = release
    assert worker.run().status == WorkerStatus.RELEASE_REJECTED
    assert not acquisition.calls


def test_acquisition_finishing_after_expiry_cannot_submit(setup):
    worker, client, acquisition, clock = setup

    def acquire(event_id):
        clock.value = T + timedelta(minutes=5, microseconds=1)
        return DATA

    acquisition.acquire = acquire
    assert worker.run().status == WorkerStatus.ACQUISITION_FAILED
    assert not client.submissions


def test_new_invocation_cannot_replay_old_run_release(setup):
    worker, client, acquisition, clock = setup
    client.release = lambda work, run: ReleaseGrant(work, UUID(int=9), T)
    assert worker.run().status == WorkerStatus.SUCCEEDED
    other = CaptainWorker(
        client,
        acquisition,
        clock,
        expected_user="worker",
        current_user=lambda: "worker",
        new_run_id=lambda: UUID(int=10),
    )
    assert other.run().status == WorkerStatus.RELEASE_REJECTED
    assert len(acquisition.calls) == 1


@pytest.mark.parametrize(
    "kind,expiry,refresh",
    [
        (SessionExpiryKind.UNKNOWN, None, RefreshObservation.UNKNOWN),
        (SessionExpiryKind.SESSION, None, RefreshObservation.NOT_OBSERVED),
        (SessionExpiryKind.FIXED, T + timedelta(days=20), RefreshObservation.NOT_OBSERVED),
    ],
)
def test_session_classifications(kind, expiry, refresh):
    before = SessionObservation(T, Auth.AUTHENTICATED, kind, expiry)
    evidence = summarize_session(
        (before, replace(before, observed_at=T + timedelta(seconds=1))), T + timedelta(days=7)
    )
    assert evidence.expiry_kind == kind and evidence.refresh == refresh
    assert evidence.next_target == T + timedelta(days=7)
    assert not evidence.manual_reauthentication_required


@pytest.mark.parametrize(
    "days,refresh", [(21, RefreshObservation.OBSERVED), (19, RefreshObservation.UNKNOWN)]
)
def test_only_expiry_increase_is_defensible_metadata_change(days, refresh):
    before = SessionObservation(
        T, Auth.AUTHENTICATED, SessionExpiryKind.FIXED, T + timedelta(days=20)
    )
    after = replace(
        before, observed_at=T + timedelta(seconds=1), expires_at=T + timedelta(days=days)
    )
    assert summarize_session((before, after), None).refresh == refresh


def test_session_contract_cannot_store_secret_values():
    with pytest.raises(TypeError):
        SessionObservation(T, Auth.AUTHENTICATED, value="SECRET_SENTINEL")
    with pytest.raises(ValueError):
        SessionObservation(T, Auth.AUTHENTICATED, SessionExpiryKind.FIXED)
    with pytest.raises(ValueError):
        summarize_session(
            (
                SessionObservation(T, Auth.AUTHENTICATED),
                SessionObservation(T - timedelta(seconds=1), Auth.AUTHENTICATED),
            ),
            None,
        )


def test_realistic_unknown_session_evidence_submitted_separately(setup):
    worker, client, acquisition, _ = setup
    acquisition.data = replace(
        DATA,
        observations=(
            SessionObservation(T, Auth.NOT_CONFIRMED),
            SessionObservation(T, Auth.AUTHENTICATED),
        ),
    )
    result = worker.run()
    assert result.health.authentication == Auth.AUTHENTICATED
    assert result.health.refresh == RefreshObservation.UNKNOWN
    assert result.health.expiry_kind == SessionExpiryKind.UNKNOWN
    assert client.submissions[0][2] == result.health
