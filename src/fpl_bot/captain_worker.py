"""Bounded, no-post Captain worker. No concrete transport or account credentials."""

import getpass
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from fpl_bot.captain_handoff import (
    SCHEMA_VERSION,
    AuthenticationStatus,
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_session_health import SessionObservation, summarize_session
from fpl_bot.captain_state import SessionHealthEvidence, identity
from fpl_bot.errors import CaptainReviewBrowserError


@dataclass(frozen=True, slots=True)
class WorkerAssignment:
    schema_version: int
    assignment: CaptainAssignment
    attempt_id: UUID
    next_target: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported worker assignment schema")
        if not isinstance(self.assignment, CaptainAssignment):
            raise ValueError("invalid assignment")
        identity(self.attempt_id)
        if self.next_target is not None:
            require_utc(self.next_target)


@dataclass(frozen=True, slots=True)
class ReleaseGrant:
    work: WorkerAssignment
    run_id: UUID
    issued_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.work, WorkerAssignment):
            raise ValueError("invalid release assignment")
        identity(self.run_id)
        require_utc(self.issued_at)


class ReceiptStatus(StrEnum):
    ACCEPTED = "accepted"
    REPLAY = "replay"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class HandoffReceipt:
    generation_id: UUID
    attempt_id: UUID
    payload_digest: str
    status: ReceiptStatus


class WorkerStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NO_ASSIGNMENT = "no_assignment"
    INVALID_ASSIGNMENT = "invalid_assignment"
    IDENTITY_MISMATCH = "identity_mismatch"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    WAIT_EXHAUSTED = "wait_exhausted"
    RELEASE_REJECTED = "release_rejected"
    CONTROLLER_UNAVAILABLE = "controller_unavailable"
    AUTHENTICATION_REQUIRED = "review_authentication_required"
    ACQUISITION_FAILED = "acquisition_failed"
    HANDOFF_REJECTED = "handoff_rejected"
    HANDOFF_UNCERTAIN = "handoff_uncertain"


class AcquisitionStage(StrEnum):
    WORKER_CONFIGURATION = "worker_configuration"
    PROFILE_VALIDATION = "profile_validation"
    EXECUTABLE_RESOLUTION = "executable_resolution"
    RUNTIME_INITIALIZATION = "runtime_initialization"
    PROFILE_OWNERSHIP = "profile_ownership"
    BROWSER_LAUNCH = "browser_launch"
    BROWSER_RUNNING = "browser_running"
    NAVIGATION = "navigation"
    REVIEW_SESSION = "review_session"
    TABLE_PROCESSING = "table_processing"
    CLEANUP = "cleanup"
    INTERNAL = "internal"


class AcquisitionFailureCode(StrEnum):
    WORKER_CONFIGURATION_INVALID = "worker_configuration_invalid"
    BROWSER_EXECUTABLE_UNAVAILABLE = "browser_executable_unavailable"
    BROWSER_RUNTIME_INITIALIZATION_FAILED = "browser_runtime_initialization_failed"
    BROWSER_PROCESS_LAUNCH_FAILED = "browser_process_launch_failed"
    PROFILE_MISSING_OR_INACCESSIBLE = "profile_missing_or_inaccessible"
    PROFILE_IN_USE = "profile_in_use"
    FILESYSTEM_PERMISSION_DENIED = "filesystem_permission_denied"
    BROWSER_EXITED_IMMEDIATELY = "browser_exited_immediately"
    NAVIGATION_FAILED = "navigation_failed"
    REVIEW_AUTHENTICATION_REQUIRED = "review_authentication_required"
    REVIEW_APPLICATION_FAILURE = "review_application_failure"
    ACQUISITION_TIMEOUT = "acquisition_timeout"
    UNEXPECTED_INTERNAL_FAILURE = "unexpected_internal_failure"


_SAFE_EXCEPTION_CLASSES = frozenset(
    {
        "CaptainReviewBrowserError",
        "FileNotFoundError",
        "PermissionError",
        "OSError",
        "InternalError",
    }
)


