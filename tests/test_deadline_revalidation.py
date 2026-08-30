from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from fpl_bot.deadline_revalidation import (
    DeadlineExecutionRevalidator,
    EarlyDeadlineExecutionError,
    ScheduledDeadlineInstruction,
    ScheduledInstructionValidationError,
    StaleDeadlineInstructionError,
)
from fpl_bot.errors import DataValidationError, FplApiError
from fpl_bot.models import EventKind, EventReport
from fpl_bot.post_execution import (
    DeadlinePostExecutionCoordinator,
    DeadlinePostExecutionResult,
)
from fpl_bot.posting_state import (
    ClaimDecision,
    EventPostingContext,
    InMemoryPostingStateStore,
    PostingAuditRecord,
    PostingClaim,
    PostingStatus,
)
from fpl_bot.x_api import CreatedXPost

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
AFTER_DEADLINE_UTC = DEADLINE_UTC + timedelta(minutes=1)
X_POST_ID = "987654321"


class FakeFplSource:
    def __init__(
        self,
        events: list[str],
        *,
        bootstrap: Mapping[str, Any] | Exception,
        fixtures: Sequence[Mapping[str, Any]] | Exception,
    ) -> None:
        self.events = events
        self.bootstrap = bootstrap
        self.fixtures = fixtures
        self.fixture_event_ids: list[int] = []

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        self.events.append("bootstrap")
        if isinstance(self.bootstrap, Exception):
            raise self.bootstrap
        return self.bootstrap

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        self.events.append("fixtures")
        self.fixture_event_ids.append(event_id)
        if isinstance(self.fixtures, Exception):
            raise self.fixtures
        return self.fixtures


class RecordingStateStore(InMemoryPostingStateStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__(claim_id_factory=lambda: "claim-1")
        self.events = events
        self.reconciled_contexts: list[EventPostingContext] = []

    def get_event(self, event_id: int) -> PostingAuditRecord | None:
        self.events.append("state_get")
        return super().get_event(event_id)

    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        self.events.append("state_reconcile")
        self.reconciled_contexts.append(context)
        return super().reconcile_unclaimed_event(context)

    def claim_event(
        self,
        context: EventPostingContext,
        *,
        claimed_at_utc: datetime,
    ) -> ClaimDecision:
        self.events.append("claim")
        return super().claim_event(context, claimed_at_utc=claimed_at_utc)

    def mark_posting_attempt(
        self,
        claim: PostingClaim,
        *,
        posting_attempted_at_utc: datetime,
    ) -> PostingAuditRecord:
        self.events.append("mark_in_progress")
        return super().mark_posting_attempt(
            claim,
            posting_attempted_at_utc=posting_attempted_at_utc,
        )

    def record_success(
        self,
        claim: PostingClaim,
        *,
        x_post_id: str,
    ) -> PostingAuditRecord:
        self.events.append("record_success")
        return super().record_success(claim, x_post_id=x_post_id)


class ClaimDuringReconcileStateStore(RecordingStateStore):
    def reconcile_unclaimed_event(self, context: EventPostingContext) -> PostingAuditRecord:
        self.events.append("state_reconcile")
        self.reconciled_contexts.append(context)
        existing = InMemoryPostingStateStore.get_event(self, context.event_id)
        assert existing is not None
        assert existing.status is None
        decision = InMemoryPostingStateStore.claim_event(
            self,
            existing.context,
            claimed_at_utc=DEADLINE_UTC,
        )
        assert decision.granted is True
        return InMemoryPostingStateStore.reconcile_unclaimed_event(self, context)


class RecordingPostExecutor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.reports: list[EventReport] = []

    def execute(self, report: EventReport) -> DeadlinePostExecutionResult:
        self.events.append("coordinator")
        self.reports.append(report)
        return DeadlinePostExecutionResult(
            context=EventPostingContext(
                event_id=report.event.event_id,
                event_code=report.event_code,
                official_deadline_utc=report.event.deadline_utc,
            ),
            tweet=report.tweet,
            x_post_id=X_POST_ID,
        )


class RecordingXCreator:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.events.append("x_create")
        self.messages.append(text)
        return CreatedXPost(post_id=X_POST_ID, text=text)


def bootstrap_payload(
    *,
    event_id: int = EVENT_ID,
    deadline: datetime = DEADLINE_UTC,
    deadline_value: object | None = None,
) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": event_id,
                "name": f"Gameweek {event_id}",
                "deadline_time": (
                    deadline.isoformat().replace("+00:00", "Z")
                    if deadline_value is None
                    else deadline_value
                ),
                "is_current": False,
                "is_next": False,
            }
        ],
        "teams": [
            {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
            for team_id in range(1, 21)
        ],
    }


