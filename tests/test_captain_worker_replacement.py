"""Static safety contract for the same-user Windows worker replacement."""

import ast
import json
import subprocess
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


def test_replacement_checks_staging_and_preserves_both_occupied_rollback_generations():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Test-Path -LiteralPath $staging -or Test-Path -LiteralPath $rollback" not in source
    assert "if (Test-Path -LiteralPath $staging)" in source
    assert "CaptainCloudWorker01.rollback-legacy-unversioned" in source
    assert "CaptainCloudWorker01.rollback-versioned-$versionedRollbackBuild" in source
    assert "Versioned rollback archive already occupied; no replacement performed" in source
    assert "Versioned rollback preservation" in source
    assert "Assert-WorkerArchiveContent $rollback" in source
    assert "Assert-WorkerArchiveContent $versionedRollbackPath" in source
    assert "versioned_rollback_preserved = $versionedRollbackPreserved" in source
    assert "versioned_rollback_build = $versionedRollbackBuild" in source
    assert source.index("Bundle file closure mismatch") < source.index(
        "$versionedRollbackPreserved = $false"
    )
    assert "Multiple versioned rollback archives require operator review" in source
    assert "Both unversioned rollback generations require operator review" in source
    assert "Existing rollback is versioned; inspect it before replacement" in source
    assert "Legacy rollback preservation" in source
    assert "Rename-Item -LiteralPath $rollback" in source
    assert "legacy_rollback_preserved = $legacyRollbackPreserved" in source
    assert "legacy_rollback_path = $legacyRollbackPath" in source


def _run_archive_helper(tmp_path: Path, *, occupied=False, corrupt_hash=False):
    source = SCRIPT.read_text(encoding="utf-8")
    helpers = source[source.index("function Get-TreeHashes") : source.index("$identity =")]
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    archive = tmp_path / ("CaptainCloudWorker01.rollback-versioned-" + "a" * 40)
    rollback.mkdir()
    (rollback / "worker-build.json").write_text('{"commit":"' + "a" * 40 + '"}')
    if occupied:
        archive.mkdir()
        (archive / "sentinel").write_text("preserved")

    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    command = (
        "$ErrorActionPreference='Stop'\n"
        + "function Get-FileHash { param($LiteralPath, $Algorithm) "
        + "$stream=[IO.File]::OpenRead($LiteralPath); "
        + "try { $hash=[Security.Cryptography.SHA256]::Create().ComputeHash($stream); "
        + "[pscustomobject]@{Hash=[BitConverter]::ToString($hash).Replace('-','')} } "
        + "finally { $stream.Dispose() } }\n"
        + helpers
        + f"\n$rollback={quote(rollback)}\n$archive={quote(archive)}\n"
        + "$before=Get-TreeHashes $rollback\n"
        + ("$before['worker-build.json']='0' * 64\n" if corrupt_hash else "")
        + "try { Preserve-VersionedRollback $rollback $archive $before; 'OK' } "
        + "catch { 'FAIL:' + $_.Exception.Message }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.stdout.strip(), rollback, archive


def test_versioned_rollback_archive_renames_without_overwriting(tmp_path):
    outcome, rollback, archive = _run_archive_helper(tmp_path)
    assert outcome == "OK"
    assert not rollback.exists()
    assert json.loads((archive / "worker-build.json").read_text())["commit"] == "a" * 40


def test_occupied_archive_rejects_without_touching_either_generation(tmp_path):
    outcome, rollback, archive = _run_archive_helper(tmp_path, occupied=True)
    assert outcome.startswith("FAIL:Versioned rollback archive target occupied")
    assert rollback.exists()
    assert (archive / "sentinel").read_text() == "preserved"


def test_post_rename_hash_error_retains_archive_for_review(tmp_path):
    outcome, rollback, archive = _run_archive_helper(tmp_path, corrupt_hash=True)
    assert outcome.startswith("FAIL:Versioned rollback preservation hash mismatch")
    assert not rollback.exists()
    assert (archive / "worker-build.json").exists()


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
