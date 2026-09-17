param([switch]$Restore)
$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$zone = 'europe-west2-b'
$instance = 'captain-browser-london-windows-trial'
$artifacts = Join-Path (Split-Path $PSScriptRoot) 'captain-rehearsal-artifacts'
$backupPath = Join-Path $artifacts 'boot-observer-metadata-backup.json'
$generatedPath = Join-Path $artifacts 'boot-observer-startup.ps1'
$key = 'windows-startup-script-ps1'
# Preserve only the startup-script/guest-attributes settings; never save other metadata.
$raw = & gcloud.cmd compute instances describe $instance --project=$project --zone=$zone --format='json(metadata.items)'
if ($LASTEXITCODE -ne 0) { throw 'Captain VM metadata inspection failed' }
$items = @(($raw | ConvertFrom-Json).metadata.items)
$current = @($items | Where-Object { $_.key -eq $key })
$guest = @($items | Where-Object { $_.key -eq 'enable-guest-attributes' })
if ($current.Count -ne 1) { throw 'Expected existing Windows baseline startup script' }
$utf8 = New-Object System.Text.UTF8Encoding($false)
if ($Restore) {
    $backup = Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json
    if ($current[0].value -ne [System.IO.File]::ReadAllText($generatedPath)) {
        throw 'Startup metadata changed concurrently; do not overwrite'
    }
    $restorePath = Join-Path $artifacts 'boot-observer-original-startup.ps1'
    [System.IO.File]::WriteAllText($restorePath, $backup.startup_script, $utf8)
    & gcloud.cmd compute instances add-metadata $instance --project=$project --zone=$zone `
        --metadata-from-file="$key=$restorePath"
    if ($LASTEXITCODE -ne 0) { throw 'Startup-script restoration failed' }
    if ($backup.guest_attributes_present) {
        & gcloud.cmd compute instances add-metadata $instance --project=$project --zone=$zone `
            --metadata="enable-guest-attributes=$($backup.guest_attributes_value)"
    } else {
        & gcloud.cmd compute instances remove-metadata $instance --project=$project --zone=$zone `
            --keys=enable-guest-attributes
    }
    if ($LASTEXITCODE -ne 0) { throw 'Guest-attributes setting restoration failed' }
    Write-Output 'Original Captain startup metadata restored; observer will not run on later boots.'
    exit 0
}
if (Test-Path -LiteralPath $backupPath) { throw 'Observer backup already exists; do not overwrite' }
if ($current[0].value.Contains('CAPTAIN_TEMPORARY_BOOT_OBSERVER')) { throw 'Observer already installed' }
New-Item -ItemType Directory -Path $artifacts -Force | Out-Null
$backup = @{startup_script=$current[0].value; guest_attributes_present=($guest.Count -eq 1)
    guest_attributes_value=if ($guest.Count -eq 1) {$guest[0].value} else {$null}}
[System.IO.File]::WriteAllText($backupPath, ($backup | ConvertTo-Json), $utf8)
$observer = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'observe-captain-worker-boot.ps1'))
$encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($observer))
$prefix = "# CAPTAIN_TEMPORARY_BOOT_OBSERVER: read-only, bounded, no browser/user impersonation.`n"
$prefix += "Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand','$encoded') | Out-Null`n"
[System.IO.File]::WriteAllText($generatedPath, ($prefix + $current[0].value), $utf8)
& gcloud.cmd compute instances add-metadata $instance --project=$project --zone=$zone `
    --metadata=enable-guest-attributes=TRUE --metadata-from-file="$key=$generatedPath"
if ($LASTEXITCODE -ne 0) { throw 'Observer metadata installation failed; backup preserved' }
Write-Output 'Temporary bounded observer installed; original baseline script preserved verbatim.'