def fixture_payload(kind: EventKind) -> list[dict[str, int]]:
    regular_pairs = [(team_id, team_id + 1) for team_id in range(1, 21, 2)]
    if kind is EventKind.GW:
        pairs = regular_pairs
    elif kind is EventKind.BGW:
        pairs = regular_pairs[:-1]
    elif kind is EventKind.DGW:
        pairs = [*regular_pairs, (1, 2)]
    else:
        pairs = [*regular_pairs[:-1], (1, 2)]
    return [
        {"id": index, "event": EVENT_ID, "team_h": home, "team_a": away}
        for index, (home, away) in enumerate(pairs, start=1)
    ]


def recording_clock(events: list[str], value: datetime):
    def clock() -> datetime:
        events.append("now")
        return value

    return clock


def setup_revalidator(
    *,
    bootstrap: Mapping[str, Any] | Exception | None = None,
    fixtures: Sequence[Mapping[str, Any]] | Exception | None = None,
    now: datetime = AFTER_DEADLINE_UTC,
) -> tuple[
    DeadlineExecutionRevalidator,
    FakeFplSource,
    RecordingStateStore,
    RecordingPostExecutor,
    list[str],
]:
    events: list[str] = []
    source = FakeFplSource(
        events,
        bootstrap=bootstrap if bootstrap is not None else bootstrap_payload(),
        fixtures=fixtures if fixtures is not None else fixture_payload(EventKind.GW),
    )
    store = RecordingStateStore(events)
    post_executor = RecordingPostExecutor(events)
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        post_executor,
        clock=recording_clock(events, now),
    )
    return revalidator, source, store, post_executor, events


def instruction() -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, DEADLINE_UTC)


def assert_no_posting_activity(
    store: RecordingStateStore,
    post_executor: RecordingPostExecutor,
) -> None:
    assert store.events.count("state_get") == 0
    assert store.events.count("state_reconcile") == 0
    assert store.events.count("claim") == 0
    assert store.events.count("mark_in_progress") == 0
    assert post_executor.reports == []


def seed_posting_status(store: RecordingStateStore, status: PostingStatus) -> None:
    context = EventPostingContext(
        event_id=EVENT_ID,
        event_code="GW3",
        official_deadline_utc=DEADLINE_UTC,
        scheduled_task_id="future-task-3",
    )
    decision = InMemoryPostingStateStore.claim_event(
        store,
        context,
        claimed_at_utc=DEADLINE_UTC - timedelta(minutes=1),
    )
    assert decision.claim is not None
    if status is PostingStatus.CLAIMED:
        return
    InMemoryPostingStateStore.mark_posting_attempt(
        store,
        decision.claim,
        posting_attempted_at_utc=DEADLINE_UTC,
    )
    if status is PostingStatus.IN_PROGRESS:
        return
    if status is PostingStatus.SUCCEEDED:
        InMemoryPostingStateStore.record_success(store, decision.claim, x_post_id=X_POST_ID)
    elif status is PostingStatus.FAILED:
        InMemoryPostingStateStore.record_failure(
            store,
            decision.claim,
            error_detail="definite failure",
        )
    else:
        InMemoryPostingStateStore.record_uncertain(
            store,
            decision.claim,
            error_detail="ambiguous write outcome",
        )


def test_matching_identity_revalidates_then_calls_coordinator_exactly_once() -> None:
    revalidator, source, store, post_executor, events = setup_revalidator()

    result = revalidator.execute(instruction())

    assert result.x_post_id == X_POST_ID
    assert source.fixture_event_ids == [EVENT_ID]
    assert len(post_executor.reports) == 1
    assert events == [
        "bootstrap",
        "now",
        "fixtures",
        "state_get",
        "state_reconcile",
        "coordinator",
    ]


def test_missing_expected_event_is_stale_before_any_posting_activity() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        bootstrap=bootstrap_payload(event_id=4)
    )

    with pytest.raises(StaleDeadlineInstructionError, match="absent"):
        revalidator.execute(instruction())

    assert events == ["bootstrap"]
    assert_no_posting_activity(store, post_executor)


