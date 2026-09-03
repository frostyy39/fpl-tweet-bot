import base64
import ctypes
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import fpl_bot.windows_dpapi as dpapi_module
import fpl_bot.x_oauth as x_oauth_module
from fpl_bot.windows_dpapi import CRYPTPROTECT_UI_FORBIDDEN, WindowsDpapiProtector
from fpl_bot.x_api import AuthenticatedXUser, XHttpRequest, XHttpResponse
from fpl_bot.x_errors import (
    XIdentityMismatchError,
    XOAuthCallbackError,
    XOAuthConfigurationError,
    XOAuthHandoffError,
    XOAuthTokenExchangeError,
    XResponseValidationError,
    XTransportError,
)
from fpl_bot.x_oauth import (
    DPAPI_FILE_MAGIC,
    OAUTH_CALLBACK_HOST,
    OAUTH_CALLBACK_PATH,
    OAUTH_CALLBACK_PORT,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    X_AUTHORIZE_URL,
    X_TOKEN_URL,
    AuthorizationCode,
    ExclusiveTokenFileHandoff,
    LocalOAuthConfig,
    OAuthAttempt,
    OAuthClientCredentials,
    OAuthTokenBundle,
    XOAuthTokenClient,
    authorize_test_account,
    build_authorization_url,
    generate_oauth_attempt,
    parse_callback_target,
)
from fpl_bot.x_oauth_callback import LoopbackOAuthCallbackReceiver
from fpl_bot.x_oauth_cli import render_authorization_success

CLIENT_ID_PLACEHOLDER = "client-id-placeholder"
CLIENT_SECRET_PLACEHOLDER = "client-secret-placeholder"
ACCESS_TOKEN_PLACEHOLDER = "access-token-placeholder"
REFRESH_TOKEN_PLACEHOLDER = "refresh-token-placeholder"
AUTHORIZATION_CODE_PLACEHOLDER = "authorization-code-placeholder"
FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


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


class FakeReceiver:
    def __init__(self, code: str = AUTHORIZATION_CODE_PLACEHOLDER) -> None:
        self.code = code
        self.expected_state: str | None = None
        self.browser_opened = False

    def receive(
        self,
        expected_state: str,
        on_listening: Callable[[], None],
    ) -> AuthorizationCode:
        self.expected_state = expected_state
        on_listening()
        self.browser_opened = True
        return AuthorizationCode(self.code)


class RecordingHandoff:
    def __init__(self) -> None:
        self.calls: list[tuple[OAuthTokenBundle, AuthenticatedXUser]] = []

    def store(self, tokens: OAuthTokenBundle, user: AuthenticatedXUser) -> None:
        self.calls.append((tokens, user))


class DeterministicProtector:
    def __init__(self) -> None:
        self.plaintext_calls: list[bytes] = []

    def protect(self, plaintext: bytes) -> bytes:
        self.plaintext_calls.append(plaintext)
        return b"encrypted-placeholder:" + hashlib.sha256(plaintext).digest()


class FailingProtector:
    def protect(self, plaintext: bytes) -> bytes:
        raise RuntimeError(f"must not escape: {ACCESS_TOKEN_PLACEHOLDER}")


class FakeNativeFunction:
    def __init__(self, callback: Callable[..., object]) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self.callback(*args)


class DeniedReceiver:
    def receive(
        self,
        expected_state: str,
        on_listening: Callable[[], None],
    ) -> AuthorizationCode:
        on_listening()
        raise XOAuthCallbackError("X authorization was denied or failed")


def deterministic_entropy(length: int) -> bytes:
    return bytes(index % 256 for index in range(length))


def token_response_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "token_type": "bearer",
        "expires_in": 7200,
        "access_token": ACCESS_TOKEN_PLACEHOLDER,
        "scope": " ".join(OAUTH_SCOPES),
        "refresh_token": REFRESH_TOKEN_PLACEHOLDER,
    }
    payload.update(overrides)
    return payload


def json_response(status_code: int, payload: object) -> XHttpResponse:
    return XHttpResponse(status_code, json.dumps(payload).encode())


def fixed_attempt() -> OAuthAttempt:
    return generate_oauth_attempt(deterministic_entropy)


