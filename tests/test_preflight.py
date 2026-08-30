import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import fpl_bot.preflight as preflight_module
from fpl_bot.deadline_revalidation import (
    DeadlineExecutionRevalidator,
    ScheduledDeadlineInstruction,
    StaleDeadlineInstructionError,
)
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.post_execution import DeadlinePostExecutionCoordinator
from fpl_bot.posting_state import EventPostingContext, InMemoryPostingStateStore, PostingStatus
from fpl_bot.preflight import DeadlinePreflight, PreflightStatus
from fpl_bot.preflight_arming import (
    PreflightAuditStatus,
    PreflightTaskArmingResult,
    PreflightTaskArmingStatus,
)
from fpl_bot.task_arming import TaskArmingResult, TaskArmingStatus
from fpl_bot.x_api import CreatedXPost

EVENT_ID = 3
EXPECTED_DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
NOW_UTC = EXPECTED_DEADLINE_UTC - timedelta(minutes=5)
FINAL_TASK_NAME = "projects/test/locations/europe-west2/queues/fpl/tasks/final"
PREFLIGHT_TASK_NAME = "projects/test/locations/europe-west2/queues/fpl/tasks/preflight"
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"
X_POST_ID = "987654321"


class StaticFplSource:
    def __init__(self, bootstrap: Mapping[str, Any] | Exception) -> None:
        self.bootstrap = bootstrap
        self.bootstrap_calls = 0
        self.fixture_calls = 0

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        self.bootstrap_calls += 1
        if isinstance(self.bootstrap, Exception):
            raise self.bootstrap
        return self.bootstrap

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        self.fixture_calls += 1
        return [
            {
                "id": fixture_id,
                "event": event_id,
                "team_h": team_id,
                "team_a": team_id + 1,
            }
            for fixture_id, team_id in enumerate(range(1, 21, 2), start=1)
        ]


class RecordingFinalArmer:
    def __init__(self, status: TaskArmingStatus = TaskArmingStatus.ARMED) -> None:
        self.status = status
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult:
        self.instructions.append(instruction)
        existing = (
            PostingStatus.SUCCEEDED if self.status is TaskArmingStatus.POSTING_CLOSED else None
        )
        return TaskArmingResult(
            instruction=instruction,
            task_name=FINAL_TASK_NAME,
            status=self.status,
            existing_posting_status=existing,
        )


class RecordingPreflightArmer:
    def __init__(
        self,
        status: PreflightTaskArmingStatus = PreflightTaskArmingStatus.SCHEDULED,
    ) -> None:
        self.status = status
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def arm(self, instruction: ScheduledDeadlineInstruction) -> PreflightTaskArmingResult:
        self.instructions.append(instruction)
        existing = (
            PostingStatus.SUCCEEDED
            if self.status is PreflightTaskArmingStatus.POSTING_CLOSED
            else None
        )
        return PreflightTaskArmingResult(
            instruction=instruction,
            task_name=PREFLIGHT_TASK_NAME,
            status=self.status,
            existing_posting_status=existing,
        )


class ForbiddenPostExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, report):
        self.calls += 1
        raise AssertionError("stale task must not reach posting")


class RecordingXCreator:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.messages.append(text)
        return CreatedXPost(post_id=X_POST_ID, text=text)


def instruction(deadline: datetime = EXPECTED_DEADLINE_UTC) -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, deadline)


def bootstrap(deadline: datetime = EXPECTED_DEADLINE_UTC) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": EVENT_ID,
                "name": "Gameweek 3",
                "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
                "is_current": False,
                "is_next": False,
            }
        ],
        "teams": [
            {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
            for team_id in range(1, 21)
        ],
    }


