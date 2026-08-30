from datetime import UTC, datetime, timedelta

import pytest

from fpl_bot.cloud_tasks import (
    CloudTaskCreateAmbiguousError,
    CloudTaskCreateDisposition,
    CloudTaskCreateRejectedError,
    CloudTaskDefinition,
    CloudTaskDefinitionConflictError,
    CloudTaskNameReservedError,
    deterministic_task_id,
    serialize_instruction,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.posting_state import (
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingAuditRecord,
    PostingStatus,
)
from fpl_bot.task_arming import (
    DeadlineTaskArmer,
    ScheduledTaskAuditStatus,
    TaskArmingAuditPersistenceError,
    TaskArmingStatus,
    TaskOutcomeAuditPersistenceError,
)

DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
NOW_UTC = DEADLINE_UTC - timedelta(hours=1)


def instruction(
    *,
    event_id: int = 3,
    deadline: datetime = DEADLINE_UTC,
) -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(event_id, deadline)


class FakeTaskBoundary:
    def __init__(
        self,
        disposition: CloudTaskCreateDisposition | Exception = CloudTaskCreateDisposition.CREATED,
    ) -> None:
        self.disposition = disposition
        self.built: list[ScheduledDeadlineInstruction] = []
        self.create_calls: list[CloudTaskDefinition] = []

    def build_task(self, scheduled: ScheduledDeadlineInstruction) -> CloudTaskDefinition:
        self.built.append(scheduled)
        task_id = deterministic_task_id(scheduled)
        return CloudTaskDefinition(
            task_id=task_id,
            task_name=f"projects/test/locations/europe-west2/queues/deadlines/tasks/{task_id}",
            schedule_time_utc=scheduled.expected_deadline_utc,
            payload=serialize_instruction(scheduled),
        )

    def create_task(self, definition: CloudTaskDefinition) -> CloudTaskCreateDisposition:
        self.create_calls.append(definition)
        if isinstance(self.disposition, Exception):
            raise self.disposition
        return self.disposition


class RecordingStateStore(InMemoryPostingStateStore):
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


class FailFinalAuditStore(RecordingStateStore):
    def __init__(self, failed_status: ScheduledTaskAuditStatus) -> None:
        super().__init__()
        self.failed_status = failed_status

    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        if context.scheduled_task_status == self.failed_status.value:
            raise OSError("simulated audit failure")
        return super().reconcile_unclaimed_event(context)


class ClaimBeforeCreateStore(RecordingStateStore):
    def __init__(self) -> None:
        super().__init__()
        self._get_count = 0

    def get_event(self, event_id: int) -> PostingAuditRecord | None:
        self._get_count += 1
        if self._get_count == 2:
            existing = super().get_event(event_id)
            assert existing is not None
            posting_context = EventPostingContext(
                event_id=event_id,
                event_code="GW3",
                official_deadline_utc=existing.context.official_deadline_utc,
                scheduled_task_id=existing.context.scheduled_task_id,
                scheduled_task_status=existing.context.scheduled_task_status,
            )
            InMemoryPostingStateStore.reconcile_unclaimed_event(self, posting_context)
            decision = InMemoryPostingStateStore.claim_event(
                self,
                posting_context,
                claimed_at_utc=NOW_UTC,
            )
            assert decision.granted is True
        return super().get_event(event_id)


def make_armer(
    store: InMemoryPostingStateStore,
    boundary: FakeTaskBoundary,
    *,
    now: datetime = NOW_UTC,
) -> DeadlineTaskArmer:
    return DeadlineTaskArmer(store, boundary, clock=lambda: now)


def test_future_deadline_creates_once_and_records_armed_metadata() -> None:
    store = RecordingStateStore()
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is TaskArmingStatus.ARMED
    assert len(boundary.create_calls) == 1
    assert boundary.create_calls[0].schedule_time_utc == DEADLINE_UTC
    record = store.get_event(3)
    assert record is not None
    assert record.status is None
    assert record.context.event_code is None
    assert record.context.official_deadline_utc == DEADLINE_UTC
    assert record.context.scheduled_task_id == deterministic_task_id(instruction())
    assert record.context.scheduled_task_status == ScheduledTaskAuditStatus.ARMED.value
    assert store.claim_calls == 0


@pytest.mark.parametrize("now", [DEADLINE_UTC, DEADLINE_UTC + timedelta(seconds=1)])
def test_reached_or_passed_deadline_is_overdue_without_create(now: datetime) -> None:
    store = RecordingStateStore()
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary, now=now).arm(instruction())

    assert result.status is TaskArmingStatus.OVERDUE_SAME_DAY
    assert boundary.create_calls == []
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_status == ScheduledTaskAuditStatus.OVERDUE_SAME_DAY.value