@dataclass(frozen=True, slots=True)
class AcquisitionFailureDiagnostic:
    """Allowlisted, non-secret evidence from the local browser boundary."""

    schema_version: int
    code: AcquisitionFailureCode
    stage: AcquisitionStage
    browser_executable_resolved: bool
    profile_directory_exists: bool
    browser_process_created: bool
    navigation_began: bool
    duration_ms: int
    exception_class: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported acquisition diagnostic schema")
        if not isinstance(self.code, AcquisitionFailureCode) or not isinstance(
            self.stage, AcquisitionStage
        ):
            raise ValueError("invalid acquisition diagnostic classification")
        if any(
            type(value) is not bool
            for value in (
                self.browser_executable_resolved,
                self.profile_directory_exists,
                self.browser_process_created,
                self.navigation_began,
            )
        ):
            raise ValueError("invalid acquisition diagnostic evidence")
        if type(self.duration_ms) is not int or not 0 <= self.duration_ms <= 1_800_000:
            raise ValueError("invalid acquisition diagnostic duration")
        if self.exception_class not in _SAFE_EXCEPTION_CLASSES:
            raise ValueError("unsafe acquisition diagnostic exception class")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "stage": self.stage.value,
            "browser_executable_resolved": self.browser_executable_resolved,
            "profile_directory_exists": self.profile_directory_exists,
            "browser_process_created": self.browser_process_created,
            "navigation_began": self.navigation_began,
            "duration_ms": self.duration_ms,
            "exception_class": self.exception_class,
        }


class AcquisitionDiagnosticError(Exception):
    """Carries only an already-sanitized browser failure diagnostic."""

    def __init__(self, diagnostic: AcquisitionFailureDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.code.value)


class WorkerClock(Protocol):
    def now(self) -> datetime: ...


