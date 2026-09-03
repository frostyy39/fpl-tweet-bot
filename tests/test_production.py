import importlib
import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

import fpl_bot.production as production
from fpl_bot.api import FplApiClient
from fpl_bot.checker_http_handler import CheckerHttpResult
from fpl_bot.cloud_tasks import CloudTaskValidationError, serialize_instruction
from fpl_bot.deadline_http_app import (
    CHECKER_RUN_ROUTE,
    DEADLINE_TASK_ROUTE,
    PREFLIGHT_TASK_ROUTE,
    create_app,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.posting_state import InMemoryPostingStateStore, PostingStatus
from fpl_bot.preflight_http_handler import PreflightHttpResult
from fpl_bot.production import (
    CLOUD_RUN_BASE_URL_VARIABLE,
    CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE,
    CLOUD_TASKS_LOCATION_ID_VARIABLE,
    CLOUD_TASKS_QUEUE_ID_VARIABLE,
    FIRESTORE_DATABASE_ID_VARIABLE,
    GCP_PROJECT_ID_VARIABLE,
    GCP_PROJECT_NUMBER_VARIABLE,
    X_TOKEN_SECRET_ID_VARIABLE,
    ProductionConfigurationError,
    ProductionRuntimeConfig,
    create_production_app,
)
from fpl_bot.x_api import XApiClient, XHttpRequest, XHttpResponse
from fpl_bot.x_errors import XConfigurationError
from fpl_bot.x_oauth import X_OAUTH_CLIENT_ID_VARIABLE, X_OAUTH_CLIENT_SECRET_VARIABLE
from fpl_bot.x_token_refresh import InMemoryXTokenStateStore, XOAuthTokenState

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
CHECKER_TIME_UTC = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
EXPECTED_TWEET = "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"
TEST_USER_ID = "123456789"
TEST_POST_ID = "987654321"


def valid_environment() -> dict[str, str]:
    return {
        "X_ENVIRONMENT": "test",
        "X_POSTING_ENABLED": "true",
        "X_EXPECTED_USER_ID": TEST_USER_ID,
        X_OAUTH_CLIENT_ID_VARIABLE: "unit-test-client-id-placeholder",
        X_OAUTH_CLIENT_SECRET_VARIABLE: "unit-test-client-secret-placeholder",
        X_TOKEN_SECRET_ID_VARIABLE: "unit-test-x-token-state",
        GCP_PROJECT_ID_VARIABLE: "fpl-bot-test",
        GCP_PROJECT_NUMBER_VARIABLE: "123456789012",
        FIRESTORE_DATABASE_ID_VARIABLE: "(default)",
        CLOUD_TASKS_LOCATION_ID_VARIABLE: "europe-west2",
        CLOUD_TASKS_QUEUE_ID_VARIABLE: "deadline-posts",
        CLOUD_RUN_BASE_URL_VARIABLE: "https://fpl-bot-test.example",
        CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE: (
            "task-caller@fpl-bot-test.iam.gserviceaccount.com"
        ),
    }


def disabled_environment() -> dict[str, str]:
    environ = valid_environment()
    environ["X_POSTING_ENABLED"] = "false"
    return environ


def bootstrap(deadline: datetime = DEADLINE_UTC) -> dict[str, Any]:
    return {
        "events": [
            {
                "id": EVENT_ID,
                "name": "Gameweek 3",
                "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
                "is_current": False,
                "is_next": True,
            }
        ],
        "teams": [
            {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
            for team_id in range(1, 21)
        ],
    }


class StaticFplSource:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.bootstrap_calls = 0
        self.fixture_calls = 0

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        self.bootstrap_calls += 1
        return self.payload

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        self.fixture_calls += 1
        return [
            {
                "id": fixture_id,
                "event": event_id,
                "team_h": team_id,
                "team_a": team_id + 1,
            }
            for fixture_id, team_id in enumerate(range(1, 21, 2), start=1)
        ]


class RecordingCloudTasksClient:
    def __init__(self) -> None:
        self.create_requests: list[Any] = []
        self.get_requests: list[Any] = []

    def create_task(self, request: Any, *, retry: Any) -> Any:
        assert retry is None
        self.create_requests.append(request)
        return request["task"]

    def get_task(self, request: Any, *, retry: Any) -> Any:
        assert retry is None
        self.get_requests.append(request)
        raise AssertionError("No duplicate lookup expected")


class ForbiddenXTransport:
    def __init__(self) -> None:
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        self.requests.append(request)
        raise AssertionError("X transport must not be used")


class SuccessfulXTransport:
    def __init__(self) -> None:
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        assert timeout_seconds == 10.0
        self.requests.append(request)
        if request.method == "GET":
            return XHttpResponse(
                200,
                json.dumps({"data": {"id": TEST_USER_ID, "username": "FPLBotTest"}}).encode(),
            )
        return XHttpResponse(
            201,
            json.dumps({"data": {"id": TEST_POST_ID, "text": EXPECTED_TWEET}}).encode(),
        )


class SuccessfulRefreshTransport:
    def __init__(self) -> None:
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        assert timeout_seconds == 10.0
        self.requests.append(request)
        return XHttpResponse(
            200,
            json.dumps(
                {
                    "access_token": "unit-test-refreshed-access-placeholder",
                    "refresh_token": "unit-test-refreshed-refresh-placeholder",
                    "token_type": "bearer",
                    "expires_in": 7200,
                    "scope": "tweet.read users.read tweet.write offline.access",
                }
            ).encode(),
            {"Content-Type": "application/json"},
        )


class FakeRevalidator:
    def execute(self, instruction: ScheduledDeadlineInstruction):
        raise AssertionError("The injectable test app must not require production configuration")


def instruction(deadline: datetime = DEADLINE_UTC) -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(EVENT_ID, deadline)


def valid_token_store(
    *,
    expires_at: datetime = DEADLINE_UTC + timedelta(days=1),
) -> InMemoryXTokenStateStore:
    return InMemoryXTokenStateStore(
        XOAuthTokenState(
            access_token="unit-test-token-placeholder",
            refresh_token="unit-test-refresh-placeholder",
            expires_at_utc=expires_at,
        )
    )


def _app_with_in_memory_state(
    monkeypatch: pytest.MonkeyPatch,
    source: StaticFplSource,
    *,
    now: datetime,
    x_transport: ForbiddenXTransport | SuccessfulXTransport,
    task_client: RecordingCloudTasksClient | None = None,
) -> tuple[Any, InMemoryPostingStateStore, RecordingCloudTasksClient]:
    store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    client = task_client or RecordingCloudTasksClient()
    monkeypatch.setattr(
        production,
        "FirestorePostingStateStore",
        lambda firestore_client: store,
    )
    app = create_production_app(
        valid_environment(),
        fpl_source=source,
        firestore_client=MagicMock(),
        cloud_tasks_client=client,
        x_transport=x_transport,
        x_token_store=valid_token_store(),
        clock=lambda: now,
    )
    return app, store, client


def test_valid_configuration_builds_all_three_routes_without_external_activity() -> None:
    source = StaticFplSource(bootstrap())
    firestore_client = MagicMock()
    task_client = RecordingCloudTasksClient()
    x_transport = ForbiddenXTransport()

    app = create_production_app(
        valid_environment(),
        fpl_source=source,
        firestore_client=firestore_client,
        cloud_tasks_client=task_client,
        x_transport=x_transport,
        x_token_store=valid_token_store(),
        clock=lambda: CHECKER_TIME_UTC,
    )

    routes = {
        (rule.rule, tuple(sorted(rule.methods - {"OPTIONS"}))) for rule in app.url_map.iter_rules()
    }
    assert (CHECKER_RUN_ROUTE, ("POST",)) in routes
    assert (PREFLIGHT_TASK_ROUTE, ("POST",)) in routes
    assert (DEADLINE_TASK_ROUTE, ("POST",)) in routes
    assert source.bootstrap_calls == 0
    firestore_client.transaction.assert_not_called()
    assert task_client.create_requests == []
    assert x_transport.requests == []


def test_disabled_posting_configuration_builds_without_external_activity() -> None:
    source = StaticFplSource(bootstrap())
    firestore_client = MagicMock()
    task_client = RecordingCloudTasksClient()
    x_transport = ForbiddenXTransport()

    app = create_production_app(
        disabled_environment(),
        fpl_source=source,
        firestore_client=firestore_client,
        cloud_tasks_client=task_client,
        x_transport=x_transport,
        x_token_store=valid_token_store(),
        clock=lambda: CHECKER_TIME_UTC,
    )

    assert app is not None
    assert source.bootstrap_calls == 0
    assert source.fixture_calls == 0
    firestore_client.transaction.assert_not_called()
    assert task_client.create_requests == []
    assert task_client.get_requests == []
    assert x_transport.requests == []


def test_production_factory_constructs_cloud_token_store_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[object] = []

    class FakeCloudStore:
        def __init__(self, config: object, **kwargs: object) -> None:
            constructed.append((config, kwargs))

        def read(self):
            raise AssertionError("Application construction must not read the token secret")

        def replace_if_revision(self, expected_revision, replacement):
            raise AssertionError("Application construction must not rotate token state")

    monkeypatch.setattr(production, "GoogleCloudXTokenStateStore", FakeCloudStore)
    app = create_production_app(
        disabled_environment(),
        fpl_source=StaticFplSource(bootstrap()),
        firestore_client=MagicMock(),
        cloud_tasks_client=RecordingCloudTasksClient(),
        secret_manager_client=MagicMock(),
        x_transport=ForbiddenXTransport(),
        clock=lambda: CHECKER_TIME_UTC,
    )

    assert app is not None
    assert len(constructed) == 1


@pytest.mark.parametrize(
    "variable",
    [
        GCP_PROJECT_ID_VARIABLE,
        GCP_PROJECT_NUMBER_VARIABLE,
        CLOUD_TASKS_LOCATION_ID_VARIABLE,
        CLOUD_TASKS_QUEUE_ID_VARIABLE,
        CLOUD_RUN_BASE_URL_VARIABLE,
        CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE,
        X_TOKEN_SECRET_ID_VARIABLE,
    ],
)
def test_missing_required_infrastructure_configuration_fails_before_app_creation(
    variable: str,
) -> None:
    environ = valid_environment()
    environ.pop(variable)

    with pytest.raises(ProductionConfigurationError, match=variable):
        create_production_app(environ)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (GCP_PROJECT_ID_VARIABLE, "invalid/project"),
        (CLOUD_TASKS_LOCATION_ID_VARIABLE, "invalid/location"),
        (CLOUD_TASKS_QUEUE_ID_VARIABLE, "invalid queue"),
    ],
)
def test_invalid_cloud_resource_configuration_fails_closed(variable: str, value: str) -> None:
    environ = valid_environment()
    environ[variable] = value

    with pytest.raises(CloudTaskValidationError):
        ProductionRuntimeConfig.from_environment(environ)


