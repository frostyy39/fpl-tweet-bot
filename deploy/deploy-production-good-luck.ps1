[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory=$true)][string]$Image
)

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$projectNumber = '524790767721'
$serviceRegion = 'europe-west1'
$controlRegion = 'europe-west2'
$service = 'good-luck-production'
$database = 'production-good-luck-state'
$queue = 'production-good-luck-deadline'
$scheduler = 'good-luck-production-checker'
$runtime = "good-luck-production-runtime@$project.iam.gserviceaccount.com"
$invoker = "good-luck-production-invoker@$project.iam.gserviceaccount.com"
$origin = "https://$service-$projectNumber.$serviceRegion.run.app"
$identityFile = Join-Path $PSScriptRoot '..\src\fpl_bot\production_x_identity.py'
$identitySource = Get-Content -LiteralPath $identityFile -Raw
$identityMatch = [regex]::Match(
    $identitySource,
    'PRODUCTION_X_USER_ID:\s*Final\[str\s*\|\s*None\]\s*=\s*"([1-9][0-9]*)"'
)
if (-not $identityMatch.Success) {
    throw 'Reviewed production X identity is not committed; deployment is prohibited.'
}
$productionUserId = $identityMatch.Groups[1].Value
if ($Image -notmatch '^europe-west2-docker\.pkg\.dev/fpl-frosty-bot-v1/good-luck-images/production:[a-f0-9]{40}$') {
    throw 'A commit-tagged production Good Luck image is required.'
}

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Production Good Luck deployment stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Deploy disabled production Good Luck resources')) {
    return
}

$business = Cloud firestore databases describe "--database=$database" "--project=$project" `
    '--format=json' | ConvertFrom-Json
$oauth = Cloud firestore databases describe '--database=production-shared-x-oauth' `
    "--project=$project" '--format=json' | ConvertFrom-Json
if ($business.locationId -ne $controlRegion -or $oauth.locationId -ne $controlRegion) {
    throw 'Production Good Luck state boundaries are unavailable in the reviewed location.'
}

$settings = @(
    "GCP_PROJECT_ID=$project",
    "GCP_PROJECT_NUMBER=$projectNumber",
    "FIRESTORE_DATABASE_ID=$database",
    'X_OAUTH_FIRESTORE_DATABASE_ID=production-shared-x-oauth',
    "CLOUD_TASKS_LOCATION_ID=$controlRegion",
    "CLOUD_TASKS_QUEUE_ID=$queue",
    "CLOUD_RUN_BASE_URL=$origin",
    "CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL=$invoker",
    "CLOUD_TASKS_OIDC_AUDIENCE=$origin",
    'X_TOKEN_SECRET_ID=production-x-oauth-token-state',
    'X_ENVIRONMENT=production',
    "X_EXPECTED_USER_ID=$productionUserId",
    'X_POSTING_ENABLED=false'
) -join ','
$secrets = @(
    'X_OAUTH_CLIENT_ID=x-oauth-client-id:latest',
    'X_OAUTH_CLIENT_SECRET=x-oauth-client-secret:latest'
) -join ','
Cloud run deploy $service "--project=$project" "--region=$serviceRegion" "--image=$Image" `
    '--no-allow-unauthenticated' "--service-account=$runtime" '--min-instances=0' `
    '--max-instances=2' '--cpu=1' '--memory=512Mi' '--concurrency=8' '--timeout=90' `
    "--add-custom-audiences=$origin" "--set-env-vars=$settings" "--set-secrets=$secrets" '--quiet'
Cloud run services add-iam-policy-binding $service "--project=$project" `
    "--region=$serviceRegion" "--member=serviceAccount:$invoker" `
    '--role=roles/run.invoker' '--quiet'

$queues = @(Cloud tasks queues list "--project=$project" "--location=$controlRegion" `
    '--format=value(name.basename())')
if ($queues -notcontains $queue) {
    Cloud tasks queues create $queue "--project=$project" "--location=$controlRegion" `
        '--max-attempts=10' '--min-backoff=30s' '--max-backoff=300s' '--max-doublings=3' `
        '--max-dispatches-per-second=1' '--max-concurrent-dispatches=1'
}
else {
    Cloud tasks queues update $queue "--project=$project" "--location=$controlRegion" `
        '--max-attempts=10' '--min-backoff=30s' '--max-backoff=300s' '--max-doublings=3' `
        '--max-dispatches-per-second=1' '--max-concurrent-dispatches=1'
}
Cloud tasks queues pause $queue "--project=$project" "--location=$controlRegion"
Cloud tasks queues add-iam-policy-binding $queue "--project=$project" `
    "--location=$controlRegion" "--member=serviceAccount:$runtime" `
    '--role=roles/cloudtasks.enqueuer'

$schedulers = @(Cloud scheduler jobs list "--project=$project" "--location=$controlRegion" `
    '--format=value(name.basename())')
if ($schedulers -notcontains $scheduler) {
    Cloud scheduler jobs create http $scheduler "--project=$project" "--location=$controlRegion" `
        "--schedule=0 6 * * *" '--time-zone=Europe/London' '--http-method=POST' `
        "--uri=$origin/checker/run" "--oidc-service-account-email=$invoker" `
        "--oidc-token-audience=$origin" '--attempt-deadline=60s'
}
else {
    Cloud scheduler jobs update http $scheduler "--project=$project" "--location=$controlRegion" `
        "--schedule=0 6 * * *" '--time-zone=Europe/London' '--http-method=POST' `
        "--uri=$origin/checker/run" "--oidc-service-account-email=$invoker" `
        "--oidc-token-audience=$origin" '--attempt-deadline=60s'
}
Cloud scheduler jobs pause $scheduler "--project=$project" "--location=$controlRegion"

$servicePolicy = Cloud run services get-iam-policy $service "--project=$project" `
    "--region=$serviceRegion" '--format=json' | ConvertFrom-Json
$invokers = @(
    $servicePolicy.bindings | Where-Object role -eq 'roles/run.invoker' | ForEach-Object members
)
if ($invokers.Count -ne 1 -or $invokers[0] -ne "serviceAccount:$invoker") {
    throw 'Production Good Luck invocation policy is broader than its dedicated invoker.'
}
$queueState = Cloud tasks queues describe $queue "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$schedulerState = Cloud scheduler jobs describe $scheduler "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$tasks = @(Cloud tasks list "--queue=$queue" "--project=$project" `
    "--location=$controlRegion" '--format=value(name)')
if ($queueState -ne 'PAUSED' -or $schedulerState -ne 'PAUSED' -or $tasks.Count -ne 0) {
    throw 'Production Good Luck scheduling boundary is not safely paused and empty.'
}
Write-Output "Disabled production Good Luck deployed for immutable X user $productionUserId."
