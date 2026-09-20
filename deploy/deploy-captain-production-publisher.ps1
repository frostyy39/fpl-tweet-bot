[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory=$true)][string]$Image
)

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$projectNumber = '524790767721'
$region = 'europe-west1'
$service = 'captain-production-publisher'
$runtime = "captain-production-publisher@$project.iam.gserviceaccount.com"
$invoker = "captain-prod-pub-invoker@$project.iam.gserviceaccount.com"
$origin = "https://$service-$projectNumber.$region.run.app"
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

if ($Image -notmatch '^europe-west2-docker\.pkg\.dev/fpl-frosty-bot-v1/captain-images/production-publisher:[a-f0-9]{40}$') {
    throw 'A commit-tagged production publisher image is required.'
}

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Production Captain publisher deployment stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Deploy disabled production-account Captain publisher')) {
    return
}

$database = Cloud firestore databases describe '--database=production-shared-x-oauth' `
    "--project=$project" '--format=json' | ConvertFrom-Json
if ($database.locationId -ne 'europe-west2' -or $database.type -ne 'FIRESTORE_NATIVE') {
    throw 'Production OAuth database boundary is unavailable.'
}
$authoritySecret = Cloud secrets describe production-x-oauth-token-state "--project=$project" `
    '--format=value(name)'
if (-not $authoritySecret) {
    throw 'Production OAuth token authority secret is unavailable.'
}

$settings = @(
    "GCP_PROJECT_ID=$project",
    "GCP_PROJECT_NUMBER=$projectNumber",
    'FIRESTORE_DATABASE_ID=captain-state',
    'X_OAUTH_FIRESTORE_DATABASE_ID=production-shared-x-oauth',
    "CAPTAIN_PUBLISHER_ORIGIN=$origin",
    "CAPTAIN_PUBLISHER_INVOKER_EMAIL=$invoker",
    'X_TOKEN_SECRET_ID=production-x-oauth-token-state',
    'X_ENVIRONMENT=production',
    "X_EXPECTED_USER_ID=$productionUserId",
    'X_POSTING_ENABLED=false'
) -join ','
$secrets = @(
    'X_OAUTH_CLIENT_ID=x-oauth-client-id:latest',
    'X_OAUTH_CLIENT_SECRET=x-oauth-client-secret:latest'
) -join ','
Cloud run deploy $service "--project=$project" "--region=$region" "--image=$Image" `
    '--no-allow-unauthenticated' "--service-account=$runtime" '--min-instances=0' `
    '--max-instances=2' '--cpu=1' '--memory=512Mi' '--concurrency=8' '--timeout=90' `
    "--add-custom-audiences=$origin" "--set-env-vars=$settings" "--set-secrets=$secrets" '--quiet'
Cloud run services add-iam-policy-binding $service "--project=$project" "--region=$region" `
    "--member=serviceAccount:$invoker" '--role=roles/run.invoker' '--quiet'

$policy = Cloud run services get-iam-policy $service "--project=$project" "--region=$region" `
    '--format=json' | ConvertFrom-Json
$invokers = @(
    $policy.bindings |
        Where-Object role -eq 'roles/run.invoker' |
        ForEach-Object members
)
if ($invokers.Count -ne 1 -or $invokers[0] -ne "serviceAccount:$invoker") {
    throw 'Production publisher invocation policy is broader than its dedicated invoker.'
}
Write-Output "Disabled production Captain publisher deployed for immutable X user $productionUserId."
