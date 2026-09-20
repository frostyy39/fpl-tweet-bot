# Additive production-account OAuth boundary. Creates no token version and performs no X request.
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$database = 'production-shared-x-oauth'
$location = 'europe-west2'
$tokenSecret = 'production-x-oauth-token-state'
$runtime = "captain-production-publisher@$project.iam.gserviceaccount.com"
$invoker = "captain-prod-pub-invoker@$project.iam.gserviceaccount.com"
$versionRoleId = 'captainXTokenVersionWriter'
$versionRole = "projects/$project/roles/$versionRoleId"
$versionPermissions = @(
    'secretmanager.versions.add',
    'secretmanager.versions.disable',
    'secretmanager.versions.get',
    'secretmanager.versions.list'
)
$staticSecrets = @('x-oauth-client-id', 'x-oauth-client-secret')

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Production OAuth provisioning stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Provision isolated production X OAuth resources')) {
    return
}

$databases = Cloud firestore databases list "--project=$project" '--format=json' | ConvertFrom-Json
$existingDatabase = $databases | Where-Object name -eq "projects/$project/databases/$database"
if ($existingDatabase) {
    if ($existingDatabase.locationId -ne $location -or $existingDatabase.type -ne 'FIRESTORE_NATIVE') {
        throw 'Existing production OAuth database has the wrong location or mode.'
    }
}
else {
    Cloud firestore databases create "--project=$project" "--database=$database" `
        "--location=$location" '--type=firestore-native' '--delete-protection'
}

$accounts = Cloud iam service-accounts list "--project=$project" '--format=value(email)'
if ($accounts -notcontains $runtime) {
    Cloud iam service-accounts create captain-production-publisher "--project=$project" `
        '--display-name=Captain production publisher runtime'
}
if ($accounts -notcontains $invoker) {
    Cloud iam service-accounts create captain-prod-pub-invoker "--project=$project" `
        '--display-name=Captain production publisher invoker'
}

$secrets = Cloud secrets list "--project=$project" '--format=value(name)'
if ($secrets -notcontains $tokenSecret) {
    Cloud secrets create $tokenSecret "--project=$project" '--replication-policy=automatic'
}

$member = "serviceAccount:$runtime"
$role = Cloud iam roles describe $versionRoleId "--project=$project" '--format=json' |
    ConvertFrom-Json
if (Compare-Object @($role.includedPermissions | Sort-Object) @($versionPermissions | Sort-Object)) {
    throw 'Existing token-version role differs from the reviewed least-privilege contract.'
}
Cloud projects add-iam-policy-binding $project "--member=$member" '--role=roles/datastore.user' `
    "--condition=expression=resource.name=='projects/$project/databases/captain-state',title=captain_production_state_only"
Cloud projects add-iam-policy-binding $project "--member=$member" '--role=roles/datastore.user' `
    "--condition=expression=resource.name=='projects/$project/databases/$database',title=production_oauth_database_only"

Cloud secrets add-iam-policy-binding $tokenSecret "--project=$project" "--member=$member" `
    '--role=roles/secretmanager.secretAccessor'
Cloud secrets add-iam-policy-binding $tokenSecret "--project=$project" "--member=$member" `
    "--role=$versionRole"
foreach ($secret in $staticSecrets) {
    Cloud secrets add-iam-policy-binding $secret "--project=$project" "--member=$member" `
        '--role=roles/secretmanager.secretAccessor'
}

$projectPolicy = Cloud projects get-iam-policy $project '--format=json' | ConvertFrom-Json
$unconditionalDatastore = @(
    $projectPolicy.bindings |
        Where-Object { $_.role -eq 'roles/datastore.user' -and -not $_.condition } |
        ForEach-Object members
)
if ($unconditionalDatastore -contains $member) {
    throw 'Production publisher has unconditional project-wide Datastore access.'
}
$computePermissions = @(
    $projectPolicy.bindings |
        Where-Object { $_.role -match 'compute' } |
        ForEach-Object members
)
if ($computePermissions -contains $member) {
    throw 'Production publisher has a Compute role; refusing unsafe provisioning.'
}

Write-Output 'Production OAuth boundary provisioned with zero OAuth token material.'
