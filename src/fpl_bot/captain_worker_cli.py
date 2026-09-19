"""Password-free startup entry point for the same-user Captain NO-POST worker."""

import argparse
import json
import re
import signal
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from fpl_bot.captain_result_directory import create_result_directory
from fpl_bot.captain_transport import (
    CaptainWorkerHttpClient,
    MetadataIdentityTokenSource,
    MetadataTokenConfig,
)
from fpl_bot.captain_worker import CaptainWorker
from fpl_bot.captain_worker_browser import ProvenBrowserAcquisition


class Clock:
    def now(self):
        return datetime.now(UTC)


def _worker_build_id(config_path: Path) -> str:
    """Read only the package commit marker; never infer version from mutable source."""
    try:
        document = json.loads(
            (config_path.parent / "worker-build.json").read_text(encoding="utf-8")
        )
        if set(document) != {"schema_version", "commit"} or document["schema_version"] != 1:
            return "unverified"
        commit = document["commit"]
        return (
            commit
            if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
            else "unverified"
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unverified"


class BoundedPollingClient:
    """Wait only following a pending release, in bounded cancellable intervals."""

    def __init__(self, client, clock, stop):
        self.client, self.clock, self.stop = client, clock, stop

    def obtain_assignment(self, run_id):
        return self.client.obtain_assignment(run_id)

    def await_release(self, work, run_id, *, timeout_seconds, cancelled):
        reply = self.client.await_release(
            work, run_id, timeout_seconds=timeout_seconds, cancelled=cancelled
        )
        if reply is None:
            remaining = (work.assignment.timing.expiry_utc - self.clock.now()).total_seconds()
            self.stop.wait(max(0, min(10, remaining)))
        return reply

    def submit_handoff(self, *args):
        return self.client.submit_handoff(*args)

    def report_failure(self, *args):
        return self.client.report_failure(*args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    started = datetime.now(UTC)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if set(config) != {"controller_origin", "profile_dir", "expected_user", "audit_parent"}:
            raise ValueError("invalid worker configuration")
        origin = config["controller_origin"]
        tokens = MetadataIdentityTokenSource(MetadataTokenConfig(origin))
        client = BoundedPollingClient(
            CaptainWorkerHttpClient(origin, origin, tokens), Clock(), stop
        )
        output = Path(config["audit_parent"]) / str(uuid4())
        create_result_directory(output)
        result = CaptainWorker(
            client,
            ProvenBrowserAcquisition(Path(config["profile_dir"])),
            Clock(),
            expected_user=config["expected_user"],
            cancelled=stop.is_set,
        ).run()
        audit = {
            "schema_version": 2,
            "no_post": True,
            "worker_build_id": _worker_build_id(args.config),
            "started_at_utc": started.isoformat(),
            "ended_at_utc": datetime.now(UTC).isoformat(),
            "status": result.status.value,
            "exit_code": result.exit_code,
            "handoff": result.handoff.to_payload() if result.handoff else None,
            "payload_digest": result.handoff.payload_digest if result.handoff else None,
            "acquisition_failure": (
                result.acquisition_failure.to_payload() if result.acquisition_failure else None
            ),
        }
        with (output / "audit.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(audit, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return result.exit_code
    except Exception:
        # Never echo arbitrary metadata/HTTP/profile exceptions (potential secrets).
        print("captain_worker_failed_closed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
