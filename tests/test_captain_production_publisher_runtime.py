import ast
from dataclasses import fields
from pathlib import Path

import pytest

from fpl_bot.captain_production_publisher_runtime import (
    PRODUCTION_OAUTH_DATABASE,
    PRODUCTION_TOKEN_SECRET,
    ProductionCaptainPublisherConfig,
)
from fpl_bot.captain_publisher_http import PublisherAuthConfig
from fpl_bot.runtime_config import ProductionConfigurationError

PRODUCTION_ID = "987654321012345678"


def environment():
    return {
        "GCP_PROJECT_ID": "fpl-frosty-bot-v1",
        "GCP_PROJECT_NUMBER": "524790767721",
        "FIRESTORE_DATABASE_ID": "captain-state",
        "X_OAUTH_FIRESTORE_DATABASE_ID": PRODUCTION_OAUTH_DATABASE,
        "CAPTAIN_PUBLISHER_ORIGIN": (
            "https://captain-production-publisher-524790767721.europe-west1.run.app"
        ),
        "CAPTAIN_PUBLISHER_INVOKER_EMAIL": (
            "captain-prod-pub-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com"
        ),
        "X_TOKEN_SECRET_ID": PRODUCTION_TOKEN_SECRET,
        "X_OAUTH_CLIENT_ID": "synthetic-client-id",
        "X_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "X_ENVIRONMENT": "production",
        "X_EXPECTED_USER_ID": PRODUCTION_ID,
        "X_POSTING_ENABLED": "false",
    }


def test_production_runtime_is_disabled_and_uses_only_production_authority():
    config = ProductionCaptainPublisherConfig.environment(
        environment(), configured_user_id=PRODUCTION_ID
    )
    assert config.captain_database == "captain-state"
    assert config.oauth_database == "production-shared-x-oauth"
    assert config.token_secret_id == "production-x-oauth-token-state"
    assert config.destination_user_id == PRODUCTION_ID
    assert config.posting_enabled is False
    assert all(getattr(config, field.name) != "(default)" for field in fields(config))


def test_production_publisher_invoker_is_an_explicit_supported_boundary():
    config = ProductionCaptainPublisherConfig.environment(
        environment(), configured_user_id=PRODUCTION_ID
    )
    auth = PublisherAuthConfig(config.origin, config.invoker_email)
    assert auth.invoker_email.startswith("captain-prod-pub-invoker@")


def test_production_runtime_cannot_start_before_reviewed_identity_is_committed():
    with pytest.raises(ProductionConfigurationError, match="has not been reviewed"):
        ProductionCaptainPublisherConfig.environment(environment(), configured_user_id=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FIRESTORE_DATABASE_ID", "(default)"),
        ("X_OAUTH_FIRESTORE_DATABASE_ID", "shared-x-oauth"),
        ("X_TOKEN_SECRET_ID", "x-oauth-token-state"),
        ("X_ENVIRONMENT", "test"),
        ("X_EXPECTED_USER_ID", "1732468005336907776"),
        ("CAPTAIN_PUBLISHER_INVOKER_EMAIL", "captain-worker@example.com"),
    ],
)
def test_production_configuration_rejects_test_good_luck_and_arbitrary_targets(name, value):
    values = environment()
    values[name] = value
    with pytest.raises(ProductionConfigurationError):
        ProductionCaptainPublisherConfig.environment(values, configured_user_id=PRODUCTION_ID)


def test_production_publisher_imports_no_compute_worker_or_good_luck_runtime():
    source = (
        Path(__file__).parents[1] / "src/fpl_bot/captain_production_publisher_runtime.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports.isdisjoint(
        {
            "fpl_bot.captain_compute",
            "fpl_bot.captain_worker",
            "fpl_bot.captain_cloud_runtime",
            "fpl_bot.post_execution",
        }
    )


def test_reproducible_production_deployment_is_disabled_private_and_unscheduled():
    root = Path(__file__).parents[1]
    deploy = (root / "deploy/deploy-captain-production-publisher.ps1").read_text(encoding="utf-8")
    provision = (root / "deploy/provision-production-x-oauth.ps1").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/CaptainProductionPublisher.Dockerfile").read_text(encoding="utf-8")
    assert "X_POSTING_ENABLED=false" in deploy
    assert "X_ENVIRONMENT=production" in deploy
    assert "production-shared-x-oauth" in deploy
    assert "production-x-oauth-token-state" in deploy
    assert "shared-x-oauth" not in deploy.replace("production-shared-x-oauth", "")
    assert "x-oauth-token-state" not in deploy.replace("production-x-oauth-token-state", "")
    assert "--no-allow-unauthenticated" in deploy
    assert "captain-prod-pub-invoker@" in deploy
    assert "cloud scheduler" not in deploy.casefold()
    assert "gcloud tasks" not in deploy.casefold()
    assert "databases/(default)" not in provision
    assert "captain_production_publisher_runtime:create_app()" in dockerfile
