import json
from io import StringIO

from fpl_bot.test_post import TEST_POST_TEXT, main
from fpl_bot.x_api import CreatedXPost, XApiClient, XHttpRequest, XHttpResponse
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import XAuthenticationError

EXPECTED_USER_ID = "123456789"
POST_ID = "987654321"
ACCESS_TOKEN_PLACEHOLDER = "unit-test-access-token-placeholder"


class RecordingClient:
    def __init__(self, config: XPostingConfig) -> None:
        self.config = config
        self.messages: list[str] = []

    def create_text_post(self, text: str) -> CreatedXPost:
        self.messages.append(text)
        return CreatedXPost(post_id=POST_ID, text=text)


class NeverCreateClient:
    def __init__(self, config: XPostingConfig) -> None:
        raise AssertionError("X client must not be created")


class FakeTransport:
    def __init__(self, responses: list[XHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[XHttpRequest] = []

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        assert timeout_seconds == 10.0
        self.requests.append(request)
        return self.responses.pop(0)


def live_environment() -> dict[str, str]:
    return {
        "X_ENVIRONMENT": "test",
        "X_POSTING_ENABLED": "true",
        "X_EXPECTED_USER_ID": EXPECTED_USER_ID,
        "X_USER_ACCESS_TOKEN": ACCESS_TOKEN_PLACEHOLDER,
    }


def json_response(status_code: int, payload: object) -> XHttpResponse:
    return XHttpResponse(status_code=status_code, body=json.dumps(payload).encode())


def test_default_dry_run_does_not_create_x_client() -> None:
    stdout = StringIO()

    exit_code = main(
        [], environ=live_environment(), client_factory=NeverCreateClient, stdout=stdout
    )

    assert exit_code == 0
    assert "Dry run only; no X API request was made." in stdout.getvalue()
    assert TEST_POST_TEXT in stdout.getvalue()


def test_live_mode_with_missing_access_token_fails_before_client_creation() -> None:
    environ = live_environment()
    del environ["X_USER_ACCESS_TOKEN"]
    stderr = StringIO()

    exit_code = main(
        ["--live"],
        environ=environ,
        client_factory=NeverCreateClient,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "X_USER_ACCESS_TOKEN is required" in stderr.getvalue()


def test_live_flag_alone_does_not_enable_posting() -> None:
    stderr = StringIO()

    exit_code = main(
        ["--live"],
        environ={},
        client_factory=NeverCreateClient,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "X posting is disabled" in stderr.getvalue()


def test_explicit_live_mode_invokes_posting_boundary_exactly_once() -> None:
    clients: list[RecordingClient] = []

    def client_factory(config: XPostingConfig) -> RecordingClient:
        client = RecordingClient(config)
        clients.append(client)
        return client

    exit_code = main(["--live"], environ=live_environment(), client_factory=client_factory)

    assert exit_code == 0
    assert len(clients) == 1
    assert clients[0].messages == [TEST_POST_TEXT]


def test_successful_live_response_reports_post_id() -> None:
    stdout = StringIO()
    transport = FakeTransport(
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(201, {"data": {"id": POST_ID, "text": TEST_POST_TEXT}}),
        ]
    )

    exit_code = main(
        ["--live"],
        environ=live_environment(),
        client_factory=lambda config: XApiClient(config, transport=transport),
        stdout=stdout,
    )

    assert exit_code == 0
    assert stdout.getvalue().splitlines() == [
        "Controlled X test Post created.",
        f"X Post ID: {POST_ID}",
    ]
    assert [request.method for request in transport.requests] == ["GET", "POST"]


def test_expected_user_mismatch_fails_closed_before_create_post() -> None:
    stderr = StringIO()
    transport = FakeTransport(
        [
            json_response(
                200,
                {"data": {"id": "222222222", "username": "unexpected_user"}},
            )
        ]
    )

    exit_code = main(
        ["--live"],
        environ=live_environment(),
        client_factory=lambda config: XApiClient(config, transport=transport),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "posting prohibited" in stderr.getvalue()
    assert [request.method for request in transport.requests] == ["GET"]


def test_malformed_success_fails_as_ambiguous_without_retry() -> None:
    stderr = StringIO()
    transport = FakeTransport(
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(201, {"data": {"text": TEST_POST_TEXT}}),
        ]
    )

    exit_code = main(
        ["--live"],
        environ=live_environment(),
        client_factory=lambda config: XApiClient(config, transport=transport),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "without a fully validated create-Post response" in stderr.getvalue()
    assert "do not retry automatically" in stderr.getvalue()
    assert len(transport.requests) == 2


def test_unsuccessful_api_response_fails_clearly_without_retry() -> None:
    stderr = StringIO()
    transport = FakeTransport(
        [
            json_response(
                200,
                {"data": {"id": EXPECTED_USER_ID, "username": "fpl_test_bot"}},
            ),
            json_response(403, {"title": "write permission required"}),
        ]
    )

    exit_code = main(
        ["--live"],
        environ=live_environment(),
        client_factory=lambda config: XApiClient(config, transport=transport),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "HTTP 403: write permission required" in stderr.getvalue()
    assert len(transport.requests) == 2


def test_secret_is_absent_from_live_failure_output() -> None:
    stderr = StringIO()

    class RejectingClient:
        def __init__(self, config: XPostingConfig) -> None:
            assert config.user_access_token == ACCESS_TOKEN_PLACEHOLDER

        def create_text_post(self, text: str) -> CreatedXPost:
            raise XAuthenticationError("X rejected the credentials with HTTP 401", 401)

    exit_code = main(
        ["--live"],
        environ=live_environment(),
        client_factory=RejectingClient,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "HTTP 401" in stderr.getvalue()
    assert ACCESS_TOKEN_PLACEHOLDER not in stderr.getvalue()


def test_cli_test_suite_cannot_post_without_live_flag() -> None:
    exit_code = main([], environ={}, client_factory=NeverCreateClient)

    assert exit_code == 0
