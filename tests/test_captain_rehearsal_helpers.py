"""Rehearsal deployment helpers with a fake CLI, never real Google calls."""

import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell.exe")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")
GID = "3c114305-3fa1-5fa3-860e-a56783156a5a"


def probe(tmp_path, *, queue="PAUSED", kind="warmup", early=True):
    scheduled = datetime.now(UTC) + (timedelta(days=1) if early else timedelta(minutes=4))
    iso = scheduled.isoformat().replace("+00:00", "Z")
    payload = json.dumps(
        {
            "identity": f"captain-{GID}-warmup",
            "destination_user_id": "1",
            "kind": kind,
            "scheduled_at": iso,
            "digest": "immutable-test-digest",
        }
    ).encode("utf-8")
    origin = "https://captain-controller-524790767721.europe-west1.run.app"
    task = json.dumps(
        {
            "scheduleTime": iso,
            "httpRequest": {
                "url": origin + "/captain/tasks/warmup",
                "httpMethod": "POST",
                "body": base64.b64encode(payload).decode("ascii"),
                "oidcToken": {
                    "audience": origin,
                    "serviceAccountEmail": (
                        "captain-tasks@fpl-frosty-bot-v1.iam.gserviceaccount.com"
                    ),
                },
            },
        }
    )
    source = (ROOT / "deploy/probe-captain-early-task.ps1").read_text(encoding="utf-8")
    source = source.replace(
        "(Split-Path $PSScriptRoot)", "'" + str(tmp_path).replace("'", "''") + "'"
    )
    encoded_source = base64.b64encode(source.encode("utf-8")).decode("ascii")
    wrapper = rf"""
$taskFixture = '{task}'
function gcloud.cmd {{
    $global:LASTEXITCODE = 0
    $route = ($args[0..2] -join ' ')
    if ($route -eq 'tasks queues describe') {{ '{queue}' }}
    elseif ($args[0] -eq 'tasks' -and $args[1] -eq 'describe') {{ $taskFixture }}
    elseif ($args[1] -eq 'create-http-task') {{
        Write-Output ('CALLED_CREATE ' + ($args -join ' '))
    }}
    elseif ($args[1] -eq 'run') {{ Write-Output ('CALLED_RUN ' + $args[2]); '{{}}' }}
    elseif ($args[1] -eq 'list') {{ 'captain-{GID}-warmup' }}
    else {{ throw 'Unexpected cloud operation' }}
}}
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_source}'))
try {{ & ([scriptblock]::Create($source)) -GenerationId '{GID}' }}
catch {{ [Console]::Error.WriteLine('PROBE_STOPPED'); exit 1 }}
"""
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, payload


def test_early_task_probe_copies_exact_payload_and_uses_separate_task(tmp_path):
    result, payload = probe(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("CALLED_CREATE ") == 1
    assert result.stdout.count("CALLED_RUN captain-rehearsal-early-") == 1
    assert "--schedule-time=" in result.stdout
    assert "--oidc-token-audience=https://captain-controller-" in result.stdout
    files = list((tmp_path / "captain-rehearsal-artifacts").glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == payload
    assert "resume" not in result.stdout


@pytest.mark.parametrize("kwargs", [{"queue": "RUNNING"}, {"kind": "release"}, {"early": False}])
def test_early_task_probe_fails_before_dispatch_for_invalid_preconditions(tmp_path, kwargs):
    result, _ = probe(tmp_path, **kwargs)
    assert result.returncode == 1
    assert "CALLED_CREATE" not in result.stdout
    assert "CALLED_RUN" not in result.stdout


def test_rehearsal_scripts_parse_in_windows_powershell():
    paths = [
        ROOT / "deploy/observe-captain-worker-boot.ps1",
        ROOT / "deploy/install-captain-boot-observer.ps1",
        ROOT / "deploy/probe-captain-early-task.ps1",
        ROOT / "deploy/replace-captain-worker-remote.ps1",
    ]
    script = "$failed = $false\n"
    for path in paths:
        script += (
            "$tokens=$null; $errors=$null\n"
            "[Management.Automation.Language.Parser]::ParseFile('"
            + str(path).replace("'", "''")
            + "',[ref]$tokens,[ref]$errors) | Out-Null\n"
            "if ($errors.Count) {$failed=$true; $errors | Out-String | Write-Output}\n"
        )
    script += "if ($failed) {exit 1}"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rehearsal_operator_supports_bounded_reconciliation():
    source = (ROOT / "deploy/captain-rehearsal-operator.yaml").read_text(encoding="utf-8")
    assert "elif action == 'reconcile':" in source
    assert "route = '/captain/control/reconcile'" in source
