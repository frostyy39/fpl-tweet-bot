import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-native in-memory Task XML only")
def test_preparation_builds_password_limited_boot_task_without_registration():
    script = Path("deploy/prepare-captain-worker-remote.ps1").resolve()
    # Only construct a new IN-MEMORY task. No registration, credentials, filesystem
    # preparation, browser execution or existing-task mutation occurs.
    code = f"""
$ErrorActionPreference='Stop'
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent()
$root='C:\\Users\\captaintrial\\AppData\\Local\\FPLBot\\CaptainCloudWorker01'
$python='C:\\Users\\captaintrial\\AppData\\Local\\FPLBot\\CaptainControlledTrial01\\venv\\Scripts\\python.exe'
$contents=Get-Content -LiteralPath '{script}' -Raw
$begin=$contents.IndexOf('# NewTask')
$end=$contents.IndexOf('$xml =', $begin)
. ([scriptblock]::Create($contents.Substring($begin,$end-$begin)))
[xml]$xml=$task.XmlText
if ($xml.Task.Principals.Principal.LogonType -ne 'Password' -or
    $xml.Task.Principals.Principal.RunLevel -ne 'LeastPrivilege' -or
    $xml.Task.Triggers.BootTrigger.Delay -ne 'PT30S' -or
    $xml.Task.Settings.ExecutionTimeLimit -ne 'PT25M' -or
    $xml.Task.Settings.AllowStartOnDemand -ne 'false' -or
    $xml.Task.Settings.MultipleInstancesPolicy -ne 'IgnoreNew' -or
    $xml.Task.Settings.RunOnlyIfNetworkAvailable -ne 'true') {{ throw 'Incorrect task policy' }}
Write-Output 'definition-only-passed'
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "definition-only-passed"
    text = script.read_text(encoding="utf-8")
    assert "RegisterTask" not in text and "Register-ScheduledTask" not in text
