import inspect
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from fpl_bot.cloud_token_store import (
    DEFAULT_TOKEN_METADATA_COLLECTION,
    CloudXTokenStateStoreConfig,
    GoogleCloudXTokenStateStore,
    deserialize_token_state,
    serialize_token_state,
)
from fpl_bot.x_errors import (
    XTokenAuthorityPersistenceError,
    XTokenAuthorityUnconfirmedError,
    XTokenConcurrencyError,
    XTokenSecretStorageError,
    XTokenStateError,
    XTokenStoreError,
)
from fpl_bot.x_oauth import OAUTH_SCOPES, OAuthClientCredentials
from fpl_bot.x_token_refresh import (
    RefreshingXAccessTokenProvider,
    XOAuthTokenState,
    XTokenRefreshLease,
)

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
PROJECT_ID = "fpl-bot-test"
SECRET_ID = "unit-test-x-token-state"
USER_ID = "123456789"
ACCESS_TOKEN = "unit-test-access-token-placeholder"
REFRESH_TOKEN = "unit-test-refresh-token-placeholder"
NEW_ACCESS_TOKEN = "unit-test-new-access-token-placeholder"
NEW_REFRESH_TOKEN = "unit-test-new-refresh-token-placeholder"


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> Mapping[str, Any] | None:
        return None if self._data is None else dict(self._data)


class FakeDocument:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.data = data
        self.transaction_reads = 0
        self.fail_next_nontransaction_read = False

    def get(self, *, transaction: Any | None = None) -> FakeSnapshot:
        if transaction is None and self.fail_next_nontransaction_read:
            self.fail_next_nontransaction_read = False
            raise RuntimeError("authority read unavailable")
        if transaction is not None:
            self.transaction_reads += 1
        return FakeSnapshot(self.data)


class FakeCollection:
    def __init__(self, document: FakeDocument) -> None:
        self._document = document
        self.requested_ids: list[str] = []

    def document(self, document_id: str) -> FakeDocument:
        self.requested_ids.append(document_id)
        return self._document


class FakeTransaction:
    def __init__(self, client: "FakeFirestore") -> None:
        self._client = client

    def update(self, reference: FakeDocument, fields: Mapping[str, Any]) -> None:
        assert self._client.in_transaction
        assert reference.data is not None
        reference.data.update(fields)


class FakeFirestore:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.document = FakeDocument(data)
        self.collection_ref = FakeCollection(self.document)
        self.collection_names: list[str] = []
        self.in_transaction = False
        self.fail_transaction: str | None = None
        self.transaction_calls = 0

    def collection(self, name: str) -> FakeCollection:
        self.collection_names.append(name)
        return self.collection_ref

    def transaction(self) -> FakeTransaction:
        self.transaction_calls += 1
        return FakeTransaction(self)


class FakeTransactionalWrapper:
    def __init__(self, client: FakeFirestore) -> None:
        self._client = client

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(transaction: FakeTransaction) -> Any:
            failure = self._client.fail_transaction
            self._client.fail_transaction = None
            if failure == "before":
                raise RuntimeError("transaction unavailable")
            self._client.in_transaction = True
            try:
                result = function(transaction)
            finally:
                self._client.in_transaction = False
            if failure == "after":
                raise RuntimeError("commit acknowledgement lost")
            return result

        return wrapped


class FakeSecrets:
    def __init__(self, versions: dict[str, bytes] | None = None) -> None:
        self.versions = dict(versions or {})
        self.access_requests: list[str] = []
        self.add_requests: list[Mapping[str, Any]] = []
        self.disable_requests: list[str] = []
        self.fail_add = False
        self.fail_disable = False
        self.after_add: Callable[[], None] | None = None
        self.firestore: FakeFirestore | None = None

    def access_secret_version(self, request: Mapping[str, Any]) -> Any:
        if self.firestore is not None:
            assert not self.firestore.in_transaction
        name = request["name"]
        self.access_requests.append(name)
        if name not in self.versions:
            raise RuntimeError("not found")
        return SimpleNamespace(name=name, payload=SimpleNamespace(data=self.versions[name]))

    def add_secret_version(self, request: Mapping[str, Any]) -> Any:
        if self.firestore is not None:
            assert not self.firestore.in_transaction
        self.add_requests.append(request)
        if self.fail_add:
            raise RuntimeError("secret write unavailable")
        number = max([int(name.rsplit("/", 1)[1]) for name in self.versions] or [0]) + 1
        name = version_name(number)
        self.versions[name] = request["payload"]["data"]
        if self.after_add is not None:
            self.after_add()
        return SimpleNamespace(name=name)

    def disable_secret_version(self, request: Mapping[str, Any]) -> Any:
        if self.firestore is not None:
            assert not self.firestore.in_transaction
        name = request["name"]
        self.disable_requests.append(name)
        if self.fail_disable:
            raise RuntimeError("cleanup unavailable")
        return SimpleNamespace(name=name)