def make_service(
    source: StaticFplSource,
    *,
    store: InMemoryPostingStateStore | None = None,
    final_armer: RecordingFinalArmer | None = None,
    preflight_armer: RecordingPreflightArmer | None = None,
    now: datetime = NOW_UTC,
) -> tuple[
    DeadlinePreflight,
    InMemoryPostingStateStore,
    RecordingFinalArmer,
    RecordingPreflightArmer,
]:
    actual_store = store or InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    actual_final = final_armer or RecordingFinalArmer()
    actual_preflight = preflight_armer or RecordingPreflightArmer()
    return (
        DeadlinePreflight(
            source,
            actual_store,
            actual_final,
            actual_preflight,
            clock=lambda: now,
        ),
        actual_store,
        actual_final,
        actual_preflight,
    )


def test_unchanged_deadline_records_passed_without_fixtures_or_task_arming() -> None:
    source = StaticFplSource(bootstrap())
    service, store, final_armer, preflight_armer = make_service(source)

    result = service.execute(instruction())

    assert result.status is PreflightStatus.OK
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0
    assert store.get_event(EVENT_ID).context.preflight_status == PreflightAuditStatus.PASSED.value


def test_delayed_unchanged_preflight_is_too_late_not_passed() -> None:
    source = StaticFplSource(bootstrap())
    service, store, final_armer, preflight_armer = make_service(
        source,
        now=EXPECTED_DEADLINE_UTC + timedelta(minutes=1),
    )

    result = service.execute(instruction())

    assert result.status is PreflightStatus.TOO_LATE
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.SKIPPED_TOO_LATE.value
    )


def test_late_preflight_does_not_inhibit_late_final_execution_or_duplicate_guard() -> None:
    now = EXPECTED_DEADLINE_UTC + timedelta(minutes=1)
    source = StaticFplSource(bootstrap())
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    store.reconcile_unclaimed_event(
        EventPostingContext(
            EVENT_ID,
            None,
            EXPECTED_DEADLINE_UTC,
            scheduled_task_id=FINAL_TASK_NAME,
            scheduled_task_status="scheduled",
        )
    )
    preflight, _, final_armer, preflight_armer = make_service(
        source,
        store=store,
        now=now,
    )
    x_creator = RecordingXCreator()
    coordinator = DeadlinePostExecutionCoordinator(store, x_creator, clock=lambda: now)
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        coordinator,
        clock=lambda: now,
    )

    preflight_result = preflight.execute(instruction())
    after_preflight = store.get_event(EVENT_ID)

    assert preflight_result.status is PreflightStatus.TOO_LATE
    assert after_preflight is not None
    assert after_preflight.status is None
    assert after_preflight.context.scheduled_task_id == FINAL_TASK_NAME
    assert after_preflight.context.scheduled_task_status == "scheduled"
    assert after_preflight.context.preflight_status == (PreflightAuditStatus.SKIPPED_TOO_LATE.value)
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0
    assert x_creator.messages == []

    first = revalidator.execute(instruction())
    duplicate = revalidator.execute(instruction())

    assert first.x_post_id == X_POST_ID
    assert duplicate.existing_status is PostingStatus.SUCCEEDED
    assert x_creator.messages == [EXPECTED_TWEET]
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED


def test_deadline_moved_later_today_arms_corrected_final_and_preflight() -> None:
    moved = EXPECTED_DEADLINE_UTC + timedelta(hours=2)
    source = StaticFplSource(bootstrap(moved))
    service, store, final_armer, preflight_armer = make_service(source)

    result = service.execute(instruction())

    assert result.status is PreflightStatus.STALE_REARMED
    assert result.authoritative_instruction == instruction(moved)
    assert final_armer.instructions == [result.authoritative_instruction]
    assert preflight_armer.instructions == [result.authoritative_instruction]
    assert store.get_event(EVENT_ID).context.official_deadline_utc == moved
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.STALE_REARMED.value
    )