def test_invalid_project_number_fails_closed() -> None:
    environ = valid_environment()
    environ[GCP_PROJECT_NUMBER_VARIABLE] = "not-a-number"

    with pytest.raises(ProductionConfigurationError, match=GCP_PROJECT_NUMBER_VARIABLE):
        ProductionRuntimeConfig.from_environment(environ)


@pytest.mark.parametrize(
    "url",
    [
        "http://fpl-bot-test.example",
        "https://user@fpl-bot-test.example",
        "https://fpl-bot-test.example/unexpected-path",
    ],
)
def test_invalid_execution_base_url_fails_closed(url: str) -> None:
    environ = valid_environment()
    environ[CLOUD_RUN_BASE_URL_VARIABLE] = url

    with pytest.raises(CloudTaskValidationError, match=CLOUD_RUN_BASE_URL_VARIABLE):
        ProductionRuntimeConfig.from_environment(environ)


def test_invalid_task_caller_service_account_fails_closed() -> None:
    environ = valid_environment()
    environ[CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL_VARIABLE] = (
        "bad account@fpl-bot-test.iam.gserviceaccount.com"
    )

    with pytest.raises(CloudTaskValidationError, match="service account"):
        ProductionRuntimeConfig.from_environment(environ)


def test_invalid_firestore_database_id_fails_closed() -> None:
    environ = valid_environment()
    environ[FIRESTORE_DATABASE_ID_VARIABLE] = "Invalid Database"

    with pytest.raises(ProductionConfigurationError, match=FIRESTORE_DATABASE_ID_VARIABLE):
        ProductionRuntimeConfig.from_environment(environ)


