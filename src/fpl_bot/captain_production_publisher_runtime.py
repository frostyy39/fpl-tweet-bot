"""Production-account composition for the separate, disabled Captain publisher."""

import os
from dataclasses import dataclass

from fpl_bot.api import FplApiClient
from fpl_bot.captain_firestore import FirestoreCaptainRepository
from fpl_bot.captain_http import GoogleOidcAuthorizer, default_google_token_verifier
from fpl_bot.captain_publisher_runtime import UtcClock, _boolean, compose
from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, GoogleCloudXTokenStateStore
from fpl_bot.production_x_identity import PRODUCTION_X_USER_ID
from fpl_bot.runtime_config import ProductionConfigurationError, required_runtime_value
from fpl_bot.x_api import XApiClient
from fpl_bot.x_config import PRODUCTION_ENVIRONMENT, X_ID_PATTERN, XPostingConfig
from fpl_bot.x_oauth import OAuthClientCredentials
from fpl_bot.x_token_refresh import RefreshingXAccessTokenProvider, XOAuthRefreshClient

PRODUCTION_OAUTH_DATABASE = "production-shared-x-oauth"
PRODUCTION_TOKEN_SECRET = "production-x-oauth-token-state"


@dataclass(frozen=True, slots=True)
class ProductionCaptainPublisherConfig:
    project: str
    project_number: str
    captain_database: str
    oauth_database: str
    origin: str
    invoker_email: str
    token_secret_id: str
    destination_user_id: str
    oauth_credentials: OAuthClientCredentials
    posting_enabled: bool

    def __post_init__(self):
        if (
            self.project != "fpl-frosty-bot-v1"
            or self.captain_database != "captain-state"
            or self.oauth_database != PRODUCTION_OAUTH_DATABASE
            or self.origin
            != "https://captain-production-publisher-524790767721.europe-west1.run.app"
            or self.invoker_email
            != f"captain-prod-pub-invoker@{self.project}.iam.gserviceaccount.com"
            or self.token_secret_id != PRODUCTION_TOKEN_SECRET
            or not X_ID_PATTERN.fullmatch(self.destination_user_id)
            or not self.project_number.isdecimal()
            or self.project_number.startswith("0")
            or type(self.posting_enabled) is not bool
        ):
            raise ProductionConfigurationError(
                "invalid isolated production Captain publisher configuration"
            )

    @classmethod
    def environment(cls, environ=None, *, configured_user_id=PRODUCTION_X_USER_ID):
        source = os.environ if environ is None else environ
        if configured_user_id is None or not X_ID_PATTERN.fullmatch(configured_user_id):
            raise ProductionConfigurationError(
                "production X identity has not been reviewed and configured"
            )
        expected = required_runtime_value(source, "X_EXPECTED_USER_ID")
        if expected != configured_user_id:
            raise ProductionConfigurationError(
                "production Captain publisher identity differs from reviewed source identity"
            )
        if required_runtime_value(source, "X_ENVIRONMENT") != PRODUCTION_ENVIRONMENT:
            raise ProductionConfigurationError("production Captain publisher has no test mode")
        return cls(
            required_runtime_value(source, "GCP_PROJECT_ID"),
            required_runtime_value(source, "GCP_PROJECT_NUMBER"),
            required_runtime_value(source, "FIRESTORE_DATABASE_ID"),
            required_runtime_value(source, "X_OAUTH_FIRESTORE_DATABASE_ID"),
            required_runtime_value(source, "CAPTAIN_PUBLISHER_ORIGIN"),
            required_runtime_value(source, "CAPTAIN_PUBLISHER_INVOKER_EMAIL"),
            required_runtime_value(source, "X_TOKEN_SECRET_ID"),
            expected,
            OAuthClientCredentials(
                required_runtime_value(source, "X_OAUTH_CLIENT_ID"),
                required_runtime_value(source, "X_OAUTH_CLIENT_SECRET"),
            ),
            _boolean(source, "X_POSTING_ENABLED"),
        )


def create_app():
    from google.cloud import firestore_v1, secretmanager

    config = ProductionCaptainPublisherConfig.environment()
    repository = FirestoreCaptainRepository(
        project=config.project, database=config.captain_database
    )
    token_store = GoogleCloudXTokenStateStore(
        CloudXTokenStateStoreConfig(
            project_id=config.project,
            project_number=config.project_number,
            secret_id=config.token_secret_id,
            expected_user_id=config.destination_user_id,
        ),
        firestore_client=firestore_v1.Client(
            project=config.project, database=config.oauth_database
        ),
        secret_manager_client=secretmanager.SecretManagerServiceClient(),
    )
    provider = RefreshingXAccessTokenProvider(
        token_store,
        XOAuthRefreshClient(),
        config.oauth_credentials,
        refresh_coordinator=token_store,
    )
    x_client = XApiClient(
        XPostingConfig(
            environment=PRODUCTION_ENVIRONMENT,
            posting_enabled=config.posting_enabled,
            expected_user_id=config.destination_user_id,
        ),
        token_provider=provider,
    )
    return compose(
        config,
        repository,
        FplApiClient(),
        UtcClock(),
        x_client,
        GoogleOidcAuthorizer(default_google_token_verifier),
        destination_user_id=config.destination_user_id,
    )