class RecordingRefreshClient:
    def __init__(self, firestore: FakeFirestore | None = None) -> None:
        self.firestore = firestore
        self.calls = 0

    def refresh(
        self,
        current: XOAuthTokenState,
        credentials: OAuthClientCredentials,
    ) -> XOAuthTokenState:
        if self.firestore is not None:
            assert not self.firestore.in_transaction
        self.calls += 1
        return token_state(
            access_token=NEW_ACCESS_TOKEN,
            refresh_token=NEW_REFRESH_TOKEN,
            expires_at=NOW + timedelta(hours=2),
        )


def version_name(number: int) -> str:
    return f"projects/{PROJECT_ID}/secrets/{SECRET_ID}/versions/{number}"


def token_state(
    *,
    access_token: str = ACCESS_TOKEN,
    refresh_token: str = REFRESH_TOKEN,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> XOAuthTokenState:
    return XOAuthTokenState(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_utc=expires_at,
    )


def metadata(
    *,
    revision: int = 1,
    current_version: str | None = None,
    previous_version: str | None = None,
    lease_owner: str | None = None,
    lease_expiry: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": revision,
        "secret_version_name": current_version or version_name(1),
        "previous_secret_version_name": previous_version,
        "updated_at_utc": NOW - timedelta(hours=1),
        "refresh_lease_owner": lease_owner,
        "refresh_lease_expires_at_utc": lease_expiry,
    }


def cloud_store(
    *,
    document: dict[str, Any] | None = None,
    secrets: FakeSecrets | None = None,
) -> tuple[GoogleCloudXTokenStateStore, FakeFirestore, FakeSecrets]:
    firestore = FakeFirestore(document or metadata())
    secret_client = secrets or FakeSecrets({version_name(1): serialize_token_state(token_state())})
    secret_client.firestore = firestore
    store = GoogleCloudXTokenStateStore(
        CloudXTokenStateStoreConfig(PROJECT_ID, SECRET_ID, USER_ID),
        firestore_client=firestore,
        secret_manager_client=secret_client,
        transactional_wrapper=FakeTransactionalWrapper(firestore),
        clock=lambda: NOW,
    )
    return store, firestore, secret_client


def acquire(store: GoogleCloudXTokenStateStore, owner: str = "owner-a"):
    return store.acquire_refresh_lease(
        "1",
        owner_id=owner,
        now_utc=NOW,
        expires_at_utc=NOW + timedelta(minutes=1),
    )


def test_reader_uses_firestore_selected_explicit_secret_version() -> None:
    state_two = token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)
    secrets = FakeSecrets(
        {
            version_name(1): serialize_token_state(token_state()),
            version_name(2): serialize_token_state(state_two),
        }
    )
    store, firestore, _ = cloud_store(
        document=metadata(revision=2, current_version=version_name(2)),
        secrets=secrets,
    )

    snapshot = store.read()

    assert snapshot.revision == "2"
    assert snapshot.state.access_token == NEW_ACCESS_TOKEN
    assert secrets.access_requests == [version_name(2)]
    assert firestore.collection_names == [DEFAULT_TOKEN_METADATA_COLLECTION]
    assert firestore.collection_ref.requested_ids == [f"x-user-{USER_ID}"]


