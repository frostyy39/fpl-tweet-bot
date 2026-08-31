import base64
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest

from fpl_bot.x_api import XApiClient, XHttpRequest, XHttpResponse
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import (
    XAmbiguousWriteError,
    XIdentityMismatchError,
    XTokenConcurrencyError,
    XTokenRefreshError,
    XTokenStateError,
    XTokenStoreError,
    XTransportError,
)
from fpl_bot.x_oauth import (
    OAUTH_SCOPES,
    X_TOKEN_URL,
    OAuthClientCredentials,
    OAuthTokenBundle,
)
from fpl_bot.x_token_refresh import (
    DEFAULT_REFRESH_MARGIN,
    InMemoryXTokenStateStore,
    RefreshingXAccessTokenProvider,
    VersionedXTokenState,
    XOAuthRefreshClient,
    XOAuthTokenState,
)

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
EXPECTED_USER_ID = "123456789"
POST_ID = "987654321"
ACCESS_TOKEN = "unit-test-access-token-placeholder"
REFRESH_TOKEN = "unit-test-refresh-token-placeholder"
NEW_ACCESS_TOKEN = "unit-test-new-access-token-placeholder"
NEW_REFRESH_TOKEN = "unit-test-new-refresh-token-placeholder"
WINNER_ACCESS_TOKEN = "unit-test-winner-access-token-placeholder"
WINNER_REFRESH_TOKEN = "unit-test-winner-refresh-token-placeholder"
CLIENT_ID = "unit-test-client-id-placeholder"
CLIENT_SECRET = "unit-test-client-secret-placeholder"


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


class RecordingRefreshClient:
    def __init__(
        self,
        replacement: XOAuthTokenState | None = None,
        error: Exception | None = None,
    ) -> None:
        self.replacement = replacement or token_state(
            access_token=NEW_ACCESS_TOKEN,
            refresh_token=NEW_REFRESH_TOKEN,
            expires_at=NOW + timedelta(hours=2),
        )
        self.error = error
        self.calls: list[XOAuthTokenState] = []

    def refresh(
        self,
        current: XOAuthTokenState,
        credentials: OAuthClientCredentials,
    ) -> XOAuthTokenState:
        self.calls.append(current)
        assert credentials.client_id == CLIENT_ID
        assert credentials.client_secret == CLIENT_SECRET
        if self.error is not None:
            raise self.error
        return self.replacement


class FailingUpdateStore(InMemoryXTokenStateStore):
    def replace_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        raise RuntimeError("store write unavailable")


class RefreshRaceStore:
    """Install another instance's newer state when the stale writer performs CAS."""

    def __init__(self, original: XOAuthTokenState, winner: XOAuthTokenState) -> None:
        self.state = original
        self.revision = "1"
        self.winner = winner
        self.replace_calls = 0

    def read(self) -> VersionedXTokenState:
        return VersionedXTokenState(self.revision, self.state)

    def replace_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        self.replace_calls += 1
        assert expected_revision == "1"
        self.state = self.winner
        self.revision = "2"
        return False


def credentials() -> OAuthClientCredentials:
    return OAuthClientCredentials(CLIENT_ID, CLIENT_SECRET)


def token_state(
    *,
    access_token: str = ACCESS_TOKEN,
    refresh_token: str = REFRESH_TOKEN,
    expires_at: datetime = NOW + timedelta(hours=1),
    scopes: tuple[str, ...] = OAUTH_SCOPES,
) -> XOAuthTokenState:
    return XOAuthTokenState(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_utc=expires_at,
        scopes=scopes,
    )


def provider(
    state: XOAuthTokenState,
    refresh_client: RecordingRefreshClient,
    *,
    store: InMemoryXTokenStateStore | RefreshRaceStore | None = None,
) -> RefreshingXAccessTokenProvider:
    return RefreshingXAccessTokenProvider(
        store or InMemoryXTokenStateStore(state),
        refresh_client,
        credentials(),
        clock=lambda: NOW,
    )


