from datetime import timedelta

import pytest
from test_cloud_token_store import NOW, RecordingRefreshClient, acquire, cloud_store
from test_x_refresh_crash_safety import assert_blocked, consumer

from fpl_bot.x_errors import XTokenRefreshUncertainError


def test_production_uncertainty_blocks_only_the_production_authority():
    test_store, _, _ = cloud_store()
    production_store, _, _ = cloud_store()
    lease = acquire(production_store, owner="production-captain")
    assert lease is not None
    assert production_store.mark_refresh_uncertain(lease)

    assert_blocked(production_store)
    assert test_store.read().revision == "1"
    assert (
        test_store.acquire_refresh_lease(
            "1",
            owner_id="fplbottest-good-luck",
            now_utc=NOW,
            expires_at_utc=NOW + timedelta(minutes=1),
        )
        is not None
    )


def test_test_uncertainty_never_authorizes_the_production_authority():
    test_store, _, _ = cloud_store()
    production_store, _, _ = cloud_store()
    lease = acquire(test_store, owner="fplbottest-captain")
    assert lease is not None
    assert test_store.mark_refresh_uncertain(lease)

    with pytest.raises(XTokenRefreshUncertainError):
        consumer(
            test_store, RecordingRefreshClient(), "fplbottest-good-luck"
        ).get_valid_access_token()
    assert production_store.read().revision == "1"
    assert (
        production_store.acquire_refresh_lease(
            "1",
            owner_id="production-good-luck",
            now_utc=NOW,
            expires_at_utc=NOW + timedelta(minutes=1),
        )
        is not None
    )


def test_each_environment_retains_the_same_schema_two_crash_safety_contract():
    for owner in ("fplbottest-captain", "production-captain"):
        store, firestore, _ = cloud_store()
        lease = acquire(store, owner=owner)
        assert lease is not None
        assert store.mark_refresh_uncertain(lease)
        assert firestore.document.data["schema_version"] == 2
        assert firestore.document.data["refresh_attempt"]["state"] == "uncertain"
        assert_blocked(store)