def fixed_credentials() -> OAuthClientCredentials:
    return OAuthClientCredentials(CLIENT_ID_PLACEHOLDER, CLIENT_SECRET_PLACEHOLDER)


def fixed_tokens() -> OAuthTokenBundle:
    return OAuthTokenBundle(
        access_token=ACCESS_TOKEN_PLACEHOLDER,
        refresh_token=REFRESH_TOKEN_PLACEHOLDER,
        token_type="bearer",
        scopes=OAUTH_SCOPES,
        expires_in_seconds=7200,
        received_at_utc=FIXED_NOW,
    )


def test_local_config_requires_environment_secrets_and_absolute_handoff_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(XOAuthConfigurationError, match="X_OAUTH_CLIENT_SECRET"):
        LocalOAuthConfig.from_environment(
            {
                "X_OAUTH_CLIENT_ID": CLIENT_ID_PLACEHOLDER,
                "X_OAUTH_TOKEN_OUTPUT_FILE": str(tmp_path / "tokens.json"),
            }
        )

    with pytest.raises(XOAuthConfigurationError, match="absolute path"):
        LocalOAuthConfig.from_environment(
            {
                "X_OAUTH_CLIENT_ID": CLIENT_ID_PLACEHOLDER,
                "X_OAUTH_CLIENT_SECRET": CLIENT_SECRET_PLACEHOLDER,
                "X_OAUTH_TOKEN_OUTPUT_FILE": "tokens.json",
            }
        )


def test_local_config_validates_optional_expected_reauthorization_identity(
    tmp_path: Path,
) -> None:
    environment = {
        "X_OAUTH_CLIENT_ID": CLIENT_ID_PLACEHOLDER,
        "X_OAUTH_CLIENT_SECRET": CLIENT_SECRET_PLACEHOLDER,
        "X_OAUTH_TOKEN_OUTPUT_FILE": str(tmp_path / "tokens.dpapi"),
        "X_EXPECTED_USER_ID": "123456789",
    }

    config = LocalOAuthConfig.from_environment(environment)

    assert config.expected_user_id == "123456789"
    for invalid in ("", "0", "-1", "not-an-id"):
        environment["X_EXPECTED_USER_ID"] = invalid
        with pytest.raises(XOAuthConfigurationError, match="positive numeric"):
            LocalOAuthConfig.from_environment(environment)


def test_pkce_and_state_generation_are_deterministic_with_injected_entropy() -> None:
    attempt = fixed_attempt()

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", attempt.state)
    assert re.fullmatch(r"[A-Za-z0-9_-]{86}", attempt.code_verifier)
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(attempt.code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert attempt.code_challenge == expected_challenge


def test_default_state_and_verifier_are_cryptographically_random_per_attempt() -> None:
    first = generate_oauth_attempt()
    second = generate_oauth_attempt()

    assert first.state != second.state
    assert first.code_verifier != second.code_verifier


def test_authorization_url_contains_exact_callback_scopes_and_s256_parameters() -> None:
    attempt = fixed_attempt()
    parsed = urlsplit(build_authorization_url(CLIENT_ID_PLACEHOLDER, attempt))
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == X_AUTHORIZE_URL
    assert query == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID_PLACEHOLDER],
        "redirect_uri": [OAUTH_REDIRECT_URI],
        "scope": ["tweet.read users.read tweet.write offline.access"],
        "state": [attempt.state],
        "code_challenge": [attempt.code_challenge],
        "code_challenge_method": ["S256"],
    }


def test_callback_accepts_exact_path_code_and_state() -> None:
    callback = parse_callback_target(
        f"{OAUTH_CALLBACK_PATH}?code={AUTHORIZATION_CODE_PLACEHOLDER}&state=expected-state",
        "expected-state",
    )

    assert callback.value == AUTHORIZATION_CODE_PLACEHOLDER


def test_wrong_callback_state_is_rejected() -> None:
    with pytest.raises(XOAuthCallbackError, match="state did not match"):
        parse_callback_target(
            f"{OAUTH_CALLBACK_PATH}?code={AUTHORIZATION_CODE_PLACEHOLDER}&state=wrong-state",
            "expected-state",
        )


