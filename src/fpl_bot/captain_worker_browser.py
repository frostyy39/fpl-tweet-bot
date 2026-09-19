"""Projection-only bridge to the proven browser lifecycle and canonical table parser."""

from pathlib import Path
from time import monotonic

from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CleanupStatus,
    CompletenessStatus,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_review_browser import (
    PlaywrightReviewBrowserAcquirer,
    parse_review_projection_table,
)
from fpl_bot.captain_worker import (
    AcquiredDataset,
    AcquisitionDiagnosticError,
    AcquisitionFailureCode,
    AcquisitionFailureDiagnostic,
    AcquisitionStage,
)
from fpl_bot.errors import CaptainReviewBrowserError


class ProvenBrowserAcquisition:
    def __init__(self, profile_directory: Path) -> None:
        self.profile_directory = profile_directory

    def acquire(self, event_id: int) -> AcquiredDataset:
        started = monotonic()
        profile_exists = False
        acquirer = None
        stage = AcquisitionStage.PROFILE_VALIDATION
        observations = []
        try:
            profile_exists = self.profile_directory.is_dir()
            if not profile_exists:
                raise CaptainReviewBrowserError("dedicated_profile_required")
            acquirer = PlaywrightReviewBrowserAcquirer(
                self.profile_directory, session_observer=observations.append
            )
            snapshot = acquirer.acquire(event_id)  # Owns/closes Chrome and releases the profile.
            stage = AcquisitionStage.TABLE_PROCESSING
            if (
                acquirer.last_lifecycle.get("authentication") != "authenticated"
                or acquirer.last_lifecycle.get("ownership") != "released"
            ):
                raise CaptainReviewBrowserError("browser_profile_unclean")
            table = parse_review_projection_table(snapshot, event_id)
            return AcquiredDataset(
                tuple(
                    ProjectionRecord(
                        row.source_row_ordinal,
                        row.display_name,
                        row.team_short_code,
                        row.projected_points,
                    )
                    for row in table.rows
                ),
                RowCounts(
                    table.raw_body_row_count,
                    table.genuine_player_row_count,
                    table.auxiliary_row_count,
                    len(table.rows),
                ),
                CompletenessStatus.COMPLETE,
                AuthenticationStatus.AUTHENTICATED,
                CleanupStatus.RELEASED,
                tuple(observations),
            )
        except AcquisitionDiagnosticError:
            raise
        except CaptainReviewBrowserError as error:
            raise AcquisitionDiagnosticError(
                _diagnostic(
                    error.category,
                    stage,
                    acquirer,
                    profile_exists,
                    started,
                    "CaptainReviewBrowserError",
                )
            ) from None
        except PermissionError:
            raise AcquisitionDiagnosticError(
                _diagnostic(
                    "filesystem_permission_denied",
                    stage,
                    acquirer,
                    profile_exists,
                    started,
                    "PermissionError",
                )
            ) from None
        except FileNotFoundError:
            raise AcquisitionDiagnosticError(
                _diagnostic(
                    "filesystem_missing",
                    stage,
                    acquirer,
                    profile_exists,
                    started,
                    "FileNotFoundError",
                )
            ) from None
        except OSError:
            raise AcquisitionDiagnosticError(
                _diagnostic(
                    "filesystem_permission_denied",
                    stage,
                    acquirer,
                    profile_exists,
                    started,
                    "OSError",
                )
            ) from None
        except Exception:
            raise AcquisitionDiagnosticError(
                _diagnostic(
                    "unexpected_internal_failure",
                    stage,
                    acquirer,
                    profile_exists,
                    started,
                    "InternalError",
                )
            ) from None


def _diagnostic(
    category: str,
    fallback_stage: AcquisitionStage,
    acquirer,
    profile_exists: bool,
    started: float,
    exception_class: str,
) -> AcquisitionFailureDiagnostic:
    lifecycle = getattr(acquirer, "last_lifecycle", {}) if acquirer is not None else {}
    stage_value = getattr(acquirer, "last_stage", fallback_stage.value)
    try:
        stage = AcquisitionStage(stage_value)
    except (TypeError, ValueError):
        stage = fallback_stage
    executable = lifecycle.get("executable_resolved") == "true"
    process = lifecycle.get("browser_process_created") == "true"
    navigation = lifecycle.get("navigation_began") == "true"
    code, stage = _classify(category, stage, process, navigation)
    duration_ms = min(1_800_000, max(0, int((monotonic() - started) * 1000)))
    return AcquisitionFailureDiagnostic(
        1,
        code,
        stage,
        executable,
        profile_exists,
        process,
        navigation,
        duration_ms,
        exception_class,
    )


