"""Explicit production dependency composition with no import-time side effects."""

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from flask import Flask

from fpl_bot.api import FplApiClient
from fpl_bot.cloud_tasks import (
    CloudTasksConfig,
    CloudTaskValidationError,
    GoogleCloudTasksAdapter,
    GooglePreflightCloudTasksAdapter,
)
from fpl_bot.deadline_checker import DeadlineChecker
from fpl_bot.deadline_http_app import (
    DEADLINE_TASK_ROUTE,
    PREFLIGHT_TASK_ROUTE,
    create_app,
)
from fpl_bot.deadline_planning import DeadlinePlanner
from fpl_bot.deadline_revalidation import DeadlineExecutionRevalidator
from fpl_bot.errors import FplBotError
from fpl_bot.firestore_state import FirestoreClient, FirestorePostingStateStore
from fpl_bot.post_execution import DeadlinePostExecutionCoordinator
from fpl_bot.preflight import DeadlinePreflight
from fpl_bot.preflight_arming import PreflightTaskArmer
from fpl_bot.service import FplDataSource
from fpl_bot.task_arming import DeadlineTaskArmer
from fpl_bot.x_api import XApiClient, XHttpTransport
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_oauth import (
    X_OAUTH_CLIENT_ID_VARIABLE,
    X_OAUTH_CLIENT_SECRET_VARIABLE,
    OAuthClientCredentials,
)
from fpl_bot.x_token_refresh import (
    RefreshingXAccessTokenProvider,
    XOAuthRefreshClient,
    XTokenStateStore,
)

GCP_PROJECT_ID_VARIABLE = "GCP_PROJECT_ID"
FIRESTORE_DATABASE_ID_VARIABLE = "FIRESTORE_DATABASE_ID"
CLOUD_TASKS_LOCATION_ID_VARIABLE = "CLOUD_TASKS_LOCATION_ID"
CLOUD_TASKS_QUEUE_ID_VARIABLE = "CLOUD_TASKS_QUEUE_ID"
CLOUD_RUN_BASE_URL_VARIABLE = "CLOUD_RUN_BASE_URL"
CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE = "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL"
CLOUD_TASKS_OIDC_AUDIENCE_VARIABLE = "CLOUD_TASKS_OIDC_AUDIENCE"

DEFAULT_FIRESTORE_DATABASE_ID = "(default)"
FIRESTORE_DATABASE_ID_PATTERN = re.compile(r"(?:\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])\Z")


class ProductionConfigurationError(FplBotError):
    """Raised before adapter construction when runtime configuration is incomplete."""


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    """Validated non-secret infrastructure settings plus redacted X configuration."""

    gcp_project_id: str
    firestore_database_id: str
    deadline_tasks: CloudTasksConfig
    preflight_tasks: CloudTasksConfig
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
        database_id = (
            _optional_value(
                source.get(FIRESTORE_DATABASE_ID_VARIABLE),
                FIRESTORE_DATABASE_ID_VARIABLE,
            )
            or DEFAULT_FIRESTORE_DATABASE_ID
        )
        if not FIRESTORE_DATABASE_ID_PATTERN.fullmatch(database_id):
            raise ProductionConfigurationError(
                f"{FIRESTORE_DATABASE_ID_VARIABLE} is not a valid Firestore database ID"
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
        x_posting = XPostingConfig.from_environment(source)
        x_posting.require_posting_identity_guard()
        x_oauth_credentials = OAuthClientCredentials(
            client_id=_required_value(source, X_OAUTH_CLIENT_ID_VARIABLE),
            client_secret=_required_value(source, X_OAUTH_CLIENT_SECRET_VARIABLE),
        )

        return cls(
            gcp_project_id=project_id,
            firestore_database_id=database_id,
            deadline_tasks=deadline_tasks,
            preflight_tasks=preflight_tasks,
            x_posting=x_posting,
            x_oauth_credentials=x_oauth_credentials,
        )


Clock = Callable[[], datetime]


def create_production_app(
    environ: Mapping[str, str] | None = None,
    *,
    fpl_source: FplDataSource | None = None,
    firestore_client: FirestoreClient | None = None,
    cloud_tasks_client: Any | None = None,
    x_transport: XHttpTransport | None = None,
    x_token_store: XTokenStateStore | None = None,
    x_refresh_transport: XHttpTransport | None = None,
    clock: Clock | None = None,
) -> Flask:
    """Validate configuration and compose the existing V1 graph into one Flask app."""

    config = ProductionRuntimeConfig.from_environment(environ)
    if x_token_store is None:
        raise ProductionConfigurationError(
            "A mutable secure X token-state store with distributed compare-and-swap is required"
        )
    source = fpl_source if fpl_source is not None else FplApiClient()
    state_store = FirestorePostingStateStore(
        firestore_client if firestore_client is not None else _default_firestore_client(config)
    )
    task_client = (
        cloud_tasks_client if cloud_tasks_client is not None else _default_cloud_tasks_client()
    )

    token_provider = RefreshingXAccessTokenProvider(
        x_token_store,
        XOAuthRefreshClient(transport=x_refresh_transport, now=clock),
        config.x_oauth_credentials,
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


def _required_value(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if value is None or not isinstance(value, str) or not value or value != value.strip():
        raise ProductionConfigurationError(f"{name} is required and must be non-empty")
    if not value.isprintable():
        raise ProductionConfigurationError(f"{name} must contain only printable characters")
    return value


def _optional_value(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip() or not value.isprintable():
        raise ProductionConfigurationError(
            f"{name} must be printable and contain no outer whitespace"
        )
    return value or None


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
