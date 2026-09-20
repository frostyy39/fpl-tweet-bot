import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import fpl_bot.captain_review_browser as review
import fpl_bot.captain_worker_browser as bridge
from fpl_bot.captain_browser_runtime import OWNERSHIP_FILE
from fpl_bot.captain_review_browser import ReviewDomCell, ReviewDomTable, ReviewPageSnapshot
from fpl_bot.captain_worker import (
    AcquisitionDiagnosticError,
    AcquisitionFailureCode,
    AcquisitionStage,
)
from fpl_bot.errors import CaptainReviewBrowserError


@pytest.mark.parametrize(
    "ownership,points,success",
    [
        ("released", "6.48", True),
        ("unclean", "6.48", False),
        ("released", "invalid", False),
    ],
)
def test_bridge_reuses_closed_browser_and_canonical_parser(monkeypatch, ownership, points, success):
    events = []
    profile = Path("dedicated")
    monkeypatch.setattr(Path, "is_dir", lambda self: self == profile)

    class FakeAcquirer:
        def __init__(self, path, *, session_observer):
            assert path == Path("dedicated")
            self.last_lifecycle = {"authentication": "authenticated", "ownership": ownership}

        def acquire(self, event_id):
            assert event_id == 5
            events.append("closed")
            return ReviewPageSnapshot(
                (
                    ReviewDomTable(
                        header_rows=(
                            tuple(
                                ReviewDomCell(s)
                                for s in ("PLAYER", "PRICE", "GW5", "TOTAL", "ELITE OWN%")
                            ),
                        ),
                        body_rows=(
                            tuple(
                                ReviewDomCell(s)
                                for s in (
                                    "João Pedro\nCHE • FWD",
                                    "unused",
                                    points,
                                    "unused",
                                    "unused",
                                )
                            ),
                        ),
                    ),
                )
            )

    monkeypatch.setattr(bridge, "PlaywrightReviewBrowserAcquirer", FakeAcquirer)
    source = bridge.ProvenBrowserAcquisition(Path("dedicated"))
    if success:
        data = source.acquire(5)
        assert data.records[0].review_name == "João Pedro"
        assert data.records[0].source_ordinal == 1
        assert data.records[0].official_element_id is None
        assert data.counts.extracted == 1
        assert data.cleanup.value == "released"
    else:
        with pytest.raises(AcquisitionDiagnosticError):
            source.acquire(5)
    assert events == ["closed"]


@pytest.mark.parametrize(
    "category,stage,lifecycle,code,expected_stage",
    [
        (
            "stable_chrome_unavailable",
            "executable_resolution",
            {},
            AcquisitionFailureCode.BROWSER_EXECUTABLE_UNAVAILABLE,
            AcquisitionStage.EXECUTABLE_RESOLUTION,
        ),
        (
            "browser_dependency_unavailable",
            "runtime_initialization",
            {"executable_resolved": "true"},
            AcquisitionFailureCode.BROWSER_RUNTIME_INITIALIZATION_FAILED,
            AcquisitionStage.RUNTIME_INITIALIZATION,
        ),
        (
            "browser_profile_in_use",
            "profile_ownership",
            {"executable_resolved": "true"},
            AcquisitionFailureCode.PROFILE_IN_USE,
            AcquisitionStage.PROFILE_OWNERSHIP,
        ),
        (
            "browser_launch_failed",
            "browser_launch",
            {"executable_resolved": "true"},
            AcquisitionFailureCode.BROWSER_PROCESS_LAUNCH_FAILED,
            AcquisitionStage.BROWSER_LAUNCH,
        ),
        (
            "browser_exited_immediately",
            "browser_running",
            {"executable_resolved": "true", "browser_process_created": "true"},
            AcquisitionFailureCode.BROWSER_EXITED_IMMEDIATELY,
            AcquisitionStage.BROWSER_RUNNING,
        ),
        (
            "browser_navigation_failed",
            "navigation",
            {
                "executable_resolved": "true",
                "browser_process_created": "true",
                "navigation_began": "true",
            },
            AcquisitionFailureCode.NAVIGATION_FAILED,
            AcquisitionStage.NAVIGATION,
        ),
        (
            "table_load_timeout",
            "review_session",
            {
                "executable_resolved": "true",
                "browser_process_created": "true",
                "navigation_began": "true",
            },
            AcquisitionFailureCode.ACQUISITION_TIMEOUT,
            AcquisitionStage.REVIEW_SESSION,
        ),
        (
            "browser_navigation_timeout",
            "navigation",
            {
                "executable_resolved": "true",
                "browser_process_created": "true",
                "navigation_began": "true",
            },
            AcquisitionFailureCode.ACQUISITION_TIMEOUT,
            AcquisitionStage.NAVIGATION,
        ),
        (
            "reauthentication_required",
            "review_session",
            {
                "executable_resolved": "true",
                "browser_process_created": "true",
                "navigation_began": "true",
            },
            AcquisitionFailureCode.REVIEW_AUTHENTICATION_REQUIRED,
            AcquisitionStage.REVIEW_SESSION,
        ),
        (
            "projection_table_unavailable",
            "review_session",
            {
                "executable_resolved": "true",
                "browser_process_created": "true",
                "navigation_began": "true",
            },
            AcquisitionFailureCode.REVIEW_APPLICATION_FAILURE,
            AcquisitionStage.REVIEW_SESSION,
        ),
    ],
)
def test_bridge_emits_typed_redacted_failure(
    tmp_path, monkeypatch, category, stage, lifecycle, code, expected_stage
):
    profile = tmp_path / "profile"
    profile.mkdir()

    class FakeAcquirer:
        def __init__(self, *_args, **_kwargs):
            self.last_stage = stage
            self.last_lifecycle = lifecycle

        def acquire(self, _event_id):
            from fpl_bot.errors import CaptainReviewBrowserError

            raise CaptainReviewBrowserError(category)

    monkeypatch.setattr(bridge, "PlaywrightReviewBrowserAcquirer", FakeAcquirer)
    with pytest.raises(AcquisitionDiagnosticError) as raised:
        bridge.ProvenBrowserAcquisition(profile).acquire(6)
    diagnostic = raised.value.diagnostic
    assert diagnostic.code == code
    assert diagnostic.stage == expected_stage
    assert diagnostic.profile_directory_exists
    assert set(diagnostic.to_payload()) == {
        "schema_version",
        "code",
        "stage",
        "browser_executable_resolved",
        "profile_directory_exists",
        "browser_process_created",
        "navigation_began",
        "duration_ms",
        "exception_class",
    }


