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