def test_oauth_denial_callback_is_handled_without_echoing_description() -> None:
    target = (
        f"{OAUTH_CALLBACK_PATH}?error=access_denied&state=expected-state"
        "&error_description=sensitive-provider-text"
    )

    with pytest.raises(XOAuthCallbackError) as error:
        parse_callback_target(target, "expected-state")

    assert "denied or failed" in str(error.value)
    assert "sensitive-provider-text" not in str(error.value)


@pytest.mark.parametrize(
    "target",
    [
        "/wrong?code=code&state=expected-state",
        f"{OAUTH_CALLBACK_PATH}?state=expected-state",
        f"{OAUTH_CALLBACK_PATH}?code=one&code=two&state=expected-state",
        f"{OAUTH_CALLBACK_PATH}?code=code&state=expected-state&state=expected-state",
    ],
)
def test_malformed_or_wrong_path_callback_is_rejected(target: str) -> None:
    with pytest.raises(XOAuthCallbackError):
        parse_callback_target(target, "expected-state")


def test_loopback_receiver_binds_only_configured_interface_and_port() -> None:
    captured_address: tuple[str, int] | None = None
    browser_called = False

    class TimeoutServer:
        timeout = 0.0

        def __init__(self, address: tuple[str, int], handler: object) -> None:
            nonlocal captured_address
            captured_address = address

        def handle_request(self) -> None:
            raise AssertionError("Timeout should occur before handling a request")

        def server_close(self) -> None:
            return

    times = iter((0.0, 2.0))
    receiver = LoopbackOAuthCallbackReceiver(
        timeout_seconds=1.0,
        server_factory=TimeoutServer,
        monotonic=lambda: next(times),
    )

    def on_listening() -> None:
        nonlocal browser_called
        browser_called = True

    with pytest.raises(XOAuthCallbackError, match="Timed out"):
        receiver.receive("expected-state", on_listening)

    assert captured_address == (OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT)
    assert captured_address == ("127.0.0.1", 8765)
    assert browser_called is True


def test_confidential_token_exchange_uses_basic_auth_and_exact_form_body() -> None:
    transport = FakeTransport([json_response(200, token_response_payload())])
    client = XOAuthTokenClient(transport=transport, now=lambda: FIXED_NOW)

    tokens = client.exchange_authorization_code(
        AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER),
        fixed_attempt(),
        fixed_credentials(),
    )

    assert tokens.access_token == ACCESS_TOKEN_PLACEHOLDER
    assert tokens.refresh_token == REFRESH_TOKEN_PLACEHOLDER
    assert tokens.received_at_utc == FIXED_NOW
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == X_TOKEN_URL
    expected_basic = base64.b64encode(
        f"{CLIENT_ID_PLACEHOLDER}:{CLIENT_SECRET_PLACEHOLDER}".encode()
    ).decode()
    assert request.headers["Authorization"] == f"Basic {expected_basic}"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs((request.body or b"").decode()) == {
        "code": [AUTHORIZATION_CODE_PLACEHOLDER],
        "grant_type": ["authorization_code"],
        "redirect_uri": [OAUTH_REDIRECT_URI],
        "code_verifier": [fixed_attempt().code_verifier],
    }
    assert "client_id" not in parse_qs((request.body or b"").decode())


@pytest.mark.parametrize(
    "payload",
    [
        token_response_payload(access_token=None),
        token_response_payload(refresh_token=None),
        token_response_payload(token_type="not-bearer"),
        token_response_payload(scope="tweet.read users.read tweet.write"),
        token_response_payload(scope="tweet.read users.read tweet.write offline.access tweet.read"),
        token_response_payload(expires_in=0),
        token_response_payload(expires_in=True),
        [],
    ],
)
def test_malformed_token_exchange_response_is_rejected(payload: object) -> None:
    transport = FakeTransport([json_response(200, payload)])
    client = XOAuthTokenClient(transport=transport, now=lambda: FIXED_NOW)

    with pytest.raises(XOAuthTokenExchangeError):
        client.exchange_authorization_code(
            AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER),
            fixed_attempt(),
            fixed_credentials(),
        )


def test_invalid_json_token_response_is_rejected() -> None:
    transport = FakeTransport([XHttpResponse(200, b"not-json")])

    with pytest.raises(XOAuthTokenExchangeError, match="not valid JSON"):
        XOAuthTokenClient(transport=transport).exchange_authorization_code(
            AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER),
            fixed_attempt(),
            fixed_credentials(),
        )