def test_invalid_x_token_secret_id_fails_closed() -> None:
    environ = valid_environment()
    environ[X_TOKEN_SECRET_ID_VARIABLE] = "invalid/secret"

    with pytest.raises(ProductionConfigurationError, match=X_TOKEN_SECRET_ID_VARIABLE):
        ProductionRuntimeConfig.from_environment(environ)


def test_malformed_expected_x_user_id_fails_closed() -> None:
    environ = valid_environment()
    environ["X_EXPECTED_USER_ID"] = "not-numeric"

    with pytest.raises(XConfigurationError, match="positive numeric"):
        ProductionRuntimeConfig.from_environment(environ)


@pytest.mark.parametrize("posting_enabled", ["true", "false"])
def test_unsupported_x_environment_fails_closed(posting_enabled: str) -> None:
    environ = valid_environment()
    environ["X_ENVIRONMENT"] = "production"
    environ["X_POSTING_ENABLED"] = posting_enabled

    with pytest.raises(XConfigurationError, match="no production mode"):
        ProductionRuntimeConfig.from_environment(environ)


def test_disabled_production_configuration_still_requires_expected_identity() -> None:
    environ = disabled_environment()
    environ.pop("X_EXPECTED_USER_ID")

    with pytest.raises(XConfigurationError, match="X_EXPECTED_USER_ID is required"):
        ProductionRuntimeConfig.from_environment(environ)


