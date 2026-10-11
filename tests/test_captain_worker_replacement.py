"""Static safety contract for the same-user Windows worker replacement."""

import ast
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "replace-captain-worker-remote.ps1"
MODULES = ROOT / "deploy" / "captain-worker-modules.py"


def _powershell(command: str) -> str:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.stdout.strip()


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _archive_helpers() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    return source[source.index("function Get-TreeHashes") : source.index("$identity =")]


HASH_STUB = (
    "function Get-FileHash { param($LiteralPath, $Algorithm) "
    "$stream=[IO.File]::OpenRead($LiteralPath); "
    "try { $hash=[Security.Cryptography.SHA256]::Create().ComputeHash($stream); "
    "[pscustomobject]@{Hash=[BitConverter]::ToString($hash).Replace('-','')} } "
    "finally { $stream.Dispose() } }\n"
)


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
    assert '"CaptainCloudWorker01.rollback-versioned-$Build"' in source
    assert "Versioned rollback archive target occupied; no replacement performed" in source
    assert "Versioned rollback preservation" in source
    assert "Assert-WorkerArchiveContent $rollback" in source
    assert "Assert-WorkerArchiveContent $Directory" in source
    assert "versioned_rollback_preserved = $versionedRollbackPreserved" in source
    assert "versioned_rollback_build = $versionedRollbackBuild" in source
    assert source.index("Bundle file closure mismatch") < source.index(
        "$versionedRollbackPreserved = $false"
    )
    assert "foreach ($existing in $existingArchives)" in source
    assert "Select-VersionedArchivePath $parent" in source
    assert "versioned_archive_count = $archiveInventory.Count" in source
    assert "already_installed_verified" in source
    assert "Both unversioned rollback generations require operator review" in source
    assert "Existing rollback is versioned; inspect it before replacement" in source
    assert "Legacy rollback preservation" in source
    assert "Rename-Item -LiteralPath $rollback" in source
    assert "legacy_rollback_preserved = $legacyRollbackPreserved" in source
    assert "legacy_rollback_path = $legacyRollbackPath" in source
    assert source.index("Preserve-VersionedRollback $rollback") < source.index(
        "Rename-Item -LiteralPath $root"
    )
    assert source.index("Rename-Item -LiteralPath $root") < source.index(
        "Rename-Item -LiteralPath $staging"
    )


def _run_archive_helper(tmp_path: Path, *, occupied=False, corrupt_hash=False):
    helpers = _archive_helpers()
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    archive = tmp_path / ("CaptainCloudWorker01.rollback-versioned-" + "a" * 40)
    rollback.mkdir()
    (rollback / "worker-build.json").write_text('{"commit":"' + "a" * 40 + '"}')
    if occupied:
        archive.mkdir()
        (archive / "sentinel").write_text("preserved")

    command = (
        "$ErrorActionPreference='Stop'\n"
        + HASH_STUB
        + helpers
        + f"\n$rollback={_quote(rollback)}\n$archive={_quote(archive)}\n"
        + "$before=Get-TreeHashes $rollback\n"
        + ("$before['worker-build.json']='0' * 64\n" if corrupt_hash else "")
        + "try { Preserve-VersionedRollback $rollback $archive $before; 'OK' } "
        + "catch { 'FAIL:' + $_.Exception.Message }"
    )
    return _powershell(command), rollback, archive


def _run_archive_rotation(
    tmp_path: Path, older_builds: tuple[str, ...], *, same_build_content: str | None = None
):
    current = tmp_path / "CaptainCloudWorker01"
    current.mkdir()
    (current / "current.txt").write_text("must remain untouched", encoding="utf-8")
    (current / "audits").mkdir()
    (current / "audits" / "gw6.json").write_text("historical", encoding="utf-8")
    (tmp_path / "FPLReviewCaptainProfile").mkdir()
    (tmp_path / "FPLReviewCaptainProfile" / "Cookies").write_text("private", encoding="utf-8")
    (tmp_path / "production-x.dpapi").write_text("private", encoding="utf-8")
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    rollback.mkdir()
    build = "a" * 40
    (rollback / "worker-build.json").write_text(
        json.dumps({"schema_version": 1, "commit": build}), encoding="utf-8"
    )
    (rollback / "worker.py").write_text("trusted", encoding="utf-8")
    inventory = []
    for older in older_builds:
        archive = tmp_path / ("CaptainCloudWorker01.rollback-versioned-" + older)
        archive.mkdir()
        (archive / "worker-build.json").write_text(
            json.dumps({"schema_version": 1, "commit": older}), encoding="utf-8"
        )
        (archive / "worker.py").write_text(
            same_build_content if older == build and same_build_content else "trusted",
            encoding="utf-8",
        )
        inventory.append(archive)
    inventory_code = "\n".join(
        f"$inventory += [pscustomobject]@{{Build='{older}'; "
        f"Hashes=(Get-TreeHashes {_quote(archive)})}}"
        for older, archive in zip(older_builds, inventory, strict=True)
    )
    command = (
        "$ErrorActionPreference='Stop'\n"
        + HASH_STUB
        + _archive_helpers()
        + f"\n$parent={_quote(tmp_path)}\n$rollback={_quote(rollback)}\n"
        + "$inventory=@()\n"
        + inventory_code
        + "\n$before=Get-TreeHashes $rollback\n"
        + "try { $target=Select-VersionedArchivePath $parent ('a' * 40) $before $inventory; "
        + "Preserve-VersionedRollback $rollback $target $before; "
        + "'OK:' + [IO.Path]::GetFileName($target) } "
        + "catch { 'FAIL:' + $_.Exception.Message }"
    )
    return _powershell(command), current, rollback, inventory


