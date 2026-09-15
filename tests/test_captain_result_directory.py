import json
import os
import subprocess
import sys

import pytest

from fpl_bot import captain_result_directory as directories


def test_descriptor_grants_account_sid_not_default_owner():
    sid = "S-1-5-21-100-200-300-1001"
    descriptor = directories._result_sddl(sid)
    assert descriptor == (f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
    assert ";;;OW)" not in descriptor
    assert ";;;WD)" not in descriptor
    assert ";;;AU)" not in descriptor


def test_linux_preserves_private_exclusive_creation(monkeypatch):
    monkeypatch.setattr(directories.sys, "platform", "linux")
    calls = []

    class Target:
        def mkdir(self, **kwargs):
            calls.append(kwargs)

    directories.create_result_directory(Target())
    assert calls == [{"mode": 0o700, "parents": False, "exist_ok": False}]


def test_windows_creation_failure_propagates_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(directories.sys, "platform", "win32")

    def fail(path):
        raise PermissionError("denied")

    monkeypatch.setattr(directories, "_create_windows_directory", fail)
    with pytest.raises(PermissionError):
        directories.create_result_directory(tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACL integration")
def test_real_windows_user_owned_acl_inherited_by_audit_and_duplicate_refused(tmp_path):
    target = tmp_path / "result"
    directories.create_result_directory(target)
    artifact = target / "audit.json"
    artifact.write_text('{"tweet":"🧢"}', encoding="utf-8")
    assert json.loads(artifact.read_text(encoding="utf-8"))["tweet"] == "🧢"

    def inspect_acl():
        script = """
        $ErrorActionPreference = 'Stop'
        $sidType = [System.Security.Principal.SecurityIdentifier]
        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $paths = @($env:CAPTAIN_TEST_RESULT, "$env:CAPTAIN_TEST_RESULT\\audit.json")
        $items = foreach ($p in $paths) {
            if ([System.IO.Directory]::Exists($p)) {
                $acl = [System.IO.Directory]::GetAccessControl($p)
            } else {
                $acl = [System.IO.File]::GetAccessControl($p)
            }
            $rules = @($acl.GetAccessRules($true, $true, $sidType) | ForEach-Object {
                @{sid=$_.IdentityReference.Value; rights=[int]$_.FileSystemRights;
                  inherited=$_.IsInherited; type=$_.AccessControlType.ToString()}
            })
            @{owner=$acl.GetOwner($sidType).Value; rules=$rules;
              sddl=$acl.GetSecurityDescriptorSddlForm('All')}
        }
        @{user=$user; items=@($items)} | ConvertTo-Json -Depth 5 -Compress
        """
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "CAPTAIN_TEST_RESULT": str(target)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = inspect_acl()
    for item in before["items"]:
        assert item["owner"] == before["user"]
        assert {rule["sid"] for rule in item["rules"]} == {
            before["user"],
            "S-1-5-18",
            "S-1-5-32-544",
        }
        assert all(rule["type"] == "Allow" for rule in item["rules"])
        assert all(rule["rights"] == 2032127 for rule in item["rules"])  # FullControl
    assert all(rule["inherited"] for rule in before["items"][1]["rules"])
    with pytest.raises(FileExistsError):
        directories.create_result_directory(target)
    assert inspect_acl() == before
    assert artifact.read_text(encoding="utf-8") == '{"tweet":"🧢"}'
