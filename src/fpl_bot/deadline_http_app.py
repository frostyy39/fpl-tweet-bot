"""Thin Flask adapter for private Cloud Run task and checker routes."""

from flask import Flask, jsonify, request

from fpl_bot.checker_http_handler import DeadlineCheckerBoundary, handle_checker_run
from fpl_bot.deadline_task_handler import DeadlineRevalidatorBoundary, handle_deadline_task

CHECKER_RUN_ROUTE = "/checker/run"
DEADLINE_TASK_ROUTE = "/tasks/deadline"


def create_app(
    revalidator: DeadlineRevalidatorBoundary,
    *,
    checker: DeadlineCheckerBoundary | None = None,
) -> Flask:
    """Create an injectable app; Cloud Run/IAM authenticates before this route runs."""
    app = Flask(__name__)

    @app.post(DEADLINE_TASK_ROUTE)
    def deadline_task() -> tuple[object, int]:
        outcome = handle_deadline_task(request.get_data(cache=False), revalidator)
        return jsonify(outcome.json_body()), outcome.status_code

    if checker is not None:

        @app.post(CHECKER_RUN_ROUTE)
        def checker_run() -> tuple[object, int]:
            outcome = handle_checker_run(checker)
            return jsonify(outcome.json_body()), outcome.status_code

    return app
