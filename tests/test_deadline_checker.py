from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fpl_bot.cloud_tasks import (
    CloudTaskCreateAmbiguousError,
    CloudTaskCreateRejectedError,
    CloudTaskDefinitionConflictError,
    CloudTaskNameReservedError,
    CloudTaskReconciliationError,
)
from fpl_bot.deadline_checker import (
    DeadlineChecker,
    DeadlineCheckerResult,
    DeadlineCheckerStatus,
)
from fpl_bot.deadline_planning import (
    DeadlinePlanner,
    DeadlinePlanningDecision,
    DeadlinePlanningStatus,
)
from fpl_bot.deadline_revalidation import (
    DeadlineExecutionRevalidator,
    ScheduledDeadlineInstruction,
    StaleDeadlineInstructionError,
)
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.post_execution import (
    DeadlinePostExecutionCoordinator,
    DeadlinePostExecutionResult,
    XPostSuccessPersistenceError,
)
from fpl_bot.posting_state import (
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingStatus,
)
from fpl_bot.preflight_arming import (
    PreflightTaskArmingResult,
    PreflightTaskArmingStatus,
)
from fpl_bot.task_arming import (
    TaskArmingAuditPersistenceError,
    TaskArmingResult,
    TaskArmingStatus,
    TaskOutcomeAuditPersistenceError,
)
from fpl_bot.x_api import CreatedXPost
from fpl_bot.x_errors import XAmbiguousWriteError, XRequestRejectedError

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
BEFORE_DEADLINE_UTC = DEADLINE_UTC - timedelta(minutes=5)
AFTER_DEADLINE_UTC = DEADLINE_UTC + timedelta(minutes=5)
TASK_NAME = "projects/test/locations/europe-west2/queues/fpl/tasks/fpl-deadline"
POST_ID = "987654321"
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"


