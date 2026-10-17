import getpass
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fpl_bot import captain_worker_cli as cli
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_release_diagnostics import ReleaseRejectionCode, release_diagnostic
from fpl_bot.captain_review_browser import ReviewRankedPrefixEvidence
from fpl_bot.captain_worker import (
    AcquisitionFailureCode,
    AcquisitionFailureDiagnostic,
    AcquisitionStage,
    WorkerResult,
    WorkerStatus,
)


def test_no_assignment_cli_writes_private_utf8_audit_without_touching_browser(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "controller_origin": "https://captain-controller.example",
                "profile_dir": str(tmp_path / "unopened-profile"),
                "expected_user": getpass.getuser(),
                "audit_parent": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def __init__(self, *_):
            pass

        def obtain_assignment(self, run_id):
            return None

    monkeypatch.setattr(cli, "CaptainWorkerHttpClient", Client)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        cli.ProvenBrowserAcquisition,
        "acquire",
        lambda *_: (_ for _ in ()).throw(AssertionError("no browser")),
    )
    assert cli.main(["--config", str(config)]) == 0
    audits = list(tmp_path.glob("*/audit.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_bytes().decode("utf-8", errors="strict"))
    assert audit["status"] == "no_assignment" and audit["no_post"] is True
    assert audit["schema_version"] == 4
    assert audit["projection_coverage"] is None
    assert audit["worker_build_id"] == "unverified"
    assert audit["acquisition_failure"] is None
    assert audit["release_rejection"] is None
    assert audit["handoff"] is None
    assert not (tmp_path / "unopened-profile").exists()


def test_invalid_cli_configuration_fails_closed_before_metadata_or_browser(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config.json"
    config.write_text('{"password":"never-used"}', encoding="utf-8")
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main(["--config", str(config)]) == 2
    assert capsys.readouterr().out == "captain_worker_failed_closed\n"
    assert list(tmp_path.glob("*/audit.json")) == []


def test_cli_writes_versioned_redacted_acquisition_failure(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "controller_origin": "https://captain-controller.example",
                "profile_dir": str(tmp_path / "profile"),
                "expected_user": getpass.getuser(),
                "audit_parent": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    commit = "a" * 40
    (tmp_path / "worker-build.json").write_text(
        json.dumps({"schema_version": 1, "commit": commit}), encoding="utf-8"
    )
    diagnostic = AcquisitionFailureDiagnostic(
        1,
        AcquisitionFailureCode.NAVIGATION_FAILED,
        AcquisitionStage.NAVIGATION,
        True,
        True,
        True,
        True,
        29,
        "CaptainReviewBrowserError",
    )

    class Client:
        def __init__(self, *_):
            pass

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return WorkerResult(WorkerStatus.ACQUISITION_FAILED, acquisition_failure=diagnostic)

    monkeypatch.setattr(cli, "CaptainWorkerHttpClient", Client)
    monkeypatch.setattr(cli, "CaptainWorker", Worker)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main(["--config", str(config)]) == 1
    audit_path = next(path for path in tmp_path.glob("*/audit.json"))
    raw = audit_path.read_text(encoding="utf-8")
    audit = json.loads(raw)
    assert audit["worker_build_id"] == commit
    assert audit["acquisition_failure"] == diagnostic.to_payload()
    assert audit["release_rejection"] is None
    for secret in (
        "captain-controller.example",
        str(tmp_path / "profile"),
        "COOKIE_TOKEN_PASSWORD_SENTINEL",
    ):
        assert secret.casefold() not in raw.casefold()


def test_cli_persists_only_allowlisted_release_rejection(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "controller_origin": "https://captain-controller.example",
                "profile_dir": str(tmp_path / "SECRET_PROFILE"),
                "expected_user": getpass.getuser(),
                "audit_parent": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    target = datetime(2026, 10, 10, 8, tzinfo=UTC)
    diagnostic = release_diagnostic(
        ReleaseRejectionCode.ISSUED_IN_WORKER_FUTURE,
        CaptainTiming(target + timedelta(hours=2)),
        target,
        target + timedelta(milliseconds=100),
        target + timedelta(milliseconds=150),
        target + timedelta(milliseconds=175),
    )

    class Client:
        def __init__(self, *_):
            pass

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return WorkerResult(WorkerStatus.RELEASE_REJECTED, release_rejection=diagnostic)

    monkeypatch.setattr(cli, "CaptainWorkerHttpClient", Client)
    monkeypatch.setattr(cli, "CaptainWorker", Worker)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main(["--config", str(config)]) == 1
    raw = next(tmp_path.glob("*/audit.json")).read_text(encoding="utf-8")
    audit = json.loads(raw)
    assert audit["schema_version"] == 4
    assert audit["projection_coverage"] is None
    assert audit["release_rejection"] == diagnostic.to_payload()
    assert audit["handoff"] is None
    assert "SECRET_PROFILE" not in raw


def test_worker_build_id_rejects_malformed_manifest(tmp_path):
    for document in (
        {"schema_version": 1, "commit": "not-a-commit"},
        {"schema_version": 2, "commit": "a" * 40},
        {"schema_version": 1, "commit": "a" * 40, "extra": True},
    ):
        (tmp_path / "worker-build.json").write_text(json.dumps(document), encoding="utf-8")
        assert cli._worker_build_id(tmp_path / "config.json") == "unverified"


def test_cli_persists_sanitized_coverage_without_overwriting_old_audit(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "controller_origin": "https://captain-controller.example",
                "profile_dir": str(tmp_path / "SECRET_PROFILE"),
                "expected_user": getpass.getuser(),
                "audit_parent": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    historical = tmp_path / "historical"
    historical.mkdir()
    old_audit = historical / "audit.json"
    old_audit.write_text("ORIGINAL_AUDIT", encoding="utf-8")
    proof = ReviewRankedPrefixEvidence(
        8, datetime(2026, 10, 17, tzinfo=UTC), 50, "b" * 64, Decimal(9), Decimal(4), 1, 5
    )

    class Worker:
        def __init__(self, client, acquisition, *_args, **_kwargs):
            self.acquisition = acquisition

        def run(self):
            self.acquisition.last_coverage = proof
            return WorkerResult(WorkerStatus.SUCCEEDED)

    monkeypatch.setattr(cli, "CaptainWorkerHttpClient", lambda *_: object())
    monkeypatch.setattr(cli, "CaptainWorker", Worker)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main(["--config", str(config)]) == 0
    paths = [p for p in tmp_path.glob("*/audit.json") if p != old_audit]
    assert len(paths) == 1
    raw = paths[0].read_text(encoding="utf-8")
    audit = json.loads(raw)
    assert audit["schema_version"] == 4
    assert audit["projection_coverage"] == proof.to_payload()
    assert "SECRET_PROFILE" not in raw
    assert old_audit.read_text(encoding="utf-8") == "ORIGINAL_AUDIT"