def response(payload: object, status: int = 200) -> XHttpResponse:
    return XHttpResponse(status, json.dumps(payload).encode())


def refresh_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "access_token": NEW_ACCESS_TOKEN,
        "refresh_token": NEW_REFRESH_TOKEN,
        "token_type": "bearer",
        "expires_in": 7200,
        "scope": " ".join(OAUTH_SCOPES),
    }
    payload.update(overrides)
    return payload


def posting_config() -> XPostingConfig:
    return XPostingConfig(
        environment="test",
        posting_enabled=True,
        expected_user_id=EXPECTED_USER_ID,
    )


@pytest.mark.parametrize(
    "expires_at",
    [NOW + DEFAULT_REFRESH_MARGIN + timedelta(microseconds=1), NOW + timedelta(hours=1)],
)
def test_token_valid_beyond_margin_does_not_refresh(expires_at: datetime) -> None:
    refresh_client = RecordingRefreshClient()

    access_token = provider(
        token_state(expires_at=expires_at),
        refresh_client,
    ).get_valid_access_token()

    assert access_token == ACCESS_TOKEN
    assert refresh_client.calls == []


def test_existing_oauth_bootstrap_bundle_converts_to_absolute_utc_expiry() -> None:
    state = XOAuthTokenState.from_oauth_bundle(
        OAuthTokenBundle(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            token_type="bearer",
            scopes=OAUTH_SCOPES,
            expires_in_seconds=7200,
            received_at_utc=NOW,
        )
    )

    assert state.expires_at_utc == NOW + timedelta(hours=2)


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW - timedelta(seconds=1),
        NOW,
        NOW + DEFAULT_REFRESH_MARGIN - timedelta(microseconds=1),
        NOW + DEFAULT_REFRESH_MARGIN,
    ],
)
def test_expired_or_margin_token_refreshes_exactly_once(expires_at: datetime) -> None:
    refresh_client = RecordingRefreshClient()

    access_token = provider(
        token_state(expires_at=expires_at),
        refresh_client,
    ).get_valid_access_token()

    assert access_token == NEW_ACCESS_TOKEN
    assert len(refresh_client.calls) == 1


def test_refresh_margin_is_explicitly_configurable() -> None:
    refresh_client = RecordingRefreshClient()
    token_provider = RefreshingXAccessTokenProvider(
        InMemoryXTokenStateStore(token_state(expires_at=NOW + timedelta(seconds=30))),
        refresh_client,
        credentials(),
        clock=lambda: NOW,
        refresh_margin=timedelta(0),
    )

    assert token_provider.get_valid_access_token() == ACCESS_TOKEN
    assert refresh_client.calls == []


def test_refresh_request_uses_confidential_basic_auth_exact_form_and_endpoint() -> None:
    transport = FakeTransport([response(refresh_payload())])
    refreshed = XOAuthRefreshClient(transport=transport, now=lambda: NOW).refresh(
        token_state(expires_at=NOW),
        credentials(),
    )

    assert refreshed.access_token == NEW_ACCESS_TOKEN
    assert refreshed.refresh_token == NEW_REFRESH_TOKEN
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == X_TOKEN_URL
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected_basic}"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs((request.body or b"").decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": [REFRESH_TOKEN],
    }
    assert "client_id" not in parse_qs((request.body or b"").decode())


def test_invalid_confidential_client_credentials_fail_before_network() -> None:
    transport = FakeTransport([])

    with pytest.raises(XTokenRefreshError, match="client credentials"):
        XOAuthRefreshClient(transport=transport, now=lambda: NOW).refresh(
            token_state(expires_at=NOW),
            OAuthClientCredentials("", CLIENT_SECRET),
        )

    assert transport.requests == []