class FakePlanner:
    def __init__(
        self,
        outcomes: DeadlinePlanningDecision
        | Exception
        | Sequence[DeadlinePlanningDecision | Exception],
        events: list[str] | None = None,
    ) -> None:
        if isinstance(outcomes, Sequence) and not isinstance(outcomes, (str, bytes)):
            self.outcomes = list(outcomes)
        else:
            self.outcomes = [outcomes]
        self.events = events
        self.calls = 0

    def plan(self) -> DeadlinePlanningDecision:
        self.calls += 1
        if self.events is not None:
            self.events.append("plan")
        index = min(self.calls - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeArmer:
    def __init__(
        self,
        outcome: TaskArmingStatus | TaskArmingResult | Exception,
        *,
        posting_status: PostingStatus | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.outcome = outcome
        self.posting_status = posting_status
        self.events = events
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult:
        self.instructions.append(instruction)
        if self.events is not None:
            self.events.append("arm")
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if isinstance(self.outcome, TaskArmingResult):
            return self.outcome
        return TaskArmingResult(
            instruction=instruction,
            task_name=TASK_NAME,
            status=self.outcome,
            existing_posting_status=self.posting_status,
        )


class FakeExecutor:
    def __init__(
        self,
        outcome: DeadlinePostExecutionResult | Exception,
        events: list[str] | None = None,
    ) -> None:
        self.outcome = outcome
        self.events = events
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def execute(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> DeadlinePostExecutionResult:
        self.instructions.append(instruction)
        if self.events is not None:
            self.events.append("execute")
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakePreflightArmer:
    def __init__(self, outcome: PreflightTaskArmingStatus | Exception) -> None:
        self.outcome = outcome
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def arm(self, instruction: ScheduledDeadlineInstruction) -> PreflightTaskArmingResult:
        self.instructions.append(instruction)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return PreflightTaskArmingResult(
            instruction=instruction,
            task_name=f"{TASK_NAME}-preflight",
            status=self.outcome,
        )


class StaticFplSource:
    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        return bootstrap_payload()

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        assert event_id == EVENT_ID
        return fixture_payload()


class RecordingXCreator:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.messages.append(text)
        return CreatedXPost(post_id=POST_ID, text=text)


class CallbackOverdueArmer:
    def __init__(self, callback: DeadlineExecutionRevalidator) -> None:
        self.callback = callback
        self.instructions: list[ScheduledDeadlineInstruction] = []

    def arm(self, instruction: ScheduledDeadlineInstruction) -> TaskArmingResult:
        self.instructions.append(instruction)
        self.callback.execute(instruction)
        return TaskArmingResult(
            instruction=instruction,
            task_name=TASK_NAME,
            status=TaskArmingStatus.OVERDUE_SAME_DAY,
        )


def instruction(
    *,
    event_id: int = EVENT_ID,
    deadline: datetime = DEADLINE_UTC,
) -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(event_id, deadline)


def eligible(
    scheduled_instruction: ScheduledDeadlineInstruction | None = None,
) -> DeadlinePlanningDecision:
    return DeadlinePlanningDecision(
        DeadlinePlanningStatus.ELIGIBLE_TO_ARM,
        scheduled_instruction or instruction(),
    )


def not_today() -> DeadlinePlanningDecision:
    return DeadlinePlanningDecision(DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY)


def context() -> EventPostingContext:
    return EventPostingContext(EVENT_ID, "GW3", DEADLINE_UTC)


def posted_result() -> DeadlinePostExecutionResult:
    return DeadlinePostExecutionResult(context(), EXPECTED_TWEET, x_post_id=POST_ID)


def duplicate_result(status: PostingStatus) -> DeadlinePostExecutionResult:
    return DeadlinePostExecutionResult(context(), EXPECTED_TWEET, existing_status=status)


def make_checker(
    planner: FakePlanner | DeadlinePlanner,
    armer,
    executor,
    *,
    now: datetime,
    events: list[str] | None = None,
    preflight_armer: FakePreflightArmer | None = None,
) -> DeadlineChecker:
    def clock() -> datetime:
        if events is not None:
            events.append("clock")
        return now

    return DeadlineChecker(
        planner,
        armer,
        executor,
        preflight_task_armer=preflight_armer,
        clock=clock,
    )


def bootstrap_payload() -> dict[str, Any]:
    return {
        "events": [
            {
                "id": EVENT_ID,
                "name": "Gameweek 3",
                "deadline_time": DEADLINE_UTC.isoformat().replace("+00:00", "Z"),
                "is_current": False,
                "is_next": False,
            }
        ],
        "teams": [
            {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
            for team_id in range(1, 21)
        ],
    }


def fixture_payload() -> list[dict[str, int]]:
    return [
        {"id": index, "event": EVENT_ID, "team_h": team_id, "team_a": team_id + 1}
        for index, team_id in enumerate(range(1, 21, 2), start=1)
    ]


def real_execution_pipeline(
    store: InMemoryPostingStateStore | None = None,
) -> tuple[DeadlineExecutionRevalidator, InMemoryPostingStateStore, RecordingXCreator]:
    actual_store = store or InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    x_creator = RecordingXCreator()
    coordinator = DeadlinePostExecutionCoordinator(
        actual_store,
        x_creator,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    revalidator = DeadlineExecutionRevalidator(
        StaticFplSource(),
        actual_store,
        coordinator,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    return revalidator, actual_store, x_creator


def test_not_today_returns_no_action_without_arming_or_execution() -> None:
    planner = FakePlanner(not_today())
    armer = FakeArmer(TaskArmingStatus.ARMED)
    executor = FakeExecutor(posted_result())

    result = make_checker(planner, armer, executor, now=BEFORE_DEADLINE_UTC).run()

    assert result.status is DeadlineCheckerStatus.NO_ACTION_NOT_TODAY
    assert result.instruction is None
    assert armer.instructions == []
    assert executor.instructions == []


@pytest.mark.parametrize(
    ("arming_status", "checker_status"),
    [
        (TaskArmingStatus.ARMED, DeadlineCheckerStatus.TASK_ARMED),
        (TaskArmingStatus.ALREADY_ARMED, DeadlineCheckerStatus.TASK_ALREADY_ARMED),
        (TaskArmingStatus.RECONCILED_ARMED, DeadlineCheckerStatus.TASK_RECONCILED_ARMED),
    ],
)
def test_successful_future_arming_finishes_without_direct_execution(
    arming_status: TaskArmingStatus,
    checker_status: DeadlineCheckerStatus,
) -> None:
    planned = instruction()
    armer = FakeArmer(arming_status)
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible(planned)),
        armer,
        executor,
        now=BEFORE_DEADLINE_UTC,
    ).run()

    assert result.status is checker_status
    assert result.instruction is planned
    assert armer.instructions == [planned]
    assert executor.instructions == []


@pytest.mark.parametrize(
    "preflight_status",
    [
        PreflightTaskArmingStatus.SCHEDULED,
        PreflightTaskArmingStatus.ALREADY_SCHEDULED,
        PreflightTaskArmingStatus.RECONCILED_SCHEDULED,
        PreflightTaskArmingStatus.TOO_LATE,
    ],
)
def test_future_deadline_arms_preflight_without_direct_execution(
    preflight_status: PreflightTaskArmingStatus,
) -> None:
    planned = instruction()
    preflight = FakePreflightArmer(preflight_status)
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible(planned)),
        FakeArmer(TaskArmingStatus.ARMED),
        executor,
        now=BEFORE_DEADLINE_UTC,
        preflight_armer=preflight,
    ).run()

    assert result.status is DeadlineCheckerStatus.TASK_ARMED
    assert result.preflight_status is preflight_status
    assert result.preflight_failure_type is None
    assert preflight.instructions == [planned]
    assert executor.instructions == []


def test_preflight_arming_failure_is_reported_without_undoing_final_task() -> None:
    planned = instruction()
    preflight = FakePreflightArmer(RuntimeError("preflight unavailable"))
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible(planned)),
        FakeArmer(TaskArmingStatus.ARMED),
        executor,
        now=BEFORE_DEADLINE_UTC,
        preflight_armer=preflight,
    ).run()

    assert result.status is DeadlineCheckerStatus.TASK_ARMED
    assert result.preflight_status is None
    assert result.preflight_failure_type == "RuntimeError"
    assert preflight.instructions == [planned]
    assert executor.instructions == []


