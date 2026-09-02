import inspect
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fpl_bot.cloud_token_store import (
    CloudXTokenStateStoreConfig,
    GoogleCloudXTokenStateStore,
    InitialTokenStateStatus,
    serialize_token_state,
)
from fpl_bot.x_errors import (
    XTokenAuthorityPersistenceError,
    XTokenAuthorityUnconfirmedError,
    XTokenBootstrapReconciliationError,
    XTokenSecretStorageError,
    XTokenStateError,
)
from fpl_bot.x_oauth import DPAPI_FILE_MAGIC, OAUTH_SCOPES
from fpl_bot.x_token_bootstrap import (
    LocalDpapiTokenStateReader,
    bootstrap_x_token_state,
)
from fpl_bot.x_token_refresh import XOAuthTokenState

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
RECEIVED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PROJECT_ID = "fpl-bot-test"
PROJECT_NUMBER = "524790767721"
SECRET_ID = "unit-test-token-state"
USER_ID = "123456789"
ACCESS_TOKEN = "unit-test-access-placeholder"
REFRESH_TOKEN = "unit-test-refresh-placeholder"


class FakeUnprotector:
    def __init__(self, plaintext: bytes) -> None:
        self.plaintext = plaintext
        self.calls = 0

    def unprotect(self, ciphertext: bytes) -> bytes:
        assert ciphertext == b"protected"
        self.calls += 1
        return self.plaintext


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> Mapping[str, Any] | None:
        return None if self._data is None else dict(self._data)


class FakeDocument:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.fail_nontransaction_read = False

    def get(self, *, transaction: Any | None = None) -> FakeSnapshot:
        if transaction is None and self.fail_nontransaction_read:
            self.fail_nontransaction_read = False
            raise RuntimeError("authority read unavailable")
        return FakeSnapshot(self.data)


class FakeCollection:
    def __init__(self, document: FakeDocument) -> None:
        self.document_ref = document
        self.requested_id: str | None = None

    def document(self, document_id: str) -> FakeDocument:
        self.requested_id = document_id
        return self.document_ref


class FakeTransaction:
    def __init__(self, firestore: "FakeFirestore") -> None:
        self.firestore = firestore

    def create(self, reference: FakeDocument, data: Mapping[str, Any]) -> None:
        assert self.firestore.in_transaction
        assert reference.data is None
        reference.data = dict(data)

    def update(self, reference: FakeDocument, fields: Mapping[str, Any]) -> None:
        assert self.firestore.in_transaction
        assert reference.data is not None
        reference.data.update(fields)


class FakeFirestore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.document = FakeDocument(data)
        self.collection_ref = FakeCollection(self.document)
        self.in_transaction = False
        self.fail_transaction: str | None = None
        self.fail_reconciliation_read = False

    def collection(self, name: str) -> FakeCollection:
        assert name == "x_oauth_token_authority"
        return self.collection_ref

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeTransactionalWrapper:
    def __init__(self, firestore: FakeFirestore) -> None:
        self.firestore = firestore

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(transaction: FakeTransaction) -> Any:
            failure = self.firestore.fail_transaction
            self.firestore.fail_transaction = None
            if failure == "before":
                raise RuntimeError("transaction unavailable")
            self.firestore.in_transaction = True
            try:
                result = function(transaction)
            finally:
                self.firestore.in_transaction = False
            if failure == "after":
                if self.firestore.fail_reconciliation_read:
                    self.firestore.document.fail_nontransaction_read = True
                raise RuntimeError("commit acknowledgement lost")
            return result

        return wrapped


class FakeSecrets:
    def __init__(self, versions: dict[str, bytes] | None = None) -> None:
        self.versions = dict(versions or {})
        self.calls: list[str] = []
        self.fail_add = False
        self.fail_add_after_write = False
        self.firestore: FakeFirestore | None = None
        self.access_response_name: str | None = None
        self.response_project: str | None = None

    def _response_name(self, name: str) -> str:
        if self.response_project is None:
            return name
        return name.replace(f"projects/{PROJECT_ID}/", f"projects/{self.response_project}/")

    def _require_outside_transaction(self) -> None:
        if self.firestore is not None:
            assert not self.firestore.in_transaction

    def list_secret_versions(self, request: Mapping[str, Any]) -> list[Any]:
        self._require_outside_transaction()
        self.calls.append("list")
        return [SimpleNamespace(name=self._response_name(name)) for name in self.versions]

    def add_secret_version(self, request: Mapping[str, Any]) -> Any:
        self._require_outside_transaction()
        self.calls.append("add")
        if self.fail_add:
            raise RuntimeError("write unavailable")
        name = version_name(len(self.versions) + 1)
        self.versions[name] = request["payload"]["data"]
        if self.fail_add_after_write:
            raise RuntimeError("write acknowledgement lost")
        return SimpleNamespace(name=self._response_name(name))

    def access_secret_version(self, request: Mapping[str, Any]) -> Any:
        self._require_outside_transaction()
        self.calls.append("access")
        name = request["name"]
        return SimpleNamespace(
            name=self.access_response_name or self._response_name(name),
            payload=SimpleNamespace(data=self.versions[name]),
        )

    def disable_secret_version(self, request: Mapping[str, Any]) -> Any:
        self._require_outside_transaction()
        self.calls.append("disable")
        return SimpleNamespace(name=request["name"])


