"""Thin Flask adapter for the private Cloud Run deadline-task route."""

from flask import Flask, jsonify, request

from fpl_bot.deadline_task_handler import DeadlineRevalidatorBoundary, handle_deadline_task

DEADLINE_TASK_ROUTE = "/tasks/deadline"


def create_app(revalidator: DeadlineRevalidatorBoundary) -> Flask:
    """Create an injectable app; Cloud Run/IAM authenticates before this route runs."""
    app = Flask(__name__)

    @app.post(DEADLINE_TASK_ROUTE)
    def deadline_task() -> tuple[object, int]:
        outcome = handle_deadline_task(request.get_data(cache=False), revalidator)
        return jsonify(outcome.json_body()), outcome.status_code

    return app