@pytest.mark.parametrize("authority", ["latest", "live", "01", "2/extra"])
def test_latest_aliases_and_noncanonical_versions_are_never_authoritative(authority: str) -> None:
    document = metadata(
        current_version=f"projects/{PROJECT_ID}/secrets/{SECRET_ID}/versions/{authority}"
    )
    store, _, secrets = cloud_store(document=document)

    with pytest.raises(XTokenStateError, match="explicit numeric"):
        store.read()

    assert secrets.access_requests == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"schema_version":1}',
        b'{"schema_version":1,"schema_version":1}',
        json.dumps(
            {
                "access_token": ACCESS_TOKEN,
                "expires_at_utc": "2026-08-29T11:00:00+01:00",
                "refresh_token": REFRESH_TOKEN,
                "schema_version": 1,
                "scopes": list(OAUTH_SCOPES),
                "token_type": "bearer",
            }
        ).encode(),
    ],
)
def test_malformed_secret_payload_fails_closed(payload: bytes) -> None:
    with pytest.raises(XTokenStateError):
        deserialize_token_state(payload)


def test_firestore_authority_metadata_contains_no_token_values() -> None:
    store, firestore, _ = cloud_store()
    replacement = token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    assert store.replace_if_revision("1", replacement) is True

    rendered = repr(firestore.document.data)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert NEW_ACCESS_TOKEN not in rendered
    assert NEW_REFRESH_TOKEN not in rendered


def test_cas_expected_revision_succeeds_once_and_stale_revision_loses() -> None:
    store, _, secrets = cloud_store()
    replacement = token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    assert store.replace_if_revision("1", replacement) is True
    assert store.replace_if_revision("1", token_state()) is False
    assert store.read().state.refresh_token == NEW_REFRESH_TOKEN
    assert len(secrets.add_requests) == 1


def test_active_refresh_lease_grants_exactly_one_owner() -> None:
    store, _, _ = cloud_store()

    first = acquire(store, "owner-a")
    second = acquire(store, "owner-b")

    assert first is not None
    assert first.owner_id == "owner-a"
    assert second is None


def test_authority_transition_requires_exact_lease_owner_and_revision() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store, "owner-a")
    assert lease is not None
    forged = XTokenRefreshLease("1", "owner-b", lease.expires_at_utc)

    assert store.replace_if_revision_with_lease(forged, token_state()) is False

    assert firestore.document.data["revision"] == 1
    assert firestore.document.data["refresh_lease_owner"] == "owner-a"
    assert secrets.add_requests == []


def test_expired_refresh_lease_is_recoverable() -> None:
    store, firestore, _ = cloud_store(
        document=metadata(
            lease_owner="crashed-owner",
            lease_expiry=NOW - timedelta(seconds=1),
        )
    )

    lease = acquire(store, "recovery-owner")

    assert lease is not None
    assert firestore.document.data["refresh_lease_owner"] == "recovery-owner"


def test_oauth_refresh_is_outside_firestore_transaction_and_persists_before_use() -> None:
    expiring = token_state(expires_at=NOW)
    store, firestore, secrets = cloud_store(
        secrets=FakeSecrets({version_name(1): serialize_token_state(expiring)})
    )
    refresh = RecordingRefreshClient(firestore)
    provider = RefreshingXAccessTokenProvider(
        store,
        refresh,
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "owner-a",
    )

    result = provider.get_valid_access_token()

    assert result == NEW_ACCESS_TOKEN
    assert refresh.calls == 1
    assert store.read().state.refresh_token == NEW_REFRESH_TOKEN
    assert len(secrets.add_requests) == 1


def test_active_lease_prevents_second_oauth_refresh_entitlement() -> None:
    expiring = token_state(expires_at=NOW)
    store, _, _ = cloud_store(
        secrets=FakeSecrets({version_name(1): serialize_token_state(expiring)})
    )
    assert acquire(store, "winner") is not None
    refresh = RecordingRefreshClient()
    provider = RefreshingXAccessTokenProvider(
        store,
        refresh,
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "loser",
    )

    with pytest.raises(XTokenConcurrencyError):
        provider.get_valid_access_token()

    assert refresh.calls == 0