def test_token_exchange_api_error_does_not_expose_response_body() -> None:
    transport = FakeTransport(
        [XHttpResponse(401, b"client-secret-placeholder refresh-token-placeholder")]
    )

    with pytest.raises(XOAuthTokenExchangeError) as error:
        XOAuthTokenClient(transport=transport).exchange_authorization_code(
            AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER),
            fixed_attempt(),
            fixed_credentials(),
        )

    assert "HTTP 401" in str(error.value)
    assert CLIENT_SECRET_PLACEHOLDER not in str(error.value)
    assert REFRESH_TOKEN_PLACEHOLDER not in str(error.value)


def test_token_exchange_network_failure_is_not_retried() -> None:
    transport = FakeTransport([XTransportError("network failed")])

    with pytest.raises(XOAuthTokenExchangeError, match="restart authorization"):
        XOAuthTokenClient(transport=transport).exchange_authorization_code(
            AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER),
            fixed_attempt(),
            fixed_credentials(),
        )

    assert len(transport.requests) == 1


def test_secret_bearing_objects_redact_values_from_repr(tmp_path: Path) -> None:
    config = LocalOAuthConfig.from_environment(
        {
            "X_OAUTH_CLIENT_ID": CLIENT_ID_PLACEHOLDER,
            "X_OAUTH_CLIENT_SECRET": CLIENT_SECRET_PLACEHOLDER,
            "X_OAUTH_TOKEN_OUTPUT_FILE": str(tmp_path / "tokens.json"),
        }
    )
    attempt = fixed_attempt()
    code = AuthorizationCode(AUTHORIZATION_CODE_PLACEHOLDER)
    tokens = fixed_tokens()
    request = XHttpRequest(
        "POST",
        X_TOKEN_URL,
        {"Authorization": f"Basic {CLIENT_SECRET_PLACEHOLDER}"},
        AUTHORIZATION_CODE_PLACEHOLDER.encode(),
    )
    response = XHttpResponse(200, ACCESS_TOKEN_PLACEHOLDER.encode())

    combined_repr = " ".join(map(repr, (config, attempt, code, tokens, request, response)))
    for secret_value in (
        CLIENT_ID_PLACEHOLDER,
        CLIENT_SECRET_PLACEHOLDER,
        attempt.state,
        attempt.code_verifier,
        attempt.code_challenge,
        AUTHORIZATION_CODE_PLACEHOLDER,
        ACCESS_TOKEN_PLACEHOLDER,
        REFRESH_TOKEN_PLACEHOLDER,
    ):
        assert secret_value not in combined_repr