def handoff_payload(**overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "version": 1,
        "x_user_id": USER_ID,
        "x_username": "FPLBotTest",
        "token_type": "bearer",
        "scope": " ".join(OAUTH_SCOPES),
        "expires_in": 7200,
        "received_at_utc": RECEIVED.isoformat(),
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode()


def local_state(tmp_path: Path, **overrides: Any):
    repository = tmp_path / "repository"
    repository.mkdir()
    token_file = tmp_path / "external.dpapi"
    token_file.write_bytes(DPAPI_FILE_MAGIC + b"protected")
    reader = LocalDpapiTokenStateReader(
        repository_root=repository,
        unprotector=FakeUnprotector(handoff_payload(**overrides)),
    )
    return reader.read(token_file, expected_user_id=USER_ID)


def read_plaintext(tmp_path: Path, plaintext: bytes):
    repository = tmp_path / "repository"
    repository.mkdir()
    token_file = tmp_path / "external.dpapi"
    token_file.write_bytes(DPAPI_FILE_MAGIC + b"protected")
    return LocalDpapiTokenStateReader(
        repository_root=repository,
        unprotector=FakeUnprotector(plaintext),
    ).read(token_file, expected_user_id=USER_ID)


def token_state(*, expires_at: datetime = NOW + timedelta(hours=1)) -> XOAuthTokenState:
    return XOAuthTokenState(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        expires_at_utc=expires_at,
    )


def version_name(number: int) -> str:
    return f"projects/{PROJECT_ID}/secrets/{SECRET_ID}/versions/{number}"


def test_numeric_project_version_name_is_canonicalized_to_configured_project() -> None:
    config = CloudXTokenStateStoreConfig(
        PROJECT_ID,
        SECRET_ID,
        USER_ID,
        project_number=PROJECT_NUMBER,
    )

    canonical = config.canonicalize_api_version_name(
        f"projects/{PROJECT_NUMBER}/secrets/{SECRET_ID}/versions/1"
    )

    assert canonical == version_name(1)


def test_project_id_version_name_remains_valid() -> None:
    config = CloudXTokenStateStoreConfig(
        PROJECT_ID,
        SECRET_ID,
        USER_ID,
        project_number=PROJECT_NUMBER,
    )

    assert config.canonicalize_api_version_name(version_name(1)) == version_name(1)


@pytest.mark.parametrize(
    "response_name",
    [
        f"projects/999999999999/secrets/{SECRET_ID}/versions/1",
        f"projects/{PROJECT_NUMBER}/secrets/wrong-secret/versions/1",
        f"projects/{PROJECT_NUMBER}/secrets/{SECRET_ID}/versions/latest",
        f"projects/{PROJECT_NUMBER}/secrets/{SECRET_ID}/versions/live",
    ],
)
def test_unexpected_project_secret_or_alias_fails_canonicalization(response_name: str) -> None:
    config = CloudXTokenStateStoreConfig(
        PROJECT_ID,
        SECRET_ID,
        USER_ID,
        project_number=PROJECT_NUMBER,
    )

    with pytest.raises(XTokenStateError):
        config.canonicalize_api_version_name(response_name)


def authority_document(version: int = 1) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": version,
        "secret_version_name": version_name(version),
        "previous_secret_version_name": None,
        "updated_at_utc": NOW,
        "refresh_lease_owner": None,
        "refresh_lease_expires_at_utc": None,
    }


