import inspect
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import fpl_bot.x_oauth_verify as verify_module
import fpl_bot.x_oauth_verify_cli as cli_module
from fpl_bot.production import GCP_PROJECT_NUMBER_VARIABLE, ProductionConfigurationError
from fpl_bot.x_api import AuthenticatedXUser, XHttpRequest, XHttpResponse, XIdentityClient
from fpl_bot.x_errors import (
    XApiResponseError,
    XConfigurationError,
    XIdentityMismatchError,
    XOAuthEndpointError,
    XResponseValidationError,
    XTokenAuthorityPersistenceError,
    XTokenRefreshError,
    XTokenRefreshResponseError,
    XTokenRefreshTransportError,
    XTokenSecretStorageError,
    XTokenStoreError,
)
from fpl_bot.x_oauth import OAUTH_SCOPES
from fpl_bot.x_oauth_verify import (
    XOAuthIdentityVerificationResult,
    XOAuthIdentityVerifier,
    create_cloud_oauth_identity_verifier,
    diagnose_verification_failure,
)
from fpl_bot.x_token_refresh import (
    InMemoryXTokenStateStore,
    VersionedXTokenState,
    XOAuthTokenState,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
EXPECTED_USER_ID = "1732468005336907776"
OLD_ACCESS_TOKEN = "old-access-token-placeholder"
NEW_ACCESS_TOKEN = "new-access-token-placeholder"
REFRESH_TOKEN = "refresh-token-placeholder"


def valid_environment() -> dict[str, str]:
    return {
        "GCP_PROJECT_ID": "fpl-bot-test",
        GCP_PROJECT_NUMBER_VARIABLE: "123456789012",
        "FIRESTORE_DATABASE_ID": "(default)",
        "CLOUD_TASKS_LOCATION_ID": "europe-west2",
        "CLOUD_TASKS_QUEUE_ID": "fpl-deadline",
        "CLOUD_RUN_BASE_URL": "https://fpl-bot-test.example",
        "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL": (
            "fpl-bot-invoker@fpl-bot-test.iam.gserviceaccount.com"
        ),
        "X_ENVIRONMENT": "test",
        "X_POSTING_ENABLED": "false",
        "X_EXPECTED_USER_ID": EXPECTED_USER_ID,
        "X_TOKEN_SECRET_ID": "x-oauth-token-state",
        "X_OAUTH_CLIENT_ID": "client-id-placeholder",
        "X_OAUTH_CLIENT_SECRET": "client-secret-placeholder",
    }


class RecordingTransport:
    def __init__(self, outcomes: Iterable[XHttpResponse | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        assert timeout_seconds == 10.0
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StaticIdentityReader:
    def __init__(self, user_id: str = EXPECTED_USER_ID) -> None:
        self.user_id = user_id
        self.calls = 0

    def get_authenticated_user(self) -> AuthenticatedXUser:
        self.calls += 1
        return AuthenticatedXUser(self.user_id, "fpl_test_bot")


class FailingPersistenceStore:
    def __init__(self, state: XOAuthTokenState) -> None:
        self.state = state

    def read(self) -> VersionedXTokenState:
        return VersionedXTokenState("1", self.state)

    def replace_if_revision(self, expected_revision: str, replacement: XOAuthTokenState) -> bool:
        raise XTokenStoreError("test persistence failure")


def token_state(*, expires_at: datetime) -> XOAuthTokenState:
    return XOAuthTokenState(
        access_token=OLD_ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        expires_at_utc=expires_at,
        scopes=OAUTH_SCOPES,
    )


def identity_response(user_id: str = EXPECTED_USER_ID) -> XHttpResponse:
    return XHttpResponse(
        200,
        json.dumps({"data": {"id": user_id, "username": "fpl_test_bot"}}).encode(),
    )


def refresh_response() -> XHttpResponse:
    return XHttpResponse(
        200,
        json.dumps(
            {
                "access_token": NEW_ACCESS_TOKEN,
                "expires_in": 7200,
                "refresh_token": "replacement-refresh-token-placeholder",
                "scope": " ".join(OAUTH_SCOPES),
                "token_type": "bearer",
            }
        ).encode(),
        {"Content-Type": "application/json"},
    )


def build_verifier(
    state: XOAuthTokenState,
    *,
    refresh_transport: RecordingTransport,
    identity_transport: RecordingTransport,
    store: Any | None = None,
) -> XOAuthIdentityVerifier:
    return create_cloud_oauth_identity_verifier(
        valid_environment(),
        x_token_store=store or InMemoryXTokenStateStore(state),
        x_refresh_transport=refresh_transport,
        x_identity_transport=identity_transport,
        clock=lambda: NOW,
    )


def test_verifier_requires_posting_disabled() -> None:
    environ = valid_environment()
    environ["X_POSTING_ENABLED"] = "true"

    with pytest.raises(ProductionConfigurationError, match="must be false"):
        create_cloud_oauth_identity_verifier(environ, x_token_store=object())


def test_verifier_rejects_unsupported_environment() -> None:
    environ = valid_environment()
    environ["X_ENVIRONMENT"] = "production"

    with pytest.raises(XConfigurationError, match="X_ENVIRONMENT"):
        create_cloud_oauth_identity_verifier(environ, x_token_store=object())


def test_verifier_rejects_invalid_expected_identity() -> None:
    environ = valid_environment()
    environ["X_EXPECTED_USER_ID"] = "not-an-id"

    with pytest.raises(XConfigurationError, match="X_EXPECTED_USER_ID"):
        create_cloud_oauth_identity_verifier(environ, x_token_store=object())


def test_verifier_rejects_malformed_posting_enabled_value() -> None:
    environ = valid_environment()
    environ["X_POSTING_ENABLED"] = "disabled"

    with pytest.raises(XConfigurationError, match="X_POSTING_ENABLED"):
        create_cloud_oauth_identity_verifier(environ, x_token_store=object())


@pytest.mark.parametrize("value", [None, "", "not-a-number", "0123"])
def test_project_number_is_required_and_validated(value: str | None) -> None:
    environ = valid_environment()
    if value is None:
        environ.pop(GCP_PROJECT_NUMBER_VARIABLE)
    else:
        environ[GCP_PROJECT_NUMBER_VARIABLE] = value

    with pytest.raises(ProductionConfigurationError, match=GCP_PROJECT_NUMBER_VARIABLE):
        create_cloud_oauth_identity_verifier(environ, x_token_store=object())


def test_expired_authoritative_token_refreshes_then_reads_identity_once() -> None:
    refresh = RecordingTransport([refresh_response()])
    identity = RecordingTransport([identity_response()])
    verifier = build_verifier(
        token_state(expires_at=NOW - timedelta(minutes=1)),
        refresh_transport=refresh,
        identity_transport=identity,
    )

    result = verifier.verify()

    assert result == XOAuthIdentityVerificationResult(EXPECTED_USER_ID)
    assert [request.url for request in refresh.requests] == ["https://api.x.com/2/oauth2/token"]
    assert [request.url for request in identity.requests] == ["https://api.x.com/2/users/me"]
    assert identity.requests[0].method == "GET"
    assert identity.requests[0].headers["Authorization"] == f"Bearer {NEW_ACCESS_TOKEN}"


def test_verifier_composition_performs_no_token_or_identity_operation() -> None:
    class OperationForbiddenStore:
        def read(self) -> VersionedXTokenState:
            raise AssertionError("token state must remain unread during composition")

        def replace_if_revision(
            self, expected_revision: str, replacement: XOAuthTokenState
        ) -> bool:
            raise AssertionError("token state must remain unchanged during composition")

    identity = RecordingTransport([])
    refresh = RecordingTransport([])

    verifier = create_cloud_oauth_identity_verifier(
        valid_environment(),
        x_token_store=OperationForbiddenStore(),
        x_refresh_transport=refresh,
        x_identity_transport=identity,
        clock=lambda: NOW,
    )

    assert isinstance(verifier, XOAuthIdentityVerifier)
    assert refresh.requests == []
    assert identity.requests == []


def test_current_authoritative_token_reads_identity_without_refresh() -> None:
    refresh = RecordingTransport([])
    identity = RecordingTransport([identity_response()])
    verifier = build_verifier(
        token_state(expires_at=NOW + timedelta(minutes=6)),
        refresh_transport=refresh,
        identity_transport=identity,
    )

    verifier.verify()

    assert refresh.requests == []
    assert len(identity.requests) == 1


def test_refresh_failure_performs_zero_identity_requests() -> None:
    refresh = RecordingTransport([XHttpResponse(401, b"{}")])
    identity = RecordingTransport([])
    verifier = build_verifier(
        token_state(expires_at=NOW - timedelta(minutes=1)),
        refresh_transport=refresh,
        identity_transport=identity,
    )

    with pytest.raises(XTokenRefreshError):
        verifier.verify()

    assert len(refresh.requests) == 1
    assert identity.requests == []


def test_token_persistence_failure_performs_zero_identity_requests() -> None:
    state = token_state(expires_at=NOW - timedelta(minutes=1))
    refresh = RecordingTransport([refresh_response()])
    identity = RecordingTransport([])
    verifier = build_verifier(
        state,
        store=FailingPersistenceStore(state),
        refresh_transport=refresh,
        identity_transport=identity,
    )

    with pytest.raises(XTokenStoreError):
        verifier.verify()

    assert len(refresh.requests) == 1
    assert identity.requests == []


def test_account_mismatch_fails_closed_after_one_identity_read() -> None:
    identity = RecordingTransport([identity_response("999999999")])
    verifier = build_verifier(
        token_state(expires_at=NOW + timedelta(minutes=6)),
        refresh_transport=RecordingTransport([]),
        identity_transport=identity,
    )

    with pytest.raises(XIdentityMismatchError):
        verifier.verify()

    assert [request.method for request in identity.requests] == ["GET"]


def test_identity_http_failure_fails_closed() -> None:
    identity = RecordingTransport([XHttpResponse(503, b"{}")])
    verifier = build_verifier(
        token_state(expires_at=NOW + timedelta(minutes=6)),
        refresh_transport=RecordingTransport([]),
        identity_transport=identity,
    )

    with pytest.raises(XApiResponseError):
        verifier.verify()


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"data":{"id":true,"username":"fpl_test_bot"}}',
        b'{"data":{"id":"1732468005336907776","username":"bad-name!"}}',
    ],
)
def test_identity_response_parsing_remains_strict(payload: bytes) -> None:
    identity = RecordingTransport([XHttpResponse(200, payload)])
    verifier = build_verifier(
        token_state(expires_at=NOW + timedelta(minutes=6)),
        refresh_transport=RecordingTransport([]),
        identity_transport=identity,
    )

    with pytest.raises(XResponseValidationError):
        verifier.verify()


