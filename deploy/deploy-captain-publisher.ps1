[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory=$true)][string]$Image,
    [Parameter(Mandatory=$true)][string]$TokenSecretId,
    [Parameter(Mandatory=$true)][string]$OAuthClientIdSecret,
    [Parameter(Mandatory=$true)][string]$OAuthClientSecretSecret
)
$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$projectNumber = '524790767721'
$region = 'europe-west1'
$service = 'captain-publisher'
$runtime = "captain-publisher@$project.iam.gserviceaccount.com"
$invoker = "captain-publisher-invoker@$project.iam.gserviceaccount.com"
$origin = "https://$service-$projectNumber.$region.run.app"
$versionRoleId = 'captainXTokenVersionWriter'
$versionRole = "projects/$project/roles/$versionRoleId"
$versionPermissions = @(
    'secretmanager.versions.add',
    'secretmanager.versions.disable',
    'secretmanager.versions.get',
    'secretmanager.versions.list'
)

if ($Image -notmatch '^europe-west2-docker\.pkg\.dev/fpl-frosty-bot-v1/captain-images/publisher:[a-f0-9]{40}$') {
    throw 'A Captain-only, commit-tagged publisher image is required.'
}
foreach ($value in @($TokenSecretId, $OAuthClientIdSecret, $OAuthClientSecretSecret)) {
    if ($value -notmatch '^[A-Za-z0-9_-]{1,255}$') { throw 'Invalid Secret Manager resource ID.' }
}

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) { throw "Captain publisher deployment stopped: gcloud exit $LASTEXITCODE" }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Deploy disabled FPLBotTest-only Captain publisher')) { return }
$accounts = Cloud iam service-accounts list "--project=$project" '--format=value(email)'
if ($accounts -notcontains $invoker) {
    Cloud iam service-accounts create captain-publisher-invoker "--project=$project" '--display-name=Captain publisher task invoker'
}

$member = "serviceAccount:$runtime"
$roles = Cloud iam roles list "--project=$project" '--format=value(name)'
if ($roles -notcontains $versionRole) {
    Cloud iam roles create $versionRoleId "--project=$project" '--title=Captain X token version writer' "--permissions=$($versionPermissions -join ',')" '--stage=GA' '--quiet'
} else {
    $role = Cloud iam roles describe $versionRoleId "--project=$project" '--format=json' | ConvertFrom-Json
    if ((Compare-Object @($role.includedPermissions | Sort-Object) @($versionPermissions | Sort-Object))) {
        throw 'Existing Captain token-version role permissions differ; refusing to broaden it.'
    }
}
Cloud secrets add-iam-policy-binding $TokenSecretId "--project=$project" "--member=$member" '--role=roles/secretmanager.secretAccessor' '--quiet'
$tokenPolicy = Cloud secrets get-iam-policy $TokenSecretId "--project=$project" '--format=json' | ConvertFrom-Json
$legacyManager = @($tokenPolicy.bindings | Where-Object role -eq 'roles/secretmanager.secretVersionManager' | ForEach-Object members)
if ($legacyManager -contains $member) {
    Cloud secrets remove-iam-policy-binding $TokenSecretId "--project=$project" "--member=$member" '--role=roles/secretmanager.secretVersionManager' '--quiet'
}
Cloud secrets add-iam-policy-binding $TokenSecretId "--project=$project" "--member=$member" "--role=$versionRole" '--quiet'
foreach ($secret in @($OAuthClientIdSecret, $OAuthClientSecretSecret)) {
    Cloud secrets add-iam-policy-binding $secret "--project=$project" "--member=$member" '--role=roles/secretmanager.secretAccessor' '--quiet'
}

$settings = @(
    "GCP_PROJECT_ID=$project",
    "GCP_PROJECT_NUMBER=$projectNumber",
    'FIRESTORE_DATABASE_ID=captain-state',
    'X_OAUTH_FIRESTORE_DATABASE_ID=shared-x-oauth',
    "CAPTAIN_PUBLISHER_ORIGIN=$origin",
    "CAPTAIN_PUBLISHER_INVOKER_EMAIL=$invoker",
    "X_TOKEN_SECRET_ID=$TokenSecretId",
    'X_ENVIRONMENT=test',
    'X_EXPECTED_USER_ID=1732468005336907776',
    'X_POSTING_ENABLED=false'
) -join ','
$secrets = @(
    "X_OAUTH_CLIENT_ID=$OAuthClientIdSecret`:latest",
    "X_OAUTH_CLIENT_SECRET=$OAuthClientSecretSecret`:latest"
) -join ','
Cloud run deploy $service "--project=$project" "--region=$region" "--image=$Image" '--no-allow-unauthenticated' "--service-account=$runtime" '--min-instances=0' '--max-instances=2' '--cpu=1' '--memory=512Mi' '--concurrency=8' '--timeout=90' "--add-custom-audiences=$origin" "--set-env-vars=$settings" "--set-secrets=$secrets" '--quiet'
Cloud run services add-iam-policy-binding $service "--project=$project" "--region=$region" "--member=serviceAccount:$invoker" '--role=roles/run.invoker' '--quiet'

$policy = Cloud run services get-iam-policy $service "--project=$project" "--region=$region" '--format=json'
$invokers = @((ConvertFrom-Json ($policy -join "`n")).bindings | Where-Object role -eq 'roles/run.invoker' | ForEach-Object members)
if ($invokers.Count -ne 1 -or $invokers[0] -ne "serviceAccount:$invoker") {
    throw 'Publisher invocation policy is broader than the dedicated invoker.'
}
Write-Output 'Disabled Captain publisher deployed. No scheduler or task was armed.'
