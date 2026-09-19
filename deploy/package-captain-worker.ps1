$ErrorActionPreference = 'Stop'
$python = (Resolve-Path '.venv/Scripts/python.exe').Path
$head = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify source checkpoint' }
$destination = Join-Path (Get-Location) "captain-rehearsal-artifacts/worker-$head"
if (Test-Path -LiteralPath $destination) { throw 'Package output already exists; do not overwrite' }
New-Item -ItemType Directory -Path "$destination/fpl_bot" | Out-Null
$modules = & $python deploy/captain-worker-modules.py
if ($LASTEXITCODE -ne 0) { throw 'Worker dependency closure failed' }
foreach ($module in $modules) {
    Copy-Item -LiteralPath "src/fpl_bot/$module.py" -Destination "$destination/fpl_bot/$module.py"
}
Copy-Item -LiteralPath deploy/captain-worker-config.json -Destination "$destination/worker-config.json"
Copy-Item -LiteralPath deploy/prepare-captain-worker-remote.ps1 -Destination "$destination/prepare-captain-worker-remote.ps1"
$manifest = @{ schema_version = 1; commit = $head } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText(
    "$destination/worker-build.json", $manifest + "`n", (New-Object System.Text.UTF8Encoding($false)))
$zip = "$destination.zip"
Compress-Archive -Path "$destination/*" -DestinationPath $zip -CompressionLevel Optimal
Get-FileHash -LiteralPath $zip -Algorithm SHA256
Write-Output "Worker-only bundle: $zip"
