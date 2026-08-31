"""Typed failures for the X API integration boundary."""

from fpl_bot.errors import FplBotError


class XApiError(FplBotError):
    """Base class for expected X integration failures."""


class XConfigurationError(XApiError):
    """Raised when write-safety or credential configuration is incomplete."""


class XTransportError(XApiError):
    """Raised when a read-only X request fails at the network boundary."""


class XResponseValidationError(XApiError):
    """Raised when a read response does not match the required X schema."""


class XRequestRejectedError(XApiError):
    """Raised when X unequivocally rejects a request."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class XAuthenticationError(XRequestRejectedError):
    """Raised when X rejects the supplied user-context credentials."""


class XPermissionError(XRequestRejectedError):
    """Raised when the authenticated app/user lacks endpoint permission."""


class XRateLimitError(XRequestRejectedError):
    """Raised when X rejects a request because its rate limit was exceeded."""


class XApiResponseError(XApiError):
    """Raised for a non-write X API failure that is not a definite client rejection."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class XIdentityMismatchError(XApiError):
    """Raised when authenticated X identity differs from the configured account ID."""


class XAmbiguousWriteError(XApiError):
    """Raised when a Post might exist but no safe success result is available."""


class XOAuthError(XApiError):
    """Base class for safe local OAuth authorization failures."""


class XOAuthConfigurationError(XOAuthError):
    """Raised when local OAuth configuration is missing or unsafe."""


class XOAuthCallbackError(XOAuthError):
    """Raised when the loopback OAuth callback is invalid or denied."""


class XOAuthTokenExchangeError(XOAuthError):
    """Raised when an authorization code cannot be safely exchanged."""


class XOAuthHandoffError(XOAuthError):
    """Raised when tokens cannot be handed off outside the repository."""


class XTokenError(XApiError):
    """Base class for safe unattended user-token acquisition failures."""


class XTokenStateError(XTokenError):
    """Raised when persisted OAuth token state is absent or invalid."""


class XTokenRefreshError(XTokenError):
    """Raised when OAuth refresh cannot produce a validated credential."""


class XTokenStoreError(XTokenError):
    """Raised when authoritative token state cannot be read or updated safely."""


class XTokenConcurrencyError(XTokenError):
    """Raised when a refresh race cannot be reconciled to valid authoritative state."""