def test_delayed_preflight_rearms_a_deadline_moved_later_today() -> None:
    moved = EXPECTED_DEADLINE_UTC + timedelta(hours=3)
    service, _, final_armer, preflight_armer = make_service(
        StaticFplSource(bootstrap(moved)),
        now=EXPECTED_DEADLINE_UTC + timedelta(minutes=1),
    )

    result = service.execute(instruction())

    assert result.status is PreflightStatus.STALE_REARMED
    assert final_armer.instructions == [instruction(moved)]
    assert preflight_armer.instructions == [instruction(moved)]


def test_deadline_moved_earlier_but_future_arms_corrected_final_task() -> None:
    moved = NOW_UTC + timedelta(minutes=2)
    preflight_armer = RecordingPreflightArmer(PreflightTaskArmingStatus.TOO_LATE)
    service, _, final_armer, actual_preflight = make_service(
        StaticFplSource(bootstrap(moved)),
        preflight_armer=preflight_armer,
    )

    result = service.execute(instruction())

    assert result.status is PreflightStatus.STALE_REARMED
    assert result.final_task_status is TaskArmingStatus.ARMED
    assert result.preflight_task_status is PreflightTaskArmingStatus.TOO_LATE
    assert final_armer.instructions == [instruction(moved)]
    assert actual_preflight.instructions == [instruction(moved)]


def test_delayed_preflight_rearms_a_deadline_moved_earlier_but_still_future() -> None:
    old_deadline = datetime(2026, 8, 29, 12, 30, tzinfo=UTC)
    now = datetime(2026, 8, 29, 10, 1, tzinfo=UTC)
    moved = datetime(2026, 8, 29, 11, 0, tzinfo=UTC)
    service, _, final_armer, preflight_armer = make_service(
        StaticFplSource(bootstrap(moved)),
        now=now,
    )

    result = service.execute(instruction(old_deadline))

    assert result.status is PreflightStatus.STALE_REARMED
    assert final_armer.instructions == [instruction(moved)]
    assert preflight_armer.instructions == [instruction(moved)]


def test_deadline_changed_to_reached_or_past_never_arms_or_posts() -> None:
    moved = NOW_UTC - timedelta(seconds=1)
    service, store, final_armer, preflight_armer = make_service(StaticFplSource(bootstrap(moved)))

    result = service.execute(instruction())

    assert result.status is PreflightStatus.OVERDUE_CHANGE
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.OVERDUE_CHANGE.value
    )


def test_delayed_preflight_with_changed_past_deadline_never_arms_or_posts() -> None:
    old_deadline = datetime(2026, 8, 29, 12, 30, tzinfo=UTC)
    now = datetime(2026, 8, 29, 11, 1, tzinfo=UTC)
    moved = datetime(2026, 8, 29, 11, 0, tzinfo=UTC)
    service, store, final_armer, preflight_armer = make_service(
        StaticFplSource(bootstrap(moved)),
        now=now,
    )

    result = service.execute(instruction(old_deadline))

    assert result.status is PreflightStatus.OVERDUE_CHANGE
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.OVERDUE_CHANGE.value
    )


def test_deadline_moved_to_another_london_day_does_not_arm_tasks() -> None:
    moved = EXPECTED_DEADLINE_UTC + timedelta(days=1)
    service, store, final_armer, preflight_armer = make_service(StaticFplSource(bootstrap(moved)))

    result = service.execute(instruction())

    assert result.status is PreflightStatus.STALE_OTHER_DAY
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.STALE_OTHER_DAY.value
    )


def test_expected_event_missing_is_stale_without_task_arming() -> None:
    service, _, final_armer, preflight_armer = make_service(StaticFplSource({"events": []}))

    result = service.execute(instruction())

    assert result.status is PreflightStatus.STALE_EVENT_MISSING
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []


@pytest.mark.parametrize(
    "payload",
    [FplApiError("unavailable"), {"events": "malformed"}],
)
def test_unavailable_or_malformed_fpl_is_retryable_and_never_arms(payload: object) -> None:
    source = StaticFplSource(payload)  # type: ignore[arg-type]
    service, store, final_armer, preflight_armer = make_service(source)

    with pytest.raises((FplApiError, DataValidationError)):
        service.execute(instruction())

    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0
    assert store.get_event(EVENT_ID).context.preflight_status == (
        PreflightAuditStatus.FAILED_RETRYABLE.value
    )