def test_not_today_and_overdue_paths_never_arm_preflight() -> None:
    preflight = FakePreflightArmer(PreflightTaskArmingStatus.SCHEDULED)
    no_action = make_checker(
        FakePlanner(not_today()),
        FakeArmer(TaskArmingStatus.ARMED),
        FakeExecutor(posted_result()),
        now=BEFORE_DEADLINE_UTC,
        preflight_armer=preflight,
    ).run()
    overdue = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        FakeExecutor(posted_result()),
        now=AFTER_DEADLINE_UTC,
        preflight_armer=preflight,
    ).run()

    assert no_action.status is DeadlineCheckerStatus.NO_ACTION_NOT_TODAY
    assert overdue.status is DeadlineCheckerStatus.OVERDUE_EXECUTED
    assert preflight.instructions == []


@pytest.mark.parametrize("now", [DEADLINE_UTC, AFTER_DEADLINE_UTC])
def test_exact_or_passed_deadline_uses_direct_execution(now: datetime) -> None:
    planned = instruction()
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible(planned)),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        executor,
        now=now,
    ).run()

    assert result.status is DeadlineCheckerStatus.OVERDUE_EXECUTED
    assert result.instruction is planned
    assert executor.instructions == [planned]


def test_checker_orders_clock_plan_arm_then_overdue_execution() -> None:
    events: list[str] = []
    planned = instruction()

    make_checker(
        FakePlanner(eligible(planned), events),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY, events=events),
        FakeExecutor(posted_result(), events),
        now=AFTER_DEADLINE_UTC,
        events=events,
    ).run()

    assert events == ["clock", "plan", "arm", "execute"]