def test_verifier_dependency_graph_has_no_post_capability() -> None:
    verifier = build_verifier(
        token_state(expires_at=NOW + timedelta(minutes=6)),
        refresh_transport=RecordingTransport([]),
        identity_transport=RecordingTransport([identity_response()]),
    )

    assert isinstance(verifier._identity_reader, XIdentityClient)
    assert not hasattr(verifier._identity_reader, "create_text_post")
    assert not hasattr(verifier._identity_reader, "_send")
    source = inspect.getsource(verify_module)
    for forbidden in (
        "XApiClient",
        "XPostCreator",
        "PostExecutionCoordinator",
        "create_text_post",
        "2/tweets",
    ):
        assert forbidden not in source


def test_direct_verifier_invokes_reader_exactly_once() -> None:
    reader = StaticIdentityReader()

    result = XOAuthIdentityVerifier(reader, EXPECTED_USER_ID).verify()

    assert result.user_id == EXPECTED_USER_ID
    assert reader.calls == 1


def test_cli_failure_output_never_contains_exception_or_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sensitive-token-value"

    class FailingVerifier:
        def verify(self) -> XOAuthIdentityVerificationResult:
            raise RuntimeError(secret)

    monkeypatch.setattr(
        cli_module,
        "create_cloud_oauth_identity_verifier",
        lambda: FailingVerifier(),
    )

    assert cli_module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "result": "verification_failed",
        "stage": "internal",
        "category": "unexpected_failure",
    }
    assert secret not in captured.err


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            XOAuthEndpointError(401, "invalid_client"),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "invalid_client",
                "http_status": 401,
            },
        ),
        (
            XOAuthEndpointError(400, "invalid_grant"),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "invalid_grant",
                "http_status": 400,
            },
        ),
        (
            XOAuthEndpointError(400, "invalid_scope"),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "invalid_scope",
                "http_status": 400,
            },
        ),
        (
            XOAuthEndpointError(503),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "oauth_http_error",
                "http_status": 503,
            },
        ),
        (
            XTokenRefreshTransportError("secret transport detail"),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "transport_failure",
            },
        ),
        (
            XTokenRefreshResponseError("secret response detail"),
            {
                "result": "verification_failed",
                "stage": "oauth_refresh",
                "category": "invalid_token_response",
            },
        ),
        (
            XTokenSecretStorageError("secret storage detail"),
            {
                "result": "verification_failed",
                "stage": "token_persistence",
                "category": "secret_store_failure",
            },
        ),
        (
            XTokenAuthorityPersistenceError("projects/p/secrets/s/versions/2"),
            {
                "result": "verification_failed",
                "stage": "token_authority",
                "category": "authority_failure",
            },
        ),
        (
            XIdentityMismatchError("secret identity detail"),
            {
                "result": "verification_failed",
                "stage": "identity",
                "category": "identity_mismatch",
            },
        ),
    ],
)
def test_failure_diagnostics_are_allowlisted_and_stage_specific(
    error: Exception,
    expected: dict[str, str | int],
) -> None:
    payload = diagnose_verification_failure(error).as_payload()

    assert payload == expected
    assert set(payload) <= {"result", "stage", "category", "http_status"}
    assert "detail" not in json.dumps(payload)


