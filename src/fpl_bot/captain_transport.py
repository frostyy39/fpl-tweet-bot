"""Authenticated, redirect-refusing Windows-worker HTTP transport."""

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from fpl_bot.captain_handoff import CaptainAssignment
from fpl_bot.captain_orchestration_timing import require_utc, utc_text
from fpl_bot.captain_serialization import to_document
from fpl_bot.captain_worker import (
    HandoffReceipt,
    ReceiptStatus,
    ReleaseGrant,
    WorkerAssignment,
    WorkerStatus,
)


class WorkerTransportError(RuntimeError):
    def __init__(self, category: str, *, ambiguous: bool = False):
        self.category, self.ambiguous = category, ambiguous
        super().__init__(category)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class MetadataTokenConfig:
    audience: str
    service_account: str = "default"
    timeout_seconds: float = 5.0
    metadata_origin: str = "http://metadata.google.internal"

    def __post_init__(self):
        if not self.audience.startswith("https://") or urlsplit(self.audience).username:
            raise ValueError("identity-token audience must be HTTPS")
        if not self.service_account or not 0 < self.timeout_seconds <= 10:
            raise ValueError("invalid metadata token configuration")
        if self.metadata_origin != "http://metadata.google.internal":
            raise ValueError("unexpected metadata service origin")


class MetadataIdentityTokenSource:
    def __init__(self, config: MetadataTokenConfig, opener=None):
        self.config = config
        self.opener = opener or build_opener(_NoRedirect())

    def token(self) -> str:
        path = "/computeMetadata/v1/instance/service-accounts/"
        url = (
            self.config.metadata_origin
            + path
            + quote(self.config.service_account, safe="")
            + "/identity?format=full&audience="
            + quote(self.config.audience, safe="")
        )
        request = Request(url, headers={"Metadata-Flavor": "Google"})
        try:
            response = self.opener.open(request, timeout=self.config.timeout_seconds)
            token = response.read(16385).decode("ascii")
        except Exception as exc:
            raise WorkerTransportError("metadata_token_unavailable") from exc
        if len(token) > 16384 or token.count(".") != 2 or any(c.isspace() for c in token):
            raise WorkerTransportError("metadata_token_invalid")
        return token


def worker_assignment_payload(work: WorkerAssignment) -> dict:
    return {
        "schema_version": work.schema_version,
        "assignment": work.assignment.to_payload(),
        "attempt_id": str(work.attempt_id),
        "next_target": utc_text(work.next_target) if work.next_target else None,
    }


def parse_worker_assignment(data) -> WorkerAssignment:
    if type(data) is not dict or set(data) != {
        "schema_version",
        "assignment",
        "attempt_id",
        "next_target",
    }:
        raise ValueError("invalid worker assignment response")
    next_target = (
        datetime.fromisoformat(data["next_target"].replace("Z", "+00:00"))
        if data["next_target"]
        else None
    )
    if next_target:
        require_utc(next_target)
    return WorkerAssignment(
        data["schema_version"],
        CaptainAssignment.from_payload(data["assignment"]),
        UUID(data["attempt_id"]),
        next_target,
    )


class CaptainWorkerHttpClient:
    """No redirects and a new short-lived token per bounded controller call."""

    def __init__(self, endpoint: str, audience: str, tokens, opener=None, timeout_seconds=10.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("worker endpoint must be a credential-free HTTPS origin")
        if "good-luck" in endpoint.casefold() or audience != endpoint.rstrip("/"):
            raise ValueError("Captain worker audience/endpoint mismatch")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("invalid worker timeout")
        self.endpoint, self.audience, self.tokens = endpoint.rstrip("/"), audience, tokens
        self.opener, self.timeout = opener or build_opener(_NoRedirect()), timeout_seconds

    def _call(self, route: str, payload: dict, *, timeout=None):
        token = self.tokens.token()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.endpoint + route,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        try:
            response = self.opener.open(request, timeout=timeout or self.timeout)
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise WorkerTransportError("controller_response_too_large")
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise WorkerTransportError("credentialed_redirect_rejected") from None
            if exc.code == 410:
                return 410, {"status": "stale"}
            if exc.code in {401, 403}:
                raise WorkerTransportError("controller_authentication_denied") from None
            if exc.code == 409:
                raise WorkerTransportError("controller_state_conflict") from None
            raise WorkerTransportError("controller_rejected") from None
        except (TimeoutError, URLError) as exc:
            raise WorkerTransportError("controller_timeout", ambiguous=True) from exc
        except WorkerTransportError:
            raise
        except Exception as exc:
            raise WorkerTransportError("controller_malformed_response") from exc

    def obtain_assignment(self, run_id: UUID):
        status, data = self._call("/captain/worker/assignment", {"run_id": str(run_id)})
        return None if status == 204 else parse_worker_assignment(data)

    def await_release(self, work, run_id, *, timeout_seconds, cancelled):
        if cancelled():
            return False
        status, data = self._call(
            "/captain/worker/release",
            {"run_id": str(run_id), "work": worker_assignment_payload(work)},
            timeout=min(timeout_seconds, self.timeout),
        )
        if status == 202:
            return None
        if status == 410:
            return False
        issued = datetime.fromisoformat(data["issued_at"].replace("Z", "+00:00"))
        return ReleaseGrant(parse_worker_assignment(data["work"]), UUID(data["run_id"]), issued)

    def submit_handoff(self, handoff, digest, health):
        status, data = self._call(
            "/captain/worker/handoff",
            {
                "handoff": handoff.to_payload(),
                "digest": digest,
                "health": to_document(health) if health else None,
            },
        )
        if status not in {200, 201}:
            raise WorkerTransportError("handoff_rejected")
        return HandoffReceipt(
            UUID(data["generation_id"]),
            UUID(data["attempt_id"]),
            data["payload_digest"],
            ReceiptStatus(data["status"]),
        )

    def report_failure(self, work, run_id, status):
        self._call(
            "/captain/worker/failure",
            {
                "run_id": str(run_id),
                "status": WorkerStatus(status).value,
                "work": worker_assignment_payload(work) if work else None,
            },
        )
