"""Local browser prerequisites; never opens browser databases or secret values."""

import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from fpl_bot.errors import CaptainReviewBrowserError

OWNERSHIP_FILE = ".captain-browser-owner"


def stable_candidates(platform: str | None = None) -> tuple[Path, ...]:
    platform = platform or sys.platform
    if platform == "win32":
        return tuple(
            Path(root) / "Google/Chrome/Application/chrome.exe"
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (root := os.environ.get(name))
        )
    if platform == "linux":
        installed = [Path("/opt/google/chrome/chrome"), Path("/usr/bin/google-chrome-stable")]
        if command := shutil.which("google-chrome-stable"):
            installed.append(Path(command))
        return tuple(installed)
    raise CaptainReviewBrowserError("browser_platform_unsupported")


def discover_chrome(candidates: tuple[Path, ...]) -> Path:
    found = {path.resolve() for path in candidates if path.is_file()}
    # The distribution launcher wraps this same executable, rather than being another browser.
    binary = Path("/opt/google/chrome/chrome")
    launcher = Path("/opt/google/chrome/google-chrome")
    if binary in found and launcher in found:
        found.remove(launcher)
    if not found:
        raise CaptainReviewBrowserError("stable_chrome_unavailable")
    if len(found) != 1:
        raise CaptainReviewBrowserError("stable_chrome_ambiguous")
    return found.pop()


def chrome_version(executable: Path) -> str:
    try:
        if sys.platform == "win32":
            safe_path = str(executable).replace("'", "''")
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Item -LiteralPath '{safe_path}').VersionInfo.ProductVersion",
            ]
        else:
            command = [str(executable), "--version"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        match = re.fullmatch(r"(?:Google Chrome )?(\d+(?:\.\d+){3})\s*", result.stdout)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    raise CaptainReviewBrowserError("stable_chrome_unavailable")


def require_keyring(platform: str | None = None, runner=None) -> str:
    """Check only the default collection's existence/unlocked property, never its items."""
    if (platform or sys.platform) == "win32":
        return "os_managed"
    if (platform or sys.platform) != "linux":
        raise CaptainReviewBrowserError("browser_platform_unsupported")
    runner = runner or subprocess.run
    base = ["gdbus", "call", "--session", "--dest", "org.freedesktop.secrets"]
    try:
        alias = runner(
            base
            + [
                "--object-path",
                "/org/freedesktop/secrets",
                "--method",
                "org.freedesktop.Secret.Service.ReadAlias",
                "default",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        match = re.fullmatch(
            r"\(objectpath '(/org/freedesktop/secrets/collection/[A-Za-z0-9_]+)',\)\s*",
            alias.stdout,
        )
        if not match:
            raise ValueError
        locked = runner(
            base
            + [
                "--object-path",
                match.group(1),
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.freedesktop.Secret.Collection",
                "Locked",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if locked.stdout.strip() != "(<false>,)":
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        raise CaptainReviewBrowserError("browser_keyring_unavailable") from None
    return "default_collection_unlocked"


def reject_personal_profile(path: Path) -> None:
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).resolve()
    for name in ("google-chrome", "google-chrome-beta", "google-chrome-unstable", "chromium"):
        root = config / name
        if path == root or root in path.parents:
            raise CaptainReviewBrowserError("dedicated_profile_required")
    if sys.platform == "linux" and path.exists():
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise CaptainReviewBrowserError("dedicated_profile_required")


@contextmanager
def own_profile(profile: Path):
    """A crash leaves evidence; no automatic stale-lock deletion or adoption."""
    lock = profile / OWNERSHIP_FILE
    if any(
        os.path.lexists(profile / name)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
    ):
        raise CaptainReviewBrowserError("browser_profile_in_use")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise CaptainReviewBrowserError("browser_profile_in_use") from None
    os.close(fd)
    try:
        yield
    except BaseException:
        # Preserve uncertain ownership for deliberate operator review.
        raise
    else:
        if any(
            os.path.lexists(profile / name)
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
        ):
            raise CaptainReviewBrowserError("browser_profile_unclean")
        lock.unlink()
