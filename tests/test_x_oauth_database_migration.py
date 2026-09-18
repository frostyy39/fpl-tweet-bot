"""Synthetic metadata only; no X, Secret Manager or live Firestore."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from unittest.mock import MagicMock

import pytest
from test_cloud_token_store import FakeFirestore, FakeTransaction, FakeTransactionalWrapper
from test_production import disabled_environment

from fpl_bot import production
from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, _parse_metadata
from fpl_bot.runtime_config import ProductionConfigurationError, XCloudRuntimeConfig
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError
from fpl_bot.x_oauth_database_migration import authority_digest, relocate_authority
from fpl_bot.x_oauth_migration import LegacyAuthorityExpectation

CONFIG = CloudXTokenStateStoreConfig("unit-project", "unit-secret", "123456")
NOW = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
RAW = LegacyAuthorityExpectation(
    5, f"{CONFIG.secret_name}/versions/5", f"{CONFIG.secret_name}/versions/4", NOW
).migrated_document(CONFIG)


class Transaction(FakeTransaction):
    def set(self, reference, data):
        assert self._client.in_transaction
        reference.data = deepcopy(data)

    def create(self, reference, data):
        assert reference.data is None
        self.set(reference, data)


class Database(FakeFirestore):
    def __init__(self, data):
        super().__init__(data)
        self.lock = RLock()

    def transaction(self):
        self.transaction_calls += 1
        return Transaction(self)


def wrapper(function):
    def run(transaction):
        with transaction._client.lock:
            return FakeTransactionalWrapper(transaction._client)(function)(transaction)

    return run


def move(source, destination, **overrides):
    return relocate_authority(
        CONFIG,
        source_client=source,
        destination_client=destination,
        source_database="(default)",
        destination_database="shared-x-oauth",
        expected_digest=authority_digest(RAW, CONFIG),
        expected_revision=5,
        now=NOW,
        consumers_quiesced=True,
        transactional_wrapper=wrapper,
        **overrides,
    )


def test_exact_copy_preserves_schema_revision_and_secret_pointer_and_replays():
    source, destination = Database(deepcopy(RAW)), Database(None)
    assert move(source, destination) == "relocated"
    assert destination.document.data == RAW
    assert source.document.data["status"] == "retired"
    assert source.document.data["retired_authority"] == RAW
    assert move(source, destination) == "already_relocated"
    assert destination.document.data == RAW
    assert _parse_metadata(destination.document.data, CONFIG).revision == 5
    with pytest.raises(XTokenStateError):
        _parse_metadata(source.document.data, CONFIG)


@pytest.mark.parametrize("database", ["source", "destination"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_interrupted_commits_rerun_exact_migration_never_restore_source(database, boundary):
    source, destination = Database(deepcopy(RAW)), Database(None)
    target = source if database == "source" else destination
    target.fail_transaction = boundary
    with pytest.raises(XTokenStoreError, match="keep quiesced"):
        move(source, destination)
    if destination.document.data is not None:
        assert source.document.data["status"] == "retired"
    assert move(source, destination) in {"relocated", "already_relocated"}
    assert destination.document.data == RAW


@pytest.mark.parametrize(
    "change",
    [
        {"revision": 6},
        {"revision": True},
        {"schema_version": 1},
        {"secret_version_name": f"{CONFIG.secret_name}/versions/7"},
        {"updated_at_utc": NOW + timedelta(seconds=1)},
        {"cookie_value": "synthetic-secret-must-not-copy"},
    ],
)
def test_source_mismatch_never_fences_or_copies(change):
    raw = {**RAW, **change}
    source, destination = Database(deepcopy(raw)), Database(None)
    with pytest.raises(XTokenStateError):
        move(source, destination)
    assert source.document.data == raw
    assert destination.document.data is None


@pytest.mark.parametrize("state", ["claimed", "dispatched", "persisting", "uncertain"])
def test_active_or_uncertain_source_never_copied(state):
    raw = {
        **RAW,
        "refresh_attempt_generation": 1,
        "refresh_lease_owner": "synthetic-attempt",
        "refresh_lease_expires_at_utc": NOW,
        "refresh_attempt": {
            "attempt_id": "synthetic-attempt",
            "generation": 1,
            "credential_revision": 5,
            "state": state,
            "claimed_at_utc": NOW,
            "updated_at_utc": NOW,
            "dispatched_at_utc": None if state == "claimed" else NOW,
            "candidate_version_name": None,
            "classification": None,
        },
    }
    source, destination = Database(raw), Database(None)
    with pytest.raises(XTokenStateError):
        move(source, destination)
    assert destination.document.data is None


def test_destination_present_before_fencing_is_rejected_even_if_identical():
    source, destination = Database(deepcopy(RAW)), Database(deepcopy(RAW))
    with pytest.raises(XTokenStateError, match="before source retirement"):
        move(source, destination)
    assert source.document.data == RAW


def test_destination_advanced_after_cutover_is_never_overwritten():
    source, destination = Database(deepcopy(RAW)), Database(None)
    move(source, destination)
    destination.document.data["revision"] = 6
    with pytest.raises(XTokenStateError):
        move(source, destination)
    assert destination.document.data["revision"] == 6


def test_missing_source_is_not_inferred_from_destination():
    with pytest.raises(XTokenStateError):
        move(Database(None), Database(deepcopy(RAW)))


@pytest.mark.parametrize("value", [None, "", "(default)", "Invalid Name"])
def test_oauth_database_required_no_fallback(value):
    environment = disabled_environment()
    if value is None:
        del environment["X_OAUTH_FIRESTORE_DATABASE_ID"]
    else:
        environment["X_OAUTH_FIRESTORE_DATABASE_ID"] = value
    with pytest.raises(ProductionConfigurationError, match="X_OAUTH_FIRESTORE_DATABASE_ID"):
        XCloudRuntimeConfig.from_environment(environment)


@pytest.mark.parametrize("business", ["(default)", "captain-state"])
def test_business_and_oauth_database_independent(business):
    environment = disabled_environment()
    environment["FIRESTORE_DATABASE_ID"] = business
    config = XCloudRuntimeConfig.from_environment(environment)
    assert config.firestore_database_id == business
    assert config.x_oauth_firestore_database_id == "shared-x-oauth"


def test_production_composition_never_uses_business_client_for_oauth(monkeypatch):
    business, oauth, store = MagicMock(), MagicMock(), MagicMock()
    captured = []

    def construct(config, **kwargs):
        captured.append(kwargs["firestore_client"])
        return store

    monkeypatch.setattr(production, "GoogleCloudXTokenStateStore", construct)
    monkeypatch.setattr(production, "_default_oauth_firestore_client", lambda config: oauth)
    production.create_production_app(
        disabled_environment(),
        firestore_client=business,
        cloud_tasks_client=MagicMock(),
        secret_manager_client=MagicMock(),
    )
    assert captured == [oauth]
    business.get.assert_not_called()
    oauth.get.assert_not_called()


def test_no_quiescence_no_transaction():
    source = Database(deepcopy(RAW))
    with pytest.raises(XTokenStateError):
        relocate_authority(
            CONFIG,
            source_client=source,
            destination_client=Database(None),
            source_database="(default)",
            destination_database="shared-x-oauth",
            expected_digest=authority_digest(RAW, CONFIG),
            expected_revision=5,
            now=NOW,
            consumers_quiesced=False,
        )
    assert source.transaction_calls == 0


def test_concurrent_identical_relocation_retains_one_authority():
    source, destination = Database(deepcopy(RAW)), Database(None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: move(source, destination), range(2)))
    assert sorted(results) == ["already_relocated", "relocated"]
    assert destination.document.data == RAW
    assert source.document.data["status"] == "retired"


def test_source_retired_destination_unavailable_leaves_zero_usable_authorities():
    source, destination = Database(deepcopy(RAW)), Database(None)
    destination.fail_transaction = "before"
    with pytest.raises(XTokenStoreError):
        move(source, destination)
    with pytest.raises(XTokenStateError):
        _parse_metadata(source.document.data, CONFIG)
    assert destination.document.data is None
    assert move(source, destination) == "relocated"


def test_committed_refresh_evidence_preserved_without_new_generation():
    raw = {
        **RAW,
        "refresh_attempt_generation": 1,
        "refresh_attempt": {
            "attempt_id": "synthetic-completed-attempt",
            "generation": 1,
            "credential_revision": 4,
            "state": "committed",
            "claimed_at_utc": NOW,
            "updated_at_utc": NOW,
            "dispatched_at_utc": NOW,
            "candidate_version_name": RAW["secret_version_name"],
            "classification": None,
        },
    }
    source, destination = Database(deepcopy(raw)), Database(None)
    relocate_authority(
        CONFIG,
        source_client=source,
        destination_client=destination,
        source_database="(default)",
        destination_database="shared-x-oauth",
        expected_digest=authority_digest(raw, CONFIG),
        expected_revision=5,
        now=NOW,
        consumers_quiesced=True,
        transactional_wrapper=wrapper,
    )
    assert destination.document.data == raw


def test_reproducible_iam_grants_only_named_databases():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "deploy/provision-shared-x-oauth.ps1").read_text()
    assert "--condition=expression=resource.name==" in script
    assert "captain-state" in script and "shared-x-oauth" in script
    assert "databases/(default)" not in script
    assert "--role=roles/compute" not in script
    assert "--role=roles/owner" not in script