def test_malformed_posting_enabled_configuration_fails_before_app_creation() -> None:
    environ = valid_environment()
    environ["X_POSTING_ENABLED"] = "yes"

    with pytest.raises(XConfigurationError, match="either 'true' or 'false'"):
        create_production_app(environ)


def test_missing_x_oauth_client_secret_fails_without_secret_output() -> None:
    environ = valid_environment()
    environ.pop(X_OAUTH_CLIENT_SECRET_VARIABLE)

    with pytest.raises(ProductionConfigurationError) as error:
        ProductionRuntimeConfig.from_environment(environ)

    assert X_OAUTH_CLIENT_SECRET_VARIABLE in str(error.value)
    assert "unit-test-client-id-placeholder" not in str(error.value)


def test_runtime_config_repr_redacts_x_oauth_client_credentials() -> None:
    config = ProductionRuntimeConfig.from_environment(valid_environment())

    assert "unit-test-client-id-placeholder" not in repr(config)
    assert "unit-test-client-secret-placeholder" not in repr(config)


def test_configuration_error_does_not_expose_oauth_client_secret() -> None:
    environ = valid_environment()
    environ[CLOUD_RUN_BASE_URL_VARIABLE] = "not-an-https-origin"

    with pytest.raises(CloudTaskValidationError) as error:
        ProductionRuntimeConfig.from_environment(environ)

    assert "unit-test-client-secret-placeholder" not in str(error.value)
    assert "unit-test-client-secret-placeholder" not in repr(error.value)


def test_task_configuration_uses_exact_private_routes_and_shared_oidc_audience() -> None:
    config = ProductionRuntimeConfig.from_environment(valid_environment())

    assert config.deadline_tasks.execution_url == "https://fpl-bot-test.example/tasks/deadline"
    assert config.preflight_tasks.execution_url == "https://fpl-bot-test.example/tasks/preflight"
    assert config.deadline_tasks.oidc_audience == "https://fpl-bot-test.example"
    assert config.preflight_tasks.oidc_audience == "https://fpl-bot-test.example"


def test_checker_route_reaches_existing_planner_and_arms_final_then_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = StaticFplSource(bootstrap())
    x_transport = ForbiddenXTransport()
    app, _, task_client = _app_with_in_memory_state(
        monkeypatch,
        source,
        now=CHECKER_TIME_UTC,
        x_transport=x_transport,
    )

    response = app.test_client().post(CHECKER_RUN_ROUTE, json={"ignored": "input"})

    assert response.status_code == 200
    assert response.get_json() == {"result": CheckerHttpResult.TASK_ARMED.value}
    assert source.bootstrap_calls == 1
    assert len(task_client.create_requests) == 2
    assert task_client.create_requests[0]["task"].http_request.url.endswith(DEADLINE_TASK_ROUTE)
    assert task_client.create_requests[1]["task"].http_request.url.endswith(PREFLIGHT_TASK_ROUTE)
    assert x_transport.requests == []


