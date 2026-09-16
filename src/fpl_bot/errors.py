"""Application-specific exceptions."""


class FplBotError(Exception):
    """Base class for expected FPL bot failures."""


class FplApiError(FplBotError):
    """Raised when authoritative FPL data cannot be retrieved."""


class FplApiTransportError(FplApiError):
    """Raised when the public FPL endpoint cannot be reached."""


class FplApiTlsError(FplApiTransportError):
    """Raised when TLS validation fails while reaching FPL."""


class FplApiTimeoutError(FplApiTransportError):
    """Raised when an FPL request exceeds its explicit timeout."""


class FplApiHttpError(FplApiError):
    """Raised for a non-success response without retaining its body."""

    def __init__(self, status_code: int) -> None:
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be a valid numeric HTTP status")
        self.status_code = status_code
        super().__init__(f"FPL HTTP request failed with status {status_code}")


class FplApiInvalidJsonError(FplApiError):
    """Raised when FPL returns a response that is not valid JSON."""


class FplApiPayloadValidationError(FplApiError):
    """Raised when the top-level FPL HTTP payload has the wrong shape."""


class DataValidationError(FplBotError):
    """Raised when FPL data is malformed or internally inconsistent."""


class FplBootstrapValidationError(DataValidationError):
    """Raised when bootstrap-static data cannot be parsed safely."""


class DeadlineEventSelectionError(DataValidationError):
    """Raised when authoritative events contradict safe selection rules."""


class MultipleSameDayEventsError(DeadlineEventSelectionError):
    """Raised when multiple deadlines occupy the current London day."""


class DeadlineTimezoneError(DataValidationError, ValueError):
    """Raised when UTC/London deadline comparison cannot be performed safely."""


class NoSuitableEventError(FplBotError):
    """Raised when FPL exposes no current or future event to process."""


class CaptainError(FplBotError):
    """Base class for deterministic Captain feature failures."""


class CaptainProjectionError(CaptainError):
    """Raised when projection data is missing, malformed, or insufficient."""


class CaptainReviewBrowserError(CaptainProjectionError):
    """Safe typed failure from the local FPL Review browser boundary."""

    _ALLOWED_CATEGORIES = frozenset(
        {
            "browser_dependency_unavailable",
            "session_observation_failed",
            "browser_platform_unsupported",
            "browser_keyring_unavailable",
            "browser_profile_in_use",
            "browser_profile_unclean",
            "stable_chrome_ambiguous",
            "browser_launch_failed",
            "dedicated_profile_required",
            "identity_resolution_failed",
            "invalid_projection_table",
            "premium_unavailable",
            "projection_table_unavailable",
            "reauthentication_required",
            "stable_chrome_unavailable",
            "table_load_timeout",
            "upcoming_event_column_missing",
        }
    )

    def __init__(self, category: str) -> None:
        if category not in self._ALLOWED_CATEGORIES:
            raise ValueError("Unsupported FPL Review browser failure category")
        self.category = category
        super().__init__(f"FPL Review browser acquisition failed: {category}")


class CaptainPlayerResolutionError(CaptainError):
    """Raised when a projection cannot resolve to authoritative FPL player data."""


class CaptainFixtureError(CaptainError):
    """Raised when an enriched Captain selection has no valid event fixture."""


class CaptainRenderingError(CaptainError):
    """Raised when the canonical Captain post cannot be rendered safely."""