def test_secret_manager_add_failure_does_not_advance_authority() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store)
    assert lease is not None
    secrets.fail_add = True

    with pytest.raises(XTokenSecretStorageError):
        store.replace_if_revision_with_lease(lease, token_state())

    assert firestore.document.data["revision"] == 1
    assert firestore.document.data["secret_version_name"] == version_name(1)


def test_provider_secret_write_failure_releases_no_unconfirmed_access_token() -> None:
    expiring = token_state(expires_at=NOW)
    secrets = FakeSecrets({version_name(1): serialize_token_state(expiring)})
    secrets.fail_add = True
    store, firestore, _ = cloud_store(secrets=secrets)
    refresh = RecordingRefreshClient()
    provider = RefreshingXAccessTokenProvider(
        store,
        refresh,
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "owner-a",
    )

    with pytest.raises(XTokenSecretStorageError):
        provider.get_valid_access_token()

    assert refresh.calls == 1
    assert firestore.document.data["revision"] == 1
    assert firestore.document.data["refresh_lease_owner"] is None


def test_provider_definite_authority_failure_never_releases_candidate_token() -> None:
    expiring = token_state(expires_at=NOW)
    secrets = FakeSecrets({version_name(1): serialize_token_state(expiring)})
    store, firestore, _ = cloud_store(secrets=secrets)
    secrets.after_add = lambda: setattr(firestore, "fail_transaction", "before")
    provider = RefreshingXAccessTokenProvider(
        store,
        RecordingRefreshClient(),
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "owner-a",
    )

    with pytest.raises(XTokenAuthorityPersistenceError):
        provider.get_valid_access_token()

    assert firestore.document.data["revision"] == 1
    assert firestore.document.data["secret_version_name"] == version_name(1)
    assert firestore.document.data["refresh_lease_owner"] is None


def test_provider_uses_candidate_only_after_ambiguous_commit_is_confirmed() -> None:
    expiring = token_state(expires_at=NOW)
    secrets = FakeSecrets({version_name(1): serialize_token_state(expiring)})
    store, firestore, _ = cloud_store(secrets=secrets)
    secrets.after_add = lambda: setattr(firestore, "fail_transaction", "after")
    provider = RefreshingXAccessTokenProvider(
        store,
        RecordingRefreshClient(),
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "owner-a",
    )

    access_token = provider.get_valid_access_token()

    assert access_token == NEW_ACCESS_TOKEN
    assert firestore.document.data["revision"] == 2
    assert firestore.document.data["secret_version_name"] == version_name(2)


def test_unreconciled_ambiguous_authority_never_releases_candidate_token() -> None:
    expiring = token_state(expires_at=NOW)
    secrets = FakeSecrets({version_name(1): serialize_token_state(expiring)})
    store, firestore, _ = cloud_store(secrets=secrets)

    def lose_commit_acknowledgement_and_readback() -> None:
        firestore.fail_transaction = "after"
        firestore.document.fail_next_nontransaction_read = True

    secrets.after_add = lose_commit_acknowledgement_and_readback
    provider = RefreshingXAccessTokenProvider(
        store,
        RecordingRefreshClient(),
        OAuthClientCredentials("client-id-placeholder", "client-secret-placeholder"),
        refresh_coordinator=store,
        clock=lambda: NOW,
        lease_owner_factory=lambda: "owner-a",
    )

    with pytest.raises(XTokenAuthorityUnconfirmedError):
        provider.get_valid_access_token()

    assert firestore.document.data["revision"] == 2
    assert firestore.document.data["secret_version_name"] == version_name(2)


def test_candidate_version_becomes_authoritative_only_after_firestore_cas() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store)
    assert lease is not None

    assert store.replace_if_revision_with_lease(
        lease,
        token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN),
    )

    assert firestore.document.data["revision"] == 2
    assert firestore.document.data["secret_version_name"] == version_name(2)
    assert secrets.access_requests == []
    assert store.read().state.access_token == NEW_ACCESS_TOKEN


def test_candidate_version_with_definite_firestore_failure_remains_non_authoritative() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store)
    assert lease is not None
    firestore.fail_transaction = "before"

    with pytest.raises(XTokenAuthorityPersistenceError) as captured:
        store.replace_if_revision_with_lease(lease, token_state())

    assert captured.value.candidate_version_name == version_name(2)
    assert firestore.document.data["secret_version_name"] == version_name(1)
    assert version_name(2) in secrets.versions


