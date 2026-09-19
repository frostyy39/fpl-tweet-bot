"""Offline executable tests for the temporary read-only Windows boot observer."""

import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell.exe")
SCRIPT = Path(__file__).resolve().parents[1] / "deploy/observe-captain-worker-boot.ps1"
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")


def observe(
    tmp_path, *, principal="captaintrial", invalid_utf8=False, secret=False, diagnostic=None
):
    audit = tmp_path / "audit.json"
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": 1,
        "no_post": True,
        "status": "no_assignment",
        "exit_code": 0,
        "started_at_utc": now,
        "ended_at_utc": now,
        "handoff": {"forbidden": "SECRET_VALUE"} if secret else None,
        "payload_digest": None,
    }
    if diagnostic is not None:
        payload.update(
            schema_version=2,
            worker_build_id="a" * 40,
            acquisition_failure=diagnostic,
        )
    audit.write_bytes(b"\xff" if invalid_utf8 else json.dumps(payload).encode("utf-8"))
    fixture = str(audit).replace("'", "''")
    source = rf"""
$fixture = '{fixture}'
$principalFixture = '{principal}'
function Get-CimInstance {{ [pscustomobject]@{{LastBootUpTime=[DateTime]::Now.AddMinutes(-1)}} }}
function Get-ScheduledTask {{
    $root = 'C:\Users\captaintrial\AppData\Local\FPLBot\CaptainCloudWorker01'
    [pscustomobject]@{{
        Principal=[pscustomobject]@{{UserId=$principalFixture;LogonType='Password';RunLevel='Limited'}}
        Triggers=@([pscustomobject]@{{CimClass=[pscustomobject]@{{CimClassName='MSFT_TaskBootTrigger'}};Delay='PT30S'}})
        Actions=@([pscustomobject]@{{Execute='C:\Users\captaintrial\AppData\Local\FPLBot\CaptainControlledTrial01\venv\Scripts\python.exe';
            Arguments=('-m fpl_bot.captain_worker_cli --config "' + $root + '\worker-config.json"');
            WorkingDirectory=$root}})
        Settings=[pscustomobject]@{{ExecutionTimeLimit='PT25M';MultipleInstances='IgnoreNew';
            RestartCount=0;AllowDemandStart=$false;RunOnlyIfNetworkAvailable=$true}}
        State='Ready'
    }}
}}
function Get-ScheduledTaskInfo {{
    [pscustomobject]@{{LastRunTime=[DateTime]::Now;LastTaskResult=0}}
}}
function Get-Process {{}}
function Get-ChildItem {{ Get-Item -LiteralPath $fixture }}
function Test-Path {{ $false }}
function Get-FileHash {{
    param($LiteralPath, $Algorithm)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {{
        [pscustomobject]@{{Hash=[BitConverter]::ToString($hash.ComputeHash([IO.File]::ReadAllBytes($LiteralPath))).Replace('-','')}}
    }} finally {{ $hash.Dispose() }}
}}
function Get-WinEvent {{
    param($ListLog, $FilterHashtable, $ErrorAction)
    if ($ListLog) {{ [pscustomobject]@{{IsEnabled=$true}} }}
}}
function Invoke-WebRequest {{
    param([switch]$UseBasicParsing, $Method, $TimeoutSec, $Uri, $Headers, $ContentType, $Body)
    $expectedUri = 'http://metadata.google.internal/computeMetadata/v1/instance/'
    $expectedUri += 'guest-attributes/captain-rehearsal/boot-report'
    if ($Uri -ne $expectedUri) {{
        throw 'Unexpected network destination'
    }}
}}
function Start-Sleep {{ throw 'Completed observations must not sleep' }}
""" + SCRIPT.read_text(encoding="utf-8")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(source.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def report(result):
    lines = [line for line in result.stdout.splitlines() if "CAPTAIN_BOOT_OBSERVER " in line]
    assert lines, result.stderr
    return json.loads(lines[-1].split("CAPTAIN_BOOT_OBSERVER ", 1)[1])


def test_boot_observer_reports_no_assignment_without_running_browser(tmp_path):
    result = observe(tmp_path)
    data = report(result)
    assert result.returncode == 0, result.stderr
    assert data["observation"] == "completed"
    assert data["task_contract_matches"] is True
    assert data["current_boot_interactive_logons"] == 0
    assert data["chrome_process_count"] == 0
    assert not any(data["lifecycle_markers"].values())
    assert data["audit"]["status"] == "no_assignment"
    assert data["audit"]["strict_utf8"] is True
    assert data["audit"]["handoff_absent"] is True
    assert data["postable"] is False


def test_boot_observer_does_not_forward_arbitrary_audit_content(tmp_path):
    result = observe(tmp_path, secret=True)
    assert "SECRET_VALUE" not in result.stdout + result.stderr
    assert report(result)["audit"]["handoff_absent"] is False


def test_boot_observer_rejects_non_utf8_audit(tmp_path):
    result = observe(tmp_path, invalid_utf8=True)
    assert result.returncode == 1
    assert report(result)["observation"] == "observer_failed_closed"


def test_boot_observer_rejects_unexpected_task_identity(tmp_path):
    result = observe(tmp_path, principal="SYSTEM")
    assert result.returncode == 1
    assert report(result)["observation"] == "task_contract_mismatch"


def test_boot_observer_forwards_only_allowlisted_acquisition_diagnostic(tmp_path):
    diagnostic = {
        "schema_version": 1,
        "code": "browser_process_launch_failed",
        "stage": "browser_launch",
        "browser_executable_resolved": True,
        "profile_directory_exists": True,
        "browser_process_created": False,
        "navigation_began": False,
        "duration_ms": 29,
        "exception_class": "CaptainReviewBrowserError",
        "ignored_secret": "COOKIE_TOKEN_SENTINEL",
    }
    result = observe(tmp_path, diagnostic=diagnostic)
    data = report(result)
    assert result.returncode == 0, result.stderr
    assert data["audit"]["worker_build_id"] == "a" * 40
    assert data["audit"]["acquisition_failure"] == {
        key: value for key, value in diagnostic.items() if key != "ignored_secret"
    }
    assert "COOKIE_TOKEN_SENTINEL" not in result.stdout + result.stderr


def test_boot_observer_rejects_unrecognized_acquisition_diagnostic(tmp_path):
    result = observe(
        tmp_path,
        diagnostic={
            "schema_version": 1,
            "code": "arbitrary_failure",
            "stage": "internal",
            "browser_executable_resolved": False,
            "profile_directory_exists": False,
            "browser_process_created": False,
            "navigation_began": False,
            "duration_ms": 0,
            "exception_class": "InternalError",
        },
    )
    assert result.returncode == 1
    assert report(result)["observation"] == "observer_failed_closed"