@pytest.mark.parametrize(
    "live_deadline",
    [DEADLINE_UTC + timedelta(minutes=5), DEADLINE_UTC - timedelta(minutes=5)],
    ids=["moved-later", "moved-earlier"],
)
def test_changed_deadline_is_stale_before_any_posting_activity(
    live_deadline: datetime,
) -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        bootstrap=bootstrap_payload(deadline=live_deadline)
    )

    with pytest.raises(StaleDeadlineInstructionError, match="deadline changed"):
        revalidator.execute(instruction())

    assert events == ["bootstrap"]
    assert_no_posting_activity(store, post_executor)


def test_execution_before_unchanged_deadline_fails_before_fixtures_or_posting() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        now=DEADLINE_UTC - timedelta(seconds=1)
    )

    with pytest.raises(EarlyDeadlineExecutionError, match="before"):
        revalidator.execute(instruction())

    assert events == ["bootstrap", "now"]
    assert_no_posting_activity(store, post_executor)


@pytest.mark.parametrize("now", [DEADLINE_UTC, AFTER_DEADLINE_UTC], ids=["exact", "after"])
def test_execution_at_or_after_unchanged_deadline_can_proceed(now: datetime) -> None:
    revalidator, _, _, post_executor, _ = setup_revalidator(now=now)

    revalidator.execute(instruction())

    assert len(post_executor.reports) == 1


def test_bootstrap_fetch_failure_fails_closed() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        bootstrap=FplApiError("simulated bootstrap failure")
    )

    with pytest.raises(FplApiError, match="bootstrap failure"):
        revalidator.execute(instruction())

    assert events == ["bootstrap"]
    assert_no_posting_activity(store, post_executor)


def test_malformed_live_event_deadline_fails_closed() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        bootstrap=bootstrap_payload(deadline_value="not-a-deadline")
    )

    with pytest.raises(DataValidationError, match="Invalid FPL deadline"):
        revalidator.execute(instruction())

    assert events == ["bootstrap"]
    assert_no_posting_activity(store, post_executor)


def test_fixture_fetch_failure_fails_closed() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        fixtures=FplApiError("simulated fixture failure")
    )

    with pytest.raises(FplApiError, match="fixture failure"):
        revalidator.execute(instruction())

    assert events == ["bootstrap", "now", "fixtures"]
    assert_no_posting_activity(store, post_executor)


def test_malformed_fixture_data_fails_closed() -> None:
    malformed = fixture_payload(EventKind.GW)
    malformed[0] = {**malformed[0], "team_h": 99}
    revalidator, _, store, post_executor, events = setup_revalidator(fixtures=malformed)

    with pytest.raises(DataValidationError, match="unknown team ID 99"):
        revalidator.execute(instruction())

    assert events == ["bootstrap", "now", "fixtures"]
    assert_no_posting_activity(store, post_executor)


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        (EventKind.GW, "GW3"),
        (EventKind.BGW, "BGW3"),
        (EventKind.DGW, "DGW3"),
        (EventKind.BDGW, "BDGW3"),
    ],
)
def test_fresh_fixtures_determine_live_event_code(
    kind: EventKind,
    expected_code: str,
) -> None:
    revalidator, _, _, post_executor, _ = setup_revalidator(fixtures=fixture_payload(kind))

    revalidator.execute(instruction())

    assert post_executor.reports[0].classification.kind is kind
    assert post_executor.reports[0].event_code == expected_code
    assert post_executor.reports[0].tweet.endswith(f"#{expected_code}")


def test_fresh_code_reconciles_unclaimed_metadata_without_losing_task_audit() -> None:
    revalidator, _, store, post_executor, events = setup_revalidator(
        fixtures=fixture_payload(EventKind.DGW)
    )
    old_context = EventPostingContext(
        event_id=EVENT_ID,
        event_code="GW3",
        official_deadline_utc=DEADLINE_UTC,
        scheduled_task_id="future-task-3",
        scheduled_task_status="armed",
        preflight_status="complete",
    )
    InMemoryPostingStateStore.reconcile_unclaimed_event(store, old_context)
    events.clear()

    revalidator.execute(instruction())

    assert post_executor.reports[0].event_code == "DGW3"
    persisted = InMemoryPostingStateStore.get_event(store, EVENT_ID)
    assert persisted is not None
    assert persisted.status is None
    assert persisted.context.event_code == "DGW3"
    assert persisted.context.scheduled_task_id == "future-task-3"
    assert persisted.context.scheduled_task_status == "armed"
    assert persisted.context.preflight_status == "complete"
    assert events == [
        "bootstrap",
        "now",
        "fixtures",
        "state_get",
        "state_reconcile",
        "coordinator",
    ]


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
def test_existing_posting_state_skips_reconciliation_and_remains_closed(
    status: PostingStatus,
) -> None:
    events: list[str] = []
    source = FakeFplSource(
        events,
        bootstrap=bootstrap_payload(),
        fixtures=fixture_payload(EventKind.DGW),
    )
    store = RecordingStateStore(events)
    seed_posting_status(store, status)
    before = InMemoryPostingStateStore.get_event(store, EVENT_ID)
    events.clear()
    x_client = RecordingXCreator(events)
    post_executor = DeadlinePostExecutionCoordinator(
        store,
        x_client,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        post_executor,
        clock=recording_clock(events, AFTER_DEADLINE_UTC),
    )

    result = revalidator.execute(instruction())

    assert result.posted is False
    assert result.existing_status is status
    assert events == ["bootstrap", "now", "fixtures", "state_get", "claim"]
    assert x_client.messages == []
    assert InMemoryPostingStateStore.get_event(store, EVENT_ID) == before


