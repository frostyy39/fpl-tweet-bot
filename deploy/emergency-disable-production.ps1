[CmdletBinding(SupportsShouldProcess)]
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$serviceRegion = 'europe-west1'
$controlRegion = 'europe-west2'

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) {
        throw "Emergency disable stopped: gcloud exit $LASTEXITCODE"
    }
}

if (-not $Apply) {
    Write-Output 'PLAN ONLY: disable both production posting gates; pause both planners; pause Good Luck queue; pause Captain queue only after VM TERMINATED. No state or OAuth reset.'
    return
}
if (-not $PSCmdlet.ShouldProcess($project, 'Fail closed both production posting paths')) {
    return
}

# Disable writes before changing delivery controls. These updates retain images,
# destinations, databases and OAuth configuration.
foreach ($service in @('captain-production-publisher','good-luck-production')) {
    Cloud run services update $service "--project=$project" "--region=$serviceRegion" `
        '--update-env-vars=X_POSTING_ENABLED=false' '--quiet'
}
Cloud scheduler jobs pause captain-planner "--project=$project" `
    "--location=$controlRegion" '--quiet'
Cloud scheduler jobs pause good-luck-production-checker "--project=$project" `
    "--location=$controlRegion" '--quiet'
Cloud tasks queues pause production-good-luck-deadline "--project=$project" `
    "--location=$controlRegion" '--quiet'

$vm = Cloud compute instances describe captain-browser-london-windows-trial `
    "--project=$project" '--zone=europe-west2-b' '--format=value(status)'
if ($vm -ne 'TERMINATED') {
    throw 'Production writes/planners are disabled. Leave the Captain queue available only for its durable fenced CLEANUP delivery; reconcile STOP to confirmed TERMINATED, then rerun this command. Never reset state or issue an unfenced raw stop.'
}
Cloud tasks queues pause captain-orchestration "--project=$project" `
    "--location=$controlRegion" '--quiet'
Write-Output 'Production is fail-closed. Both posting gates and recurring controls are disabled; durable state and OAuth authority were preserved.'
