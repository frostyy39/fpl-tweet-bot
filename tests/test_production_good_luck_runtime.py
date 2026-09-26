from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask
from test_cloud_token_store import NOW, acquire, cloud_store
from test_x_refresh_crash_safety import assert_blocked

from fpl_bot.captain_production_publisher_runtime import (
    PRODUCTION_OAUTH_DATABASE,
    PRODUCTION_TOKEN_SECRET,
)
from fpl_bot.deadline_http_app import CHECKER_RUN_ROUTE, DEADLINE_TASK_ROUTE, PREFLIGHT_TASK_ROUTE
from fpl_bot.posting_state import EventPostingContext, InMemoryPostingStateStore
from fpl_bot.production_good_luck_runtime import (
    BUSINESS_DATABASE,
    INVOKER_EMAIL,
    OAUTH_DATABASE,
    PROJECT_ID,
    PROJECT_NUMBER,
    SERVICE_ORIGIN,
    TASK_LOCATION,
    TASK_QUEUE,
    TOKEN_SECRET,
    ProductionGoodLuckConfig,
    create_app,
)
from fpl_bot.production_x_identity import PRODUCTION_X_USER_ID
from fpl_bot.runtime_config import ProductionConfigurationError

TEST_X_USER_ID = "1732468005336907776"


def environment(*, posting_enabled: str = "false") -> dict[str, str]:
    return {
        "GCP_PROJECT_ID": PROJECT_ID,
        "GCP_PROJECT_NUMBER": PROJECT_NUMBER,
        "FIRESTORE_DATABASE_ID": BUSINESS_DATABASE,
        "X_OAUTH_FIRESTORE_DATABASE_ID": OAUTH_DATABASE,
        "X_TOKEN_SECRET_ID": TOKEN_SECRET,
        "CLOUD_TASKS_LOCATION_ID": TASK_LOCATION,
        "CLOUD_TASKS_QUEUE_ID": TASK_QUEUE,
        "CLOUD_RUN_BASE_URL": SERVICE_ORIGIN,
        "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL": INVOKER_EMAIL,
        "CLOUD_TASKS_OIDC_AUDIENCE": SERVICE_ORIGIN,
        "X_OAUTH_CLIENT_ID": "synthetic-client-id",
        "X_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "X_ENVIRONMENT": "production",
        "X_EXPECTED_USER_ID": PRODUCTION_X_USER_ID or "",
        "X_POSTING_ENABLED": posting_enabled,
    }


def test_production_good_luck_uses_fixed_isolated_resources_and_shared_production_oauth():
    config = ProductionGoodLuckConfig.from_environment(environment()).runtime
    assert config.firestore_database_id == "production-good-luck-state"
    assert config.firestore_database_id != "(default)"
    assert config.x_oauth_firestore_database_id == PRODUCTION_OAUTH_DATABASE
    assert config.x_token_secret_id == PRODUCTION_TOKEN_SECRET
    assert config.x_posting.expected_user_id == "1249335464571650048"
    assert config.x_posting.expected_user_id != TEST_X_USER_ID
    assert config.deadline_tasks.queue_id == "production-good-luck-deadline"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FIRESTORE_DATABASE_ID", "(default)"),
        ("X_OAUTH_FIRESTORE_DATABASE_ID", "shared-x-oauth"),
        ("X_TOKEN_SECRET_ID", "x-oauth-token-state"),
        ("CLOUD_TASKS_QUEUE_ID", "fpl-deadline"),
        ("CLOUD_RUN_BASE_URL", "https://fpl-bot.example"),
        (
            "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL",
            "fpl-bot-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com",
        ),
        ("CLOUD_TASKS_OIDC_AUDIENCE", "https://fpl-bot.example"),
        ("X_ENVIRONMENT", "test"),
        ("X_EXPECTED_USER_ID", TEST_X_USER_ID),
        ("X_EXPECTED_USER_ID", "999999999"),
    ],
)
def test_production_good_luck_rejects_test_or_arbitrary_configuration(name, value):
    values = environment()
    values[name] = value
    with pytest.raises(ProductionConfigurationError, match=name):
        ProductionGoodLuckConfig.from_environment(values)


def test_missing_production_configuration_fails_closed():
    values = environment()
    del values["FIRESTORE_DATABASE_ID"]
    with pytest.raises(ProductionConfigurationError, match="FIRESTORE_DATABASE_ID"):
        ProductionGoodLuckConfig.from_environment(values)


def test_disabled_runtime_never_composes_state_tasks_oauth_or_x():
    composed = False

    def factory(_):
        nonlocal composed
        composed = True
        raise AssertionError("disabled runtime composed production adapters")

    app = create_app(environment(), production_factory=factory)
    client = app.test_client()
    for route in (CHECKER_RUN_ROUTE, DEADLINE_TASK_ROUTE, PREFLIGHT_TASK_ROUTE):
        response = client.post(route, data=b'{"untrusted":"payload"}')
        assert response.status_code == 200
        assert response.get_json() == {"status": "disabled"}
    assert composed is False


