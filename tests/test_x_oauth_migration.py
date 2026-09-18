"""No live secrets/provider; exact metadata CAS and interrupted migration coverage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_cloud_token_store import FakeFirestore, FakeTransactionalWrapper

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, _parse_metadata
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError
from fpl_bot.x_oauth_migration import LegacyAuthorityExpectation, migrate_legacy_authority

CONFIG = CloudXTokenStateStoreConfig("unit-project", "unit-secret", "123456")
EXPECT = LegacyAuthorityExpectation(
    5,
    f"{CONFIG.secret_name}/versions/5",
    f"{CONFIG.secret_name}/versions/4",
    datetime(2026, 9, 18, 17, 30, 2, tzinfo=UTC),
)


def migrate(client, **overrides):
    return migrate_legacy_authority(
        CONFIG,
        EXPECT,
        firestore_client=client,
        transactional_wrapper=FakeTransactionalWrapper(client),
        **{"consumers_quiesced": True, "legacy_history_reviewed": True, **overrides},
    )


def test_metadata_only_preserves_authority_and_replays():
    client = FakeFirestore(EXPECT.legacy_document())
    assert migrate(client) == "migrated"
    assert client.document.data == EXPECT.migrated_document(CONFIG)
    assert migrate(client) == "already_migrated"
    parsed = _parse_metadata(client.document.data, CONFIG)
    assert parsed.revision == 5
    assert parsed.secret_version_name == EXPECT.secret_version_name
    assert parsed.updated_at_utc == EXPECT.updated_at_utc
    assert parsed.refresh_attempt is None
    assert client.document.transaction_reads == 2


@pytest.mark.parametrize("flag", ["consumers_quiesced", "legacy_history_reviewed"])
def test_attestations_required_before_transaction(flag):
    client = FakeFirestore(EXPECT.legacy_document())
    with pytest.raises(XTokenStateError):
        migrate(client, **{flag: False})
    assert client.transaction_calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"revision": 6},
        {"schema_version": 3},
        {"unknown": "not-allowed"},
        {"secret_version_name": f"{CONFIG.secret_name}/versions/6"},
        {"previous_secret_version_name": None},
        {"updated_at_utc": EXPECT.updated_at_utc + timedelta(seconds=1)},
        {"refresh_lease_owner": "abandoned", "refresh_lease_expires_at_utc": EXPECT.updated_at_utc},
        {"refresh_lease_owner": None, "refresh_lease_expires_at_utc": EXPECT.updated_at_utc},
    ],
)
def test_unexpected_or_expired_legacy_authority_never_migrated(change):
    raw = {**EXPECT.legacy_document(), **change}
    client = FakeFirestore(dict(raw))
    with pytest.raises(XTokenStateError):
        migrate(client)
    assert client.document.data == raw


@pytest.mark.parametrize("failure", ["before", "after"])
def test_crash_before_or_after_commit_exact_retry(failure):
    client = FakeFirestore(EXPECT.legacy_document())
    client.fail_transaction = failure
    with pytest.raises(XTokenStoreError, match="unconfirmed"):
        migrate(client)
    assert migrate(client) == ("migrated" if failure == "before" else "already_migrated")


def test_concurrent_authority_change_rejected_on_transaction_retry():
    client = FakeFirestore(EXPECT.legacy_document())

    def wrapper(operation):
        def run(transaction):
            client.document.data["revision"] = 6
            return operation(transaction)

        return run

    with pytest.raises(XTokenStateError):
        migrate_legacy_authority(
            CONFIG,
            EXPECT,
            firestore_client=client,
            consumers_quiesced=True,
            legacy_history_reviewed=True,
            transactional_wrapper=wrapper,
        )
    assert client.document.data["schema_version"] == 1


def test_migration_never_replaces_new_schema_attempt_evidence():
    client = FakeFirestore(EXPECT.migrated_document(CONFIG))
    client.document.data["refresh_attempt_generation"] = 1
    before = dict(client.document.data)
    with pytest.raises(XTokenStateError):
        migrate(client)
    assert client.document.data == before


@pytest.mark.parametrize(
    "expectation",
    [
        replace(EXPECT, revision=True),
        replace(EXPECT, revision=0),
        replace(EXPECT, secret_version_name=f"{CONFIG.secret_name}/versions/latest"),
        replace(EXPECT, updated_at_utc=datetime(2026, 9, 18)),
    ],
)
def test_invalid_expectation_rejected(expectation):
    with pytest.raises(XTokenStateError):
        migrate_legacy_authority(
            CONFIG,
            expectation,
            firestore_client=FakeFirestore(EXPECT.legacy_document()),
            consumers_quiesced=True,
            legacy_history_reviewed=True,
        )


def test_missing_authority_not_initialized():
    client = FakeFirestore(None)
    with pytest.raises(XTokenStateError):
        migrate(client)
    assert client.document.data is None


@pytest.mark.parametrize("change", [{"schema_version": True}, {"revision": True}])
def test_malformed_actual_legacy_types_rejected(change):
    client = FakeFirestore({**EXPECT.legacy_document(), **change})
    with pytest.raises(XTokenStateError):
        migrate(client)


def test_malformed_actual_replay_types_rejected():
    client = FakeFirestore(EXPECT.migrated_document(CONFIG))
    client.document.data["refresh_attempt_generation"] = False
    with pytest.raises(XTokenStateError):
        migrate(client)
