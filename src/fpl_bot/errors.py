"""Application-specific exceptions."""


class FplBotError(Exception):
    """Base class for expected FPL bot failures."""


class FplApiError(FplBotError):
    """Raised when authoritative FPL data cannot be retrieved."""


class DataValidationError(FplBotError):
    """Raised when FPL data is malformed or internally inconsistent."""


class NoSuitableEventError(FplBotError):
    """Raised when FPL exposes no current or future event to process."""
