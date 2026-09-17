# A bounded duplicate-delivery diagnostic, not a timing override. It copies an
# already-durable warmup intent into a temporary provider task so the real future
# task is not consumed. The real handler still enforces fresh FPL/time/state.
param([Parameter(Mandatory=$true)][string]$GenerationId)
$ErrorActionPreference = 'Stop'
if ($GenerationId -notmatch '^[0-9a-f-]{36}$') { throw 'Invalid generation identity' }
$project = 'fpl-frosty-bot-v1'
$location = 'europe-west2'
$queue = 'captain-orchestration'
$origin = 'https://captain-controller-524790767721.europe-west1.run.app'
$expectedUrl = $origin + '/captain/tasks/warmup'
$raw = & gcloud.cmd tasks queues describe $queue --project=$project --location=$location --format='value(state)'
if ($LASTEXITCODE -ne 0 -or $raw -ne 'PAUSED') { throw 'Queue must remain paused' }
$existing = & gcloud.cmd tasks describe "captain-$GenerationId-warmup" --project=$project `
    --location=$location --queue=$queue --response-view=full --format=json
if ($LASTEXITCODE -ne 0) { throw 'Existing durable-intent task unavailable' }
$task = $existing | ConvertFrom-Json
$body = [Convert]::FromBase64String($task.httpRequest.body)
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$payload = $utf8.GetString($body) | ConvertFrom-Json
if ($task.httpRequest.url -ne $expectedUrl -or
    $task.httpRequest.httpMethod -ne 'POST' -or
    $task.httpRequest.oidcToken.audience -ne $origin -or
    $task.httpRequest.oidcToken.serviceAccountEmail -ne "captain-tasks@$project.iam.gserviceaccount.com" -or
    $payload.kind -ne 'warmup' -or $payload.identity -ne "captain-$GenerationId-warmup" -or
    $payload.destination_user_id -ne '1') { throw 'Unexpected Captain warmup task contract' }
$scheduled = [DateTimeOffset]$payload.scheduled_at
if ([DateTimeOffset]::UtcNow -ge $scheduled.AddMinutes(-30)) {
    throw 'Early diagnostic must finish well before the real warmup window'
}
$directory = Join-Path (Split-Path $PSScriptRoot) 'captain-rehearsal-artifacts'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$name = 'captain-rehearsal-early-' + [guid]::NewGuid().ToString('N')
$bodyPath = Join-Path $directory ($name + '.json')
[System.IO.File]::WriteAllBytes($bodyPath, $body)
$providerSchedule = ([DateTimeOffset]$task.scheduleTime).UtcDateTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
try {
    & gcloud.cmd tasks create-http-task $name --project=$project --location=$location --queue=$queue `
        --url=$expectedUrl --method=POST --header=Content-Type:application/json --body-file=$bodyPath `
        --oidc-service-account-email="captain-tasks@$project.iam.gserviceaccount.com" `
        --oidc-token-audience=$origin --schedule-time=$providerSchedule --format='value(name)'
    if ($LASTEXITCODE -ne 0) { throw 'Diagnostic task creation failed; do not blindly retry' }
    # RunTask is intentionally used once for this separate diagnostic only. It
    # does not resume the queue or run/delete the real scheduled warmup task.
    & gcloud.cmd tasks run $name --project=$project --location=$location --queue=$queue --format='json(name,lastAttempt)' --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Diagnostic dispatch failed; reconcile before retry' }
    Write-Output "Temporary early task dispatched once: $name"
} finally {
    # Successful delivery may have already deleted the diagnostic. Preserve the
    # real durable task and every other Captain task regardless of this result.
    $names = & gcloud.cmd tasks list --project=$project --location=$location --queue=$queue --format='value(name)'
    if ($LASTEXITCODE -ne 0) { throw "Cannot confirm diagnostic cleanup: $name" }
    if (@($names | ForEach-Object { ($_ -split '/')[-1] }) -contains $name) {
        & gcloud.cmd tasks delete $name --project=$project --location=$location --queue=$queue --quiet
        if ($LASTEXITCODE -ne 0) { throw "Diagnostic task cleanup failed: $name" }
    }
}