def test_repeated_identical_create_already_exists_is_idempotent() -> None:
    store = RecordingStateStore()
    boundary = FakeTaskBoundary()
    armer = make_armer(store, boundary)
    first = armer.arm(instruction())
    boundary.disposition = CloudTaskCreateDisposition.ALREADY_EXISTS

    second = armer.arm(instruction())

    assert first.status is TaskArmingStatus.ARMED
    assert second.status is TaskArmingStatus.ALREADY_ARMED
    assert len(boundary.create_calls) == 2
    assert {call.task_name for call in boundary.create_calls} == {first.task_name}
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_status == ScheduledTaskAuditStatus.ALREADY_ARMED.value


def test_definite_create_failure_is_audited_once_and_not_retried() -> None:
    store = RecordingStateStore()
    definition_name = FakeTaskBoundary().build_task(instruction()).task_name
    error = CloudTaskCreateRejectedError(definition_name, "PermissionDenied")
    boundary = FakeTaskBoundary(error)

    with pytest.raises(CloudTaskCreateRejectedError):
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_status == ScheduledTaskAuditStatus.CREATE_FAILED.value


def test_ambiguous_create_is_audited_once_with_same_deterministic_identity() -> None:
    store = RecordingStateStore()
    definition_name = FakeTaskBoundary().build_task(instruction()).task_name
    error = CloudTaskCreateAmbiguousError(definition_name, "TimeoutError")
    boundary = FakeTaskBoundary(error)

    with pytest.raises(CloudTaskCreateAmbiguousError) as captured:
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1
    assert captured.value.task_name == boundary.create_calls[0].task_name
    assert boundary.create_calls[0].task_id == deterministic_task_id(instruction())
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_status == ScheduledTaskAuditStatus.CREATE_AMBIGUOUS.value


def test_later_ambiguous_reconciliation_uses_only_the_same_task_name() -> None:
    store = RecordingStateStore()
    task_name = FakeTaskBoundary().build_task(instruction()).task_name
    boundary = FakeTaskBoundary(CloudTaskCreateAmbiguousError(task_name, "TimeoutError"))
    armer = make_armer(store, boundary)

    with pytest.raises(CloudTaskCreateAmbiguousError):
        armer.arm(instruction())
    boundary.disposition = CloudTaskCreateDisposition.RECONCILED
    result = armer.arm(instruction())

    assert result.status is TaskArmingStatus.RECONCILED_ARMED
    assert len(boundary.create_calls) == 2
    assert {call.task_name for call in boundary.create_calls} == {task_name}


@pytest.mark.parametrize(
    ("boundary_error", "audit_status"),
    [
        (
            CloudTaskNameReservedError("task-name"),
            ScheduledTaskAuditStatus.TASK_NAME_RESERVED,
        ),
        (
            CloudTaskDefinitionConflictError("task-name", ("payload",)),
            ScheduledTaskAuditStatus.DEFINITION_CONFLICT,
        ),
    ],
)
def test_duplicate_reconciliation_failure_is_precisely_audited(
    boundary_error: Exception,
    audit_status: ScheduledTaskAuditStatus,
) -> None:
    store = RecordingStateStore()
    boundary = FakeTaskBoundary(boundary_error)

    with pytest.raises(type(boundary_error)):
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1
    record = store.get_event(3)
    assert record is not None
    assert record.context.scheduled_task_status == audit_status.value


def test_external_success_then_final_audit_failure_never_creates_second_task() -> None:
    store = FailFinalAuditStore(ScheduledTaskAuditStatus.ARMED)
    boundary = FakeTaskBoundary()

    with pytest.raises(TaskOutcomeAuditPersistenceError) as captured:
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1
    assert captured.value.external_task_known is True
    assert captured.value.task_name == boundary.create_calls[0].task_name


