from datetime import UTC, datetime, timedelta

import pytest

from fpl_bot.cloud_tasks import (
    CloudTaskCreateDisposition,
    CloudTaskCreateRejectedError,
    CloudTaskDefinition,
    deterministic_preflight_task_id,
    serialize_instruction,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.posting_state import (
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingAuditRecord,
    PostingStatus,
)
from fpl_bot.preflight_arming import (
    PreflightAuditStatus,
    PreflightTaskArmer,
    PreflightTaskArmingStatus,
)

DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
PREFLIGHT_TIME_UTC = DEADLINE_UTC - timedelta(minutes=5)
NOW_UTC = PREFLIGHT_TIME_UTC - timedelta(hours=1)


def instruction() -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(3, DEADLINE_UTC)


class FakeTaskBoundary:
    def __init__(
        self,
        disposition: CloudTaskCreateDisposition | Exception = CloudTaskCreateDisposition.CREATED,
    ) -> None:
        self.disposition = disposition
        self.create_calls: list[CloudTaskDefinition] = []

    def build_task(self, scheduled: ScheduledDeadlineInstruction) -> CloudTaskDefinition:
        task_id = deterministic_preflight_task_id(scheduled)
        return CloudTaskDefinition(
            task_id=task_id,
            task_name=f"projects/test/locations/europe-west2/queues/fpl/tasks/{task_id}",
            schedule_time_utc=scheduled.expected_deadline_utc - timedelta(minutes=5),
            payload=serialize_instruction(scheduled),
        )

    def create_task(self, definition: CloudTaskDefinition) -> CloudTaskCreateDisposition:
        self.create_calls.append(definition)
        if isinstance(self.disposition, Exception):
            raise self.disposition
        return self.disposition


class RecordingAuditStore(InMemoryPostingStateStore):
    def __init__(self) -> None:
        super().__init__(claim_id_factory=lambda: "claim-1")
        self.claim_calls = 0
        self.reconciled: list[EventPostingContext] = []

    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        self.reconciled.append(context)
        return super().reconcile_unclaimed_event(context)

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ):
        self.claim_calls += 1
        return super().claim_event(context, claimed_at_utc=claimed_at_utc)


def make_armer(
    store: RecordingAuditStore,
    boundary: FakeTaskBoundary,
    *,
    now: datetime = NOW_UTC,
) -> PreflightTaskArmer:
    return PreflightTaskArmer(store, boundary, clock=lambda: now)


def test_future_preflight_is_scheduled_exactly_five_minutes_early_and_audited() -> None:
    store = RecordingAuditStore()
    store.reconcile_unclaimed_event(
        EventPostingContext(
            3,
            None,
            DEADLINE_UTC,
            scheduled_task_id="final-task",
            scheduled_task_status="armed",
        )
    )
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is PreflightTaskArmingStatus.SCHEDULED
    assert len(boundary.create_calls) == 1
    assert boundary.create_calls[0].schedule_time_utc == PREFLIGHT_TIME_UTC
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_id == "final-task"
    assert record.context.scheduled_task_status == "armed"
    assert record.context.preflight_status == PreflightAuditStatus.SCHEDULED.value
    assert store.claim_calls == 0


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        (
            CloudTaskCreateDisposition.ALREADY_EXISTS,
            PreflightTaskArmingStatus.ALREADY_SCHEDULED,
        ),
        (
            CloudTaskCreateDisposition.RECONCILED,
            PreflightTaskArmingStatus.RECONCILED_SCHEDULED,
        ),
    ],
)
def test_duplicate_or_reconciled_preflight_uses_same_deterministic_task(
    disposition: CloudTaskCreateDisposition,
    expected: PreflightTaskArmingStatus,
) -> None:
    store = RecordingAuditStore()
    boundary = FakeTaskBoundary(disposition)

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is expected
    assert len(boundary.create_calls) == 1
    assert boundary.create_calls[0].task_id == deterministic_preflight_task_id(instruction())


@pytest.mark.parametrize("now", [PREFLIGHT_TIME_UTC, PREFLIGHT_TIME_UTC + timedelta(seconds=1)])
def test_reached_or_passed_preflight_time_is_skipped_without_past_task(now: datetime) -> None:
    store = RecordingAuditStore()
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary, now=now).arm(instruction())

    assert result.status is PreflightTaskArmingStatus.TOO_LATE
    assert boundary.create_calls == []
    assert store.get_event(3).context.preflight_status == (
        PreflightAuditStatus.SKIPPED_TOO_LATE.value
    )
    assert store.claim_calls == 0


def test_preflight_create_rejection_is_audited_without_internal_retry() -> None:
    store = RecordingAuditStore()
    error = CloudTaskCreateRejectedError("preflight-task", "PermissionDenied")
    boundary = FakeTaskBoundary(error)

    with pytest.raises(CloudTaskCreateRejectedError):
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1
    assert store.get_event(3).context.preflight_status == PreflightAuditStatus.CREATE_FAILED.value


def test_claimed_posting_record_is_never_mutated_or_armed_by_preflight() -> None:
    store = RecordingAuditStore()
    context = EventPostingContext(3, "GW3", DEADLINE_UTC, preflight_status="original")
    decision = store.claim_event(context, claimed_at_utc=NOW_UTC - timedelta(minutes=1))
    assert decision.claim is not None
    before = store.get_event(3)
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is PreflightTaskArmingStatus.POSTING_CLOSED
    assert result.existing_posting_status is PostingStatus.CLAIMED
    assert boundary.create_calls == []
    assert store.get_event(3) == before
    assert store.claim_calls == 1
