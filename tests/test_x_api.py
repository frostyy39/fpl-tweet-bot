import json
from collections.abc import Iterable
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import fpl_bot.x_api as x_api_module
from fpl_bot.x_api import UrllibXHttpTransport, XApiClient, XHttpRequest, XHttpResponse
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import (
    XAmbiguousWriteError,
    XApiResponseError,
    XAuthenticationError,
    XConfigurationError,
    XIdentityMismatchError,
    XPermissionError,
    XRateLimitError,
    XRequestRejectedError,
    XResponseValidationError,
    XTransportError,
)

EXPECTED_USER_ID = "123456789"
POST_ID = "987654321"
ACCESS_TOKEN_PLACEHOLDER = "unit-test-token-placeholder"


class FakeTransport:
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


class RedirectingFakeOpener:
    def __init__(self, redirect_handler: object) -> None:
        self.redirect_handler = redirect_handler
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> None:
        assert timeout == 10.0
        self.requests.append(request)
        redirect_url = "https://redirect.invalid/capture"
        redirected_request = self.redirect_handler.redirect_request(
            request,
            BytesIO(),
            302,
            "Found",
            {"Location": redirect_url},
            redirect_url,
        )
        if redirected_request is not None:
            self.requests.append(redirected_request)
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": redirect_url},
            BytesIO(b"redirect refused"),
        )


def json_response(status_code: int, payload: object) -> XHttpResponse:
    return XHttpResponse(status_code, json.dumps(payload).encode())


def authenticated_user_response(
    *, user_id: str = EXPECTED_USER_ID, username: str = "fpl_test_bot"
) -> XHttpResponse:
    return json_response(200, {"data": {"id": user_id, "username": username}})


def write_enabled_config(
    *,
    expected_user_id: str | None = EXPECTED_USER_ID,
    posting_enabled: bool = True,
    token: str | None = ACCESS_TOKEN_PLACEHOLDER,
) -> XPostingConfig:
    return XPostingConfig(
        environment="test",
        posting_enabled=posting_enabled,
        expected_user_id=expected_user_id,
        user_access_token=token,
    )


def test_urllib_transport_refuses_redirect_without_forwarding_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: list[object] = []
    fake_opener: RedirectingFakeOpener | None = None

    def fake_build_opener(*handlers: object) -> RedirectingFakeOpener:
        nonlocal fake_opener
        captured_handlers.extend(handlers)
        fake_opener = RedirectingFakeOpener(handlers[0])
        return fake_opener

    monkeypatch.setattr(x_api_module, "build_opener", fake_build_opener)
    transport = UrllibXHttpTransport()
    request = XHttpRequest(
        method="GET",
        url="https://api.x.com/2/users/me",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN_PLACEHOLDER}"},
    )

    response = transport.send(request, timeout_seconds=10.0)

    assert len(captured_handlers) == 1
    assert isinstance(captured_handlers[0], x_api_module._NoRedirectHandler)
    assert response.status_code == 302
    assert fake_opener is not None
    assert len(fake_opener.requests) == 1
    assert fake_opener.requests[0].full_url == "https://api.x.com/2/users/me"
    assert fake_opener.requests[0].get_header("Authorization") == (
        f"Bearer {ACCESS_TOKEN_PLACEHOLDER}"
    )


def test_authenticated_user_response_is_parsed() -> None:
    transport = FakeTransport([authenticated_user_response()])
    client = XApiClient(write_enabled_config(), transport=transport)

    user = client.get_authenticated_user()

    assert user.user_id == EXPECTED_USER_ID
    assert user.username == "fpl_test_bot"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.x.com/2/users/me"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN_PLACEHOLDER}"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"id": "not-numeric", "username": "fpl_test_bot"}},
        {"data": {"id": EXPECTED_USER_ID, "username": "invalid-username"}},
        {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}, "errors": [{}]},
    ],
)
def test_malformed_authenticated_user_response_is_rejected(payload: object) -> None:
    transport = FakeTransport([json_response(200, payload)])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XResponseValidationError):
        client.get_authenticated_user()