def test_refreshed_token_reaches_identity_verification_then_one_post() -> None:
    refresh_client = RecordingRefreshClient()
    token_provider = provider(token_state(expires_at=NOW), refresh_client)
    x_transport = FakeTransport(
        [
            response({"data": {"id": EXPECTED_USER_ID, "username": "FPLBotTest"}}),
            response({"data": {"id": POST_ID, "text": "test message"}}, status=201),
        ]
    )

    created = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=token_provider,
    ).create_text_post("test message")

    assert created.post_id == POST_ID
    assert len(refresh_client.calls) == 1
    assert [request.method for request in x_transport.requests] == ["GET", "POST"]
    assert all(
        request.headers["Authorization"] == f"Bearer {NEW_ACCESS_TOKEN}"
        for request in x_transport.requests
    )


def test_successful_refresh_alone_performs_no_x_api_or_post_request() -> None:
    refresh_transport = FakeTransport([response(refresh_payload())])
    token_provider = RefreshingXAccessTokenProvider(
        InMemoryXTokenStateStore(token_state(expires_at=NOW)),
        XOAuthRefreshClient(transport=refresh_transport, now=lambda: NOW),
        credentials(),
        clock=lambda: NOW,
    )

    assert token_provider.get_valid_access_token() == NEW_ACCESS_TOKEN
    assert [request.url for request in refresh_transport.requests] == [X_TOKEN_URL]


def test_refreshed_token_must_be_valid_beyond_configured_safety_margin() -> None:
    refresh_client = RecordingRefreshClient(
        token_state(
            access_token=NEW_ACCESS_TOKEN,
            refresh_token=NEW_REFRESH_TOKEN,
            expires_at=NOW + DEFAULT_REFRESH_MARGIN,
        )
    )

    with pytest.raises(XTokenRefreshError, match="insufficient usable lifetime"):
        provider(token_state(expires_at=NOW), refresh_client).get_valid_access_token()

    assert len(refresh_client.calls) == 1


def test_refresh_failure_prevents_identity_and_create_post_requests() -> None:
    refresh_client = RecordingRefreshClient(error=XTokenRefreshError("refresh rejected"))
    x_transport = FakeTransport([])
    client = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=provider(token_state(expires_at=NOW), refresh_client),
    )

    with pytest.raises(XTokenRefreshError):
        client.create_text_post("test message")

    assert x_transport.requests == []


@pytest.mark.parametrize(
    "outcome",
    [
        XHttpResponse(200, b"not-json"),
        response([]),
        response(refresh_payload(access_token=None)),
        response(refresh_payload(access_token="")),
        response(refresh_payload(expires_in=0)),
        response(refresh_payload(expires_in=-1)),
        response(refresh_payload(expires_in=True)),
        response(refresh_payload(scope="tweet.read users.read offline.access")),
        response(refresh_payload(token_type="mac")),
    ],
)
def test_malformed_or_insufficient_refresh_response_fails_closed(
    outcome: XHttpResponse,
) -> None:
    client = XOAuthRefreshClient(transport=FakeTransport([outcome]), now=lambda: NOW)

    with pytest.raises(XTokenRefreshError):
        client.refresh(token_state(expires_at=NOW), credentials())


def test_rotated_refresh_token_is_authoritative_and_persisted_before_return() -> None:
    store = InMemoryXTokenStateStore(token_state(expires_at=NOW))
    refresh_client = RecordingRefreshClient()
    token_provider = provider(store.read().state, refresh_client, store=store)

    assert token_provider.get_valid_access_token() == NEW_ACCESS_TOKEN
    authoritative = store.read().state
    assert authoritative.access_token == NEW_ACCESS_TOKEN
    assert authoritative.refresh_token == NEW_REFRESH_TOKEN


