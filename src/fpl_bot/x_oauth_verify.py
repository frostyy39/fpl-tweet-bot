"""One-shot, structurally read-only OAuth refresh and X identity verification."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from fpl_bot.cloud_token_store import (
    CloudXTokenStateStoreConfig,
    GoogleCloudXTokenStateStore,
    SecretManagerClient,
)
from fpl_bot.firestore_state import FirestoreClient
from fpl_bot.runtime_config import ProductionConfigurationError, XCloudRuntimeConfig
from fpl_bot.x_api import XHttpTransport, XIdentityClient, XIdentityReader
from fpl_bot.x_config import X_POSTING_ENABLED_VARIABLE
from fpl_bot.x_errors import XIdentityMismatchError
from fpl_bot.x_token_refresh import (
    RefreshingXAccessTokenProvider,
    XOAuthRefreshClient,
    XTokenStateStore,
)


@dataclass(frozen=True, slots=True)
class XOAuthIdentityVerificationResult:
    """Non-secret result of one exact expected-account verification."""

    user_id: str


class XOAuthIdentityVerifier:
    """Verify one expected X identity through a read-only client."""

    def __init__(self, identity_reader: XIdentityReader, expected_user_id: str) -> None:
        self._identity_reader = identity_reader
        self._expected_user_id = expected_user_id

    def verify(self) -> XOAuthIdentityVerificationResult:
        user = self._identity_reader.get_authenticated_user()
        if user.user_id != self._expected_user_id:
            raise XIdentityMismatchError(
                "Authenticated X user ID does not match the configured expected user ID"
            )
        return XOAuthIdentityVerificationResult(user_id=user.user_id)


Clock = Callable[[], datetime]


def create_cloud_oauth_identity_verifier(
    environ: Mapping[str, str] | None = None,
    *,
    firestore_client: FirestoreClient | None = None,
    secret_manager_client: SecretManagerClient | None = None,
    x_token_store: XTokenStateStore | None = None,
    x_refresh_transport: XHttpTransport | None = None,
    x_identity_transport: XHttpTransport | None = None,
    clock: Clock | None = None,
) -> XOAuthIdentityVerifier:
    """Compose one ADC-backed verifier without performing any external operation."""

    config = XCloudRuntimeConfig.from_environment(environ)
    if config.x_posting.posting_enabled:
        raise ProductionConfigurationError(
            f"{X_POSTING_ENABLED_VARIABLE} must be false for OAuth identity verification"
        )
    expected_user_id = config.x_posting.require_configured_identity()

    store = x_token_store
    if store is None:
        firestore = firestore_client or _default_firestore_client(config)
        secrets = secret_manager_client or _default_secret_manager_client()
        store = GoogleCloudXTokenStateStore(
            CloudXTokenStateStoreConfig(
                project_id=config.gcp_project_id,
                project_number=config.gcp_project_number,
                secret_id=config.x_token_secret_id,
                expected_user_id=expected_user_id,
            ),
            firestore_client=firestore,
            secret_manager_client=secrets,
            clock=clock,
        )

    refresh_coordinator = (
        store
        if all(
            callable(getattr(store, name, None))
            for name in (
                "acquire_refresh_lease",
                "replace_if_revision_with_lease",
                "release_refresh_lease",
            )
        )
        else None
    )
    token_provider = RefreshingXAccessTokenProvider(
        store,
        XOAuthRefreshClient(transport=x_refresh_transport, now=clock),
        config.x_oauth_credentials,
        refresh_coordinator=refresh_coordinator,
        clock=clock,
    )
    identity_reader = XIdentityClient(
        config.x_posting,
        transport=x_identity_transport,
        token_provider=token_provider,
    )
    return XOAuthIdentityVerifier(identity_reader, expected_user_id)


def _default_firestore_client(config: XCloudRuntimeConfig) -> FirestoreClient:
    from google.cloud import firestore_v1

    return firestore_v1.Client(
        project=config.gcp_project_id,
        database=config.firestore_database_id,
    )


def _default_secret_manager_client() -> SecretManagerClient:
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()
