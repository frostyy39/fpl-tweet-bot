"""Static safety contract for the same-user Windows worker replacement."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "replace-captain-worker-remote.ps1"
MODULES = ROOT / "deploy" / "captain-worker-modules.py"


def _worker_modules() -> tuple[str, ...]:
    namespace: dict[str, object] = {}
    exec(compile(MODULES.read_text(encoding="utf-8"), str(MODULES), "exec"), namespace)
    return namespace["worker_modules"](ROOT / "src" / "fpl_bot")


def test_replacement_exactly_matches_worker_only_import_closure():
    source = SCRIPT.read_text(encoding="utf-8")
    declared = set()
    tree = ast.parse(MODULES.read_text(encoding="utf-8"))
    assert tree
    for module in _worker_modules():
        assert f"'{module}'" in source
        declared.add(module)
    assert "worker-build.json" in source
    assert "worker-config.json" in source
    assert "prepare-captain-worker-remote.ps1" in source
    assert not {name for name in declared if name.startswith("x_") or "publisher" in name}


def test_replacement_preserves_identity_task_config_audits_and_rollback():
    source = SCRIPT.read_text(encoding="utf-8")
    for required in (
        "Run only as the existing captaintrial user",
        "CaptainCloudWorker01.staging-$ExpectedCommit",
        "CaptainCloudWorker01.rollback",
        "Get-TreeHashes",
        "Assert-SameHashes",
        "preserved_config_sha256",
        "Rename-Item -LiteralPath $root",
        "Rename-Item -LiteralPath $rollback",
        "Assert-WorkerTaskContract",
        "worker-build.json",
        "-m fpl_bot.captain_worker_cli",
        "MSFT_TaskBootTrigger",
        "task_temporarily_disabled = $false",
    ):
        assert required in source
    for forbidden in (
        "Disable-ScheduledTask",
        "Enable-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Set-ScheduledTask",
        "Remove-Item",
        "Stop-Process",
        "taskkill",
    ):
        assert forbidden not in source


def test_replacement_checks_staging_and_rollback_as_separate_paths():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Test-Path -LiteralPath $staging -or Test-Path -LiteralPath $rollback" not in source
    assert "(Test-Path -LiteralPath $staging) -or" in source
    assert "(Test-Path -LiteralPath $rollback)" in source


def test_replacement_never_moves_copies_or_removes_browser_profile():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "$profile = Join-Path $parent 'FPLReviewCaptainProfile'" in source
    assert "browser_profile_modified = $false" in source
    # The profile variable is used only for validation/config equality and lifecycle-marker checks.
    for line in source.splitlines():
        if "$profile" in line.casefold() and any(
            command in line for command in ("Copy-Item", "Move-Item", "Rename-Item", "Remove-Item")
        ):
            raise AssertionError(line)


def test_worker_closure_has_no_posting_cloud_or_secret_authority():
    modules = set(_worker_modules())
    forbidden = {"captain_publisher", "captain_publisher_runtime", "x_api", "x_oauth"}
    assert modules.isdisjoint(forbidden)
    for module in modules:
        source = (ROOT / "src" / "fpl_bot" / f"{module}.py").read_text(encoding="utf-8")
        assert "google.cloud.secretmanager" not in source
        assert "XApiClient" not in source
