param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [Parameter(Mandatory=$true)][string]$ExpectedCommit
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

function Assert-WorkerTaskContract($Task, $Root, $Python) {
    $triggers = @($Task.Triggers)
    $actions = @($Task.Actions)
    $expectedArguments = '-m fpl_bot.captain_worker_cli --config "' + $Root + '\worker-config.json"'
    if ($Task.Principal.UserId.Split('\')[-1] -ne 'captaintrial' -or
        $Task.Principal.LogonType -ne 'Password' -or $Task.Principal.RunLevel -ne 'Limited' -or
        -not $Task.Settings.Enabled -or $Task.State -ne 'Ready' -or
        $triggers.Count -ne 1 -or
        $triggers[0].CimClass.CimClassName -ne 'MSFT_TaskBootTrigger' -or
        $triggers[0].Delay -ne 'PT30S' -or $actions.Count -ne 1 -or
        $actions[0].Execute -ne $Python -or $actions[0].Arguments -ne $expectedArguments -or
        $actions[0].WorkingDirectory -ne $Root -or
        $Task.Settings.ExecutionTimeLimit -ne 'PT25M' -or
        $Task.Settings.MultipleInstances -ne 'IgnoreNew' -or
        $Task.Settings.RestartCount -ne 0 -or $Task.Settings.AllowDemandStart -or
        -not $Task.Settings.RunOnlyIfNetworkAvailable) {
        throw 'Scheduled Task contract mismatch; no replacement performed'
    }
}

function Read-WorkerConfig($Path, $Root, $Profile) {
    $document = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    $names = @($document.PSObject.Properties.Name | Sort-Object)
    if (($names -join ',') -ne 'audit_parent,controller_origin,expected_user,profile_dir' -or
        $document.controller_origin -ne 'https://captain-controller-524790767721.europe-west1.run.app' -or
        $document.profile_dir -ne $Profile -or $document.expected_user -ne 'captaintrial' -or
        $document.audit_parent -ne "$Root\audits") {
        throw 'Worker configuration contract mismatch; no replacement performed'
    }
    return $document
}

function Get-TreeHashes($Directory) {
    $result = @{}
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return $result }
    foreach ($file in @(Get-ChildItem -LiteralPath $Directory -Recurse -File)) {
        $relative = $file.FullName.Substring($Directory.Length).TrimStart('\')
        $result[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $result
}

function Assert-SameHashes($Expected, $Actual) {
    if ($Expected.Count -ne $Actual.Count) { throw 'Audit preservation count mismatch' }
    foreach ($name in $Expected.Keys) {
        if (-not $Actual.ContainsKey($name) -or $Actual[$name] -ne $Expected[$name]) {
            throw 'Audit preservation hash mismatch'
        }
    }
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.Name.Split('\')[-1] -ne 'captaintrial') {
    throw 'Run only as the existing captaintrial user'
}
if ($ExpectedCommit -notmatch '^[0-9a-f]{40}$' -or
    $ExpectedSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
    throw 'Invalid expected bundle identity'
}
$expectedHash = $ExpectedSha256.ToUpperInvariant()
if ((Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Bundle hash mismatch; no replacement performed'
}

$parent = Join-Path $env:LOCALAPPDATA 'FPLBot'
$root = Join-Path $parent 'CaptainCloudWorker01'
$staging = Join-Path $parent ("CaptainCloudWorker01.staging-$ExpectedCommit")
$rollback = Join-Path $parent 'CaptainCloudWorker01.rollback'
$profile = Join-Path $parent 'FPLReviewCaptainProfile'
$python = Join-Path $parent 'CaptainControlledTrial01\venv\Scripts\python.exe'
$taskName = 'Captain-Worker-NoPost'

foreach ($path in @($root, $profile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container) -or
        ((Get-Item -LiteralPath $path -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint)) {
        throw 'Required private directory missing or redirected; no replacement performed'
    }
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Existing proven Python runtime missing; no replacement performed'
}
if ((Test-Path -LiteralPath $staging) -or
    (Test-Path -LiteralPath $rollback)) {
    throw 'Staging or rollback target already exists; inspect it before retrying'
}
if (@(Get-Process chrome -ErrorAction SilentlyContinue).Count -ne 0) {
    throw 'Chrome is running; close it normally before replacement'
}
$workers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*fpl_bot.captain_worker_cli*'
})
if ($workers.Count -ne 0) { throw 'Captain worker process is still active' }
foreach ($marker in @('.captain-browser-owner','SingletonLock','SingletonSocket','SingletonCookie')) {
    if (Test-Path -LiteralPath (Join-Path $profile $marker)) {
        throw 'Browser profile lifecycle marker exists; do not remove it automatically'
    }
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
Assert-WorkerTaskContract $task $root $python
$existingConfig = Join-Path $root 'worker-config.json'
Read-WorkerConfig $existingConfig $root $profile | Out-Null
$configHash = (Get-FileHash -LiteralPath $existingConfig -Algorithm SHA256).Hash
$auditHashes = Get-TreeHashes (Join-Path $root 'audits')
$rootAcl = Get-Acl -LiteralPath $root
if (-not $rootAcl.AreAccessRulesProtected -or
    $rootAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) {
    throw 'Existing worker ownership/ACL contract mismatch; no replacement performed'
}
$oldBuild = 'legacy_unversioned'
$oldManifest = Join-Path $root 'worker-build.json'
if (Test-Path -LiteralPath $oldManifest -PathType Leaf) {
    $old = Get-Content -LiteralPath $oldManifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($old.schema_version -ne 1 -or $old.commit -notmatch '^[0-9a-f]{40}$') {
        throw 'Existing worker manifest is malformed; no replacement performed'
    }
    $oldBuild = [string]$old.commit
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $Bundle).Path)
try {
    $requiredModules = @(
        '__init__','captain_browser_runtime','captain_handoff','captain_models',
        'captain_orchestration_timing','captain_result_directory','captain_review_browser',
        'captain_serialization','captain_session_health','captain_state','captain_timing',
        'captain_transport','captain_vm_operations','captain_worker','captain_worker_browser',
        'captain_worker_cli','errors','events','models')
    $expectedEntries = @(
        $requiredModules | ForEach-Object { "fpl_bot/$_.py" }
    ) + @('prepare-captain-worker-remote.ps1','worker-build.json','worker-config.json')
    $entries = @($archive.Entries | Where-Object {
        $_.FullName -and -not $_.FullName.EndsWith('/')
    } | ForEach-Object FullName | Sort-Object)
    if (($entries -join "`n") -ne (($expectedEntries | Sort-Object) -join "`n")) {
        throw 'Bundle file closure mismatch'
    }
    $manifestEntry = $archive.GetEntry('worker-build.json')
    $reader = New-Object IO.StreamReader(
        $manifestEntry.Open(), (New-Object Text.UTF8Encoding($false, $true)))
    try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    if ($manifest.schema_version -ne 1 -or $manifest.commit -ne $ExpectedCommit) {
        throw 'Bundle manifest mismatch'
    }
} finally {
    $archive.Dispose()
}

New-Item -ItemType Directory -Path $staging | Out-Null
$stagingAcl = New-Object Security.AccessControl.DirectorySecurity
$stagingAcl.SetOwner($identity.User)
$stagingAcl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $account = New-Object Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $stagingAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $staging -AclObject $stagingAcl

try {
    Expand-Archive -LiteralPath $Bundle -DestinationPath $staging
    $newManifest = Get-Content -LiteralPath (Join-Path $staging 'worker-build.json') `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($newManifest.schema_version -ne 1 -or $newManifest.commit -ne $ExpectedCommit) {
        throw 'Expanded manifest mismatch'
    }
    Read-WorkerConfig (Join-Path $staging 'worker-config.json') $root $profile | Out-Null
    Copy-Item -LiteralPath $existingConfig -Destination (Join-Path $staging 'worker-config.json') `
        -Force
    if ((Get-FileHash -LiteralPath (Join-Path $staging 'worker-config.json') `
            -Algorithm SHA256).Hash -ne $configHash) {
        throw 'Private configuration was not preserved byte-for-byte'
    }
    $oldAudits = Join-Path $root 'audits'
    if (Test-Path -LiteralPath $oldAudits -PathType Container) {
        Copy-Item -LiteralPath $oldAudits -Destination $staging -Recurse
    } else {
        New-Item -ItemType Directory -Path (Join-Path $staging 'audits') | Out-Null
    }
    Assert-SameHashes $auditHashes (Get-TreeHashes (Join-Path $staging 'audits'))
    $taskXml = Join-Path $root 'Captain-Worker-NoPost.xml'
    if (Test-Path -LiteralPath $taskXml -PathType Leaf) {
        Copy-Item -LiteralPath $taskXml -Destination $staging
    }

    Rename-Item -LiteralPath $root -NewName (Split-Path $rollback -Leaf)
    try {
        Rename-Item -LiteralPath $staging -NewName (Split-Path $root -Leaf)
    } catch {
        Rename-Item -LiteralPath $rollback -NewName (Split-Path $root -Leaf)
        throw 'Replacement rename failed; original worker restored'
    }

    try {
        $installed = Get-Content -LiteralPath (Join-Path $root 'worker-build.json') `
            -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($installed.schema_version -ne 1 -or $installed.commit -ne $ExpectedCommit) {
            throw 'Installed manifest verification failed'
        }
        if ((Get-FileHash -LiteralPath (Join-Path $root 'worker-config.json') `
                -Algorithm SHA256).Hash -ne $configHash) {
            throw 'Installed private configuration changed'
        }
        Assert-SameHashes $auditHashes (Get-TreeHashes (Join-Path $root 'audits'))
        $installedAcl = Get-Acl -LiteralPath $root
        if (-not $installedAcl.AreAccessRulesProtected -or
            $installedAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value `
                -ne $identity.User.Value) {
            throw 'Installed worker ownership/ACL verification failed'
        }
        & $python -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency integrity failed' }
        Push-Location $root
        try {
            & $python -c "import fpl_bot.captain_worker_cli as m; assert m.__file__.startswith(r'$root')"
            if ($LASTEXITCODE -ne 0) { throw 'Worker-only import validation failed' }
        } finally {
            Pop-Location
        }
        Assert-WorkerTaskContract (Get-ScheduledTask -TaskName $taskName -ErrorAction Stop) `
            $root $python
    } catch {
        $failed = "$staging.failed"
        if (Test-Path -LiteralPath $failed) {
            throw 'Installed verification failed and automatic rollback target is occupied'
        }
        Rename-Item -LiteralPath $root -NewName (Split-Path $failed -Leaf)
        Rename-Item -LiteralPath $rollback -NewName (Split-Path $root -Leaf)
        throw 'Installed verification failed; original worker restored and failed stage retained'
    }
} catch { throw }

[pscustomobject]@{
    status = 'replacement_verified'
    previous_build = $oldBuild
    installed_build = $ExpectedCommit
    bundle_sha256 = $expectedHash
    preserved_config_sha256 = $configHash
    preserved_audit_files = $auditHashes.Count
    rollback_retained = $true
    task_temporarily_disabled = $false
    task_enabled = $true
    task_trigger = 'boot_delay_30_seconds'
    browser_profile_modified = $false
} | Format-List