class ControllerClient(Protocol):
    """Trusted, authenticated transport port. Each call must have a bounded timeout.

    Release must atomically grant the intended attempt to ONE run_id, using durable
    controller claim state. A replacement process must NOT receive another grant for
    an active/consumed attempt. Lost grant responses fail closed; no automatic takeover.
    None means pending; False means revoked/stale. No downloaded credentials here.
    """

    def obtain_assignment(self, run_id: UUID) -> WorkerAssignment | None: ...
    def await_release(
        self,
        work: WorkerAssignment,
        run_id: UUID,
        *,
        timeout_seconds: float,
        cancelled: Callable[[], bool],
    ) -> ReleaseGrant | None | bool: ...
    def submit_handoff(
        self, handoff: ProjectionHandoff, digest: str, health: SessionHealthEvidence | None
    ) -> HandoffReceipt: ...
    def report_failure(
        self, work: WorkerAssignment | None, run_id: UUID, status: WorkerStatus
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AcquiredDataset:
    records: tuple[ProjectionRecord, ...]
    counts: RowCounts
    completeness: CompletenessStatus
    authentication: AuthenticationStatus
    cleanup: CleanupStatus
    observations: tuple[SessionObservation, ...] = ()


class ProjectionAcquisition(Protocol):
    def acquire(self, event_id: int) -> AcquiredDataset:
        """Return only after browser closure and profile release, including on failure."""
        ...


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: WorkerStatus
    handoff: ProjectionHandoff | None = None
    health: SessionHealthEvidence | None = None
    acquisition_failure: AcquisitionFailureDiagnostic | None = None

    @property
    def exit_code(self) -> int:
        return (
            0
            if self.status
            in {WorkerStatus.SUCCEEDED, WorkerStatus.NO_ASSIGNMENT, WorkerStatus.CANCELLED}
            else 1
        )


class CaptainWorker:
    def __init__(
        self,
        client: ControllerClient,
        acquisition: ProjectionAcquisition,
        clock: WorkerClock,
        *,
        expected_user: str,
        current_user: Callable[[], str] = getpass.getuser,
        cancelled: Callable[[], bool] = lambda: False,
        new_run_id: Callable[[], UUID] = uuid4,
        max_release_checks: int = 120,
    ) -> None:
        if (
            not expected_user
            or type(max_release_checks) is not int
            or not 1 <= max_release_checks <= 120
        ):
            raise ValueError("invalid worker configuration")
        self.client, self.acquisition, self.clock = client, acquisition, clock
        self.expected_user, self.current_user = expected_user, current_user
        self.cancelled, self.new_run_id = cancelled, new_run_id
        self.max_release_checks = max_release_checks
        self._used_runs: set[UUID] = set()

    def _now(self) -> datetime:
        now = self.clock.now()
        require_utc(now)
        return now

    def run(self) -> WorkerResult:
        run_id = self.new_run_id()
        identity(run_id)
        if run_id in self._used_runs:
            return WorkerResult(WorkerStatus.RELEASE_REJECTED)
        self._used_runs.add(run_id)
        work = None

        def fail(
            status: WorkerStatus,
            handoff=None,
            health=None,
            acquisition_failure: AcquisitionFailureDiagnostic | None = None,
        ) -> WorkerResult:
            with suppress(Exception):  # Never log arbitrary transport/credential exceptions.
                self.client.report_failure(work, run_id, status)
            return WorkerResult(status, handoff, health, acquisition_failure)

        if self.current_user().casefold() != self.expected_user.casefold():
            return fail(WorkerStatus.IDENTITY_MISMATCH)
        if self.cancelled():
            return fail(WorkerStatus.CANCELLED)
        try:
            candidate = self.client.obtain_assignment(run_id)
        except Exception:
            return fail(WorkerStatus.CONTROLLER_UNAVAILABLE)
        if candidate is None:
            return WorkerResult(WorkerStatus.NO_ASSIGNMENT)
        if not isinstance(candidate, WorkerAssignment):
            return fail(WorkerStatus.INVALID_ASSIGNMENT)
        work = candidate
        timing = work.assignment.timing
        try:
            for _ in range(self.max_release_checks):
                if self.cancelled():
                    return fail(WorkerStatus.CANCELLED)
                now = self._now()
                if now > timing.expiry_utc:
                    return fail(WorkerStatus.EXPIRED)
                reply = self.client.await_release(
                    work,
                    run_id,
                    timeout_seconds=min(10.0, (timing.expiry_utc - now).total_seconds()),
                    cancelled=self.cancelled,
                )
                if reply is None:
                    continue
                if (
                    not isinstance(reply, ReleaseGrant)
                    or reply.work != work
                    or reply.run_id != run_id
                ):
                    return fail(WorkerStatus.RELEASE_REJECTED)
                now = self._now()
                if (
                    not timing.permits_new_attempt(now)
                    or not timing.permits_new_attempt(reply.issued_at)
                    or reply.issued_at > now
                ):
                    return fail(WorkerStatus.RELEASE_REJECTED)
                break
            else:
                return fail(WorkerStatus.WAIT_EXHAUSTED)
        except Exception:
            return fail(WorkerStatus.CONTROLLER_UNAVAILABLE)
        if self.cancelled():
            return fail(WorkerStatus.CANCELLED)
        try:
            started = self._now()
            if not timing.permits_posting_acquisition(started):
                return fail(WorkerStatus.EXPIRED)
            data = self.acquisition.acquire(work.assignment.event_id)
            ended = self._now()
            if not isinstance(data, AcquiredDataset):
                return fail(WorkerStatus.ACQUISITION_FAILED)
            if data.authentication == AuthenticationStatus.REQUIRED:
                return fail(WorkerStatus.AUTHENTICATION_REQUIRED)
            handoff = ProjectionHandoff(
                work.schema_version,
                work.assignment,
                work.attempt_id,
                started,
                ended,
                data.records,
                data.counts,
                data.completeness,
                data.authentication,
                data.cleanup,
            )
            handoff.require_eligible(work.assignment, ended)
            health = summarize_session(data.observations, work.next_target)
            if health is not None and health.authentication != AuthenticationStatus.AUTHENTICATED:
                return fail(WorkerStatus.ACQUISITION_FAILED)
            if any(not started <= o.observed_at <= ended for o in data.observations):
                return fail(WorkerStatus.ACQUISITION_FAILED)
        except AcquisitionDiagnosticError as error:
            return fail(
                WorkerStatus.AUTHENTICATION_REQUIRED
                if error.diagnostic.code == AcquisitionFailureCode.REVIEW_AUTHENTICATION_REQUIRED
                else WorkerStatus.ACQUISITION_FAILED,
                acquisition_failure=error.diagnostic,
            )
        except CaptainReviewBrowserError as error:
            return fail(
                WorkerStatus.AUTHENTICATION_REQUIRED
                if error.category == "reauthentication_required"
                else WorkerStatus.ACQUISITION_FAILED
            )
        except Exception:
            return fail(WorkerStatus.ACQUISITION_FAILED)
        if self.cancelled():
            return fail(WorkerStatus.CANCELLED, handoff, health)
        try:
            handoff.require_eligible(work.assignment, self._now())
        except ValueError:
            return fail(WorkerStatus.EXPIRED, handoff, health)
        try:
            receipt = self.client.submit_handoff(handoff, handoff.payload_digest, health)
        except Exception as error:
            # The server may have accepted it. No acquisition or changed-payload retry.
            if getattr(error, "category", "") in {
                "handoff_rejected",
                "controller_state_conflict",
            }:
                return fail(WorkerStatus.HANDOFF_REJECTED, handoff, health)
            return fail(WorkerStatus.HANDOFF_UNCERTAIN, handoff, health)
        if (
            not isinstance(receipt, HandoffReceipt)
            or receipt.generation_id != work.assignment.generation_id
            or receipt.attempt_id != work.attempt_id
            or receipt.payload_digest != handoff.payload_digest
            or not isinstance(receipt.status, ReceiptStatus)
            or receipt.status not in {ReceiptStatus.ACCEPTED, ReceiptStatus.REPLAY}
        ):
            return fail(WorkerStatus.HANDOFF_REJECTED, handoff, health)
        return WorkerResult(WorkerStatus.SUCCEEDED, handoff, health)
