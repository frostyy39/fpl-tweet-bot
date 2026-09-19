"""Production composition for the separate, FPLBotTest-only Captain publisher."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from fpl_bot.api import FplApiClient
from fpl_bot.captain_firestore import FirestoreCaptainRepository
from fpl_bot.captain_http import GoogleOidcAuthorizer, default_google_token_verifier
from fpl_bot.captain_publisher import FPLBOTTEST_USER_ID, CaptainPublisher
from fpl_bot.captain_publisher_http import PublisherAuthConfig, create_publisher_app
from fpl_bot.captain_validation import CaptainCandidateValidator
from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, GoogleCloudXTokenStateStore
from fpl_bot.runtime_config import ProductionConfigurationError, required_runtime_value
from fpl_bot.x_api import XApiClient
from fpl_bot.x_config import TEST_ENVIRONMENT, XPostingConfig
from fpl_bot.x_oauth import OAuthClientCredentials
from fpl_bot.x_token_refresh import RefreshingXAccessTokenProvider, XOAuthRefreshClient


class UtcClock:
    def now(self):
        return datetime.now(UTC)


def _boolean(source, name):
    value = required_runtime_value(source, name).casefold()
    if value not in {"true", "false"}:
        raise ProductionConfigurationError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True, slots=True)
class CaptainPublisherConfig:
    project: str
    project_number: str
    captain_database: str
    oauth_database: str
    origin: str
    invoker_email: str
    token_secret_id: str
    oauth_credentials: OAuthClientCredentials
    posting_enabled: bool

    def __post_init__(self):
        if (
            self.project != "fpl-frosty-bot-v1"
            or self.captain_database != "captain-state"
            or self.oauth_database != "shared-x-oauth"
            or self.origin != "https://captain-publisher-524790767721.europe-west1.run.app"
            or self.invoker_email
            != f"captain-publisher-invoker@{self.project}.iam.gserviceaccount.com"
            or not self.project_number.isdecimal()
            or self.project_number.startswith("0")
            or not self.token_secret_id
            or type(self.posting_enabled) is not bool
        ):
            raise ProductionConfigurationError("invalid isolated Captain publisher configuration")

    @classmethod
    def environment(cls, environ=None):
        source = os.environ if environ is None else environ
        expected = required_runtime_value(source, "X_EXPECTED_USER_ID")
        if expected != FPLBOTTEST_USER_ID:
            raise ProductionConfigurationError("Captain publisher is hard-bound to FPLBotTest")
        environment = required_runtime_value(source, "X_ENVIRONMENT")
        if environment != TEST_ENVIRONMENT:
            raise ProductionConfigurationError("Captain publisher has no production X mode")
        return cls(
            required_runtime_value(source, "GCP_PROJECT_ID"),
            required_runtime_value(source, "GCP_PROJECT_NUMBER"),
            required_runtime_value(source, "FIRESTORE_DATABASE_ID"),
            required_runtime_value(source, "X_OAUTH_FIRESTORE_DATABASE_ID"),
            required_runtime_value(source, "CAPTAIN_PUBLISHER_ORIGIN"),
            required_runtime_value(source, "CAPTAIN_PUBLISHER_INVOKER_EMAIL"),
            required_runtime_value(source, "X_TOKEN_SECRET_ID"),
            OAuthClientCredentials(
                required_runtime_value(source, "X_OAUTH_CLIENT_ID"),
                required_runtime_value(source, "X_OAUTH_CLIENT_SECRET"),
            ),
            _boolean(source, "X_POSTING_ENABLED"),
        )


def compose(config, repository, source, clock, x_client, authorizer):
    publisher = CaptainPublisher(
        repository,
        CaptainCandidateValidator(repository, source, clock),
        x_client,
        x_client,
        clock,
        posting_enabled=config.posting_enabled,
    )
    return create_publisher_app(
        publisher,
        authorizer,
        PublisherAuthConfig(config.origin, config.invoker_email),
    )


def create_app():
    from google.cloud import firestore_v1, secretmanager

    config = CaptainPublisherConfig.environment()
    repository = FirestoreCaptainRepository(
        project=config.project, database=config.captain_database
    )
    oauth_firestore = firestore_v1.Client(project=config.project, database=config.oauth_database)
    token_store = GoogleCloudXTokenStateStore(
        CloudXTokenStateStoreConfig(
            project_id=config.project,
            project_number=config.project_number,
            secret_id=config.token_secret_id,
            expected_user_id=FPLBOTTEST_USER_ID,
        ),
        firestore_client=oauth_firestore,
        secret_manager_client=secretmanager.SecretManagerServiceClient(),
    )
    token_provider = RefreshingXAccessTokenProvider(
        token_store,
        XOAuthRefreshClient(),
        config.oauth_credentials,
        refresh_coordinator=token_store,
    )
    x_client = XApiClient(
        XPostingConfig(
            environment=TEST_ENVIRONMENT,
            posting_enabled=config.posting_enabled,
            expected_user_id=FPLBOTTEST_USER_ID,
        ),
        token_provider=token_provider,
    )
    return compose(
        config,
        repository,
        FplApiClient(),
        UtcClock(),
        x_client,
        GoogleOidcAuthorizer(default_google_token_verifier),
    )
