"""Allowlisted, non-secret diagnostics for read-only FPL planning failures."""

from dataclasses import dataclass
from enum import StrEnum

from fpl_bot.errors import (
    DataValidationError,
    DeadlineEventSelectionError,
    DeadlineTimezoneError,
    FplApiError,
    FplApiHttpError,
    FplApiInvalidJsonError,
    FplApiPayloadValidationError,
    FplApiTimeoutError,
    FplApiTlsError,
    FplApiTransportError,
    FplBootstrapValidationError,
    MultipleSameDayEventsError,
    NoSuitableEventError,
)


class FplDiagnosticStage(StrEnum):
    FPL_BOOTSTRAP = "fpl_bootstrap"
    EVENT_SELECTION = "event_selection"
    DEADLINE_PLANNING = "deadline_planning"
    CHECKER = "checker"


class FplDiagnosticCategory(StrEnum):
    TRANSPORT_ERROR = "transport_error"
    TLS_ERROR = "tls_error"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_JSON = "invalid_json"
    PAYLOAD_VALIDATION = "payload_validation"
    NO_EVENT = "no_event"
    MULTIPLE_SAME_DAY_EVENTS = "multiple_same_day_events"
    EVENT_SELECTION_FAILURE = "event_selection_failure"
    TIMEZONE_ERROR = "timezone_error"
    UNEXPECTED_INTERNAL = "unexpected_internal"


@dataclass(frozen=True, slots=True)
class FplFailureDiagnostic:
    """Only trusted enum values and an optional numeric status may be rendered."""

    stage: FplDiagnosticStage
    category: FplDiagnosticCategory
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, FplDiagnosticStage):
            raise TypeError("stage must be an allowlisted FPL diagnostic stage")
        if not isinstance(self.category, FplDiagnosticCategory):
            raise TypeError("category must be an allowlisted FPL diagnostic category")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be a valid numeric HTTP status")

    def fields(self) -> dict[str, str | int]:
        payload: dict[str, str | int] = {
            "stage": self.stage.value,
            "category": self.category.value,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload


def diagnose_fpl_failure(error: Exception) -> FplFailureDiagnostic:
    """Classify by trusted exception type without rendering exception text."""

    if isinstance(error, FplApiTimeoutError):
        return _diagnostic(FplDiagnosticStage.FPL_BOOTSTRAP, FplDiagnosticCategory.TIMEOUT)
    if isinstance(error, FplApiTlsError):
        return _diagnostic(FplDiagnosticStage.FPL_BOOTSTRAP, FplDiagnosticCategory.TLS_ERROR)
    if isinstance(error, FplApiTransportError):
        return _diagnostic(
            FplDiagnosticStage.FPL_BOOTSTRAP,
            FplDiagnosticCategory.TRANSPORT_ERROR,
        )
    if isinstance(error, FplApiHttpError):
        return FplFailureDiagnostic(
            FplDiagnosticStage.FPL_BOOTSTRAP,
            FplDiagnosticCategory.HTTP_ERROR,
            error.status_code,
        )
    if isinstance(error, FplApiInvalidJsonError):
        return _diagnostic(
            FplDiagnosticStage.FPL_BOOTSTRAP,
            FplDiagnosticCategory.INVALID_JSON,
        )
    if isinstance(error, (FplApiPayloadValidationError, FplBootstrapValidationError)):
        return _diagnostic(
            FplDiagnosticStage.FPL_BOOTSTRAP,
            FplDiagnosticCategory.PAYLOAD_VALIDATION,
        )
    if isinstance(error, MultipleSameDayEventsError):
        return _diagnostic(
            FplDiagnosticStage.EVENT_SELECTION,
            FplDiagnosticCategory.MULTIPLE_SAME_DAY_EVENTS,
        )
    if isinstance(error, NoSuitableEventError):
        return _diagnostic(
            FplDiagnosticStage.EVENT_SELECTION,
            FplDiagnosticCategory.NO_EVENT,
        )
    if isinstance(error, DeadlineEventSelectionError):
        return _diagnostic(
            FplDiagnosticStage.EVENT_SELECTION,
            FplDiagnosticCategory.EVENT_SELECTION_FAILURE,
        )
    if isinstance(error, DeadlineTimezoneError):
        return _diagnostic(
            FplDiagnosticStage.DEADLINE_PLANNING,
            FplDiagnosticCategory.TIMEZONE_ERROR,
        )
    if isinstance(error, DataValidationError):
        return _diagnostic(
            FplDiagnosticStage.DEADLINE_PLANNING,
            FplDiagnosticCategory.PAYLOAD_VALIDATION,
        )
    if isinstance(error, FplApiError):
        return _diagnostic(
            FplDiagnosticStage.FPL_BOOTSTRAP,
            FplDiagnosticCategory.TRANSPORT_ERROR,
        )
    return _diagnostic(
        FplDiagnosticStage.CHECKER,
        FplDiagnosticCategory.UNEXPECTED_INTERNAL,
    )


def _diagnostic(
    stage: FplDiagnosticStage,
    category: FplDiagnosticCategory,
) -> FplFailureDiagnostic:
    return FplFailureDiagnostic(stage, category)