def test_enabled_runtime_reuses_the_existing_production_composition():
    expected = Flask("expected")
    received = None

    def factory(values):
        nonlocal received
        received = values
        return expected

    values = environment(posting_enabled="true")
    assert create_app(values, production_factory=factory) is expected
    assert received is values


def test_test_and_production_good_luck_idempotency_records_are_independent():
    test_store = InMemoryPostingStateStore(claim_id_factory=lambda: "test-claim")
    production_store = InMemoryPostingStateStore(claim_id_factory=lambda: "production-claim")
    context = EventPostingContext(
        event_id=6,
        event_code="GW6",
        official_deadline_utc=datetime(2026, 10, 10, 10, tzinfo=UTC),
    )
    claimed_at = datetime(2026, 10, 10, 10, tzinfo=UTC)
    test_claim = test_store.claim_event(context, claimed_at_utc=claimed_at)
    production_claim = production_store.claim_event(context, claimed_at_utc=claimed_at)
    assert test_claim.granted and production_claim.granted
    assert test_claim.claim != production_claim.claim


def test_production_captain_and_good_luck_share_one_refresh_authority():
    store, _, _ = cloud_store()
    captain = acquire(store, owner="production-captain")
    assert captain is not None
    assert (
        store.acquire_refresh_lease(
            captain.expected_revision,
            owner_id="production-good-luck",
            now_utc=NOW,
            expires_at_utc=NOW + timedelta(minutes=1),
        )
        is None
    )
    assert store.mark_refresh_uncertain(captain)
    assert_blocked(store)
    assert (
        store.acquire_refresh_lease(
            captain.expected_revision,
            owner_id="production-good-luck",
            now_utc=NOW + timedelta(minutes=2),
            expires_at_utc=NOW + timedelta(minutes=3),
        )
        is None
    )


def test_reproducible_deployment_is_private_disabled_and_separate():
    root = Path(__file__).parents[1]
    provision = (root / "deploy/provision-production-good-luck.ps1").read_text(encoding="utf-8")
    deploy = (root / "deploy/deploy-production-good-luck.ps1").read_text(encoding="utf-8")
    build = (root / "deploy/good-luck-production-build.yaml").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/GoodLuckProduction.Dockerfile").read_text(encoding="utf-8")
    assert "production-good-luck-state" in provision and "production-good-luck-state" in deploy
    assert "production-shared-x-oauth" in provision and "production-shared-x-oauth" in deploy
    assert (
        "production-x-oauth-token-state" in provision and "production-x-oauth-token-state" in deploy
    )
    assert "[switch]$EnablePosting" in deploy
    assert "if ($EnablePosting) { 'true' } else { 'false' }" in deploy
    assert "production_readiness" in deploy
    assert "--no-allow-unauthenticated" in deploy
    assert '"--schedule=0 6 * * *"' in deploy
    assert "scheduler jobs pause" in deploy
    assert "tasks queues pause" in deploy
    assert "logging: CLOUD_LOGGING_ONLY" in build
    assert "--condition=None" in provision
    assert "production_good_luck_runtime:create_app()" in dockerfile
    assert "fpl-bot-runtime@" not in provision
    assert "fpl-bot-invoker@" not in deploy


def test_live_probe_definitions_are_non_posting_and_cover_effective_boundaries():
    root = Path(__file__).parents[1]
    names = (
        "production-good-luck-isolation-probe.yaml",
        "test-good-luck-production-denial-probe.yaml",
        "production-good-luck-invocation-probe.yaml",
        "production-good-luck-wrong-caller-probe.yaml",
        "production-good-luck-identity-probe.yaml",
    )
    probes = [(root / "deploy" / name).read_text(encoding="utf-8") for name in names]
    assert "good-luck-production-runtime@" in probes[0]
    assert "fpl-bot-runtime@" in probes[1]
    assert "good-luck-production-invoker@" in probes[2]
    assert "captain-worker@" in probes[3]
    assert "fpl-bot-x-verify" in probes[4]
    assert "X_POSTING_ENABLED" in probes[4] and "'false'" in probes[4]
    assert "1249335464571650048" in probes[4]
    assert "production-good-luck-state" in probes[0] and "'(default)'" in probes[0]
    assert "production-shared-x-oauth" in probes[0] and "shared-x-oauth" in probes[0]
    assert "body" not in probes[0]
    assert "results == [(200, {'status': 'disabled'})] * 3" in probes[2]
    for probe in probes:
        assert "/2/tweets" not in probe
        assert "create_text_post" not in probe
        assert "X_POSTING_ENABLED=true" not in probe
        assert "print(token" not in probe
