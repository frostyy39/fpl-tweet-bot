param([Parameter(Mandatory=$true)][string]$Bundle,
      [Parameter(Mandatory=$true)][string]$ExpectedSha256)
$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.Name.Split('\')[-1] -ne 'captaintrial') { throw 'Run as the existing captaintrial user' }
if ((Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'Bundle hash mismatch'
}
$root = Join-Path $env:LOCALAPPDATA 'FPLBot\CaptainCloudWorker01'
$python = Join-Path $env:LOCALAPPDATA 'FPLBot\CaptainControlledTrial01\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Existing proven Python runtime missing' }
if (Test-Path -LiteralPath $root) { throw 'Worker directory already exists; do not overwrite' }
if ((Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count -ne 0) { throw 'Close Chrome normally first' }
foreach ($marker in @('.captain-browser-owner','SingletonLock','SingletonSocket','SingletonCookie')) {
    if (Test-Path -LiteralPath (Join-Path "$env:LOCALAPPDATA\FPLBot\FPLReviewCaptainProfile" $marker)) {
        throw 'Profile lifecycle marker exists; stop without removing it'
    }
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency integrity failed' }
New-Item -ItemType Directory -Path $root | Out-Null
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetOwner($identity.User)
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $account = New-Object System.Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $root -AclObject $acl
Expand-Archive -LiteralPath $Bundle -DestinationPath $root
New-Item -ItemType Directory -Path "$root/audits" | Out-Null
$config = Get-Content -LiteralPath "$root/worker-config.json" -Raw | ConvertFrom-Json
if ($config.controller_origin -ne 'https://captain-controller-524790767721.europe-west2.run.app' -or
    $config.profile_dir -ne "$env:LOCALAPPDATA\FPLBot\FPLReviewCaptainProfile" -or
    $config.audit_parent -ne "$root\audits" -or $config.expected_user -ne 'captaintrial') {
    throw 'Unexpected worker configuration'
}
# NewTask builds XML in memory; it DOES NOT register or run anything.
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$task = $service.NewTask(0)
$task.RegistrationInfo.Description = 'Captain bounded authenticated NO-POST startup worker. No X credentials.'
$task.Principal.UserId = $identity.Name
$task.Principal.LogonType = 1 # Password; the user supplies it only in native GUI.
$task.Principal.RunLevel = 0 # Limited
$task.Settings.Enabled = $true
$task.Settings.StartWhenAvailable = $true
$task.Settings.RunOnlyIfNetworkAvailable = $true
$task.Settings.MultipleInstances = 2 # IgnoreNew
$task.Settings.RestartCount = 0
$task.Settings.ExecutionTimeLimit = 'PT25M'
$task.Settings.AllowDemandStart = $false
$trigger = $task.Triggers.Create(8) # TASK_TRIGGER_BOOT, not LOGON.
$trigger.Delay = 'PT30S'
$action = $task.Actions.Create(0)
$action.Path = $python
$action.Arguments = "-m fpl_bot.captain_worker_cli --config `"$root\worker-config.json`""
$action.WorkingDirectory = $root
$xml = "$root\Captain-Worker-NoPost.xml"
[System.IO.File]::WriteAllText($xml, $task.XmlText, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Prepared XML only: $xml"
Write-Output 'STOP: Import XML in Task Scheduler as Captain-Worker-NoPost. Save using the existing password privately in the native Windows dialog. Do not Run the task.'