def cloud_store(
    *,
    document: dict[str, Any] | None = None,
    versions: dict[str, bytes] | None = None,
    project_number: str | None = None,
) -> tuple[GoogleCloudXTokenStateStore, FakeFirestore, FakeSecrets]:
    firestore = FakeFirestore(document)
    secrets = FakeSecrets(versions)
    secrets.firestore = firestore
    store = GoogleCloudXTokenStateStore(
        CloudXTokenStateStoreConfig(
            PROJECT_ID,
            SECRET_ID,
            USER_ID,
            project_number=project_number,
        ),
        firestore_client=firestore,
        secret_manager_client=secrets,
        transactional_wrapper=FakeTransactionalWrapper(firestore),
        clock=lambda: NOW,
    )
    return store, firestore, secrets


def test_valid_local_bundle_converts_to_existing_token_state(tmp_path: Path) -> None:
    validated = local_state(tmp_path)

    assert validated.x_user_id == USER_ID
    assert isinstance(validated.state, XOAuthTokenState)
    assert validated.state.expires_at_utc == RECEIVED + timedelta(hours=2)
    assert validated.state.scopes == OAUTH_SCOPES


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"refresh_token": ""}, "invalid token state"),
        ({"scope": "tweet.read users.read"}, "required V1 scopes"),
        ({"received_at_utc": "2026-09-01T09:00:00"}, "timezone-aware UTC"),
    ],
)
def test_invalid_local_token_state_fails_before_cloud_mutation(
    tmp_path: Path,
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(XTokenStateError, match=message):
        local_state(tmp_path, **overrides)


def test_expired_access_token_can_be_validated_for_bootstrap(tmp_path: Path) -> None:
    validated = local_state(
        tmp_path,
        received_at_utc=(NOW - timedelta(hours=3)).isoformat(),
    )
    store, _, _ = cloud_store()

    result = bootstrap_x_token_state(validated, store, now_utc=NOW)

    assert result.access_token_status == "expired"


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_local_handoff_schema_is_exact_and_rejects_duplicate_keys(
    tmp_path: Path,
    mutation: str,
) -> None:
    raw = json.loads(handoff_payload())
    if mutation == "missing":
        raw.pop("refresh_token")
        plaintext = json.dumps(raw).encode()
    elif mutation == "extra":
        raw["event_code"] = "GW1"
        plaintext = json.dumps(raw).encode()
    else:
        plaintext = handoff_payload()[:-1] + b',"access_token":"duplicate"}'

    with pytest.raises(XTokenStateError):
        read_plaintext(tmp_path, plaintext)


def test_first_bootstrap_creates_secret_before_firestore_authority(tmp_path: Path) -> None:
    store, firestore, secrets = cloud_store()
    original_add = secrets.add_secret_version

    def add(request: Mapping[str, Any]) -> Any:
        assert firestore.document.data is None
        return original_add(request)

    secrets.add_secret_version = add  # type: ignore[method-assign]

    result = bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert result.initialization.status is InitialTokenStateStatus.INITIALIZED
    assert result.initialization.revision == "1"
    assert result.initialization.secret_version_name == version_name(1)
    assert secrets.calls == ["list", "add", "access"]


def test_firestore_authority_contains_only_non_secret_metadata(tmp_path: Path) -> None:
    store, firestore, _ = cloud_store()

    bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert firestore.document.data == authority_document()
    rendered = json.dumps(firestore.document.data, default=str)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


def test_existing_authority_is_read_only_and_creates_no_version(tmp_path: Path) -> None:
    versions = {
        version_name(1): serialize_token_state(
            token_state(expires_at=RECEIVED + timedelta(hours=2))
        )
    }
    store, _, secrets = cloud_store(
        document=authority_document(),
        versions=versions,
        project_number=PROJECT_NUMBER,
    )
    secrets.response_project = PROJECT_NUMBER

    result = bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert result.initialization.status is InitialTokenStateStatus.ALREADY_INITIALIZED
    assert secrets.calls == ["access", "access"]


def test_access_response_with_wrong_numeric_version_fails_closed() -> None:
    versions = {version_name(1): serialize_token_state(token_state())}
    store, _, secrets = cloud_store(
        document=authority_document(),
        versions=versions,
        project_number=PROJECT_NUMBER,
    )
    secrets.access_response_name = f"projects/{PROJECT_NUMBER}/secrets/{SECRET_ID}/versions/2"

    with pytest.raises(XTokenStateError):
        store.read()


def test_matching_orphaned_candidate_initializes_authority_without_duplicate_version(
    tmp_path: Path,
) -> None:
    versions = {
        version_name(1): serialize_token_state(
            token_state(expires_at=RECEIVED + timedelta(hours=2))
        )
    }
    store, firestore, secrets = cloud_store(
        versions=versions,
        project_number=PROJECT_NUMBER,
    )
    secrets.response_project = PROJECT_NUMBER

    result = bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert result.initialization.secret_version_name == version_name(1)
    assert firestore.document.data == authority_document()
    assert secrets.calls == ["list", "access", "access"]
    assert tuple(secrets.versions) == (version_name(1),)


def test_mismatched_orphaned_candidate_fails_without_duplicate_version(tmp_path: Path) -> None:
    different = XOAuthTokenState(
        access_token="different-access-placeholder",
        refresh_token="different-refresh-placeholder",
        expires_at_utc=NOW + timedelta(hours=1),
    )
    versions = {version_name(1): serialize_token_state(different)}
    store, firestore, secrets = cloud_store(versions=versions)

    with pytest.raises(XTokenBootstrapReconciliationError):
        bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert firestore.document.data is None
    assert tuple(secrets.versions) == (version_name(1),)


def test_secret_manager_failure_prevents_authority_creation(tmp_path: Path) -> None:
    store, firestore, secrets = cloud_store()
    secrets.fail_add = True

    with pytest.raises(XTokenSecretStorageError):
        bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert firestore.document.data is None


def test_ambiguous_secret_write_reconciles_one_matching_candidate(tmp_path: Path) -> None:
    store, firestore, secrets = cloud_store()
    secrets.fail_add_after_write = True

    result = bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert result.initialization.secret_version_name == version_name(1)
    assert firestore.document.data == authority_document()
    assert secrets.calls == ["list", "add", "list", "access", "access"]


def test_firestore_definite_failure_preserves_candidate_version(tmp_path: Path) -> None:
    store, firestore, secrets = cloud_store()
    firestore.fail_transaction = "before"

    with pytest.raises(XTokenAuthorityPersistenceError) as captured:
        bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert captured.value.candidate_version_name == version_name(1)
    assert version_name(1) in secrets.versions
    assert firestore.document.data is None


def test_ambiguous_firestore_commit_is_reconciled_by_readback(tmp_path: Path) -> None:
    store, firestore, secrets = cloud_store()
    firestore.fail_transaction = "after"

    result = bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert result.initialization.status is InitialTokenStateStatus.INITIALIZED
    assert result.initialization.secret_version_name == version_name(1)
    assert len(secrets.versions) == 1


def test_unreadable_ambiguous_firestore_commit_preserves_candidate_and_fails_closed(
    tmp_path: Path,
) -> None:
    store, firestore, secrets = cloud_store()
    firestore.fail_transaction = "after"
    firestore.fail_reconciliation_read = True

    with pytest.raises(XTokenAuthorityUnconfirmedError) as captured:
        bootstrap_x_token_state(local_state(tmp_path), store, now_utc=NOW)

    assert captured.value.candidate_version_name == version_name(1)
    assert len(secrets.versions) == 1


def test_mismatched_expected_identity_fails_before_cloud_access(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    token_file = tmp_path / "external.dpapi"
    token_file.write_bytes(DPAPI_FILE_MAGIC + b"protected")
    reader = LocalDpapiTokenStateReader(
        repository_root=repository,
        unprotector=FakeUnprotector(handoff_payload()),
    )

    with pytest.raises(XTokenStateError, match="expected X user"):
        reader.read(token_file, expected_user_id="987654321")


def test_token_values_are_absent_from_repr_and_failures(tmp_path: Path) -> None:
    validated = local_state(tmp_path)
    store, firestore, _ = cloud_store()
    firestore.fail_transaction = "before"

    with pytest.raises(XTokenAuthorityPersistenceError) as captured:
        bootstrap_x_token_state(validated, store, now_utc=NOW)

    rendered = " ".join((repr(validated), repr(captured.value), str(captured.value)))
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


def test_bootstrap_dependency_graph_has_no_x_or_post_capability() -> None:
    import fpl_bot.x_token_bootstrap as module
    import fpl_bot.x_token_bootstrap_cli as cli

    source = inspect.getsource(module) + inspect.getsource(cli)
    forbidden = (
        "XApiClient",
        "PostExecutionCoordinator",
        "DeadlineExecutionRevalidator",
        "RefreshingXAccessTokenProvider",
        "XOAuthRefreshClient",
        "create_text_post",
        "/2/tweets",
        "/2/users/me",
    )
    assert all(name not in source for name in forbidden)