@pytest.mark.parametrize(
    ("boundary_error", "audit_status"),
    [
        (
            CloudTaskCreateRejectedError("task-name", "PermissionDenied"),
            ScheduledTaskAuditStatus.CREATE_FAILED,
        ),
        (
            CloudTaskCreateAmbiguousError("task-name", "TimeoutError"),
            ScheduledTaskAuditStatus.CREATE_AMBIGUOUS,
        ),
    ],
)
def test_create_outcome_audit_failure_never_retries_task(
    boundary_error: Exception,
    audit_status: ScheduledTaskAuditStatus,
) -> None:
    store = FailFinalAuditStore(audit_status)
    boundary = FakeTaskBoundary(boundary_error)

    with pytest.raises(TaskOutcomeAuditPersistenceError):
        make_armer(store, boundary).arm(instruction())

    assert len(boundary.create_calls) == 1


@pytest.mark.parametrize(
    "posting_status",
    [
        PostingStatus.CLAIMED,
        PostingStatus.IN_PROGRESS,
        PostingStatus.SUCCEEDED,
        PostingStatus.FAILED,
        PostingStatus.UNCERTAIN,
    ],
)
def test_claimed_or_terminal_posting_record_is_not_mutated_or_armed(
    posting_status: PostingStatus,
) -> None:
    store = RecordingStateStore()
    context = EventPostingContext(3, "GW3", DEADLINE_UTC)
    decision = store.claim_event(context, claimed_at_utc=NOW_UTC - timedelta(minutes=1))
    assert decision.claim is not None
    if posting_status is not PostingStatus.CLAIMED:
        store.mark_posting_attempt(decision.claim, posting_attempted_at_utc=NOW_UTC)
        if posting_status is PostingStatus.SUCCEEDED:
            store.record_success(decision.claim, x_post_id="123456789")
        elif posting_status is PostingStatus.FAILED:
            store.record_failure(decision.claim, error_detail="definite failure")
        elif posting_status is PostingStatus.UNCERTAIN:
            store.record_uncertain(decision.claim, error_detail="ambiguous outcome")
    before = store.get_event(3)
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is TaskArmingStatus.POSTING_CLOSED
    assert result.existing_posting_status is posting_status
    assert boundary.create_calls == []
    assert store.get_event(3) == before


def test_claim_race_before_create_closes_without_task_or_metadata_overwrite() -> None:
    store = ClaimBeforeCreateStore()
    boundary = FakeTaskBoundary()

    result = make_armer(store, boundary).arm(instruction())

    assert result.status is TaskArmingStatus.POSTING_CLOSED
    assert result.existing_posting_status is PostingStatus.CLAIMED
    assert boundary.create_calls == []
    record = store.get_event(3)
    assert record is not None
    assert record.status is PostingStatus.CLAIMED


def test_changed_deadline_reconciles_new_identity_without_deleting_old_task() -> None:
    store = RecordingStateStore()
    boundary = FakeTaskBoundary()
    armer = make_armer(store, boundary)
    old_instruction = instruction()
    new_instruction = instruction(deadline=DEADLINE_UTC + timedelta(hours=1))

    first = armer.arm(old_instruction)
    second = armer.arm(new_instruction)

    assert first.status is TaskArmingStatus.ARMED
    assert second.status is TaskArmingStatus.ARMED
    assert first.task_name != second.task_name
    assert len(boundary.create_calls) == 2
    record = store.get_event(3)
    assert record is not None
    assert record.context.official_deadline_utc == new_instruction.expected_deadline_utc
    assert record.context.scheduled_task_id == deterministic_task_id(new_instruction)


def test_scheduling_reconciliation_preserves_existing_preflight_metadata() -> None:
    store = RecordingStateStore()
    store.reconcile_unclaimed_event(
        EventPostingContext(
            event_id=3,
            event_code=None,
            official_deadline_utc=DEADLINE_UTC,
            preflight_status="future-placeholder",
        )
    )

    make_armer(store, FakeTaskBoundary()).arm(instruction())

    record = store.get_event(3)
    assert record is not None
    assert record.context.preflight_status == "future-placeholder"


def test_precreate_audit_failure_makes_zero_create_calls() -> None:
    class BrokenStore(RecordingStateStore):
        def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
            raise OSError("simulated persistence failure")

    boundary = FakeTaskBoundary()

    with pytest.raises(TaskArmingAuditPersistenceError, match="create_task was not called"):
        make_armer(BrokenStore(), boundary).arm(instruction())

    assert boundary.create_calls == []