def test_cli_emits_only_allowlisted_typed_failure_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "credential-value-that-must-not-escape"

    class FailingVerifier:
        def verify(self) -> XOAuthIdentityVerificationResult:
            raise XOAuthEndpointError(400, "invalid_grant") from RuntimeError(secret)

    monkeypatch.setattr(
        cli_module,
        "create_cloud_oauth_identity_verifier",
        lambda: FailingVerifier(),
    )

    assert cli_module.main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload == {
        "result": "verification_failed",
        "stage": "oauth_refresh",
        "category": "invalid_grant",
        "http_status": 400,
    }
    assert secret not in captured.err


def test_unrecognized_provider_error_cannot_become_a_diagnostic_category() -> None:
    payload = diagnose_verification_failure(
        XOAuthEndpointError(400, "provider_error_containing_sensitive_detail")
    ).as_payload()

    assert payload == {
        "result": "verification_failed",
        "stage": "oauth_refresh",
        "category": "oauth_http_error",
        "http_status": 400,
    }


@pytest.mark.parametrize("status", [True, 0, 99, 600, "400"])
def test_oauth_endpoint_diagnostic_requires_numeric_http_status(status: object) -> None:
    with pytest.raises(ValueError, match="HTTP integer"):
        XOAuthEndpointError(status)  # type: ignore[arg-type]


def test_cli_success_outputs_only_non_secret_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class SuccessfulVerifier:
        def verify(self) -> XOAuthIdentityVerificationResult:
            return XOAuthIdentityVerificationResult(EXPECTED_USER_ID)

    monkeypatch.setattr(
        cli_module,
        "create_cloud_oauth_identity_verifier",
        lambda: SuccessfulVerifier(),
    )

    assert cli_module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "result": "identity_verified",
        "x_user_id": EXPECTED_USER_ID,
    }
