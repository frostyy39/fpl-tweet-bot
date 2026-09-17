"""Offline fault injection: neither consumer can retry an unknown rotating grant."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from test_cloud_token_store import (
    ACCESS_TOKEN,
    NEW_ACCESS_TOKEN,
    NEW_REFRESH_TOKEN,
    NOW,
    PROJECT_ID,
    REFRESH_TOKEN,
    SECRET_ID,
    USER_ID,
    FakeSecrets,
    FakeTransactionalWrapper,
    RecordingRefreshClient,
    acquire,
    cloud_store,
    metadata,
    token_state,
    version_name,
)
from test_x_token_refresh import FakeTransport

from fpl_bot.cloud_token_store import (
    CloudXTokenStateStoreConfig,
    GoogleCloudXTokenStateStore,
    serialize_token_state,
)
from fpl_bot.x_errors import (
    XTokenConcurrencyError,
    XTokenRefreshTransportError,
    XTokenRefreshUncertainError,
    XTokenStateError,
    XTokenStoreError,
    XTransportError,
)
from fpl_bot.x_oauth import OAuthClientCredentials
from fpl_bot.x_token_bootstrap import ValidatedLocalTokenState
from fpl_bot.x_token_refresh import RefreshingXAccessTokenProvider, XOAuthRefreshClient


class ProcessCrash(BaseException):
    """Bypass exception cleanup to simulate loss of a controller process."""


def expiring_store():
    return cloud_store(
        secrets=FakeSecrets(
            {
                version_name(1): serialize_token_state(token_state(expires_at=NOW)),
            }
        )
    )


def consumer(store, refresh, owner):
    return RefreshingXAccessTokenProvider(
        store,
        refresh,
        OAuthClientCredentials("synthetic-client", "synthetic-secret"),
        refresh_coordinator=store,
        clock=store._clock,
        lease_owner_factory=lambda: owner,
    )


def expire(store):
    store._clock = lambda: NOW + timedelta(minutes=1)  # Exact expiry is expired.


def assert_blocked(store, *, uncertain=True):
    for owner in ("good-luck", "captain"):
        refresh = RecordingRefreshClient()
        error = XTokenRefreshUncertainError if uncertain else XTokenConcurrencyError
        with pytest.raises(error):
            consumer(store, refresh, owner).get_valid_access_token()
        assert refresh.calls == 0


def test_two_consumers_follow_exact_new_authority_without_independent_copy():
    store, fs, secrets = expiring_store()
    refresh = RecordingRefreshClient(fs)
    assert consumer(store, refresh, "good-luck").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert consumer(store, refresh, "captain").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert refresh.calls == 1
    assert fs.document.data["revision"] == 2
    assert fs.document.data["refresh_attempt"]["state"] == "committed"
    assert fs.document.data["refresh_attempt"]["attempt_id"] == "good-luck"
    assert fs.document.data["refresh_lease_owner"] is None
    assert len(secrets.add_requests) == 1


def test_concurrent_good_luck_captain_claims_have_one_owner():
    store, fs, _ = expiring_store()
    lock = threading.RLock()
    original = FakeTransactionalWrapper(fs)

    def serial(function):
        callback = original(function)

        def transaction(tx):
            with lock:
                return callback(tx)

        return transaction

    store._transactional = serial
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait()
        return acquire(store, owner, dispatch=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("good-luck", "captain")))
    assert sum(lease is not None for lease in claims) == 1
    assert fs.document.data["refresh_attempt_generation"] == 1


def test_expired_pre_dispatch_claim_can_be_recovered_but_old_owner_is_fenced():
    store, fs, _ = expiring_store()
    abandoned = acquire(store, "abandoned", dispatch=False)
    assert_blocked(store, uncertain=False)
    expire(store)
    replacement = RecordingRefreshClient()
    assert consumer(store, replacement, "captain").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert not store.begin_refresh_dispatch(abandoned)
    assert replacement.calls == 1
    assert fs.document.data["refresh_attempt_generation"] == 2


def test_crash_before_dispatch_barrier_can_abort_without_provider_call():
    store, fs, _ = expiring_store()
    lease = acquire(store, dispatch=False)
    fs.fail_transaction = "before"
    with pytest.raises(XTokenStoreError):
        store.begin_refresh_dispatch(lease)
    assert store.release_refresh_lease(lease)
    assert fs.document.data["refresh_attempt"]["state"] == "aborted_before_dispatch"
    refresh = RecordingRefreshClient()
    assert consumer(store, refresh, "captain").get_valid_access_token() == NEW_ACCESS_TOKEN


def test_dispatch_commit_lost_ack_never_authorizes_http_or_old_credential_reuse():
    store, fs, _ = expiring_store()
    lease = acquire(store, dispatch=False)
    fs.fail_transaction = "after"
    with pytest.raises(XTokenStoreError):
        store.begin_refresh_dispatch(lease)
    assert not store.release_refresh_lease(lease)
    assert fs.document.data["refresh_attempt"]["state"] == "dispatched"
    assert_blocked(store, uncertain=False)
    expire(store)
    assert_blocked(store)
    assert fs.document.data["refresh_attempt"]["state"] == "uncertain"


def test_duplicate_dispatch_same_attempt_is_rejected_even_before_expiry():
    store, _, _ = expiring_store()
    lease = acquire(store)
    assert not store.begin_refresh_dispatch(lease)
    assert not store.release_refresh_lease(lease)
    assert acquire(store, "another-owner", dispatch=False) is None


@pytest.mark.parametrize("message", ["timeout", "connection reset after possible send"])
def test_ambiguous_http_failure_blocks_both_consumers_without_retry(message, caplog):
    store, fs, secrets = expiring_store()
    transport = FakeTransport([XTransportError(message)])
    refresh = XOAuthRefreshClient(transport=transport, now=lambda: NOW)
    with pytest.raises(XTokenRefreshTransportError):
        consumer(store, refresh, "captain").get_valid_access_token()
    assert fs.document.data["refresh_attempt"]["state"] == "uncertain"
    expire(store)
    assert_blocked(store)
    assert len(transport.requests) == 1
    assert secrets.add_requests == []
    assert ACCESS_TOKEN not in caplog.text and REFRESH_TOKEN not in caplog.text


@pytest.mark.parametrize("point", ["immediately_before_send", "after_possible_send"])
def test_process_crash_at_network_boundary_leaves_irreversible_barrier(point):
    store, fs, _ = expiring_store()

    class CrashingRefresh:
        def refresh(self, current, credentials):
            assert fs.document.data["refresh_attempt"]["state"] == "dispatched"
            raise ProcessCrash(point)

    with pytest.raises(ProcessCrash):
        consumer(store, CrashingRefresh(), "captain").get_valid_access_token()
    expire(store)
    assert_blocked(store)
    with pytest.raises(XTokenRefreshUncertainError):
        store.reconcile_refresh_attempt()


def test_crash_after_successful_response_before_secret_storage_requires_reauthorization():
    store, fs, secrets = expiring_store()

    def crash(request):
        raise ProcessCrash()

    secrets.add_secret_version = crash
    with pytest.raises(ProcessCrash):
        consumer(store, RecordingRefreshClient(), "captain").get_valid_access_token()
    assert fs.document.data["refresh_attempt"]["state"] == "persisting"
    expire(store)
    assert_blocked(store)
    with pytest.raises(XTokenRefreshUncertainError):
        store.reconcile_refresh_attempt()


def test_crash_after_secret_creation_before_binding_does_not_guess_orphan():
    store, fs, secrets = expiring_store()

    def crash():
        raise ProcessCrash()

    secrets.after_add = crash
    with pytest.raises(ProcessCrash):
        consumer(store, RecordingRefreshClient(), "captain").get_valid_access_token()
    assert version_name(2) in secrets.versions
    assert fs.document.data["refresh_attempt"]["candidate_version_name"] is None
    expire(store)
    assert_blocked(store)
    with pytest.raises(XTokenRefreshUncertainError):
        store.reconcile_refresh_attempt()


def test_crash_after_exact_candidate_binding_recovers_without_x_or_new_secret():
    store, fs, secrets = expiring_store()
    wrapper = store._transactional

    def crashing(function):
        callback = wrapper(function)

        def transaction(tx):
            attempt = fs.document.data["refresh_attempt"]
            if attempt and attempt["candidate_version_name"] is not None:
                raise ProcessCrash()
            return callback(tx)

        return transaction

    store._transactional = crashing
    with pytest.raises(ProcessCrash):
        consumer(store, RecordingRefreshClient(), "captain").get_valid_access_token()
    store._transactional = wrapper
    expire(store)
    assert_blocked(store)
    assert store.reconcile_refresh_attempt()
    assert store.reconcile_refresh_attempt()
    assert fs.document.data["revision"] == 2
    assert store.read().state.refresh_token == NEW_REFRESH_TOKEN
    assert len(secrets.add_requests) == 1


def test_crash_after_authority_commit_before_return_needs_no_lease_cleanup():
    store, fs, secrets = expiring_store()
    wrapper = store._transactional

    def crashing(function):
        callback = wrapper(function)

        def transaction(tx):
            result = callback(tx)
            if fs.document.data["revision"] == 2:
                raise ProcessCrash()
            return result

        return transaction

    store._transactional = crashing
    with pytest.raises(ProcessCrash):
        consumer(store, RecordingRefreshClient(), "captain").get_valid_access_token()
    store._transactional = wrapper
    refresh = RecordingRefreshClient()
    assert consumer(store, refresh, "good-luck").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert refresh.calls == 0
    assert fs.document.data["refresh_lease_owner"] is None
    assert len(secrets.add_requests) == 1


def test_post_dispatch_release_and_cas_cannot_reset_uncertain_authority():
    store, fs, secrets = expiring_store()
    lease = acquire(store)
    assert store.mark_refresh_uncertain(lease)
    assert store.mark_refresh_uncertain(lease)
    assert not store.release_refresh_lease(lease)
    assert not store.replace_if_revision("1", token_state())
    assert not store.reseed_if_revision("1", token_state())
    assert not store.replace_if_revision_with_lease(lease, token_state())
    assert fs.document.data["revision"] == 1
    assert secrets.add_requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "unknown"),
        ("generation", True),
        ("credential_revision", 2),
        ("classification", REFRESH_TOKEN),
        ("dispatched_at_utc", None),
        ("candidate_version_name", "latest"),
        ("candidate_version_name", version_name(1)),
        ("cookie", "forbidden"),
    ],
)
def test_malformed_attempt_metadata_is_rejected_before_secret_access(field, value):
    store, fs, secrets = expiring_store()
    acquire(store)
    fs.document.data["refresh_attempt"][field] = value
    with pytest.raises(XTokenStateError):
        store.read()
    assert secrets.access_requests == []


@pytest.mark.parametrize("active", [False, True])
def test_new_binary_rejects_legacy_schema_without_inventing_safe_dispatch_state(active):
    document = metadata()
    document["schema_version"] = 1
    document.pop("refresh_attempt")
    document.pop("refresh_attempt_generation")
    if active:
        document.update(refresh_lease_owner="old-runtime", refresh_lease_expires_at_utc=NOW)
    store, fs, secrets = cloud_store(document=document)
    before = copy.deepcopy(fs.document.data)
    with pytest.raises(XTokenStateError, match="coordinated migration"):
        store.read()
    assert fs.document.data == before and secrets.access_requests == []


def test_legacy_exact_schema_guard_cannot_decode_new_uncertain_state():
    # Frozen schema-1 guard from the accepted/deployed implementation. No Git or
    # provider access is required to run this compatibility regression in a wheel.
    legacy_fields = {
        "schema_version",
        "revision",
        "secret_version_name",
        "previous_secret_version_name",
        "updated_at_utc",
        "refresh_lease_owner",
        "refresh_lease_expires_at_utc",
    }
    store, fs, _ = expiring_store()
    lease = acquire(store)
    store.mark_refresh_uncertain(lease)
    assert set(fs.document.data) != legacy_fields
    assert fs.document.data["schema_version"] != 1


def test_restart_uses_same_durable_attempt_and_never_refreshes_old_grant():
    store, fs, secrets = expiring_store()
    lease = acquire(store)
    store.mark_refresh_uncertain(lease)
    restarted = GoogleCloudXTokenStateStore(
        CloudXTokenStateStoreConfig(PROJECT_ID, SECRET_ID, USER_ID),
        firestore_client=fs,
        secret_manager_client=secrets,
        transactional_wrapper=FakeTransactionalWrapper(fs),
        clock=lambda: NOW,
    )
    assert_blocked(restarted)
    assert fs.document.data["refresh_attempt"]["attempt_id"] == lease.owner_id
    assert fs.document.data["refresh_attempt_generation"] == 1


def test_actual_concurrent_consumers_send_one_refresh_and_use_committed_winner():
    store, fs, _ = expiring_store()
    entered, finish = threading.Event(), threading.Event()

    class BlockingRefresh(RecordingRefreshClient):
        def refresh(self, current, credentials):
            entered.set()
            assert finish.wait(5)
            return super().refresh(current, credentials)

    refresh = BlockingRefresh(fs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(consumer(store, refresh, "good-luck").get_valid_access_token)
        try:
            assert entered.wait(5)
            with pytest.raises(XTokenConcurrencyError):
                consumer(store, refresh, "captain").get_valid_access_token()
        finally:
            finish.set()
        assert first.result() == NEW_ACCESS_TOKEN
    assert consumer(store, refresh, "captain").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert refresh.calls == 1


def test_unknown_exception_payload_is_not_exposed_or_persisted(caplog):
    store, fs, _ = expiring_store()

    class BadClient:
        def refresh(self, current, credentials):
            raise RuntimeError(REFRESH_TOKEN + NEW_REFRESH_TOKEN)

    with pytest.raises(XTokenStoreError) as caught:
        consumer(store, BadClient(), "captain").get_valid_access_token()
    rendered = str(caught.value) + repr(fs.document.data) + caplog.text
    assert REFRESH_TOKEN not in rendered and NEW_REFRESH_TOKEN not in rendered
    assert_blocked(store)


def test_explicit_operator_reauthorization_recovers_exact_uncertain_attempt():
    store, fs, secrets = expiring_store()
    lease = acquire(store)
    store.mark_refresh_uncertain(lease)
    fresh = ValidatedLocalTokenState(
        USER_ID,
        token_state(
            access_token=NEW_ACCESS_TOKEN,
            refresh_token=NEW_REFRESH_TOKEN,
        ),
    )
    assert store.recover_uncertain_if_revision(
        "1",
        lease.owner_id,
        fresh,
        consumers_quiesced=True,
    )
    assert store.read().state.refresh_token == NEW_REFRESH_TOKEN
    assert fs.document.data["refresh_attempt"]["state"] == "operator_reauthorized"
    assert not store.recover_uncertain_if_revision(
        "1",
        lease.owner_id,
        fresh,
        consumers_quiesced=True,
    )
    assert not store.begin_refresh_dispatch(lease)
    assert len(secrets.add_requests) == 1 and fs.document.data["revision"] == 2


@pytest.mark.parametrize(
    "wrong_user,quiesced,reuse_old",
    [
        (True, True, False),
        (False, False, False),
        (False, True, True),
    ],
)
def test_operator_recovery_cannot_restore_old_credential_or_skip_safety_conditions(
    wrong_user,
    quiesced,
    reuse_old,
):
    store, _, secrets = expiring_store()
    lease = acquire(store)
    store.mark_refresh_uncertain(lease)
    fresh = ValidatedLocalTokenState(
        "999" if wrong_user else USER_ID,
        token_state(
            refresh_token=REFRESH_TOKEN if reuse_old else NEW_REFRESH_TOKEN,
        ),
    )
    with pytest.raises(XTokenStateError):
        store.recover_uncertain_if_revision(
            "1",
            lease.owner_id,
            fresh,
            consumers_quiesced=quiesced,
        )
    assert_blocked(store)
    assert secrets.add_requests == []


def test_known_replacement_reconciliation_survives_competing_terminal_commit():
    store, fs, secrets = expiring_store()
    lease = acquire(store)

    def fail_commit():
        fs.fail_transaction = "before"
        fs.fail_transaction_after = 1

    secrets.after_add = fail_commit
    with pytest.raises(XTokenStoreError):
        store.replace_if_revision_with_lease(
            lease,
            token_state(
                access_token=NEW_ACCESS_TOKEN,
                refresh_token=NEW_REFRESH_TOKEN,
            ),
        )
    store.mark_refresh_uncertain(lease)
    assert store.reconcile_refresh_attempt()
    assert store.reconcile_refresh_attempt()
    assert store.read().revision == "2" and len(secrets.add_requests) == 1


def test_secret_creation_response_lost_retains_unknown_outcome_not_old_authority():
    store, fs, secrets = expiring_store()
    original = secrets.add_secret_version

    def lose_response(request):
        original(request)
        raise RuntimeError("synthetic lost Secret Manager response")

    secrets.add_secret_version = lose_response
    with pytest.raises(XTokenStoreError):
        consumer(store, RecordingRefreshClient(), "captain").get_valid_access_token()
    assert fs.document.data["refresh_attempt"]["state"] == "uncertain"
    assert fs.document.data["refresh_attempt"]["candidate_version_name"] is None
    assert version_name(2) in secrets.versions
    assert_blocked(store)
    with pytest.raises(XTokenRefreshUncertainError):
        store.reconcile_refresh_attempt()


def test_firestore_callback_retry_cannot_repeat_oauth_or_secret_calls():
    store, fs, secrets = expiring_store()
    wrapper = store._transactional

    def retrying(function):
        callback = wrapper(function)

        def transaction(tx):
            snapshot = copy.deepcopy(fs.document.data)
            callback(tx)
            fs.document.data = snapshot  # First transaction was aborted, not committed.
            return callback(tx)

        return transaction

    store._transactional = retrying
    refresh = RecordingRefreshClient(fs)
    assert consumer(store, refresh, "good-luck").get_valid_access_token() == NEW_ACCESS_TOKEN
    assert refresh.calls == 1 and len(secrets.add_requests) == 1
    assert fs.document.data["revision"] == 2


def test_distributed_store_cannot_fall_back_to_uncoordinated_cas_refresh():
    store, _, _ = expiring_store()
    with pytest.raises(XTokenStoreError, match="requires its refresh coordinator"):
        RefreshingXAccessTokenProvider(
            store,
            RecordingRefreshClient(),
            OAuthClientCredentials("fake", "fake"),
        )


def test_expired_post_dispatch_claim_marks_uncertain_in_claim_transaction():
    store, fs, _ = expiring_store()
    acquire(store)
    assert (
        store.acquire_refresh_lease(
            "1",
            owner_id="captain",
            now_utc=NOW + timedelta(minutes=1),
            expires_at_utc=NOW + timedelta(minutes=2),
        )
        is None
    )
    assert fs.document.data["refresh_attempt"]["state"] == "uncertain"
    assert_blocked(store)
