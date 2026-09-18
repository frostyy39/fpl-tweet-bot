# Windows-safe loader for the exact reviewed Python block in the adjacent YAML.
# Encoding protects command-line quoting only; the code contains no credentials.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$lines = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'shared-oauth-isolation-probe.yaml')
$start = [Array]::IndexOf($lines, '                - |') + 1
if ($start -le 0) { throw 'Reviewed probe code block is missing.' }
$code = ($lines[$start..($lines.Length - 1)] | ForEach-Object {
    if (-not $_.StartsWith('                  ')) { throw 'Unexpected probe indentation.' }
    $_.Substring(18)
}) -join "`n"
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
$arguments = "-c,exec(__import__('base64').b64decode('$encoded'))"
gcloud.cmd run jobs create captain-oauth-isolation-probe --region=europe-west1 `
    --project=fpl-frosty-bot-v1 `
    --image=europe-west2-docker.pkg.dev/fpl-frosty-bot-v1/fpl-bot/shared-oauth-database@sha256:093b9800dd37b3943c9ca94123f647682f2b2ad76ae0b84e59d3f3b3f797578b `
    --service-account=captain-publisher@fpl-frosty-bot-v1.iam.gserviceaccount.com `
    --command=python --args=$arguments --tasks=1 --parallelism=1 --max-retries=0 `
    --task-timeout=120s
if ($LASTEXITCODE -ne 0) { throw 'Probe preparation failed; do not execute or change IAM.' }
# No automatic execution. Remove this temporary job after independently auditing it.
