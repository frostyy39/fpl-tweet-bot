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

function Assert-SameHashes($Expected, $Actual, $Description = 'Audit preservation') {
    if ($Expected.Count -ne $Actual.Count) { throw "$Description count mismatch" }
    foreach ($name in $Expected.Keys) {
        if (-not $Actual.ContainsKey($name) -or $Actual[$name] -ne $Expected[$name]) {
            throw "$Description hash mismatch"
        }
    }
}

function Preserve-VersionedRollback($Rollback, $Archive, $ExpectedHashes) {
    if (Test-Path -LiteralPath $Archive) {
        throw 'Versioned rollback archive target occupied; no replacement performed'
    }
    Rename-Item -LiteralPath $Rollback -NewName (Split-Path $Archive -Leaf)
    Assert-SameHashes $ExpectedHashes (Get-TreeHashes $Archive) `
        'Versioned rollback preservation'
}

function Assert-WorkerArchiveContent($Directory) {
    foreach ($item in @(Get-ChildItem -LiteralPath $Directory -Recurse -Force)) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            ($item.PSIsContainer -and ($item.Name -eq 'FPLReviewCaptainProfile' -or
                $item.Name -like 'CaptainCloudWorker01.rollback*'))) {
            throw 'Rollback contains redirected or browser-profile content; no replacement performed'
        }
    }
}

function Get-TreeDigest($Hashes) {
    $lines = @($Hashes.Keys | Sort-Object | ForEach-Object { "$_`t$($Hashes[$_])" })
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Read-VersionedArchive($Directory, $Root, $Profile, $OwnerSid) {
    $item = Get-Item -LiteralPath $Directory -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $item.Name -cnotmatch '^CaptainCloudWorker01\.rollback-versioned-([0-9a-f]{40})(?:-duplicate-([0-9a-f]{64}))?$') {
        throw 'Versioned rollback archive name or type invalid; no replacement performed'
    }
    $null = $item.Name -cmatch '^CaptainCloudWorker01\.rollback-versioned-([0-9a-f]{40})(?:-duplicate-([0-9a-f]{64}))?$'
    $build = $Matches[1]
    $duplicateDigest = $Matches[2]
    $manifestPath = Join-Path $Directory 'worker-build.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $Directory 'fpl_bot\captain_worker_cli.py') -PathType Leaf)) {
        throw 'Versioned rollback archive structure invalid; no replacement performed'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.commit -cne $build) {
        throw 'Versioned rollback archive identity invalid; no replacement performed'
    }
    Assert-WorkerArchiveContent $Directory
    Read-WorkerConfig (Join-Path $Directory 'worker-config.json') $Root $Profile | Out-Null
    $acl = Get-Acl -LiteralPath $Directory
    if (-not $acl.AreAccessRulesProtected -or
        $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $OwnerSid) {
        throw 'Versioned rollback archive ownership/ACL mismatch; no replacement performed'
    }
    $hashes = Get-TreeHashes $Directory
    if ($duplicateDigest -and (Get-TreeDigest $hashes) -cne $duplicateDigest) {
        throw 'Versioned rollback duplicate digest mismatch; no replacement performed'
    }
    return [pscustomobject]@{ Build = $build; Hashes = $hashes; Path = $Directory }
}

function Select-VersionedArchivePath($Parent, $Build, $RollbackHashes, $Inventory) {
    if ($Build -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Versioned rollback identity invalid; no replacement performed'
    }
    $sameBuild = @($Inventory | Where-Object Build -eq $Build)
    foreach ($existing in $sameBuild) {
        Assert-SameHashes $RollbackHashes $existing.Hashes 'Same-build versioned archive'
    }
    $name = "CaptainCloudWorker01.rollback-versioned-$Build"
    if ($sameBuild.Count -gt 0) {
        $name += "-duplicate-$(Get-TreeDigest $RollbackHashes)"
    }
    $target = Join-Path $Parent $name
    if (Test-Path -LiteralPath $target) {
        throw 'Versioned rollback archive target occupied; no replacement performed'
    }
    return $target
}

