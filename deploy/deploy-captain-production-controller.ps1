[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory=$true)][string]$Image)

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$projectNumber = '524790767721'
$region = 'europe-west1'
$controlRegion = 'europe-west2'
$service = 'captain-controller'
$origin = "https://$service-$projectNumber.$region.run.app"
$publisherOrigin = "https://captain-production-publisher-$projectNumber.$region.run.app"
$productionUserId = '1249335464571650048'

if ($Image -notmatch '^europe-west2-docker\.pkg\.dev/fpl-frosty-bot-v1/captain-images/production-controller:[a-f0-9]{40}$') {
    throw 'A commit-tagged production Captain controller image is required.'
}
function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Production Captain controller deployment stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $PSCmdlet.ShouldProcess($service, 'Deploy production routing while controls remain paused')) {
    return
}
$queueState = Cloud tasks queues describe captain-orchestration "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$plannerState = Cloud scheduler jobs describe captain-planner "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$vmState = Cloud compute instances describe captain-browser-london-windows-trial `
    "--project=$project" '--zone=europe-west2-b' '--format=value(status)'
if ($queueState -ne 'PAUSED' -or $plannerState -ne 'PAUSED' -or $vmState -ne 'TERMINATED') {
    throw 'Production Captain controller deployment requires paused controls and a stopped VM.'
}
$publisher = Cloud run services describe captain-production-publisher "--project=$project" `
    "--region=$region" '--format=json' | ConvertFrom-Json
$publisherPosting = $publisher.spec.template.spec.containers[0].env |
    Where-Object name -eq 'X_POSTING_ENABLED'
if ($publisherPosting.value -ne 'false') {
    throw 'Production publisher must remain disabled while routing is deployed.'
}

$settings = @(
    "CAPTAIN_PROJECT=$project",
    'CAPTAIN_DATABASE=captain-state',
    "CAPTAIN_ORIGIN=$origin",
    "CAPTAIN_WORKER_EMAIL=captain-worker@$project.iam.gserviceaccount.com",
    "CAPTAIN_TASKS_EMAIL=captain-tasks@$project.iam.gserviceaccount.com",
    "CAPTAIN_PLANNER_EMAIL=captain-planner@$project.iam.gserviceaccount.com",
    "CAPTAIN_DESTINATION_USER_ID=$productionUserId",
    "CAPTAIN_PUBLISHER_ORIGIN=$publisherOrigin",
    "CAPTAIN_PUBLISHER_INVOKER_EMAIL=captain-prod-pub-invoker@$project.iam.gserviceaccount.com"
) -join ','
Cloud run deploy $service "--project=$project" "--region=$region" "--image=$Image" `
    '--no-allow-unauthenticated' `
    "--service-account=captain-controller@$project.iam.gserviceaccount.com" `
    '--min-instances=0' '--max-instances=2' '--cpu=1' '--memory=512Mi' `
    '--concurrency=8' '--timeout=90' "--add-custom-audiences=$origin" `
    "--set-env-vars=$settings" '--quiet'
foreach ($identity in @('captain-worker','captain-tasks','captain-planner')) {
    Cloud run services add-iam-policy-binding $service "--project=$project" `
        "--region=$region" `
        "--member=serviceAccount:$identity@$project.iam.gserviceaccount.com" `
        '--role=roles/run.invoker' '--quiet'
}
$policy = Cloud run services get-iam-policy $service "--project=$project" `
    "--region=$region" '--format=json' | ConvertFrom-Json
$actual = @(
    $policy.bindings | Where-Object role -eq 'roles/run.invoker' | ForEach-Object members | Sort-Object
)
$expected = @('captain-planner','captain-tasks','captain-worker') |
    ForEach-Object { "serviceAccount:$_@$project.iam.gserviceaccount.com" } |
    Sort-Object
if (Compare-Object $actual $expected) {
    throw 'Captain controller invocation policy is broader than the three fixed identities.'
}
if (
    (Cloud tasks queues describe captain-orchestration "--project=$project" `
        "--location=$controlRegion" '--format=value(state)') -ne 'PAUSED' -or
    (Cloud scheduler jobs describe captain-planner "--project=$project" `
        "--location=$controlRegion" '--format=value(state)') -ne 'PAUSED'
) {
    throw 'Production Captain controller deployment changed paused control state.'
}
Write-Output 'Production Captain routing deployed. Queue/planner remain PAUSED; publisher remains disabled.'
