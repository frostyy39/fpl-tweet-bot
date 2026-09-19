import ast
from dataclasses import fields
from pathlib import Path

import pytest

from fpl_bot.captain_publisher import FPLBOTTEST_USER_ID
from fpl_bot.captain_publisher_runtime import CaptainPublisherConfig
from fpl_bot.runtime_config import ProductionConfigurationError


def environment():
    return {
        "GCP_PROJECT_ID": "fpl-frosty-bot-v1",
        "GCP_PROJECT_NUMBER": "524790767721",
        "FIRESTORE_DATABASE_ID": "captain-state",
        "X_OAUTH_FIRESTORE_DATABASE_ID": "shared-x-oauth",
        "CAPTAIN_PUBLISHER_ORIGIN": ("https://captain-publisher-524790767721.europe-west1.run.app"),
        "CAPTAIN_PUBLISHER_INVOKER_EMAIL": (
            "captain-publisher-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com"
        ),
        "X_TOKEN_SECRET_ID": "fpl-bot-x-token-state",
        "X_OAUTH_CLIENT_ID": "synthetic-client-id",
        "X_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "X_ENVIRONMENT": "test",
        "X_EXPECTED_USER_ID": FPLBOTTEST_USER_ID,
        "X_POSTING_ENABLED": "false",
    }


def test_disabled_test_only_database_configuration():
    config = CaptainPublisherConfig.environment(environment())
    assert config.captain_database == "captain-state"
    assert config.oauth_database == "shared-x-oauth"
    assert config.posting_enabled is False
    assert all(getattr(config, field.name) != "(default)" for field in fields(config))


@pytest.mark.parametrize(
    "name,value",
    [
        ("FIRESTORE_DATABASE_ID", "(default)"),
        ("X_OAUTH_FIRESTORE_DATABASE_ID", "(default)"),
        ("X_EXPECTED_USER_ID", "999"),
        ("X_ENVIRONMENT", "production"),
        ("CAPTAIN_PUBLISHER_INVOKER_EMAIL", "captain-worker@example.com"),
    ],
)
def test_configuration_cannot_target_good_luck_or_another_x_identity(name, value):
    values = environment()
    values[name] = value
    with pytest.raises(ProductionConfigurationError):
        CaptainPublisherConfig.environment(values)


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def test_publisher_has_no_compute_worker_controller_or_good_luck_write_path():
    root = Path(__file__).parents[1] / "src/fpl_bot"
    publisher_imports = set()
    for name in (
        "captain_publisher.py",
        "captain_publisher_http.py",
        "captain_publisher_runtime.py",
    ):
        publisher_imports |= imported_modules(root / name)
    forbidden = {
        "fpl_bot.captain_compute",
        "fpl_bot.captain_worker",
        "fpl_bot.captain_cloud_runtime",
        "fpl_bot.firestore_state",
        "fpl_bot.post_execution",
    }
    assert publisher_imports.isdisjoint(forbidden)

    controller = (root / "captain_cloud_runtime.py").read_text(encoding="utf-8")
    worker = (root / "captain_worker.py").read_text(encoding="utf-8")
    assert "XApiClient" not in controller
    assert "create_text_post" not in controller
    assert "XApiClient" not in worker
    assert "create_text_post" not in worker


def test_reproducible_deployment_is_disabled_private_and_not_scheduled():
    root = Path(__file__).parents[1]
    script = (root / "deploy/deploy-captain-publisher.ps1").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/CaptainPublisher.Dockerfile").read_text(encoding="utf-8")
    assert "X_POSTING_ENABLED=false" in script
    assert "X_EXPECTED_USER_ID=1732468005336907776" in script
    assert "FIRESTORE_DATABASE_ID=captain-state" in script
    assert "X_OAUTH_FIRESTORE_DATABASE_ID=shared-x-oauth" in script
    assert "--no-allow-unauthenticated" in script
    assert "captain-publisher-invoker@" in script
    assert "cloud scheduler" not in script.casefold()
    assert "gcloud tasks" not in script.casefold()
    assert "captain_publisher_runtime:create_app()" in dockerfile
    probe = (root / "deploy/shared-oauth-isolation-probe.yaml").read_text(encoding="utf-8")
    assert "access_secret_version" in probe
    assert "passed_without_output" in probe
    assert "payload.data" in probe and "print(payload)" not in probe
