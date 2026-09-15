import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_captain_dry_run import FakeFplSource

from fpl_bot import captain_worker_trial as worker
from fpl_bot.captain_dry_run import build_live_captain_report
from fpl_bot.captain_models import CaptainProjection
from fpl_bot.errors import CaptainReviewBrowserError


@pytest.fixture
def trial(monkeypatch):
    projections = tuple(CaptainProjection(i, Decimal(11 - i)) for i in range(1, 6))
    source = SimpleNamespace(
        fetch_event_projections=lambda event: projections,
        last_projections=projections,
        last_canonical_table=SimpleNamespace(
            rows=tuple(SimpleNamespace(source_row_ordinal=i * 2) for i in range(1, 6)),
            raw_body_row_count=10,
            genuine_player_row_count=5,
            auxiliary_row_count=5,
        ),
        last_acquisition=SimpleNamespace(
            acquired_at_utc=datetime(2026, 9, 5, tzinfo=UTC),
            total_projection_rows=5,
            resolved_candidate_count=5,
        ),
        last_identity_diagnostic=None,
    )
    report = build_live_captain_report(
        FakeFplSource(), source, now=datetime(2026, 9, 5, tzinfo=UTC)
    )
    context = SimpleNamespace(
        event_report=SimpleNamespace(event=report.event, event_code=report.event_code),
        players=(),
        teams=(),
    )
    monkeypatch.setattr(worker.getpass, "getuser", lambda: "trial-user")
    monkeypatch.setattr(worker, "fetch_live_captain_context", lambda client: context)
    monkeypatch.setattr(
        worker,
        "PlaywrightReviewBrowserAcquirer",
        lambda profile: SimpleNamespace(
            last_lifecycle={"ownership": "released", "authentication": "authenticated"}
        ),
    )
    monkeypatch.setattr(worker, "FplReviewBrowserProjectionSource", lambda *args: source)
    monkeypatch.setattr(worker, "build_captain_report_from_context", lambda *args: report)
    return source, report


def test_utf8_complete_audit_and_no_second_acquisition(tmp_path, trial, monkeypatch):
    output = tmp_path / "result"
    assert worker.run_trial(tmp_path / "profile", output, "trial-user") == 0
    raw = (output / "audit.json").read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    assert "🧢 CAPTAIN PICKS 🧢" in payload["tweet"]
    assert b"\xef\xbb\xbf" not in raw
    assert payload["top_three"][0]["source_row_ordinal"] == 2
    assert payload["top_three"][0]["official_ownership"] == "40.0"
    assert payload["differential"]["source_row_ordinal"] == 10
    assert payload["differential"]["projection_rank"] == 5
    assert payload["differential_candidates_examined"] == 2
    assert payload["identity_failure_count"] == 0
    assert payload["browser_lifecycle"]["ownership"] == "released"
    assert payload["acquisition_time_is_dataset_generation_time"] is False
    monkeypatch.setattr(worker, "fetch_live_captain_context", lambda client: pytest.fail("retry"))
    assert worker.run_trial(tmp_path / "profile", output, "trial-user") == 2


@pytest.mark.parametrize("category", ["reauthentication_required", "browser_profile_in_use"])
def test_typed_failures_do_not_retry(tmp_path, trial, monkeypatch, category):
    def fail(*args):
        raise CaptainReviewBrowserError(category)

    monkeypatch.setattr(worker, "build_captain_report_from_context", fail)
    assert worker.run_trial(tmp_path / "profile", tmp_path / "result", "trial-user") == 1
    audit = json.loads((tmp_path / "result/audit.json").read_text(encoding="utf-8"))
    assert audit["error_category"] == category
    assert "tweet" not in audit


def test_unexpected_exception_is_redacted(tmp_path, trial, monkeypatch):
    def fail(*args):
        raise RuntimeError("SECRET_SENTINEL")

    monkeypatch.setattr(worker, "fetch_live_captain_context", fail)
    assert worker.run_trial(tmp_path / "profile", tmp_path / "result", "trial-user") == 1
    text = (tmp_path / "result/audit.json").read_text(encoding="utf-8")
    assert "SECRET_SENTINEL" not in text
    assert "unexpected_worker_failure" in text


def test_wrong_user_and_profile_output_overlap_fail_before_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.getpass, "getuser", lambda: "wrong-user")
    monkeypatch.setattr(worker, "fetch_live_captain_context", lambda client: pytest.fail("network"))
    assert worker.run_trial(tmp_path / "profile", tmp_path / "result", "expected") == 2
    assert worker.run_trial(tmp_path, tmp_path / "result", "wrong-user") == 2
    assert not (tmp_path / "result").exists()


def test_one_shot_runner_has_no_posting_or_browser_secret_apis():
    from pathlib import Path

    text = Path(worker.__file__).read_text(encoding="utf-8")
    for forbidden in ("x_client", "oauth", "google.cloud", "storage_state", ".cookies("):
        assert forbidden not in text