@pytest.mark.parametrize("older_builds", [(), ("b" * 40,), ("b" * 40, "c" * 40)])
def test_append_only_rotation_preserves_zero_one_or_many_older_archives(tmp_path, older_builds):
    outcome, current, rollback, archives = _run_archive_rotation(tmp_path, older_builds)
    assert outcome == "OK:CaptainCloudWorker01.rollback-versioned-" + "a" * 40
    assert not rollback.exists()
    assert (current / "current.txt").read_text(encoding="utf-8") == "must remain untouched"
    assert (current / "audits" / "gw6.json").read_text(encoding="utf-8") == "historical"
    assert (tmp_path / outcome.removeprefix("OK:") / "worker.py").exists()
    assert not (tmp_path / outcome.removeprefix("OK:") / "audits").exists()
    assert not (tmp_path / outcome.removeprefix("OK:") / "FPLReviewCaptainProfile").exists()
    assert not (tmp_path / outcome.removeprefix("OK:") / "production-x.dpapi").exists()
    assert (tmp_path / "FPLReviewCaptainProfile" / "Cookies").exists()
    assert (tmp_path / "production-x.dpapi").exists()
    assert all(
        (archive / "worker.py").read_text(encoding="utf-8") == "trusted" for archive in archives
    )


def test_same_build_identical_archive_is_preserved_under_content_addressed_duplicate(tmp_path):
    outcome, current, rollback, archives = _run_archive_rotation(tmp_path, ("a" * 40,))
    assert outcome.startswith(
        "OK:CaptainCloudWorker01.rollback-versioned-" + "a" * 40 + "-duplicate-"
    )
    assert len(outcome.rsplit("-", 1)[1]) == 64
    assert not rollback.exists()
    assert (current / "current.txt").exists()
    assert (archives[0] / "worker.py").read_text(encoding="utf-8") == "trusted"
    assert (tmp_path / outcome.removeprefix("OK:") / "worker.py").exists()


def test_same_build_conflicting_archive_fails_without_moving_any_generation(tmp_path):
    outcome, current, rollback, archives = _run_archive_rotation(
        tmp_path, ("a" * 40,), same_build_content="conflicting"
    )
    assert outcome.startswith("FAIL:Same-build versioned archive hash mismatch")
    assert (current / "current.txt").exists() and (rollback / "worker.py").exists()
    assert (archives[0] / "worker.py").read_text(encoding="utf-8") == "conflicting"


def test_content_addressed_duplicate_collision_fails_without_overwrite(tmp_path):
    first, _, _, archives = _run_archive_rotation(tmp_path, ("a" * 40,))
    assert first.startswith("OK:")
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    rollback.mkdir()
    (rollback / "worker-build.json").write_text(
        json.dumps({"schema_version": 1, "commit": "a" * 40}), encoding="utf-8"
    )
    (rollback / "worker.py").write_text("trusted", encoding="utf-8")
    duplicate = tmp_path / first.removeprefix("OK:")
    command = (
        "$ErrorActionPreference='Stop'\n"
        + HASH_STUB
        + _archive_helpers()
        + f"\n$parent={_quote(tmp_path)}\n$rollback={_quote(rollback)}\n"
        + "$inventory=@()\n"
        + "foreach ($path in @('"
        + str(archives[0]).replace("'", "''")
        + "','"
        + str(duplicate).replace("'", "''")
        + "')) { $inventory += [pscustomobject]@{Build=('a' * 40); "
        + "Hashes=(Get-TreeHashes $path)} }\n"
        + "try { Select-VersionedArchivePath $parent ('a' * 40) "
        + "(Get-TreeHashes $rollback) $inventory; 'UNSAFE' } "
        + "catch { 'FAIL:' + $_.Exception.Message }"
    )
    outcome = _powershell(command)
    assert outcome.startswith("FAIL:Versioned rollback archive target occupied")
    assert (rollback / "worker.py").read_text(encoding="utf-8") == "trusted"
    assert (duplicate / "worker.py").read_text(encoding="utf-8") == "trusted"