def _classify(
    category: str,
    stage: AcquisitionStage,
    browser_process_created: bool,
    navigation_began: bool,
) -> tuple[AcquisitionFailureCode, AcquisitionStage]:
    if category in {"stable_chrome_unavailable", "stable_chrome_ambiguous"}:
        return AcquisitionFailureCode.BROWSER_EXECUTABLE_UNAVAILABLE, (
            AcquisitionStage.EXECUTABLE_RESOLUTION
        )
    if category in {"browser_dependency_unavailable", "browser_keyring_unavailable"}:
        return AcquisitionFailureCode.BROWSER_RUNTIME_INITIALIZATION_FAILED, (
            AcquisitionStage.RUNTIME_INITIALIZATION
        )
    if category == "browser_runtime_initialization_failed":
        return AcquisitionFailureCode.BROWSER_RUNTIME_INITIALIZATION_FAILED, (
            AcquisitionStage.RUNTIME_INITIALIZATION
        )
    if category in {"dedicated_profile_required", "filesystem_missing"}:
        return AcquisitionFailureCode.PROFILE_MISSING_OR_INACCESSIBLE, (
            AcquisitionStage.PROFILE_VALIDATION
        )
    if category == "browser_profile_in_use":
        return AcquisitionFailureCode.PROFILE_IN_USE, AcquisitionStage.PROFILE_OWNERSHIP
    if category == "filesystem_permission_denied":
        return AcquisitionFailureCode.FILESYSTEM_PERMISSION_DENIED, stage
    if category == "browser_launch_failed":
        if navigation_began or stage == AcquisitionStage.NAVIGATION:
            return AcquisitionFailureCode.NAVIGATION_FAILED, AcquisitionStage.NAVIGATION
        if browser_process_created or stage == AcquisitionStage.BROWSER_RUNNING:
            return AcquisitionFailureCode.BROWSER_EXITED_IMMEDIATELY, (
                AcquisitionStage.BROWSER_RUNNING
            )
        if stage == AcquisitionStage.RUNTIME_INITIALIZATION:
            return AcquisitionFailureCode.BROWSER_RUNTIME_INITIALIZATION_FAILED, stage
        return AcquisitionFailureCode.BROWSER_PROCESS_LAUNCH_FAILED, AcquisitionStage.BROWSER_LAUNCH
    if category == "browser_exited_immediately":
        return AcquisitionFailureCode.BROWSER_EXITED_IMMEDIATELY, (AcquisitionStage.BROWSER_RUNNING)
    if category == "browser_navigation_failed":
        return AcquisitionFailureCode.NAVIGATION_FAILED, AcquisitionStage.NAVIGATION
    if category == "browser_navigation_timeout":
        return AcquisitionFailureCode.ACQUISITION_TIMEOUT, AcquisitionStage.NAVIGATION
    if category == "browser_profile_unclean":
        return AcquisitionFailureCode.BROWSER_EXITED_IMMEDIATELY, AcquisitionStage.CLEANUP
    if category == "table_load_timeout":
        return AcquisitionFailureCode.ACQUISITION_TIMEOUT, AcquisitionStage.REVIEW_SESSION
    if category == "reauthentication_required":
        return AcquisitionFailureCode.REVIEW_AUTHENTICATION_REQUIRED, (
            AcquisitionStage.REVIEW_SESSION
        )
    if category in {
        "identity_resolution_failed",
        "invalid_projection_table",
        "premium_unavailable",
        "projection_table_unavailable",
        "session_observation_failed",
        "upcoming_event_column_missing",
    }:
        return AcquisitionFailureCode.REVIEW_APPLICATION_FAILURE, (
            AcquisitionStage.TABLE_PROCESSING
            if category in {"identity_resolution_failed", "invalid_projection_table"}
            else AcquisitionStage.REVIEW_SESSION
        )
    return AcquisitionFailureCode.UNEXPECTED_INTERNAL_FAILURE, AcquisitionStage.INTERNAL