def test_duplicate_unchanged_preflight_deliveries_have_no_write_boundary() -> None:
    source = StaticFplSource(bootstrap())
    service, _, final_armer, preflight_armer = make_service(source)

    first = service.execute(instruction())
    second = service.execute(instruction())

    assert first.status is PreflightStatus.OK
    assert second.status is PreflightStatus.OK
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0


def test_duplicate_delayed_preflight_deliveries_remain_too_late_without_tasks() -> None:
    source = StaticFplSource(bootstrap())
    service, _, final_armer, preflight_armer = make_service(
        source,
        now=EXPECTED_DEADLINE_UTC + timedelta(minutes=1),
    )

    first = service.execute(instruction())
    second = service.execute(instruction())

    assert first.status is PreflightStatus.TOO_LATE
    assert second.status is PreflightStatus.TOO_LATE
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []
    assert source.fixture_calls == 0


@pytest.mark.parametrize("posting_status", list(PostingStatus))
def test_delayed_preflight_does_not_mutate_any_claimed_posting_state(
    posting_status: PostingStatus,
) -> None:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    posting_context = EventPostingContext(EVENT_ID, "GW3", EXPECTED_DEADLINE_UTC)
    decision = store.claim_event(
        posting_context,
        claimed_at_utc=EXPECTED_DEADLINE_UTC - timedelta(seconds=2),
    )
    claim = decision.claim
    assert claim is not None
    if posting_status is not PostingStatus.CLAIMED:
        store.mark_posting_attempt(
            claim,
            posting_attempted_at_utc=EXPECTED_DEADLINE_UTC - timedelta(seconds=1),
        )
        if posting_status is PostingStatus.SUCCEEDED:
            store.record_success(claim, x_post_id=X_POST_ID)
        elif posting_status is PostingStatus.FAILED:
            store.record_failure(claim, error_detail="definite test failure")
        elif posting_status is PostingStatus.UNCERTAIN:
            store.record_uncertain(claim, error_detail="ambiguous test outcome")
    before = store.get_event(EVENT_ID)
    service, _, final_armer, preflight_armer = make_service(
        StaticFplSource(bootstrap()),
        store=store,
        now=EXPECTED_DEADLINE_UTC + timedelta(minutes=1),
    )

    result = service.execute(instruction())

    assert result.status is PreflightStatus.POSTING_CLOSED
    assert result.existing_posting_status is posting_status
    assert store.get_event(EVENT_ID) == before
    assert final_armer.instructions == []
    assert preflight_armer.instructions == []


def test_old_final_task_remains_harmless_after_preflight_detects_moved_deadline() -> None:
    moved = EXPECTED_DEADLINE_UTC + timedelta(hours=2)
    source = StaticFplSource(bootstrap(moved))
    store = InMemoryPostingStateStore()
    post_executor = ForbiddenPostExecutor()
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        post_executor,
        clock=lambda: EXPECTED_DEADLINE_UTC,
    )

    with pytest.raises(StaleDeadlineInstructionError):
        revalidator.execute(instruction())

    assert post_executor.calls == 0
    assert store.get_event(EVENT_ID) is None


def test_preflight_dependency_graph_contains_no_post_creation_capability() -> None:
    source = inspect.getsource(preflight_module)
    constructor_parameters = set(inspect.signature(DeadlinePreflight).parameters)

    assert "post_execution" not in source
    assert "x_api" not in source
    assert "create_text_post" not in source
    assert "DeadlineExecutionRevalidator" not in source
    assert constructor_parameters == {
        "fpl_source",
        "audit_store",
        "final_task_armer",
        "preflight_task_armer",
        "clock",
    }
