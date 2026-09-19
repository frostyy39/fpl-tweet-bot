"""Authenticated, non-generic HTTP boundary for the dedicated Captain publisher."""

from dataclasses import dataclass

from flask import Flask, jsonify, request

from fpl_bot.captain_http import RequestAuthorizer
from fpl_bot.captain_publisher import PublicationInstruction, PublishStatus
from fpl_bot.captain_state import StateConflict


@dataclass(frozen=True, slots=True)
class PublisherAuthConfig:
    audience: str
    invoker_email: str

    def __post_init__(self) -> None:
        if not self.audience.startswith("https://") or not self.invoker_email.startswith(
            "captain-publisher-invoker@"
        ):
            raise ValueError("dedicated Captain publisher invocation identity required")


def create_publisher_app(publisher, authorizer: RequestAuthorizer, auth: PublisherAuthConfig):
    app = Flask("captain-publisher")

    @app.errorhandler(PermissionError)
    def denied(_):
        return jsonify({"error": "unauthorized"}), 403

    def rejected(_):
        return jsonify({"error": "captain_publication_rejected"}), 409

    app.register_error_handler(StateConflict, rejected)
    app.register_error_handler(ValueError, rejected)

    @app.post("/captain/publisher/execute")
    def execute():
        caller = authorizer.authorize(request.headers.get("Authorization", ""), auth.audience)
        if caller.email != auth.invoker_email:
            raise PermissionError("publisher caller identity denied")
        instruction = PublicationInstruction.from_payload(request.get_json(force=True))
        result = publisher.publish(instruction)
        status = 503 if result.status == PublishStatus.UNCERTAIN else 200
        return jsonify(
            {
                "status": result.status.value,
                "event_id": result.event_id,
                "post_id": result.post_id,
            }
        ), status

    return app