def test_claim_during_reconciliation_cannot_overwrite_state_or_reach_x() -> None:
    events: list[str] = []
    source = FakeFplSource(
        events,
        bootstrap=bootstrap_payload(),
        fixtures=fixture_payload(EventKind.DGW),
    )
    store = ClaimDuringReconcileStateStore(events)
    old_context = EventPostingContext(
        event_id=EVENT_ID,
        event_code="GW3",
        official_deadline_utc=DEADLINE_UTC,
        scheduled_task_id="future-task-3",
    )
    InMemoryPostingStateStore.reconcile_unclaimed_event(store, old_context)
    events.clear()
    x_client = RecordingXCreator(events)
    post_executor = DeadlinePostExecutionCoordinator(
        store,
        x_client,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        post_executor,
        clock=recording_clock(events, AFTER_DEADLINE_UTC),
    )

    result = revalidator.execute(instruction())

    assert result.posted is False
    assert result.existing_status is PostingStatus.CLAIMED
    assert events == [
        "bootstrap",
        "now",
        "fixtures",
        "state_get",
        "state_reconcile",
        "state_get",
        "claim",
    ]
    assert x_client.messages == []
    persisted = InMemoryPostingStateStore.get_event(store, EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.CLAIMED
    assert persisted.context == old_context


def test_live_revalidation_precedes_real_coordinator_claim_and_x_boundary() -> None:
    events: list[str] = []
    source = FakeFplSource(
        events,
        bootstrap=bootstrap_payload(),
        fixtures=fixture_payload(EventKind.DGW),
    )
    store = RecordingStateStore(events)
    InMemoryPostingStateStore.reconcile_unclaimed_event(
        store,
        EventPostingContext(
            event_id=EVENT_ID,
            event_code="GW3",
            official_deadline_utc=DEADLINE_UTC,
            scheduled_task_id="future-task-3",
        ),
    )
    x_client = RecordingXCreator(events)
    post_executor = DeadlinePostExecutionCoordinator(
        store,
        x_client,
        clock=lambda: AFTER_DEADLINE_UTC,
    )
    revalidator = DeadlineExecutionRevalidator(
        source,
        store,
        post_executor,
        clock=recording_clock(events, AFTER_DEADLINE_UTC),
    )

    result = revalidator.execute(instruction())

    assert result.x_post_id == X_POST_ID
    assert events == [
        "bootstrap",
        "now",
        "fixtures",
        "state_get",
        "state_reconcile",
        "claim",
        "mark_in_progress",
        "x_create",
        "record_success",
    ]
    assert x_client.messages == ["Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #DGW3"]
    persisted = InMemoryPostingStateStore.get_event(store, EVENT_ID)
    assert persisted is not None
    assert persisted.status is PostingStatus.SUCCEEDED
    assert persisted.context.event_code == "DGW3"
    assert persisted.context.scheduled_task_id == "future-task-3"


@pytest.mark.parametrize(
    ("event_id", "deadline"),
    [
        (0, DEADLINE_UTC),
        (EVENT_ID, datetime(2026, 8, 22, 10, 30)),
        (
            EVENT_ID,
            datetime(2026, 8, 22, 11, 30, tzinfo=timezone(timedelta(hours=1))),
        ),
    ],
)
def test_instruction_identity_requires_positive_event_and_aware_utc_deadline(
    event_id: int,
    deadline: datetime,
) -> None:
    with pytest.raises(ScheduledInstructionValidationError):
        ScheduledDeadlineInstruction(event_id, deadline)