@pytest.mark.parametrize(
    "status",
    [
        PostingStatus.CLAIMED,
        PostingStatus.IN_PROGRESS,
        PostingStatus.SUCCEEDED,
        PostingStatus.FAILED,
        PostingStatus.UNCERTAIN,
    ],
)
def test_overdue_closed_posting_state_is_duplicate_without_direct_execution(
    status: PostingStatus,
) -> None:
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.POSTING_CLOSED, posting_status=status),
        executor,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.OVERDUE_DUPLICATE
    assert result.existing_posting_status is status
    assert executor.instructions == []


def test_future_closed_posting_state_is_already_handled() -> None:
    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.POSTING_CLOSED, posting_status=PostingStatus.SUCCEEDED),
        FakeExecutor(posted_result()),
        now=BEFORE_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.ALREADY_HANDLED
    assert result.existing_posting_status is PostingStatus.SUCCEEDED


def test_overdue_duplicate_execution_result_is_explicit_no_op() -> None:
    executor = FakeExecutor(duplicate_result(PostingStatus.SUCCEEDED))

    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        executor,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.OVERDUE_DUPLICATE
    assert result.existing_posting_status is PostingStatus.SUCCEEDED


def test_stale_overdue_instruction_is_no_action() -> None:
    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        FakeExecutor(StaleDeadlineInstructionError("deadline moved")),
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.STALE


@pytest.mark.parametrize(
    "failure",
    [FplApiError("temporary FPL failure"), DataValidationError("temporary malformed FPL data")],
)
def test_overdue_fpl_failure_is_retryable_without_internal_retry(failure: Exception) -> None:
    executor = FakeExecutor(failure)

    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        executor,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.RETRYABLE_FAILURE
    assert result.failure_type == type(failure).__name__
    assert len(executor.instructions) == 1


@pytest.mark.parametrize(
    "failure",
    [FplApiError("bootstrap unavailable"), DataValidationError("malformed bootstrap")],
)
def test_planner_failure_is_retryable_without_arming_or_execution(failure: Exception) -> None:
    armer = FakeArmer(TaskArmingStatus.ARMED)
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(failure),
        armer,
        executor,
        now=BEFORE_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.RETRYABLE_FAILURE
    assert result.instruction is None
    assert armer.instructions == []
    assert executor.instructions == []


@pytest.mark.parametrize(
    "arming_failure",
    [
        CloudTaskCreateRejectedError(TASK_NAME, "PermissionDenied"),
        CloudTaskNameReservedError(TASK_NAME),
        CloudTaskDefinitionConflictError(TASK_NAME, ("payload",)),
        CloudTaskCreateAmbiguousError(TASK_NAME, "TimeoutError"),
        CloudTaskReconciliationError(TASK_NAME, "Unavailable"),
        TaskArmingAuditPersistenceError("audit unavailable"),
        TaskOutcomeAuditPersistenceError(
            TASK_NAME,
            "armed",
            external_task_known=True,
        ),
    ],
)
def test_non_overdue_arming_failure_propagates_without_direct_execution(
    arming_failure: Exception,
) -> None:
    executor = FakeExecutor(posted_result())
    checker = make_checker(
        FakePlanner(eligible()),
        FakeArmer(arming_failure),
        executor,
        now=BEFORE_DEADLINE_UTC,
    )

    with pytest.raises(type(arming_failure)):
        checker.run()

    assert executor.instructions == []


def test_changed_deadline_on_later_run_uses_new_instruction_identity() -> None:
    old = instruction()
    new = instruction(deadline=DEADLINE_UTC + timedelta(hours=1))
    planner = FakePlanner([eligible(old), eligible(new)])
    armer = FakeArmer(TaskArmingStatus.ARMED)
    executor = FakeExecutor(posted_result())
    checker = make_checker(planner, armer, executor, now=BEFORE_DEADLINE_UTC)

    first = checker.run()
    second = checker.run()

    assert first.instruction is old
    assert second.instruction is new
    assert armer.instructions == [old, new]
    assert executor.instructions == []


