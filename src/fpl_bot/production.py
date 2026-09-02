"""Explicit production dependency composition with no import-time side effects."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from flask import Flask

from fpl_bot import runtime_config as _runtime_config
from fpl_bot.api import FplApiClient
from fpl_bot.cloud_tasks import (
    CloudTasksConfig,
    CloudTaskValidationError,
    GoogleCloudTasksAdapter,
    GooglePreflightCloudTasksAdapter,
)
from fpl_bot.cloud_token_store import (
    CloudXTokenStateStoreConfig,
    GoogleCloudXTokenStateStore,
    SecretManagerClient,
)
from fpl_bot.deadline_checker import DeadlineChecker
from fpl_bot.deadline_http_app import (
    DEADLINE_TASK_ROUTE,
    PREFLIGHT_TASK_ROUTE,
    create_app,
)
from fpl_bot.deadline_planning import DeadlinePlanner
from fpl_bot.deadline_revalidation import DeadlineExecutionRevalidator
from fpl_bot.firestore_state import FirestoreClient, FirestorePostingStateStore
from fpl_bot.post_execution import DeadlinePostExecutionCoordinator
from fpl_bot.preflight import DeadlinePreflight
from fpl_bot.preflight_arming import PreflightTaskArmer
from fpl_bot.runtime_config import (
    ProductionConfigurationError,
    XCloudRuntimeConfig,
    optional_runtime_value,
    required_runtime_value,
)
from fpl_bot.service import FplDataSource
from fpl_bot.task_arming import DeadlineTaskArmer
from fpl_bot.x_api import XApiClient, XHttpTransport
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_oauth import OAuthClientCredentials
from fpl_bot.x_token_refresh import (
    RefreshingXAccessTokenProvider,
    XOAuthRefreshClient,
    XTokenStateStore,
)

GCP_PROJECT_ID_VARIABLE = _runtime_config.GCP_PROJECT_ID_VARIABLE
GCP_PROJECT_NUMBER_VARIABLE = _runtime_config.GCP_PROJECT_NUMBER_VARIABLE
FIRESTORE_DATABASE_ID_VARIABLE = _runtime_config.FIRESTORE_DATABASE_ID_VARIABLE
X_TOKEN_SECRET_ID_VARIABLE = _runtime_config.X_TOKEN_SECRET_ID_VARIABLE
DEFAULT_FIRESTORE_DATABASE_ID = _runtime_config.DEFAULT_FIRESTORE_DATABASE_ID

CLOUD_TASKS_LOCATION_ID_VARIABLE = "CLOUD_TASKS_LOCATION_ID"
CLOUD_TASKS_QUEUE_ID_VARIABLE = "CLOUD_TASKS_QUEUE_ID"
CLOUD_RUN_BASE_URL_VARIABLE = "CLOUD_RUN_BASE_URL"
CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE = "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL"
CLOUD_TASKS_OIDC_AUDIENCE_VARIABLE = "CLOUD_TASKS_OIDC_AUDIENCE"


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    """Validated non-secret infrastructure settings plus redacted X configuration."""

    gcp_project_id: str
    gcp_project_number: str
    firestore_database_id: str
    deadline_tasks: CloudTasksConfig
    preflight_tasks: CloudTasksConfig
    x_token_secret_id: str
    x_posting: XPostingConfig = field(repr=False)
    x_oauth_credentials: OAuthClientCredentials = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "ProductionRuntimeConfig":
        source = os.environ if environ is None else environ
        project_id = _required_value(source, GCP_PROJECT_ID_VARIABLE)
        location_id = _required_value(source, CLOUD_TASKS_LOCATION_ID_VARIABLE)
        queue_id = _required_value(source, CLOUD_TASKS_QUEUE_ID_VARIABLE)
        base_url = _validated_base_url(_required_value(source, CLOUD_RUN_BASE_URL_VARIABLE))
        caller_email = _required_value(
            source,
            CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE,
        )
        oidc_audience = _optional_value(
            source.get(CLOUD_TASKS_OIDC_AUDIENCE_VARIABLE),
            CLOUD_TASKS_OIDC_AUDIENCE_VARIABLE,
        )
        deadline_tasks = CloudTasksConfig(
            project_id=project_id,
            location_id=location_id,
            queue_id=queue_id,
            execution_url=f"{base_url}{DEADLINE_TASK_ROUTE}",
            service_account_email=caller_email,
            oidc_audience=oidc_audience or base_url,
        )
        preflight_tasks = CloudTasksConfig(
            project_id=project_id,
            location_id=location_id,
            queue_id=queue_id,
            execution_url=f"{base_url}{PREFLIGHT_TASK_ROUTE}",
            service_account_email=caller_email,
            oidc_audience=oidc_audience or base_url,
        )
        x_cloud = XCloudRuntimeConfig.from_environment(source)
        return cls(
            gcp_project_id=project_id,
            gcp_project_number=x_cloud.gcp_project_number,
            firestore_database_id=x_cloud.firestore_database_id,
            deadline_tasks=deadline_tasks,
            preflight_tasks=preflight_tasks,
            x_token_secret_id=x_cloud.x_token_secret_id,
            x_posting=x_cloud.x_posting,
            x_oauth_credentials=x_cloud.x_oauth_credentials,
        )


Clock = Callable[[], datetime]


def create_production_app(
    environ: Mapping[str, str] | None = None,
    *,
    fpl_source: FplDataSource | None = None,
    firestore_client: FirestoreClient | None = None,
    cloud_tasks_client: Any | None = None,
    secret_manager_client: SecretManagerClient | None = None,
    x_transport: XHttpTransport | None = None,
    x_token_store: XTokenStateStore | None = None,
    x_refresh_transport: XHttpTransport | None = None,
    clock: Clock | None = None,
) -> Flask:
    """Validate configuration and compose the existing V1 graph into one Flask app."""

    config = ProductionRuntimeConfig.from_environment(environ)
    source = fpl_source if fpl_source is not None else FplApiClient()
    firestore = (
        firestore_client if firestore_client is not None else _default_firestore_client(config)
    )
    state_store = FirestorePostingStateStore(firestore)
    task_client = (
        cloud_tasks_client if cloud_tasks_client is not None else _default_cloud_tasks_client()
    )

    token_store = x_token_store
    if token_store is None:
        expected_user_id = config.x_posting.expected_user_id
        if expected_user_id is None:  # pragma: no cover - validated above
            raise ProductionConfigurationError("X expected user ID is unavailable")
        token_store = GoogleCloudXTokenStateStore(
            CloudXTokenStateStoreConfig(
                project_id=config.gcp_project_id,
                secret_id=config.x_token_secret_id,
                expected_user_id=expected_user_id,
                project_number=config.gcp_project_number,
            ),
            firestore_client=firestore,
            secret_manager_client=(
                secret_manager_client
                if secret_manager_client is not None
                else _default_secret_manager_client()
            ),
            clock=clock,
        )
    refresh_coordinator = (
        token_store
        if all(
            callable(getattr(token_store, name, None))
            for name in (
                "acquire_refresh_lease",
                "replace_if_revision_with_lease",
                "release_refresh_lease",
            )
        )
        else None
    )
    token_provider = RefreshingXAccessTokenProvider(
        token_store,
        XOAuthRefreshClient(transport=x_refresh_transport, now=clock),
        config.x_oauth_credentials,
        refresh_coordinator=refresh_coordinator,
        clock=clock,
    )
    x_client = XApiClient(
        config.x_posting,
        transport=x_transport,
        token_provider=token_provider,
    )
    post_executor = DeadlinePostExecutionCoordinator(state_store, x_client, clock=clock)
    revalidator = DeadlineExecutionRevalidator(
        source,
        state_store,
        post_executor,
        clock=clock,
    )

    deadline_task_boundary = GoogleCloudTasksAdapter(
        config.deadline_tasks,
        client=task_client,
    )
    preflight_task_boundary = GooglePreflightCloudTasksAdapter(
        config.preflight_tasks,
        client=task_client,
    )
    deadline_task_armer = DeadlineTaskArmer(
        state_store,
        deadline_task_boundary,
        clock=clock,
    )
    preflight_task_armer = PreflightTaskArmer(
        state_store,
        preflight_task_boundary,
        clock=clock,
    )

    planner = DeadlinePlanner(source, clock=clock)
    checker = DeadlineChecker(
        planner,
        deadline_task_armer,
        revalidator,
        preflight_task_armer=preflight_task_armer,
        clock=clock,
    )
    preflight = DeadlinePreflight(
        source,
        state_store,
        deadline_task_armer,
        preflight_task_armer,
        clock=clock,
    )
    return create_app(revalidator, checker=checker, preflight=preflight)


def _default_firestore_client(config: ProductionRuntimeConfig) -> FirestoreClient:
    from google.cloud import firestore_v1

    return firestore_v1.Client(
        project=config.gcp_project_id,
        database=config.firestore_database_id,
    )


def _default_cloud_tasks_client() -> Any:
    from google.cloud import tasks_v2

    return tasks_v2.CloudTasksClient()


def _default_secret_manager_client() -> SecretManagerClient:
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()


def _required_value(source: Mapping[str, str], name: str) -> str:
    return required_runtime_value(source, name)


def _optional_value(value: str | None, name: str) -> str | None:
    return optional_runtime_value(value, name)


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise CloudTaskValidationError(
            f"{CLOUD_RUN_BASE_URL_VARIABLE} must be an HTTPS origin without a path or credentials"
        )
    return value.rstrip("/")