function Assert-InstalledBundle($Root, $Archive) {
    foreach ($entry in @($Archive.Entries | Where-Object {
        $_.FullName -and -not $_.FullName.EndsWith('/') -and
        $_.FullName -ne 'worker-config.json'
    })) {
        $path = Join-Path $Root ($entry.FullName.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw 'Already-installed bundle is incomplete; no replacement performed'
        }
        $stream = $entry.Open()
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $expected = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
        finally { $sha.Dispose(); $stream.Dispose() }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $expected) {
            throw 'Already-installed bundle differs from expected ZIP; no replacement performed'
        }
    }
    $expectedModules = @($Archive.Entries | Where-Object {
        $_.FullName -like 'fpl_bot/*.py'
    } | ForEach-Object { Split-Path $_.FullName -Leaf } | Sort-Object)
    $installedModules = @(Get-ChildItem -LiteralPath (Join-Path $Root 'fpl_bot') -File -Filter '*.py' |
        ForEach-Object Name | Sort-Object)
    if (($expectedModules -join ',') -ne ($installedModules -join ',')) {
        throw 'Already-installed Python closure differs from expected ZIP; no replacement performed'
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
$legacyRollbackArchive = Join-Path $parent 'CaptainCloudWorker01.rollback-legacy-unversioned'
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
if (Test-Path -LiteralPath $staging) {
    throw 'Staging target already exists; inspect it before retrying'
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
        'captain_orchestration_timing','captain_release_diagnostics',
        'captain_result_directory','captain_review_browser',
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
    $alreadyInstalled = $oldBuild -eq $ExpectedCommit
    if ($alreadyInstalled) { Assert-InstalledBundle $root $archive }
} finally {
    $archive.Dispose()
}

$versionedRollbackPreserved = $false
$versionedRollbackPath = $null
$versionedRollbackBuild = $null
$existingArchives = @(Get-ChildItem -LiteralPath $parent -Force |
    Where-Object Name -like 'CaptainCloudWorker01.rollback-versioned-*')
$archiveInventory = @()
foreach ($existing in $existingArchives) {
    $archiveInventory += Read-VersionedArchive $existing.FullName $root $profile $identity.User.Value
}
for ($left = 0; $left -lt $archiveInventory.Count; $left++) {
    for ($right = $left + 1; $right -lt $archiveInventory.Count; $right++) {
        if ($archiveInventory[$left].Build -eq $archiveInventory[$right].Build) {
            Assert-SameHashes $archiveInventory[$left].Hashes $archiveInventory[$right].Hashes `
                'Same-build versioned archive'
        }
    }
}
if ($alreadyInstalled) {
    [pscustomobject]@{
        status = 'already_installed_verified'
        installed_build = $ExpectedCommit
        bundle_sha256 = $expectedHash
        versioned_archive_count = $archiveInventory.Count
        browser_profile_modified = $false
        task_enabled = $true
    } | Format-List
    return
}
if (Test-Path -LiteralPath $rollback) {
    if (-not (Test-Path -LiteralPath $rollback -PathType Container) -or
        ((Get-Item -LiteralPath $rollback -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint)) {
        throw 'Existing rollback is not a private directory; no replacement performed'
    }
    $rollbackManifest = Join-Path $rollback 'worker-build.json'
    if (Test-Path -LiteralPath $rollbackManifest -PathType Leaf) {
        $rollbackBuild = Get-Content -LiteralPath $rollbackManifest -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ($rollbackBuild.schema_version -ne 1 -or
            $rollbackBuild.commit -notmatch '^[0-9a-f]{40}$' -or
            $rollbackBuild.commit -eq $oldBuild) {
            throw 'Versioned rollback identity invalid; no replacement performed'
        }
        $versionedRollbackBuild = [string]$rollbackBuild.commit
        if (-not (Test-Path -LiteralPath (Join-Path $rollback 'fpl_bot\captain_worker_cli.py') `
                -PathType Leaf)) {
            throw 'Versioned rollback structure invalid; no replacement performed'
        }
        Assert-WorkerArchiveContent $rollback
        Read-WorkerConfig (Join-Path $rollback 'worker-config.json') $root $profile |
            Out-Null
        $rollbackAcl = Get-Acl -LiteralPath $rollback
        if (-not $rollbackAcl.AreAccessRulesProtected -or
            $rollbackAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value `
                -ne $identity.User.Value) {
            throw 'Versioned rollback ownership/ACL mismatch; no replacement performed'
        }
        $versionedHashes = Get-TreeHashes $rollback
        $versionedRollbackPath = Select-VersionedArchivePath $parent `
            $versionedRollbackBuild $versionedHashes $archiveInventory
        Preserve-VersionedRollback $rollback $versionedRollbackPath $versionedHashes
        $versionedRollbackPreserved = $true
        $archiveInventory += Read-VersionedArchive $versionedRollbackPath $root $profile `
            $identity.User.Value
    } elseif (Test-Path -LiteralPath $legacyRollbackArchive) {
        throw 'Both unversioned rollback generations require operator review'
    }
}
$versionedRollbackPreserved = $archiveInventory.Count -gt 0

$legacyRollbackPreserved = $false
$legacyRollbackPath = $null
if (Test-Path -LiteralPath $rollback) {
    if (-not (Test-Path -LiteralPath $rollback -PathType Container) -or
        ((Get-Item -LiteralPath $rollback -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint)) {
        throw 'Existing rollback is not a private directory; no replacement performed'
    }
    if (Test-Path -LiteralPath (Join-Path $rollback 'worker-build.json')) {
        throw 'Existing rollback is versioned; inspect it before replacement'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $rollback 'fpl_bot\captain_worker_cli.py') `
            -PathType Leaf)) {
        throw 'Existing legacy rollback structure is incomplete; no replacement performed'
    }
    Read-WorkerConfig (Join-Path $rollback 'worker-config.json') $root $profile | Out-Null
    $rollbackAcl = Get-Acl -LiteralPath $rollback
    if (-not $rollbackAcl.AreAccessRulesProtected -or
        $rollbackAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value `
            -ne $identity.User.Value) {
        throw 'Existing legacy rollback ownership/ACL mismatch; no replacement performed'
    }
    $legacyHashes = Get-TreeHashes $rollback
    Rename-Item -LiteralPath $rollback -NewName (Split-Path $legacyRollbackArchive -Leaf)
    Assert-SameHashes $legacyHashes (Get-TreeHashes $legacyRollbackArchive) `
        'Legacy rollback preservation'
    $legacyRollbackPreserved = $true
    $legacyRollbackPath = $legacyRollbackArchive
} elseif (Test-Path -LiteralPath $legacyRollbackArchive) {
    if (-not (Test-Path -LiteralPath $legacyRollbackArchive -PathType Container) -or
        ((Get-Item -LiteralPath $legacyRollbackArchive -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) -or
        (Test-Path -LiteralPath (Join-Path $legacyRollbackArchive 'worker-build.json')) -or
        -not (Test-Path -LiteralPath `
            (Join-Path $legacyRollbackArchive 'fpl_bot\captain_worker_cli.py') -PathType Leaf)) {
        throw 'Prepared legacy rollback archive is invalid; no replacement performed'
    }
    Read-WorkerConfig (Join-Path $legacyRollbackArchive 'worker-config.json') `
        $root $profile | Out-Null
    $legacyArchiveAcl = Get-Acl -LiteralPath $legacyRollbackArchive
    if (-not $legacyArchiveAcl.AreAccessRulesProtected -or
        $legacyArchiveAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value `
            -ne $identity.User.Value) {
        throw 'Prepared legacy rollback archive ownership/ACL mismatch; no replacement performed'
    }
    $legacyRollbackPreserved = $true
    $legacyRollbackPath = $legacyRollbackArchive
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
    legacy_rollback_preserved = $legacyRollbackPreserved
    legacy_rollback_path = $legacyRollbackPath
    versioned_rollback_preserved = $versionedRollbackPreserved
    versioned_rollback_path = $versionedRollbackPath
    versioned_rollback_build = $versionedRollbackBuild
    versioned_archive_count = $archiveInventory.Count
    task_temporarily_disabled = $false
    task_enabled = $true
    task_trigger = 'boot_delay_30_seconds'
    browser_profile_modified = $false
} | Format-List