def test_prior_day_event_cannot_enter_overdue_recovery() -> None:
    now = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    source = type(
        "PlanningSource",
        (),
        {
            "fetch_bootstrap_static": lambda self: {
                "events": [
                    {
                        "id": 3,
                        "name": "Gameweek 3",
                        "deadline_time": DEADLINE_UTC.isoformat().replace("+00:00", "Z"),
                        "is_current": True,
                        "is_next": False,
                    },
                    {
                        "id": 4,
                        "name": "Gameweek 4",
                        "deadline_time": "2026-08-29T10:30:00Z",
                        "is_current": False,
                        "is_next": True,
                    },
                ]
            },
        },
    )()
    armer = FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY)
    executor = FakeExecutor(posted_result())

    result = make_checker(
        DeadlinePlanner(source, clock=lambda: now),
        armer,
        executor,
        now=now,
    ).run()

    assert result.status is DeadlineCheckerStatus.NO_ACTION_NOT_TODAY
    assert armer.instructions == []
    assert executor.instructions == []


def test_cloud_task_and_overdue_recovery_race_creates_one_x_post() -> None:
    planned = instruction()
    before = make_checker(
        FakePlanner(eligible(planned)),
        FakeArmer(TaskArmingStatus.ARMED),
        FakeExecutor(posted_result()),
        now=BEFORE_DEADLINE_UTC,
    ).run()
    revalidator, store, x_creator = real_execution_pipeline()
    race_armer = CallbackOverdueArmer(revalidator)

    overdue = make_checker(
        FakePlanner(eligible(planned)),
        race_armer,
        revalidator,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert before.status is DeadlineCheckerStatus.TASK_ARMED
    assert overdue.status is DeadlineCheckerStatus.OVERDUE_DUPLICATE
    assert race_armer.instructions == [planned]
    assert x_creator.messages == [EXPECTED_TWEET]
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED


def test_repeated_overdue_checker_runs_after_success_never_post_again() -> None:
    planned = instruction()
    revalidator, store, x_creator = real_execution_pipeline()
    checker = make_checker(
        FakePlanner(eligible(planned)),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        revalidator,
        now=AFTER_DEADLINE_UTC,
    )

    first = checker.run()
    second = checker.run()

    assert first.status is DeadlineCheckerStatus.OVERDUE_EXECUTED
    assert second.status is DeadlineCheckerStatus.OVERDUE_DUPLICATE
    assert x_creator.messages == [EXPECTED_TWEET]
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED


@pytest.mark.parametrize(
    "failure",
    [
        XRequestRejectedError("definite rejection", 403),
        XAmbiguousWriteError("ambiguous write"),
        XPostSuccessPersistenceError(POST_ID),
    ],
)
def test_closed_overdue_x_outcome_is_not_retried_inside_checker(failure: Exception) -> None:
    executor = FakeExecutor(failure)

    result = make_checker(
        FakePlanner(eligible()),
        FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY),
        executor,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert result.status is DeadlineCheckerStatus.FAILED_CLOSED
    assert result.failure_type == type(failure).__name__
    assert len(executor.instructions) == 1


def test_instruction_identity_is_unchanged_and_checker_caches_no_post_content() -> None:
    planned = instruction()
    armer = FakeArmer(TaskArmingStatus.OVERDUE_SAME_DAY)
    executor = FakeExecutor(posted_result())

    result = make_checker(
        FakePlanner(eligible(planned)),
        armer,
        executor,
        now=AFTER_DEADLINE_UTC,
    ).run()

    assert armer.instructions[0] is planned
    assert executor.instructions[0] is planned
    assert result.instruction is planned
    checker_fields = {item.name for item in fields(DeadlineCheckerResult)}
    assert {"event_code", "tweet", "classification"}.isdisjoint(checker_fields)
    assert {item.name for item in fields(ScheduledDeadlineInstruction)} == {
        "expected_event_id",
        "expected_deadline_utc",
    }