def test_external_token_handoff_is_exclusive_and_contains_verified_identity(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = tmp_path / "test-account-oauth.json"
    handoff = ExclusiveTokenFileHandoff(
        target,
        repository_root=repository_root,
        platform_name="posix",
    )
    user = AuthenticatedXUser("123456789", "fpl_test_bot")

    handoff.store(fixed_tokens(), user)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["access_token"] == ACCESS_TOKEN_PLACEHOLDER
    assert payload["refresh_token"] == REFRESH_TOKEN_PLACEHOLDER
    assert payload["x_user_id"] == "123456789"
    assert payload["x_username"] == "fpl_test_bot"
    assert payload["received_at_utc"] == FIXED_NOW.isoformat()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(XOAuthHandoffError, match="already exists"):
        handoff.store(fixed_tokens(), user)


def test_windows_token_handoff_writes_only_dpapi_ciphertext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = tmp_path / "test-account-oauth.dpapi"
    protector = DeterministicProtector()
    monkeypatch.setattr(x_oauth_module, "WindowsDpapiProtector", lambda: protector)
    handoff = ExclusiveTokenFileHandoff(
        target,
        repository_root=repository_root,
        platform_name="nt",
    )

    handoff.store(
        fixed_tokens(),
        AuthenticatedXUser("123456789", "fpl_test_bot"),
    )

    stored = target.read_bytes()
    assert stored.startswith(DPAPI_FILE_MAGIC)
    assert (
        stored
        == DPAPI_FILE_MAGIC
        + b"encrypted-placeholder:"
        + hashlib.sha256(protector.plaintext_calls[0]).digest()
    )
    assert ACCESS_TOKEN_PLACEHOLDER.encode() not in stored
    assert REFRESH_TOKEN_PLACEHOLDER.encode() not in stored
    assert b"123456789" not in stored
    assert b"fpl_test_bot" not in stored
    transient_payload = json.loads(protector.plaintext_calls[0])
    assert transient_payload["access_token"] == ACCESS_TOKEN_PLACEHOLDER
    assert transient_payload["refresh_token"] == REFRESH_TOKEN_PLACEHOLDER
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [target]


def test_windows_dpapi_wrapper_uses_current_user_cryptprotectdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = b"placeholder-token-bundle"
    ciphertext = b"dpapi-ciphertext-placeholder"
    ciphertext_buffer = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
    observed: dict[str, object] = {}

    def crypt_protect_data(*args: object) -> int:
        input_blob = ctypes.cast(args[0], ctypes.POINTER(dpapi_module._DataBlob)).contents
        output_blob = ctypes.cast(args[6], ctypes.POINTER(dpapi_module._DataBlob)).contents
        observed["plaintext"] = ctypes.string_at(input_blob.pbData, input_blob.cbData)
        observed["description"] = args[1]
        observed["entropy"] = args[2]
        observed["flags"] = args[5]
        output_blob.cbData = len(ciphertext)
        output_blob.pbData = ctypes.cast(
            ciphertext_buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        return 1

    def local_free(pointer: object) -> None:
        observed["freed"] = bool(pointer)

    crypt32 = SimpleNamespace(CryptProtectData=FakeNativeFunction(crypt_protect_data))
    kernel32 = SimpleNamespace(LocalFree=FakeNativeFunction(local_free))

    def fake_windows_dll(name: str, *, use_last_error: bool) -> object:
        assert use_last_error is True
        return crypt32 if name == "crypt32" else kernel32

    monkeypatch.setattr(dpapi_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(dpapi_module.ctypes, "WinDLL", fake_windows_dll, raising=False)

    protected = WindowsDpapiProtector().protect(plaintext)

    assert protected == ciphertext
    assert observed["plaintext"] == plaintext
    assert observed["description"] == "FPL Bot local OAuth token handoff"
    assert observed["entropy"] is None
    assert observed["flags"] == CRYPTPROTECT_UI_FORBIDDEN
    assert observed["freed"] is True


def test_windows_token_handoff_refuses_existing_file_before_encryption(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = tmp_path / "existing.dpapi"
    target.write_bytes(b"preserve-existing-ciphertext")
    protector = DeterministicProtector()

    with pytest.raises(XOAuthHandoffError, match="already exists"):
        ExclusiveTokenFileHandoff(
            target,
            repository_root=repository_root,
            platform_name="nt",
            windows_protector=protector,
        )

    assert target.read_bytes() == b"preserve-existing-ciphertext"
    assert protector.plaintext_calls == []


def test_windows_encryption_failure_creates_no_file_or_secret_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = tmp_path / "failed.dpapi"
    handoff = ExclusiveTokenFileHandoff(
        target,
        repository_root=repository_root,
        platform_name="nt",
        windows_protector=FailingProtector(),
    )

    with pytest.raises(XOAuthHandoffError) as error:
        handoff.store(
            fixed_tokens(),
            AuthenticatedXUser("123456789", "fpl_test_bot"),
        )

    assert "DPAPI could not protect" in str(error.value)
    assert ACCESS_TOKEN_PLACEHOLDER not in str(error.value)
    assert REFRESH_TOKEN_PLACEHOLDER not in str(error.value)
    assert not target.exists()
    assert capsys.readouterr() == ("", "")


def test_token_handoff_refuses_repository_path(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = repository_root / "tokens.json"

    with pytest.raises(XOAuthHandoffError, match="outside the repository"):
        ExclusiveTokenFileHandoff(target, repository_root=repository_root).store(
            fixed_tokens(),
            AuthenticatedXUser("123456789", "fpl_test_bot"),
        )

    assert not target.exists()


def test_authorization_verifies_user_without_posting_or_printing_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_transport = FakeTransport([json_response(200, token_response_payload())])
    identity_transport = FakeTransport(
        [json_response(200, {"data": {"id": "123456789", "username": "fpl_test_bot"}})]
    )
    receiver = FakeReceiver()
    handoff = RecordingHandoff()
    opened_urls: list[str] = []
    config = LocalOAuthConfig(
        fixed_credentials(),
        tmp_path / "unused-token-path.json",
    )

    user = authorize_test_account(
        config,
        receiver=receiver,
        handoff=handoff,
        browser_open=lambda url: not opened_urls.append(url),
        token_transport=token_transport,
        identity_transport=identity_transport,
        entropy=deterministic_entropy,
        now=lambda: FIXED_NOW,
    )

    assert user == AuthenticatedXUser("123456789", "fpl_test_bot")
    assert receiver.browser_opened is True
    assert len(opened_urls) == 1
    assert [request.method for request in token_transport.requests] == ["POST"]
    assert [request.method for request in identity_transport.requests] == ["GET"]
    assert identity_transport.requests[0].url == "https://api.x.com/2/users/me"
    assert all(
        request.url != "https://api.x.com/2/tweets" for request in identity_transport.requests
    )
    assert len(handoff.calls) == 1
    assert capsys.readouterr() == ("", "")


def test_reauthorization_expected_identity_mismatch_prevents_token_handoff(
    tmp_path: Path,
) -> None:
    token_transport = FakeTransport([json_response(200, token_response_payload())])
    identity_transport = FakeTransport(
        [json_response(200, {"data": {"id": "987654321", "username": "other_user"}})]
    )
    handoff = RecordingHandoff()
    config = LocalOAuthConfig(
        fixed_credentials(),
        tmp_path / "unused-token-path.json",
        expected_user_id="123456789",
    )

    with pytest.raises(XIdentityMismatchError, match="expected reauthorization account"):
        authorize_test_account(
            config,
            receiver=FakeReceiver(),
            handoff=handoff,
            browser_open=lambda url: True,
            token_transport=token_transport,
            identity_transport=identity_transport,
            entropy=deterministic_entropy,
            now=lambda: FIXED_NOW,
        )

    assert handoff.calls == []
    assert len(token_transport.requests) == 1
    assert len(identity_transport.requests) == 1


def test_invalid_authenticated_user_response_prevents_token_handoff(tmp_path: Path) -> None:
    token_transport = FakeTransport([json_response(200, token_response_payload())])
    identity_transport = FakeTransport([json_response(200, {"data": {"id": "bad"}})])
    handoff = RecordingHandoff()

    with pytest.raises(XResponseValidationError):
        authorize_test_account(
            LocalOAuthConfig(fixed_credentials(), tmp_path / "unused.json"),
            receiver=FakeReceiver(),
            handoff=handoff,
            browser_open=lambda url: True,
            token_transport=token_transport,
            identity_transport=identity_transport,
            entropy=deterministic_entropy,
            now=lambda: FIXED_NOW,
        )

    assert handoff.calls == []


def test_denied_callback_prevents_token_exchange_and_handoff(tmp_path: Path) -> None:
    token_transport = FakeTransport([])
    identity_transport = FakeTransport([])
    handoff = RecordingHandoff()

    with pytest.raises(XOAuthCallbackError, match="denied or failed"):
        authorize_test_account(
            LocalOAuthConfig(fixed_credentials(), tmp_path / "unused.json"),
            receiver=DeniedReceiver(),
            handoff=handoff,
            browser_open=lambda url: True,
            token_transport=token_transport,
            identity_transport=identity_transport,
            entropy=deterministic_entropy,
            now=lambda: FIXED_NOW,
        )

    assert token_transport.requests == []
    assert identity_transport.requests == []
    assert handoff.calls == []


def test_success_output_contains_only_identity_and_safety_status() -> None:
    rendered = render_authorization_success("123456789", "fpl_test_bot")

    assert "123456789" in rendered
    assert "fpl_test_bot" in rendered
    assert "Posting remains disabled" in rendered
    assert ACCESS_TOKEN_PLACEHOLDER not in rendered
    assert REFRESH_TOKEN_PLACEHOLDER not in rendered
    assert CLIENT_SECRET_PLACEHOLDER not in rendered