def test_ambiguous_authority_commit_is_reconciled_by_firestore_reread() -> None:
    store, firestore, _ = cloud_store()
    lease = acquire(store)
    assert lease is not None
    firestore.fail_transaction = "after"

    replaced = store.replace_if_revision_with_lease(lease, token_state())

    assert replaced is True
    assert firestore.document.data["revision"] == 2
    assert firestore.document.data["secret_version_name"] == version_name(2)


def test_stale_writer_cannot_overwrite_newer_refresh_token_generation() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store, "stale-owner")
    assert lease is not None
    firestore.document.data.update(metadata(revision=2, current_version=version_name(2)))
    secrets.versions[version_name(2)] = serialize_token_state(
        token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)
    )

    assert store.replace_if_revision_with_lease(lease, token_state()) is False
    assert store.read().state.refresh_token == NEW_REFRESH_TOKEN


def test_cleanup_failure_never_invalidates_new_authoritative_version() -> None:
    state_two = token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)
    secrets = FakeSecrets(
        {
            version_name(1): serialize_token_state(token_state()),
            version_name(2): serialize_token_state(state_two),
        }
    )
    secrets.fail_disable = True
    store, firestore, _ = cloud_store(
        document=metadata(
            revision=2,
            current_version=version_name(2),
            previous_version=version_name(1),
        ),
        secrets=secrets,
    )

    assert store.replace_if_revision("2", token_state()) is True
    assert firestore.document.data["secret_version_name"] == version_name(3)
    assert secrets.disable_requests == [version_name(1)]
    assert store.read().revision == "3"


def test_secret_values_are_absent_from_reprs_and_storage_errors() -> None:
    store, firestore, secrets = cloud_store()
    lease = acquire(store)
    assert lease is not None
    secrets.fail_add = True

    with pytest.raises(XTokenStoreError) as captured:
        store.replace_if_revision_with_lease(
            lease,
            token_state(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN),
        )

    rendered = " ".join((repr(store), repr(lease), repr(captured.value), str(captured.value)))
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, NEW_ACCESS_TOKEN, NEW_REFRESH_TOKEN):
        assert secret not in rendered
        assert secret not in repr(firestore.document.data)


def test_token_payload_round_trip_is_versioned_and_utc() -> None:
    original = token_state()
    payload = serialize_token_state(original)
    restored = deserialize_token_state(payload)

    assert restored.access_token == original.access_token
    assert restored.refresh_token == original.refresh_token
    assert restored.expires_at_utc == original.expires_at_utc
    assert json.loads(payload)["schema_version"] == 1
    assert json.loads(payload)["expires_at_utc"].endswith("Z")


def test_cloud_store_source_never_uses_latest_or_alias_for_secret_access() -> None:
    source = inspect.getsource(GoogleCloudXTokenStateStore)

    assert '"latest"' not in source
    assert "version_alias" not in source
    assert "access_secret_version" in source


def test_store_construction_performs_no_secret_or_transaction_operation() -> None:
    store, firestore, secrets = cloud_store()

    assert store is not None
    assert firestore.transaction_calls == 0
    assert secrets.access_requests == []
    assert secrets.add_requests == []


def test_production_store_is_not_an_in_memory_concurrency_mechanism() -> None:
    source = inspect.getsource(GoogleCloudXTokenStateStore)

    assert "InMemoryXTokenStateStore" not in source
    assert "threading" not in source
    assert "Lock(" not in source


def test_no_external_oauth_or_secret_call_occurs_inside_transaction_callback() -> None:
    source = inspect.getsource(GoogleCloudXTokenStateStore)

    for operation_source in (
        inspect.getsource(GoogleCloudXTokenStateStore.acquire_refresh_lease),
        inspect.getsource(GoogleCloudXTokenStateStore.release_refresh_lease),
    ):
        assert "add_secret_version" not in operation_source
        assert "access_secret_version" not in operation_source
        assert ".refresh(" not in operation_source
    assert "OAuth" not in source or "refresh" in source