def test_missing_and_inaccessible_profiles_are_distinct_safe_evidence(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    with pytest.raises(AcquisitionDiagnosticError) as absent:
        bridge.ProvenBrowserAcquisition(missing).acquire(6)
    assert absent.value.diagnostic.code == AcquisitionFailureCode.PROFILE_MISSING_OR_INACCESSIBLE
    assert not absent.value.diagnostic.profile_directory_exists

    profile = tmp_path / "profile"
    profile.mkdir()
    original = Path.is_dir

    def denied(path):
        if path == profile:
            raise PermissionError("PRIVATE_PATH_SENTINEL")
        return original(path)

    monkeypatch.setattr(Path, "is_dir", denied)
    with pytest.raises(AcquisitionDiagnosticError) as inaccessible:
        bridge.ProvenBrowserAcquisition(profile).acquire(6)
    diagnostic = inaccessible.value.diagnostic
    assert diagnostic.code == AcquisitionFailureCode.FILESYSTEM_PERMISSION_DENIED
    assert "PRIVATE_PATH_SENTINEL" not in repr(diagnostic) + repr(diagnostic.to_payload())


def test_application_local_profile_is_rejected_before_browser_resolution(tmp_path, monkeypatch):
    application = tmp_path / "FPLBot" / "CaptainCloudWorker01"
    module = application / "fpl_bot" / "captain_review_browser.py"
    module.parent.mkdir(parents=True)
    module.touch()
    profile = application / "profile"
    profile.mkdir()
    (profile / review.DEDICATED_PROFILE_MARKER).write_text(
        review._STABLE_PROFILE_MARKER, encoding="utf-8"
    )
    monkeypatch.setattr(review, "__file__", str(module))
    browser_resolution_calls = []
    monkeypatch.setattr(
        review,
        "find_stable_chrome_executable",
        lambda: browser_resolution_calls.append(True),
    )

    with pytest.raises(AcquisitionDiagnosticError) as raised:
        bridge.ProvenBrowserAcquisition(profile).acquire(6)

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == AcquisitionFailureCode.PROFILE_MISSING_OR_INACCESSIBLE
    assert diagnostic.stage == AcquisitionStage.PROFILE_VALIDATION
    assert diagnostic.profile_directory_exists
    assert not diagnostic.browser_executable_resolved
    assert not diagnostic.browser_process_created
    assert not diagnostic.navigation_began
    assert browser_resolution_calls == []


def test_unexpected_browser_exception_is_sanitized(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()

    class FakeAcquirer:
        def __init__(self, *_args, **_kwargs):
            self.last_stage = "runtime_initialization"
            self.last_lifecycle = {}

        def acquire(self, _event_id):
            raise RuntimeError("COOKIE_TOKEN_PASSWORD_SENTINEL")

    monkeypatch.setattr(bridge, "PlaywrightReviewBrowserAcquirer", FakeAcquirer)
    with pytest.raises(AcquisitionDiagnosticError) as raised:
        bridge.ProvenBrowserAcquisition(profile).acquire(6)
    diagnostic = raised.value.diagnostic
    assert diagnostic.code == AcquisitionFailureCode.UNEXPECTED_INTERNAL_FAILURE
    assert diagnostic.exception_class == "InternalError"
    assert "SENTINEL" not in repr(diagnostic) + repr(diagnostic.to_payload())


@pytest.mark.parametrize(
    "failure_point,expected_category,expected_stage,process_created,navigation_began",
    [
        (
            "runtime",
            "browser_runtime_initialization_failed",
            "runtime_initialization",
            "false",
            "false",
        ),
        ("launch", "browser_launch_failed", "browser_launch", "false", "false"),
        (
            "immediate_exit",
            "browser_exited_immediately",
            "cleanup",
            "true",
            "false",
        ),
        (
            "navigation",
            "browser_navigation_failed",
            "cleanup",
            "true",
            "true",
        ),
        (
            "navigation_timeout",
            "browser_navigation_timeout",
            "cleanup",
            "true",
            "true",
        ),
    ],
)
def test_actual_playwright_boundary_preserves_safe_failure_stage(
    tmp_path,
    monkeypatch,
    failure_point,
    expected_category,
    expected_stage,
    process_created,
    navigation_began,
):
    profile = review.prepare_dedicated_profile(tmp_path / "profile")
    monkeypatch.setattr(review, "find_stable_chrome_executable", lambda: Path("chrome.exe"))
    monkeypatch.setattr(review, "chrome_version", lambda _path: "153.0.0.0")
    monkeypatch.setattr(review, "version", lambda _package: "1.62.0")
    monkeypatch.setattr(review, "require_keyring", lambda: "os_managed")

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=object())

        def __exit__(self, *_args):
            return False

    def sync_playwright():
        if failure_point == "runtime":
            raise RuntimeError("SECRET_RUNTIME_SENTINEL")
        return Manager()

    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(TimeoutError=TimeoutError, sync_playwright=sync_playwright),
    )

    def launch(*_args, **_kwargs):
        if failure_point == "launch":
            raise RuntimeError("SECRET_LAUNCH_SENTINEL")
        if failure_point == "immediate_exit":
            return SimpleNamespace(
                pages=[],
                new_page=lambda: (_ for _ in ()).throw(RuntimeError("SECRET_EXIT_SENTINEL")),
                close=lambda: None,
            )
        navigation_error = (
            TimeoutError("SECRET_TIMEOUT_SENTINEL")
            if failure_point == "navigation_timeout"
            else RuntimeError("SECRET_NAVIGATION_SENTINEL")
        )
        page = SimpleNamespace(
            goto=lambda *_args, **_kwargs: (_ for _ in ()).throw(navigation_error)
        )
        return SimpleNamespace(pages=[page], close=lambda: None)

    monkeypatch.setattr(review, "_launch_stable_chrome_context", launch)
    acquirer = review.PlaywrightReviewBrowserAcquirer(profile)
    with pytest.raises(CaptainReviewBrowserError) as raised:
        acquirer.acquire(6)
    error = raised.value
    assert getattr(error, "category", None) == expected_category
    assert acquirer.last_stage == expected_stage
    assert acquirer.last_lifecycle["browser_process_created"] == process_created
    assert acquirer.last_lifecycle["navigation_began"] == navigation_began
    assert "SENTINEL" not in str(error)
    assert (profile / OWNERSHIP_FILE).exists() is (failure_point != "navigation_timeout")


def test_worker_import_graph_has_no_posting_or_cloud_adapter():
    root = Path(__file__).parents[1] / "src" / "fpl_bot"
    pending = ["captain_worker", "captain_worker_browser"]
    seen = set()
    forbidden = {
        "captain_worker_trial",
        "captain_service",
        "x_client",
        "x_oauth",
        "firestore",
        "cloud_tasks",
        "deadline_service",
    }
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        assert module not in forbidden
        source = (root / f"{module}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("google.cloud", "google.auth"))
                if node.module.startswith("fpl_bot."):
                    pending.append(node.module.split(".")[1])
        if module in {"captain_worker", "captain_worker_browser", "captain_session_health"}:
            for forbidden_api in (".cookies(", "storage_state(", "add_cookies("):
                assert forbidden_api not in source
