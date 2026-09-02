"""Shared validated runtime configuration for cloud-hosted X token consumers."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig
from fpl_bot.errors import FplBotError
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import XTokenStateError
from fpl_bot.x_oauth import (
    X_OAUTH_CLIENT_ID_VARIABLE,
    X_OAUTH_CLIENT_SECRET_VARIABLE,
    OAuthClientCredentials,
)

GCP_PROJECT_ID_VARIABLE = "GCP_PROJECT_ID"
GCP_PROJECT_NUMBER_VARIABLE = "GCP_PROJECT_NUMBER"
FIRESTORE_DATABASE_ID_VARIABLE = "FIRESTORE_DATABASE_ID"
X_TOKEN_SECRET_ID_VARIABLE = "X_TOKEN_SECRET_ID"

DEFAULT_FIRESTORE_DATABASE_ID = "(default)"
FIRESTORE_DATABASE_ID_PATTERN = re.compile(r"(?:\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])\Z")


class ProductionConfigurationError(FplBotError):
    """Raised before adapter construction when runtime configuration is incomplete."""


@dataclass(frozen=True, slots=True)
class XCloudRuntimeConfig:
    """Validated cloud token-store, identity, and OAuth client configuration."""

    gcp_project_id: str
    gcp_project_number: str
    firestore_database_id: str
    x_token_secret_id: str
    x_posting: XPostingConfig = field(repr=False)
    x_oauth_credentials: OAuthClientCredentials = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "XCloudRuntimeConfig":
        source = os.environ if environ is None else environ
        project_id = required_runtime_value(source, GCP_PROJECT_ID_VARIABLE)
        project_number = required_runtime_value(source, GCP_PROJECT_NUMBER_VARIABLE)
        if (
            not project_number.isascii()
            or not project_number.isdigit()
            or project_number.startswith("0")
        ):
            raise ProductionConfigurationError(
                f"{GCP_PROJECT_NUMBER_VARIABLE} must be a positive numeric project number"
            )
        database_id = (
            optional_runtime_value(
                source.get(FIRESTORE_DATABASE_ID_VARIABLE),
                FIRESTORE_DATABASE_ID_VARIABLE,
            )
            or DEFAULT_FIRESTORE_DATABASE_ID
        )
        if not FIRESTORE_DATABASE_ID_PATTERN.fullmatch(database_id):
            raise ProductionConfigurationError(
                f"{FIRESTORE_DATABASE_ID_VARIABLE} is not a valid Firestore database ID"
            )
        x_posting = XPostingConfig.from_environment(source)
        expected_user_id = x_posting.require_configured_identity()
        secret_id = required_runtime_value(source, X_TOKEN_SECRET_ID_VARIABLE)
        try:
            CloudXTokenStateStoreConfig(
                project_id=project_id,
                project_number=project_number,
                secret_id=secret_id,
                expected_user_id=expected_user_id,
            )
        except XTokenStateError:
            raise ProductionConfigurationError(
                f"{X_TOKEN_SECRET_ID_VARIABLE} is not a valid Secret Manager secret ID"
            ) from None
        credentials = OAuthClientCredentials(
            client_id=required_runtime_value(source, X_OAUTH_CLIENT_ID_VARIABLE),
            client_secret=required_runtime_value(source, X_OAUTH_CLIENT_SECRET_VARIABLE),
        )
        return cls(
            gcp_project_id=project_id,
            gcp_project_number=project_number,
            firestore_database_id=database_id,
            x_token_secret_id=secret_id,
            x_posting=x_posting,
            x_oauth_credentials=credentials,
        )


def required_runtime_value(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if value is None or not isinstance(value, str) or not value or value != value.strip():
        raise ProductionConfigurationError(f"{name} is required and must be non-empty")
    if not value.isprintable():
        raise ProductionConfigurationError(f"{name} must contain only printable characters")
    return value


def optional_runtime_value(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip() or not value.isprintable():
        raise ProductionConfigurationError(
            f"{name} must be printable and contain no outer whitespace"
        )
    return value or None
