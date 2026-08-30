"""Safety-critical orchestration for one already-resolved FPL deadline Post."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fpl_bot.classification import render_event_code
from fpl_bot.errors import FplBotError
from fpl_bot.models import EventReport
from fpl_bot.posting_state import (
    EventPostingContext,
    PostingClaim,
    PostingStateStore,
    PostingStateValidationError,
    PostingStatus,
    require_utc,
)
from fpl_bot.tweet import render_v1_tweet
from fpl_bot.x_api import XPostCreator
from fpl_bot.x_errors import XAmbiguousWriteError, XApiError, XRequestRejectedError


class DeadlinePostExecutionError(FplBotError):
    """Base class for deadline-Post orchestration failures."""


class DeadlinePostValidationError(DeadlinePostExecutionError):
    """Raised before claiming when the resolved event report is inconsistent."""


class PostingStatePersistenceError(DeadlinePostExecutionError):
    """Raised when a required pre-write state transition cannot be confirmed."""


class XPostSuccessPersistenceError(DeadlinePostExecutionError):
    """Raised when X succeeded but durable success persistence is unconfirmed."""

    def __init__(self, x_post_id: str) -> None:
        super().__init__(
            "X Post creation succeeded, but durable success recording could not be confirmed; "
            "do not post again and reconcile manually"
        )
        self.x_post_id = x_post_id


class XOutcomePersistenceError(DeadlinePostExecutionError):
    """Raised when a failed or uncertain X outcome cannot be durably recorded."""

    def __init__(self, terminal_status: PostingStatus, x_error_type: str) -> None:
        super().__init__(
            f"X outcome {terminal_status.value} could not be durably recorded; "
            "do not retry the Post"
        )
        self.terminal_status = terminal_status
        self.x_error_type = x_error_type


class UnclassifiedXBoundaryError(DeadlinePostExecutionError):
    """Raised when an unexpected boundary failure has no safe X outcome classification."""


@dataclass(frozen=True, slots=True)
class DeadlinePostExecutionResult:
    """Successful Post details or a duplicate-claim no-op."""

    context: EventPostingContext
    tweet: str
    x_post_id: str | None = None
    existing_status: PostingStatus | None = None

    @property
    def posted(self) -> bool:
        return self.x_post_id is not None


Clock = Callable[[], datetime]


class DeadlinePostExecutionCoordinator:
    """Join deterministic FPL output, durable state, and guarded X posting once."""

    def __init__(
        self,
        state_store: PostingStateStore,
        x_client: XPostCreator,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._state_store = state_store
        self._x_client = x_client
        self._clock = clock or _utc_now

    def execute(self, report: EventReport) -> DeadlinePostExecutionResult:
        context, tweet = _prepare_execution(report)
        claimed_at_utc = self._validated_now("Claim timestamp")

        try:
            decision = self._state_store.claim_event(
                context,
                claimed_at_utc=claimed_at_utc,
            )
        except Exception as exc:
            raise PostingStatePersistenceError(
                "Durable posting claim could not be confirmed; no X request was made"
            ) from exc

        if not decision.granted:
            return DeadlinePostExecutionResult(
                context=context,
                tweet=tweet,
                existing_status=decision.existing_status,
            )

        claim = decision.claim
        if claim is None or claim.event_id != context.event_id:
            raise PostingStatePersistenceError(
                "Durable posting claim response was inconsistent; no X request was made"
            )

        attempted_at_utc = self._validated_now("Posting attempt timestamp")
        try:
            self._state_store.mark_posting_attempt(
                claim,
                posting_attempted_at_utc=attempted_at_utc,
            )
        except Exception as exc:
            raise PostingStatePersistenceError(
                "Posting claim was acquired, but posting_in_progress could not be confirmed; "
                "no X request was made"
            ) from exc

        try:
            created = self._x_client.create_text_post(tweet)
        except XAmbiguousWriteError as exc:
            self._record_x_outcome(claim, PostingStatus.UNCERTAIN, exc)
            raise
        except XApiError as exc:
            self._record_x_outcome(claim, PostingStatus.FAILED, exc)
            raise
        except Exception as exc:
            raise UnclassifiedXBoundaryError(
                "Unexpected X boundary failure cannot be safely classified; the event remains "
                "closed in posting_in_progress and must not be retried automatically"
            ) from exc

        try:
            self._state_store.record_success(claim, x_post_id=created.post_id)
        except Exception as exc:
            raise XPostSuccessPersistenceError(created.post_id) from exc

        return DeadlinePostExecutionResult(
            context=context,
            tweet=tweet,
            x_post_id=created.post_id,
        )

    def _validated_now(self, label: str) -> datetime:
        value = self._clock()
        try:
            require_utc(value, label)
        except PostingStateValidationError as exc:
            raise DeadlinePostValidationError(str(exc)) from None
        return value

    def _record_x_outcome(
        self,
        claim: PostingClaim,
        status: PostingStatus,
        error: XApiError,
    ) -> None:
        detail = _safe_x_audit_detail(status, error)
        try:
            if status is PostingStatus.FAILED:
                self._state_store.record_failure(claim, error_detail=detail)
            else:
                self._state_store.record_uncertain(claim, error_detail=detail)
        except Exception as exc:
            raise XOutcomePersistenceError(status, type(error).__name__) from exc


def _prepare_execution(report: EventReport) -> tuple[EventPostingContext, str]:
    if not isinstance(report, EventReport):
        raise DeadlinePostValidationError("Execution requires a resolved FPL EventReport")
    try:
        event_code = render_event_code(report.event.event_id, report.classification.kind)
        tweet = render_v1_tweet(event_code)
        context = EventPostingContext(
            event_id=report.event.event_id,
            event_code=event_code,
            official_deadline_utc=report.event.deadline_utc,
        )
    except (AttributeError, ValueError, PostingStateValidationError):
        raise DeadlinePostValidationError(
            "Resolved FPL event data is invalid for deadline posting"
        ) from None

    if report.event_code != event_code or report.tweet != tweet:
        raise DeadlinePostValidationError(
            "Resolved FPL event code or tweet is inconsistent with deterministic rendering"
        )
    return context, tweet


def _safe_x_audit_detail(status: PostingStatus, error: XApiError) -> str:
    if status is PostingStatus.UNCERTAIN:
        return f"X write outcome is ambiguous ({type(error).__name__}); no automatic retry"
    if isinstance(error, XRequestRejectedError):
        return (
            f"X operation was definitely rejected with HTTP {error.status_code} "
            f"({type(error).__name__}); no automatic retry"
        )
    return f"X operation failed before a confirmed Post ({type(error).__name__})"


def _utc_now() -> datetime:
    return datetime.now(UTC)
