import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError
from uuid import UUID

import pytest

from fpl_bot.captain_handoff import CaptainAssignment
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_transport import (
    CaptainWorkerHttpClient,
    MetadataIdentityTokenSource,
    MetadataTokenConfig,
    WorkerTransportError,
    parse_worker_assignment,
    worker_assignment_payload,
)
from fpl_bot.captain_worker import WorkerAssignment

T = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)
WORK = WorkerAssignment(
    1,
    CaptainAssignment(UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(T + timedelta(hours=2))),
    UUID(int=3),
)


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self, size):
        return self.body


class Opener:
    def __init__(self, response):
        self.response, self.requests = response, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_metadata_token_uses_exact_audience_and_never_exposes_token():
    token = "header.private-signature.payload"
    opener = Opener(Response(token.encode("ascii")))
    source = MetadataIdentityTokenSource(
        MetadataTokenConfig("https://captain.example.run.app"), opener
    )
    assert source.token() == token
    request = opener.requests[0][0]
    assert "audience=https%3A%2F%2Fcaptain.example.run.app" in request.full_url
    assert request.get_header("Metadata-flavor") == "Google"
    opener.response = RuntimeError("Bearer do-not-log")
    with pytest.raises(WorkerTransportError) as error:
        source.token()
    assert "do-not-log" not in str(error.value)


def test_worker_assignment_codec_is_strict_and_preserves_identity():
    assert parse_worker_assignment(worker_assignment_payload(WORK)) == WORK
    malformed = worker_assignment_payload(WORK)
    malformed["extra"] = True
    with pytest.raises(ValueError):
        parse_worker_assignment(malformed)


class Tokens:
    def token(self):
        return "head.payload.signature"


def test_worker_http_authenticates_and_refuses_redirects_without_secret_error():
    payload = json.dumps(worker_assignment_payload(WORK)).encode()
    opener = Opener(Response(payload))
    client = CaptainWorkerHttpClient(
        "https://captain.example.run.app", "https://captain.example.run.app", Tokens(), opener
    )
    assert client.obtain_assignment(UUID(int=9)) == WORK
    request = opener.requests[0][0]
    assert request.full_url.endswith("/captain/worker/assignment")
    assert request.get_header("Authorization") == "Bearer head.payload.signature"
    opener.response = HTTPError(
        request.full_url, 302, "redirect", {"Location": "https://evil.invalid"}, BytesIO()
    )
    with pytest.raises(WorkerTransportError) as error:
        client.obtain_assignment(UUID(int=9))
    assert error.value.category == "credentialed_redirect_rejected"
    assert "head.payload.signature" not in str(error.value)


def test_worker_endpoint_is_captain_scoped_and_audience_bound():
    with pytest.raises(ValueError):
        CaptainWorkerHttpClient("http://captain.invalid", "http://captain.invalid", Tokens())
    with pytest.raises(ValueError):
        CaptainWorkerHttpClient(
            "https://good-luck.example.run.app", "https://good-luck.example.run.app", Tokens()
        )
    with pytest.raises(ValueError):
        CaptainWorkerHttpClient(
            "https://captain.example.run.app", "https://different.example.run.app", Tokens()
        )
