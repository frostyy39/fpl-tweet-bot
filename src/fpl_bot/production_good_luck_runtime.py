"""Strict production-account Good Luck runtime with a pre-composition disabled gate."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from flask import Flask, jsonify

from fpl_bot.deadline_http_app import CHECKER_RUN_ROUTE, DEADLINE_TASK_ROUTE, PREFLIGHT_TASK_ROUTE
from fpl_bot.production import ProductionRuntimeConfig, create_production_app
from fpl_bot.production_x_identity import PRODUCTION_X_USER_ID
from fpl_bot.runtime_config import ProductionConfigurationError
from fpl_bot.x_config import PRODUCTION_ENVIRONMENT

PROJECT_ID = "fpl-frosty-bot-v1"
PROJECT_NUMBER = "524790767721"
BUSINESS_DATABASE = "production-good-luck-state"
OAUTH_DATABASE = "production-shared-x-oauth"
TOKEN_SECRET = "production-x-oauth-token-state"
TASK_LOCATION = "europe-west2"
TASK_QUEUE = "production-good-luck-deadline"
SERVICE_ORIGIN = "https://good-luck-production-524790767721.europe-west1.run.app"
INVOKER_EMAIL = "good-luck-production-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com"


@dataclass(frozen=True, slots=True)
class ProductionGoodLuckConfig:
    """Validated fixed production resource and identity boundary."""

    runtime: ProductionRuntimeConfig

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "ProductionGoodLuckConfig":
        source = os.environ if environ is None else environ
        if PRODUCTION_X_USER_ID is None:
            raise ProductionConfigurationError("Production X identity has not been reviewed")
        expected = {
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
            "X_ENVIRONMENT": PRODUCTION_ENVIRONMENT,
            "X_EXPECTED_USER_ID": PRODUCTION_X_USER_ID,
        }
        for name, expected_value in expected.items():
            if source.get(name) != expected_value:
                raise ProductionConfigurationError(
                    f"{name} must match the reviewed production Good Luck boundary"
                )
        return cls(ProductionRuntimeConfig.from_environment(source))

    @property
    def posting_enabled(self) -> bool:
        return self.runtime.x_posting.posting_enabled


ProductionAppFactory = Callable[[Mapping[str, str] | None], Flask]


def create_app(
    environ: Mapping[str, str] | None = None,
    *,
    production_factory: ProductionAppFactory = create_production_app,
) -> Flask:
    """Build the shared Good Luck graph only after the server-side enablement gate."""

    config = ProductionGoodLuckConfig.from_environment(environ)
    if config.posting_enabled:
        return production_factory(environ)
    return _disabled_app()


def _disabled_app() -> Flask:
    app = Flask(__name__)

    def disabled() -> tuple[object, int]:
        return jsonify({"status": "disabled"}), 200

    app.add_url_rule(CHECKER_RUN_ROUTE, "disabled_checker", disabled, methods=["POST"])
    app.add_url_rule(DEADLINE_TASK_ROUTE, "disabled_deadline", disabled, methods=["POST"])
    app.add_url_rule(PREFLIGHT_TASK_ROUTE, "disabled_preflight", disabled, methods=["POST"])
    return app
