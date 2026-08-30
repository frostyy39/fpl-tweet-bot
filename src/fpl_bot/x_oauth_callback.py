"""Exact-path loopback callback receiver for local X OAuth authorization."""

import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

from fpl_bot.x_errors import XOAuthCallbackError
from fpl_bot.x_oauth import (
    OAUTH_CALLBACK_HOST,
    OAUTH_CALLBACK_PATH,
    OAUTH_CALLBACK_PORT,
    AuthorizationCode,
    parse_callback_target,
)


class LoopbackOAuthCallbackReceiver:
    def __init__(
        self,
        *,
        timeout_seconds: float = 300.0,
        server_factory: Callable[..., HTTPServer] = HTTPServer,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._timeout_seconds = timeout_seconds
        self._server_factory = server_factory
        self._monotonic = monotonic

    def receive(
        self,
        expected_state: str,
        on_listening: Callable[[], None],
    ) -> AuthorizationCode:
        outcome: dict[str, AuthorizationCode | XOAuthCallbackError] = {}
        handler_class = _build_callback_handler(expected_state, outcome)
        try:
            server = self._server_factory(
                (OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT),
                handler_class,
            )
        except OSError as exc:
            raise XOAuthCallbackError(
                "Could not listen on the configured OAuth loopback callback"
            ) from exc
        deadline = self._monotonic() + self._timeout_seconds
        try:
            on_listening()
            while "result" not in outcome:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise XOAuthCallbackError("Timed out waiting for the X OAuth callback")
                server.timeout = min(remaining, 0.5)
                try:
                    server.handle_request()
                except OSError as exc:
                    raise XOAuthCallbackError("OAuth loopback callback failed") from exc
        finally:
            server.server_close()
        result = outcome["result"]
        if isinstance(result, XOAuthCallbackError):
            raise result
        return result


def _build_callback_handler(
    expected_state: str,
    outcome: dict[str, AuthorizationCode | XOAuthCallbackError],
) -> type[BaseHTTPRequestHandler]:
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != OAUTH_CALLBACK_PATH:
                self._respond(404, "Not found")
                return
            try:
                result: AuthorizationCode | XOAuthCallbackError = parse_callback_target(
                    self.path,
                    expected_state,
                )
            except XOAuthCallbackError as exc:
                result = exc
                status = 400
                message = "Authorization failed. Return to the terminal."
            else:
                status = 200
                message = "Authorization received. You may close this window."
            outcome["result"] = result
            self._respond(status, message)

        def do_POST(self) -> None:
            self._respond(405, "Method not allowed")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return OAuthCallbackHandler
