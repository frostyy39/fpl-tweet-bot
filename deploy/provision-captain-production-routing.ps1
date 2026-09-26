[CmdletBinding(SupportsShouldProcess)]
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$region = 'europe-west1'
$controlRegion = 'europe-west2'
$controller = "captain-controller@$project.iam.gserviceaccount.com"
$publisherInvoker = "captain-prod-pub-invoker@$project.iam.gserviceaccount.com"

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Captain production routing provisioning stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $Apply) {
    Write-Output 'PLAN ONLY: grant only captain-controller actAs on captain-prod-pub-invoker; verify all production controls remain paused/disabled.'
    return
}
if (-not $PSCmdlet.ShouldProcess($publisherInvoker, 'Grant fixed controller OIDC task actAs')) {
    return
}

$queueState = Cloud tasks queues describe captain-orchestration "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$plannerState = Cloud scheduler jobs describe captain-planner "--project=$project" `
    "--location=$controlRegion" '--format=value(state)'
$vmState = Cloud compute instances describe captain-browser-london-windows-trial `
    "--project=$project" '--zone=europe-west2-b' '--format=value(status)'
if ($queueState -ne 'PAUSED' -or $plannerState -ne 'PAUSED' -or $vmState -ne 'TERMINATED') {
    throw 'Captain controls must be paused and the VM terminated before routing IAM changes.'
}

Cloud iam service-accounts add-iam-policy-binding $publisherInvoker "--project=$project" `
    "--member=serviceAccount:$controller" '--role=roles/iam.serviceAccountUser' '--quiet'

$invokerPolicy = Cloud iam service-accounts get-iam-policy $publisherInvoker `
    "--project=$project" '--format=json' | ConvertFrom-Json
$users = @(
    $invokerPolicy.bindings |
        Where-Object role -eq 'roles/iam.serviceAccountUser' |
        ForEach-Object members
)
if ($users -notcontains "serviceAccount:$controller") {
    throw 'Controller did not receive the exact publisher-invoker actAs grant.'
}
$publisherPolicy = Cloud run services get-iam-policy captain-production-publisher `
    "--project=$project" "--region=$region" '--format=json' | ConvertFrom-Json
$invokers = @(
    $publisherPolicy.bindings |
        Where-Object role -eq 'roles/run.invoker' |
        ForEach-Object members
)
if (
    $invokers.Count -ne 1 -or
    $invokers[0] -ne "serviceAccount:$publisherInvoker"
) {
    throw 'Production publisher invocation boundary is not exclusive.'
}
Write-Output 'Production Captain routing IAM prepared. No service, queue, Scheduler or VM was activated.'
