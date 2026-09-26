# Additive production Good Luck isolation. Creates no task and performs no X request.
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$location = 'europe-west2'
$database = 'production-good-luck-state'
$oauthDatabase = 'production-shared-x-oauth'
$tokenSecret = 'production-x-oauth-token-state'
$runtime = "good-luck-production-runtime@$project.iam.gserviceaccount.com"
$invoker = "good-luck-production-invoker@$project.iam.gserviceaccount.com"
$build = "good-luck-production-build@$project.iam.gserviceaccount.com"
$repository = 'good-luck-images'
$sourceBucket = "gs://$project`_cloudbuild"
$versionRoleId = 'captainXTokenVersionWriter'
$versionRole = "projects/$project/roles/$versionRoleId"
$staticSecrets = @('x-oauth-client-id', 'x-oauth-client-secret')

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Production Good Luck provisioning stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Provision isolated disabled production Good Luck')) {
    return
}

$databases = Cloud firestore databases list "--project=$project" '--format=json' | ConvertFrom-Json
$existing = $databases | Where-Object name -eq "projects/$project/databases/$database"
if ($existing) {
    if ($existing.locationId -ne $location -or $existing.type -ne 'FIRESTORE_NATIVE') {
        throw 'Existing production Good Luck database has the wrong location or mode.'
    }
}
else {
    Cloud firestore databases create "--project=$project" "--database=$database" `
        "--location=$location" '--type=firestore-native' '--delete-protection'
}
$oauth = $databases | Where-Object name -eq "projects/$project/databases/$oauthDatabase"
if (-not $oauth -or $oauth.locationId -ne $location -or $oauth.type -ne 'FIRESTORE_NATIVE') {
    throw 'Reviewed production OAuth database is unavailable.'
}

$accounts = @(Cloud iam service-accounts list "--project=$project" '--format=value(email)')
if ($accounts -notcontains $runtime) {
    Cloud iam service-accounts create good-luck-production-runtime "--project=$project" `
        '--display-name=Production Good Luck runtime'
}
if ($accounts -notcontains $invoker) {
    Cloud iam service-accounts create good-luck-production-invoker "--project=$project" `
        '--display-name=Production Good Luck task invoker'
}
if ($accounts -notcontains $build) {
    Cloud iam service-accounts create good-luck-production-build "--project=$project" `
        '--display-name=Production Good Luck build identity'
}

$repositories = @(Cloud artifacts repositories list "--project=$project" "--location=$location" `
    '--format=value(name.basename())')
if ($repositories -notcontains $repository) {
    Cloud artifacts repositories create $repository "--project=$project" "--location=$location" `
        '--repository-format=docker' '--description=Production Good Luck images'
}

$runtimeMember = "serviceAccount:$runtime"
$buildMember = "serviceAccount:$build"
$versionRoleMetadata = Cloud iam roles describe $versionRoleId "--project=$project" `
    '--format=json' | ConvertFrom-Json
$requiredVersionPermissions = @(
    'secretmanager.versions.add',
    'secretmanager.versions.disable',
    'secretmanager.versions.get',
    'secretmanager.versions.list'
)
if (Compare-Object @($versionRoleMetadata.includedPermissions | Sort-Object) `
        @($requiredVersionPermissions | Sort-Object)) {
    throw 'Production token-version role differs from the reviewed schema-2 contract.'
}
Cloud projects add-iam-policy-binding $project "--member=$runtimeMember" '--role=roles/datastore.user' `
    "--condition=expression=resource.name=='projects/$project/databases/$database',title=production_good_luck_state_only"
Cloud projects add-iam-policy-binding $project "--member=$runtimeMember" '--role=roles/datastore.user' `
    "--condition=expression=resource.name=='projects/$project/databases/$oauthDatabase',title=production_oauth_database_only"

Cloud secrets add-iam-policy-binding $tokenSecret "--project=$project" "--member=$runtimeMember" `
    '--role=roles/secretmanager.secretAccessor'
Cloud secrets add-iam-policy-binding $tokenSecret "--project=$project" "--member=$runtimeMember" `
    "--role=$versionRole"
foreach ($secret in $staticSecrets) {
    Cloud secrets add-iam-policy-binding $secret "--project=$project" "--member=$runtimeMember" `
        '--role=roles/secretmanager.secretAccessor'
}
Cloud iam service-accounts add-iam-policy-binding $invoker "--project=$project" `
    "--member=$runtimeMember" '--role=roles/iam.serviceAccountUser'

Cloud artifacts repositories add-iam-policy-binding $repository "--project=$project" `
    "--location=$location" "--member=$buildMember" '--role=roles/artifactregistry.writer'
Cloud storage buckets add-iam-policy-binding $sourceBucket "--member=$buildMember" `
    '--role=roles/storage.objectViewer'
Cloud projects add-iam-policy-binding $project "--member=$buildMember" `
    '--role=roles/logging.logWriter' '--condition=None'

$policy = Cloud projects get-iam-policy $project '--format=json' | ConvertFrom-Json
$unconditionalDatastore = @(
    $policy.bindings |
        Where-Object { $_.role -eq 'roles/datastore.user' -and -not $_.condition } |
        ForEach-Object members
)
if ($unconditionalDatastore -contains $runtimeMember) {
    throw 'Production Good Luck runtime has unconditional Datastore access.'
}
$computeMembers = @(
    $policy.bindings | Where-Object { $_.role -match 'compute' } | ForEach-Object members
)
if ($computeMembers -contains $runtimeMember) {
    throw 'Production Good Luck runtime has a Compute role.'
}
$projectSecretMembers = @(
    $policy.bindings |
        Where-Object { $_.role -match 'secretmanager' } |
        ForEach-Object members
)
if ($projectSecretMembers -contains $runtimeMember) {
    throw 'Production Good Luck runtime has project-wide Secret Manager access.'
}

Write-Output 'Production Good Luck state, identities and build boundary provisioned; no task or X request created.'