def test_expected_account_id_match_allows_post_creation() -> None:
    message = "FPL Bot API integration test — TEST ACCOUNT ONLY — 2026-09-01T12:00:00Z"
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(201, {"data": {"id": POST_ID, "text": message}}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    created = client.create_text_post(message)

    assert created.post_id == POST_ID
    assert created.text == message
    assert len(transport.requests) == 2


def test_test_message_passes_through_exactly() -> None:
    message = "FPL Bot API integration test — TEST ACCOUNT ONLY — 2026-09-01T12:00:00Z"
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(201, {"data": {"id": POST_ID, "text": message}}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    client.create_text_post(message)

    post_request = transport.requests[1]
    assert post_request.method == "POST"
    assert post_request.url == "https://api.x.com/2/tweets"
    assert json.loads(post_request.body or b"") == {"text": message}


def test_expected_account_id_mismatch_blocks_posting() -> None:
    transport = FakeTransport([authenticated_user_response(user_id="222222222")])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XIdentityMismatchError, match="posting prohibited"):
        client.create_text_post("test only")

    assert [request.method for request in transport.requests] == ["GET"]


def test_missing_expected_account_id_blocks_posting_before_network_request() -> None:
    transport = FakeTransport([])
    client = XApiClient(write_enabled_config(expected_user_id=None), transport=transport)

    with pytest.raises(XConfigurationError, match="X_EXPECTED_USER_ID is required"):
        client.create_text_post("test only")

    assert transport.requests == []


def test_posting_disabled_blocks_posting_before_network_request() -> None:
    transport = FakeTransport([])
    client = XApiClient(write_enabled_config(posting_enabled=False), transport=transport)

    with pytest.raises(XConfigurationError, match="posting is disabled"):
        client.create_text_post("test only")

    assert transport.requests == []


def test_missing_user_access_token_blocks_posting_before_network_request() -> None:
    transport = FakeTransport([])
    client = XApiClient(write_enabled_config(token=None), transport=transport)

    with pytest.raises(XConfigurationError, match="X_USER_ACCESS_TOKEN is required"):
        client.create_text_post("test only")

    assert transport.requests == []


def test_successful_post_creation_extracts_post_id() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(201, {"data": {"id": POST_ID, "text": "test only"}}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    created = client.create_text_post("test only")

    assert created.post_id == POST_ID


def test_malformed_success_response_is_ambiguous_and_not_retried() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(201, {"data": {"text": "test only"}}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XAmbiguousWriteError, match="without a fully validated"):
        client.create_text_post("test only")

    assert len(transport.requests) == 2


def test_success_response_with_different_text_is_ambiguous_and_not_retried() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(201, {"data": {"id": POST_ID, "text": "different text"}}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XAmbiguousWriteError, match="without a fully validated"):
        client.create_text_post("test only")

    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, XAuthenticationError),
        (403, XPermissionError),
        (429, XRateLimitError),
    ],
)
def test_typed_authenticated_user_failures(status_code: int, error_type: type[Exception]) -> None:
    transport = FakeTransport([json_response(status_code, {"title": "request rejected"})])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(error_type):
        client.get_authenticated_user()


def test_read_api_server_failure_is_typed() -> None:
    transport = FakeTransport([json_response(503, {"title": "unavailable"})])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XApiResponseError) as error:
        client.get_authenticated_user()

    assert error.value.status_code == 503


def test_read_api_redirect_is_typed_api_failure() -> None:
    transport = FakeTransport([XHttpResponse(302, b"")])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XApiResponseError) as error:
        client.get_authenticated_user()

    assert error.value.status_code == 302
    assert len(transport.requests) == 1


def test_definite_rejected_create_post_request_is_not_retried() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(400, {"title": "invalid request"}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XRequestRejectedError) as error:
        client.create_text_post("test only")

    assert error.value.status_code == 400
    assert len(transport.requests) == 2


def test_create_post_permission_failure_is_typed_and_not_retried() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(403, {"title": "write permission required"}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XPermissionError):
        client.create_text_post("test only")

    assert len(transport.requests) == 2


def test_ambiguous_network_write_failure_is_distinct_and_not_retried() -> None:
    transport = FakeTransport([authenticated_user_response(), XTransportError("connection lost")])
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XAmbiguousWriteError, match="must not be retried automatically"):
        client.create_text_post("test only")

    assert len(transport.requests) == 2


def test_create_post_server_failure_is_ambiguous_and_not_retried() -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            json_response(503, {"title": "unavailable"}),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XAmbiguousWriteError, match="outcome may be ambiguous"):
        client.create_text_post("test only")

    assert len(transport.requests) == 2


@pytest.mark.parametrize("status_code", [302, 307, 408, 409, 418])
def test_uncertain_create_post_status_is_ambiguous_and_not_retried(
    status_code: int,
) -> None:
    transport = FakeTransport(
        [
            authenticated_user_response(),
            XHttpResponse(status_code, b""),
        ]
    )
    client = XApiClient(write_enabled_config(), transport=transport)

    with pytest.raises(XAmbiguousWriteError, match=f"HTTP {status_code}"):
        client.create_text_post("test only")

    assert len(transport.requests) == 2
