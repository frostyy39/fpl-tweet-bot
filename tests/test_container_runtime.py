import importlib
from pathlib import Path
from typing import NoReturn

import pytest
from flask import Flask

import fpl_bot.production as production
import fpl_bot.wsgi as wsgi
from fpl_bot.deadline_http_app import (
    CHECKER_RUN_ROUTE,
    DEADLINE_TASK_ROUTE,
    PREFLIGHT_TASK_ROUTE,
    create_app,
)
from fpl_bot.production import ProductionConfigurationError

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


class NoOpBoundary:
    """Route-registration stand-in; no method is invoked while constructing the app."""


def test_importing_wsgi_does_not_construct_application(monkeypatch: pytest.MonkeyPatch) -> None:
    original_factory = production.create_production_app
    calls = 0

    def forbidden_factory() -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("application construction occurred during import")

    monkeypatch.setattr(production, "create_production_app", forbidden_factory)
    importlib.reload(wsgi)

    assert calls == 0
    monkeypatch.setattr(wsgi, "create_production_app", original_factory)


def test_wsgi_factory_delegates_once_to_existing_production_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = Flask("production-test")
    calls = 0

    def fake_factory() -> Flask:
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(wsgi, "create_production_app", fake_factory)

    assert wsgi.create_app() is expected
    assert calls == 1


def test_wsgi_application_preserves_all_private_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = create_app(NoOpBoundary(), checker=NoOpBoundary(), preflight=NoOpBoundary())
    monkeypatch.setattr(wsgi, "create_production_app", lambda: expected)

    app = wsgi.create_app()
    methods_by_route = {
        rule.rule: rule.methods
        for rule in app.url_map.iter_rules()
        if rule.rule != "/static/<path:filename>"
    }

    assert set(methods_by_route) == {
        CHECKER_RUN_ROUTE,
        DEADLINE_TASK_ROUTE,
        PREFLIGHT_TASK_ROUTE,
    }
    assert all("POST" in methods for methods in methods_by_route.values())


def test_wsgi_startup_does_not_invoke_route_business_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = create_app(NoOpBoundary(), checker=NoOpBoundary(), preflight=NoOpBoundary())
    monkeypatch.setattr(wsgi, "create_production_app", lambda: expected)

    assert wsgi.create_app() is expected


def test_missing_runtime_config_fails_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "unit-test-sensitive-value-never-print"

    def create_with_missing_config() -> Flask:
        return production.create_production_app({"X_OAUTH_CLIENT_SECRET": secret})

    monkeypatch.setattr(wsgi, "create_production_app", create_with_missing_config)

    with pytest.raises(ProductionConfigurationError) as error:
        wsgi.create_app()

    output = capsys.readouterr()
    assert secret not in str(error.value)
    assert secret not in output.out
    assert secret not in output.err


def test_docker_command_uses_port_and_conservative_gunicorn_settings() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "gunicorn" in dockerfile
    assert "0.0.0.0:${PORT:-8080}" in dockerfile
    assert "--workers 1" in dockerfile
    assert "--worker-class gthread" in dockerfile
    assert "--threads 2" in dockerfile
    assert "fpl_bot.wsgi:create_app()" in dockerfile
    assert "flask run" not in dockerfile.lower()


def test_dockerfile_copies_only_runtime_inputs_and_runs_non_root() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY . " not in dockerfile
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "USER app" in dockerfile
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in dockerfile
    assert "service-account" not in dockerfile.lower()


def test_dockerignore_excludes_sensitive_and_machine_specific_files() -> None:
    patterns = set(DOCKERIGNORE.read_text(encoding="utf-8").splitlines())

    assert {
        ".git",
        ".venv",
        ".env",
        ".env.*",
        "**/*.key",
        "**/*.pem",
        "**/*.dpapi",
        "**/*.token",
        "**/oauth-token*.json",
        "**/*oauth*handoff*",
        "**/credentials*.json",
        "**/service-account*.json",
        ".idea",
        ".vscode",
        ".DS_Store",
        "Thumbs.db",
    } <= patterns


def test_dockerfile_and_ignore_contain_no_credential_values() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8") + DOCKERIGNORE.read_text(encoding="utf-8")

    assert "BEGIN PRIVATE KEY" not in content
    assert "X_USER_ACCESS_TOKEN=" not in content
    assert "X_OAUTH_CLIENT_SECRET=" not in content
    assert "X_EXPECTED_USER_ID=" not in content
