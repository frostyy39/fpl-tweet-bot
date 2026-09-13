"""Offline platform, ownership and secure-session lifecycle contracts."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_bot import captain_browser_runtime as runtime
from fpl_bot import captain_review_browser as browser
from fpl_bot.errors import CaptainReviewBrowserError


def test_linux_stable_discovery(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    assert runtime.stable_candidates("linux") == (
        Path("/opt/google/chrome/chrome"),
        Path("/usr/bin/google-chrome-stable"),
    )


def test_windows_candidates(monkeypatch, tmp_path):
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    assert runtime.stable_candidates("win32") == (
        tmp_path / "Google/Chrome/Application/chrome.exe",
    )


def test_discovery_unique_and_ambiguous(tmp_path):
    first, second = tmp_path / "chrome", tmp_path / "other"
    first.touch()
    assert runtime.discover_chrome((first, first)) == first.resolve()
    second.touch()
    with pytest.raises(CaptainReviewBrowserError, match="stable_chrome_ambiguous"):
        runtime.discover_chrome((first, second))


def test_missing_chrome(tmp_path):
    with pytest.raises(CaptainReviewBrowserError, match="stable_chrome_unavailable"):
        runtime.discover_chrome((tmp_path / "absent",))


@pytest.mark.parametrize("output", ["(<true>,)", "unexpected", "secret-value"])
def test_keyring_unavailable_redacted(output):
    values = iter(["(objectpath '/org/freedesktop/secrets/collection/login',)", output])
    with pytest.raises(CaptainReviewBrowserError, match="browser_keyring_unavailable") as error:
        runtime.require_keyring("linux", lambda *a, **k: SimpleNamespace(stdout=next(values)))
    assert output not in str(error.value)


def test_keyring_available_only_reads_collection_properties():
    calls = []
    values = iter(["(objectpath '/org/freedesktop/secrets/collection/login',)", "(<false>,)"])

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 5
        return SimpleNamespace(stdout=next(values))

    assert runtime.require_keyring("linux", run) == "default_collection_unlocked"
    assert "org.freedesktop.Secret.Service.ReadAlias" in calls[0]
    assert "Locked" in calls[1]
    assert runtime.require_keyring("win32") == "os_managed"


def test_missing_keyring_program():
    def fail(*args, **kwargs):
        raise FileNotFoundError("private detail")

    with pytest.raises(CaptainReviewBrowserError, match="browser_keyring_unavailable"):
        runtime.require_keyring("linux", fail)


def test_profile_exclusive_and_clean_release(tmp_path):
    with runtime.own_profile(tmp_path):
        assert (tmp_path / runtime.OWNERSHIP_FILE).exists()
        with (
            pytest.raises(CaptainReviewBrowserError, match="browser_profile_in_use"),
            runtime.own_profile(tmp_path),
        ):
            pass
    assert not (tmp_path / runtime.OWNERSHIP_FILE).exists()


def test_crash_preserves_ownership(tmp_path):
    with pytest.raises(RuntimeError), runtime.own_profile(tmp_path):
        raise RuntimeError
    assert (tmp_path / runtime.OWNERSHIP_FILE).exists()


@pytest.mark.parametrize("name", ["SingletonLock", "SingletonSocket", "SingletonCookie"])
def test_chrome_lock_never_deleted(tmp_path, name):
    (tmp_path / name).touch()
    with (
        pytest.raises(CaptainReviewBrowserError, match="browser_profile_in_use"),
        runtime.own_profile(tmp_path),
    ):
        pass
    assert (tmp_path / name).exists()


def test_unclean_chrome_exit_retains_owner(tmp_path):
    with (
        pytest.raises(CaptainReviewBrowserError, match="browser_profile_unclean"),
        runtime.own_profile(tmp_path),
    ):
        (tmp_path / "SingletonLock").touch()
    assert (tmp_path / runtime.OWNERSHIP_FILE).exists()


@pytest.mark.parametrize("name", ["google-chrome", "chromium", "google-chrome-beta"])
def test_personal_linux_profile_rejected(tmp_path, monkeypatch, name):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(CaptainReviewBrowserError, match="dedicated_profile_required"):
        runtime.reject_personal_profile(tmp_path / name / "Default")


def test_linux_private_profile_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 42, raising=False)
    fake = SimpleNamespace(
        parents=(), exists=lambda: True, stat=lambda: SimpleNamespace(st_uid=42, st_mode=0o40700)
    )
    runtime.reject_personal_profile(fake)


def test_linux_command_and_launch_use_secure_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "linux")
    command = browser.stable_chrome_login_command(Path("/opt/google/chrome/chrome"), tmp_path)
    assert f"--user-data-dir={tmp_path}" in command
    assert "--password-store=gnome-libsecret" in command
    seen = {}
    chrome = SimpleNamespace(launch_persistent_context=lambda **kwargs: seen.update(kwargs))
    browser._launch_stable_chrome_context(
        chrome, tmp_path, headless=True, executable=Path("/opt/google/chrome/chrome")
    )
    assert seen["channel"] == "chrome"
    assert seen["args"] == ["--password-store=gnome-libsecret"]
    assert seen["ignore_default_args"] == ["--password-store=basic", "--use-mock-keychain"]
    assert seen["chromium_sandbox"] is True


def test_manual_browser_waits_before_releasing(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "require_keyring", lambda: "ready")
    profile = tmp_path / "dedicated"

    def launch(command):
        assert (profile / runtime.OWNERSHIP_FILE).exists()

        def wait():
            assert (profile / runtime.OWNERSHIP_FILE).exists()
            return 0

        return SimpleNamespace(wait=wait)

    browser.open_manual_review_login(
        profile, chrome_executable=tmp_path / "chrome", process_factory=launch
    )
    assert not (profile / runtime.OWNERSHIP_FILE).exists()


def test_runtime_has_no_secret_or_production_apis():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "GetSecret",
        "SearchItems",
        ".cookies(",
        "storage_state(",
        "--password-store=basic",
        "--no-sandbox",
        "CloudTasksClient",
        "firestore",
        "secretmanager",
        "XApiClient",
    ):
        assert forbidden not in source


def test_acquisition_closes_before_ownership_release(tmp_path, monkeypatch):
    import sys

    profile = browser.prepare_dedicated_profile(tmp_path / "profile")
    monkeypatch.setattr(browser, "find_stable_chrome_executable", lambda: Path("chrome"))
    monkeypatch.setattr(browser, "chrome_version", lambda _: "140.0.0.0")
    monkeypatch.setattr(browser, "version", lambda _: "1.55.0")
    monkeypatch.setattr(browser, "require_keyring", lambda: "os_managed")
    events = []

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=None)

        def __exit__(self, *args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(TimeoutError=TimeoutError, sync_playwright=Manager),
    )

    def close():
        assert (profile / runtime.OWNERSHIP_FILE).exists()
        events.append("closed")

    page = SimpleNamespace(goto=lambda *a, **k: None)
    context = SimpleNamespace(pages=[page], close=close)
    monkeypatch.setattr(browser, "_launch_stable_chrome_context", lambda *a, **k: context)
    snapshot = browser.ReviewPageSnapshot(())
    monkeypatch.setattr(
        browser,
        "_wait_for_projections_view",
        lambda *a, **k: SimpleNamespace(snapshot=snapshot, diagnostic=None),
    )
    acquirer = browser.PlaywrightReviewBrowserAcquirer(profile)
    assert acquirer.acquire(4) is snapshot
    assert events == ["closed"]
    assert not (profile / runtime.OWNERSHIP_FILE).exists()
    assert acquirer.last_lifecycle["ownership"] == "released"
    assert acquirer.last_lifecycle["ended_at_utc"] >= acquirer.last_lifecycle["started_at_utc"]


@pytest.mark.parametrize("uid,mode", [(43, 0o40700), (42, 0o40755)])
def test_linux_profile_owner_and_permissions_rejected(monkeypatch, uid, mode):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "getuid", lambda: 42, raising=False)
    fake = SimpleNamespace(
        parents=(), exists=lambda: True, stat=lambda: SimpleNamespace(st_uid=uid, st_mode=mode)
    )
    with pytest.raises(CaptainReviewBrowserError, match="dedicated_profile_required"):
        runtime.reject_personal_profile(fake)
