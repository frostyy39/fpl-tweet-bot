"""Fail-closed configuration for X user-context requests."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from fpl_bot.x_errors import XConfigurationError

X_ENVIRONMENT_VARIABLE = "X_ENVIRONMENT"
X_POSTING_ENABLED_VARIABLE = "X_POSTING_ENABLED"
X_EXPECTED_USER_ID_VARIABLE = "X_EXPECTED_USER_ID"
X_USER_ACCESS_TOKEN_VARIABLE = "X_USER_ACCESS_TOKEN"

TEST_ENVIRONMENT = "test"
X_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True, slots=True)
class XPostingConfig:
    """Configuration that remains non-posting unless every guard is satisfied."""

    environment: str | None = None
    posting_enabled: bool = False
    expected_user_id: str | None = None
    user_access_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "XPostingConfig":
        source = os.environ if environ is None else environ
        return cls(
            environment=_optional_value(source.get(X_ENVIRONMENT_VARIABLE)),
            posting_enabled=_parse_posting_enabled(source.get(X_POSTING_ENABLED_VARIABLE)),
            expected_user_id=_optional_value(source.get(X_EXPECTED_USER_ID_VARIABLE)),
            user_access_token=_optional_value(source.get(X_USER_ACCESS_TOKEN_VARIABLE)),
        )

    def require_user_access_token(self) -> str:
        if self.user_access_token is None:
            raise XConfigurationError(
                f"{X_USER_ACCESS_TOKEN_VARIABLE} is required for X user-context requests"
            )
        return self.user_access_token

    def require_posting_guards(self) -> tuple[str, str]:
        expected_user_id = self.require_posting_identity_guard()
        token = self.require_user_access_token()
        return token, expected_user_id

    def require_posting_identity_guard(self) -> str:
        """Validate non-credential write guards for provider-backed clients."""
        if not self.posting_enabled:
            raise XConfigurationError(
                f"X posting is disabled; set {X_POSTING_ENABLED_VARIABLE}=true explicitly"
            )
        return self.require_configured_identity()

    def require_configured_identity(self) -> str:
        """Validate the configured test-account identity without enabling writes."""
        if self.environment != TEST_ENVIRONMENT:
            raise XConfigurationError(
                f"{X_ENVIRONMENT_VARIABLE} must be {TEST_ENVIRONMENT!r}; "
                "Milestone 2A has no production mode"
            )
        if self.expected_user_id is None:
            raise XConfigurationError(
                f"{X_EXPECTED_USER_ID_VARIABLE} is required before X posting can be enabled"
            )
        if not X_ID_PATTERN.fullmatch(self.expected_user_id):
            raise XConfigurationError(
                f"{X_EXPECTED_USER_ID_VARIABLE} must be a positive numeric X user ID"
            )
        return self.expected_user_id


def _parse_posting_enabled(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise XConfigurationError(f"{X_POSTING_ENABLED_VARIABLE} must be either 'true' or 'false'")


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