def test_omitted_replacement_refresh_token_retains_existing_token_per_oauth_contract() -> None:
    payload = refresh_payload()
    payload.pop("refresh_token")
    refreshed = XOAuthRefreshClient(
        transport=FakeTransport([response(payload)]),
        now=lambda: NOW,
    ).refresh(token_state(expires_at=NOW), credentials())

    assert refreshed.access_token == NEW_ACCESS_TOKEN
    assert refreshed.refresh_token == REFRESH_TOKEN


def test_omitted_scope_retains_previously_validated_scope_set() -> None:
    payload = refresh_payload()
    payload.pop("scope")
    current = token_state(expires_at=NOW)

    refreshed = XOAuthRefreshClient(
        transport=FakeTransport([response(payload)]),
        now=lambda: NOW,
    ).refresh(current, credentials())

    assert refreshed.scopes == current.scopes


def test_secret_values_are_absent_from_state_snapshot_request_response_and_errors() -> None:
    state = token_state()
    snapshot = VersionedXTokenState("1", state)
    request = XHttpRequest(
        "POST",
        X_TOKEN_URL,
        {"Authorization": f"Basic {CLIENT_SECRET}"},
        REFRESH_TOKEN.encode(),
    )
    token_response = XHttpResponse(401, REFRESH_TOKEN.encode())
    transport = FakeTransport([token_response])

    with pytest.raises(XTokenRefreshError) as captured:
        XOAuthRefreshClient(transport=transport, now=lambda: NOW).refresh(state, credentials())

    rendered = " ".join(map(repr, (state, snapshot, request, token_response, captured.value)))
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET):
        assert secret not in rendered
        assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime(2026, 8, 29, 10, 0),
        datetime(2026, 8, 29, 11, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_token_expiry_requires_timezone_aware_utc(invalid_time: datetime) -> None:
    with pytest.raises(XTokenStateError, match="timezone-aware UTC"):
        token_state(expires_at=invalid_time)


def test_store_update_failure_does_not_release_unconfirmed_refreshed_credential() -> None:
    store = FailingUpdateStore(token_state(expires_at=NOW))
    refresh_client = RecordingRefreshClient()

    with pytest.raises(XTokenStoreError, match="durably confirmed"):
        provider(store.read().state, refresh_client, store=store).get_valid_access_token()

    assert store.read().state.access_token == ACCESS_TOKEN


def test_store_update_failure_prevents_identity_and_create_post_requests() -> None:
    store = FailingUpdateStore(token_state(expires_at=NOW))
    x_transport = FakeTransport([])
    client = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=provider(store.read().state, RecordingRefreshClient(), store=store),
    )

    with pytest.raises(XTokenStoreError):
        client.create_text_post("test message")

    assert x_transport.requests == []


def test_refresh_race_cannot_overwrite_newer_authoritative_state() -> None:
    original = token_state(expires_at=NOW)
    winner = token_state(
        access_token=WINNER_ACCESS_TOKEN,
        refresh_token=WINNER_REFRESH_TOKEN,
        expires_at=NOW + timedelta(hours=2),
    )
    store = RefreshRaceStore(original, winner)
    refresh_client = RecordingRefreshClient()

    access_token = provider(original, refresh_client, store=store).get_valid_access_token()

    assert access_token == WINNER_ACCESS_TOKEN
    assert store.read().state is winner
    assert store.replace_calls == 1
    assert len(refresh_client.calls) == 1


def test_cas_loser_x_client_uses_only_newer_authoritative_access_token() -> None:
    original = token_state(expires_at=NOW)
    winner = token_state(
        access_token=WINNER_ACCESS_TOKEN,
        refresh_token=WINNER_REFRESH_TOKEN,
        expires_at=NOW + timedelta(hours=2),
    )
    store = RefreshRaceStore(original, winner)
    refresh_client = RecordingRefreshClient()
    x_transport = FakeTransport(
        [
            response({"data": {"id": EXPECTED_USER_ID, "username": "FPLBotTest"}}),
            response({"data": {"id": POST_ID, "text": "test message"}}, status=201),
        ]
    )
    client = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=provider(original, refresh_client, store=store),
    )

    created = client.create_text_post("test message")

    assert created.post_id == POST_ID
    assert len(refresh_client.calls) == 1
    assert store.read().state is winner
    assert all(
        request.headers["Authorization"] == f"Bearer {WINNER_ACCESS_TOKEN}"
        for request in x_transport.requests
    )
    assert all(
        request.headers["Authorization"] != f"Bearer {NEW_ACCESS_TOKEN}"
        for request in x_transport.requests
    )