def test_invalid_build_identity_is_rejected_before_any_rename(tmp_path):
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    rollback.mkdir()
    command = (
        "$ErrorActionPreference='Stop'\n"
        + _archive_helpers()
        + f"\ntry {{ Select-VersionedArchivePath {_quote(tmp_path)} 'wrong' @{{}} @(); "
        + "'UNSAFE' } catch { 'FAIL:' + $_.Exception.Message }"
    )
    assert _powershell(command).startswith("FAIL:Versioned rollback identity invalid")
    assert rollback.exists()


def test_misnamed_archive_manifest_is_rejected_before_config_or_acl_access(tmp_path):
    archive = tmp_path / ("CaptainCloudWorker01.rollback-versioned-" + "a" * 40)
    (archive / "fpl_bot").mkdir(parents=True)
    (archive / "fpl_bot" / "captain_worker_cli.py").write_text("", encoding="utf-8")
    (archive / "worker-build.json").write_text(
        json.dumps({"schema_version": 1, "commit": "b" * 40}), encoding="utf-8"
    )
    command = (
        "$ErrorActionPreference='Stop'\n"
        + _archive_helpers()
        + f"\ntry {{ Read-VersionedArchive {_quote(archive)} 'root' 'profile' 'sid'; "
        + "'UNSAFE' } catch { 'FAIL:' + $_.Exception.Message }"
    )
    assert _powershell(command).startswith("FAIL:Versioned rollback archive identity invalid")


@pytest.mark.parametrize(
    "forbidden", ["FPLReviewCaptainProfile", "CaptainCloudWorker01.rollback-old"]
)
def test_archive_content_rejects_nested_profile_or_archive(tmp_path, forbidden):
    rollback = tmp_path / "CaptainCloudWorker01.rollback"
    (rollback / forbidden).mkdir(parents=True)
    command = (
        "$ErrorActionPreference='Stop'\n"
        + _archive_helpers()
        + f"\ntry {{ Assert-WorkerArchiveContent {_quote(rollback)}; "
        + "'UNSAFE' } catch { 'FAIL:' + $_.Exception.Message }"
    )
    assert _powershell(command).startswith("FAIL:Rollback contains redirected or browser-profile")


def test_already_installed_bundle_verification_is_idempotent_and_detects_drift(tmp_path):
    root = tmp_path / "CaptainCloudWorker01"
    (root / "fpl_bot").mkdir(parents=True)
    (root / "fpl_bot" / "captain_worker_cli.py").write_text("trusted", encoding="utf-8")
    (root / "worker-build.json").write_text("manifest", encoding="utf-8")
    (root / "worker-config.json").write_text("private config stays separate", encoding="utf-8")
    archive = tmp_path / "worker.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("fpl_bot/captain_worker_cli.py", "trusted")
        bundle.writestr("worker-build.json", "manifest")
        bundle.writestr("worker-config.json", "redacted template")
    command = (
        "$ErrorActionPreference='Stop'\n"
        + HASH_STUB
        + _archive_helpers()
        + "\nAdd-Type -AssemblyName System.IO.Compression.FileSystem\n"
        + f"$archive=[IO.Compression.ZipFile]::OpenRead({_quote(archive)})\n"
        + f"try {{ Assert-InstalledBundle {_quote(root)} $archive; 'OK' }} "
        + "catch { 'FAIL:' + $_.Exception.Message } finally { $archive.Dispose() }"
    )
    assert _powershell(command) == "OK"
    assert _powershell(command) == "OK"  # Rechecking must not rotate or mutate anything.
    (root / "fpl_bot" / "captain_worker_cli.py").write_text("drift", encoding="utf-8")
    assert _powershell(command).startswith("FAIL:Already-installed bundle differs")
    assert (root / "worker-config.json").read_text(
        encoding="utf-8"
    ) == "private config stays separate"


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