def test_preflight_route_reaches_only_read_and_audit_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = StaticFplSource(bootstrap())
    x_transport = ForbiddenXTransport()
    app, store, task_client = _app_with_in_memory_state(
        monkeypatch,
        source,
        now=DEADLINE_UTC - timedelta(minutes=5),
        x_transport=x_transport,
    )

    response = app.test_client().post(
        PREFLIGHT_TASK_ROUTE,
        data=serialize_instruction(instruction()),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json() == {"result": PreflightHttpResult.PREFLIGHT_OK.value}
    assert source.bootstrap_calls == 1
    assert source.fixture_calls == 0
    assert store.get_event(EVENT_ID).status is None
    assert task_client.create_requests == []
    assert x_transport.requests == []


def test_deadline_route_reaches_guarded_posting_graph_once_and_then_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = StaticFplSource(bootstrap())
    x_transport = SuccessfulXTransport()
    app, store, task_client = _app_with_in_memory_state(
        monkeypatch,
        source,
        now=DEADLINE_UTC,
        x_transport=x_transport,
    )
    client = app.test_client()
    payload = serialize_instruction(instruction())

    first = client.post(DEADLINE_TASK_ROUTE, data=payload, content_type="application/json")
    duplicate = client.post(DEADLINE_TASK_ROUTE, data=payload, content_type="application/json")

    assert first.status_code == 200
    assert first.get_json() == {"result": "posted"}
    assert duplicate.status_code == 200
    assert duplicate.get_json() == {"result": "duplicate"}
    assert [request.method for request in x_transport.requests] == ["GET", "POST"]
    assert source.bootstrap_calls == 2
    assert source.fixture_calls == 2
    assert store.get_event(EVENT_ID).status is PostingStatus.SUCCEEDED
    assert task_client.create_requests == []


def test_deadline_route_refreshes_expiring_token_before_identity_and_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = StaticFplSource(bootstrap())
    state_store = InMemoryPostingStateStore(claim_id_factory=lambda: "claim-1")
    monkeypatch.setattr(
        production,
        "FirestorePostingStateStore",
        lambda firestore_client: state_store,
    )
    token_store = valid_token_store(expires_at=DEADLINE_UTC)
    refresh_transport = SuccessfulRefreshTransport()
    x_transport = SuccessfulXTransport()
    app = create_production_app(
        valid_environment(),
        fpl_source=source,
        firestore_client=MagicMock(),
        cloud_tasks_client=RecordingCloudTasksClient(),
        x_transport=x_transport,
        x_token_store=token_store,
        x_refresh_transport=refresh_transport,
        clock=lambda: DEADLINE_UTC,
    )

    response = app.test_client().post(
        DEADLINE_TASK_ROUTE,
        data=serialize_instruction(instruction()),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json() == {"result": "posted"}
    assert len(refresh_transport.requests) == 1
    assert [request.method for request in x_transport.requests] == ["GET", "POST"]
    assert all(
        request.headers["Authorization"] == "Bearer unit-test-refreshed-access-placeholder"
        for request in x_transport.requests
    )
    assert token_store.read().state.refresh_token == "unit-test-refreshed-refresh-placeholder"


def test_importing_production_module_constructs_no_clients_or_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud import firestore_v1, secretmanager, tasks_v2

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Import must not construct external clients or services")

    monkeypatch.setattr(FplApiClient, "__init__", forbidden)
    monkeypatch.setattr(XApiClient, "__init__", forbidden)
    monkeypatch.setattr(production.XOAuthRefreshClient, "__init__", forbidden)
    monkeypatch.setattr(firestore_v1, "Client", forbidden)
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", forbidden)
    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", forbidden)
    monkeypatch.setattr(ProductionRuntimeConfig, "from_environment", forbidden)

    importlib.reload(production)


def test_test_app_factory_remains_independent_of_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in valid_environment():
        monkeypatch.delenv(variable, raising=False)

    app = create_app(FakeRevalidator())

    assert DEADLINE_TASK_ROUTE in {rule.rule for rule in app.url_map.iter_rules()}


def test_no_service_account_key_file_is_required_for_injected_sdk_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    app = create_production_app(
        valid_environment(),
        fpl_source=StaticFplSource(bootstrap()),
        firestore_client=MagicMock(),
        cloud_tasks_client=RecordingCloudTasksClient(),
        x_transport=ForbiddenXTransport(),
        x_token_store=valid_token_store(),
        clock=lambda: CHECKER_TIME_UTC,
    )

    assert app is not None


def test_composition_reuses_existing_services_without_loops_or_parallel_clients() -> None:
    source = inspect.getsource(production.create_production_app)

    for existing_type in (
        "FplApiClient",
        "FirestorePostingStateStore",
        "GoogleCloudTasksAdapter",
        "GooglePreflightCloudTasksAdapter",
        "XApiClient",
        "XOAuthRefreshClient",
        "RefreshingXAccessTokenProvider",
        "DeadlinePostExecutionCoordinator",
        "DeadlineExecutionRevalidator",
        "DeadlinePlanner",
        "DeadlineTaskArmer",
        "PreflightTaskArmer",
        "DeadlinePreflight",
        "DeadlineChecker",
        "create_app",
    ):
        assert existing_type in source
    assert "while " not in source
    assert "retry" not in source