def test_refresh_race_without_usable_winner_fails_without_second_refresh() -> None:
    original = token_state(expires_at=NOW)
    unusable_winner = token_state(
        access_token=WINNER_ACCESS_TOKEN,
        refresh_token=WINNER_REFRESH_TOKEN,
        expires_at=NOW,
    )
    store = RefreshRaceStore(original, unusable_winner)
    refresh_client = RecordingRefreshClient()

    with pytest.raises(XTokenConcurrencyError):
        provider(original, refresh_client, store=store).get_valid_access_token()

    assert len(refresh_client.calls) == 1


def test_failed_stale_refresh_reloads_concurrent_winner_without_retry() -> None:
    original = token_state(expires_at=NOW)
    winner = token_state(
        access_token=WINNER_ACCESS_TOKEN,
        refresh_token=WINNER_REFRESH_TOKEN,
        expires_at=NOW + timedelta(hours=2),
    )

    class WinnerAfterRefreshFailure:
        def __init__(self) -> None:
            self.reads = 0

        def read(self) -> VersionedXTokenState:
            self.reads += 1
            return (
                VersionedXTokenState("1", original)
                if self.reads == 1
                else VersionedXTokenState("2", winner)
            )

        def replace_if_revision(
            self,
            expected_revision: str,
            replacement: XOAuthTokenState,
        ) -> bool:
            raise AssertionError("No update follows a rejected stale refresh")

    refresh_client = RecordingRefreshClient(error=XTokenRefreshError("stale refresh rejected"))

    access_token = provider(
        original,
        refresh_client,
        store=WinnerAfterRefreshFailure(),  # type: ignore[arg-type]
    ).get_valid_access_token()

    assert access_token == WINNER_ACCESS_TOKEN
    assert len(refresh_client.calls) == 1


def test_refresh_does_not_retry_ambiguous_create_post() -> None:
    refresh_client = RecordingRefreshClient()
    x_transport = FakeTransport(
        [
            response({"data": {"id": EXPECTED_USER_ID, "username": "FPLBotTest"}}),
            XTransportError("connection ended after send"),
        ]
    )
    client = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=provider(token_state(expires_at=NOW), refresh_client),
    )

    with pytest.raises(XAmbiguousWriteError):
        client.create_text_post("test message")

    assert len(refresh_client.calls) == 1
    assert [request.method for request in x_transport.requests] == ["GET", "POST"]


def test_identity_mismatch_after_refresh_prevents_create_post() -> None:
    refresh_client = RecordingRefreshClient()
    x_transport = FakeTransport(
        [response({"data": {"id": "999999999", "username": "WrongAccount"}})]
    )
    client = XApiClient(
        posting_config(),
        transport=x_transport,
        token_provider=provider(token_state(expires_at=NOW), refresh_client),
    )

    with pytest.raises(XIdentityMismatchError):
        client.create_text_post("test message")

    assert [request.method for request in x_transport.requests] == ["GET"]


def test_refresh_transport_failure_is_typed_and_never_retried() -> None:
    transport = FakeTransport([XTransportError("network unavailable")])

    with pytest.raises(XTokenRefreshError, match="network boundary"):
        XOAuthRefreshClient(transport=transport, now=lambda: NOW).refresh(
            token_state(expires_at=NOW),
            credentials(),
        )

    assert len(transport.requests) == 1
