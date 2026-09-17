param([Parameter(Mandatory=$true)][string]$Image)
$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$region = 'europe-west1'
$scheduleRegion = 'europe-west2'
$origin = 'https://captain-controller-524790767721.europe-west1.run.app'
if ($Image -notmatch '^europe-west2-docker\.pkg\.dev/fpl-frosty-bot-v1/captain-images/controller:[a-f0-9]{40}$') {
    throw 'A Captain-only, commit-tagged image is required'
}
function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) { throw "Captain deployment stopped: gcloud exit $LASTEXITCODE" }
}
$settings = @(
    "CAPTAIN_PROJECT=$project", 'CAPTAIN_DATABASE=captain-state', "CAPTAIN_ORIGIN=$origin",
    "CAPTAIN_WORKER_EMAIL=captain-worker@$project.iam.gserviceaccount.com",
    "CAPTAIN_TASKS_EMAIL=captain-tasks@$project.iam.gserviceaccount.com",
    "CAPTAIN_PLANNER_EMAIL=captain-planner@$project.iam.gserviceaccount.com",
    'CAPTAIN_DESTINATION_USER_ID=1'
) -join ','
Cloud @('run','deploy','captain-controller',"--project=$project","--region=$region","--image=$Image",'--no-allow-unauthenticated',"--service-account=captain-controller@$project.iam.gserviceaccount.com",'--min-instances=0','--max-instances=2','--cpu=1','--memory=512Mi','--concurrency=8','--timeout=90',"--add-custom-audiences=$origin","--set-env-vars=$settings",'--quiet')
foreach ($identity in @('captain-worker','captain-tasks','captain-planner')) {
    Cloud @('run','services','add-iam-policy-binding','captain-controller',"--project=$project","--region=$region","--member=serviceAccount:$identity@$project.iam.gserviceaccount.com",'--role=roles/run.invoker','--quiet')
}
$jobs = Cloud @('scheduler','jobs','list',"--project=$project","--location=$scheduleRegion",'--format=value(name)')
$name = "projects/$project/locations/$scheduleRegion/jobs/captain-planner"
if ($jobs -notcontains $name) {
    # Create with a remote future schedule, pause, then install the real cadence.
    # This avoids an invocation race before the Windows private registration step.
    Cloud @('scheduler','jobs','create','http','captain-planner',"--project=$project","--location=$scheduleRegion",'--schedule=0 0 1 1 *','--time-zone=Europe/London',"--uri=$origin/captain/control/tick",'--http-method=POST',"--oidc-service-account-email=captain-planner@$project.iam.gserviceaccount.com","--oidc-token-audience=$origin",'--attempt-deadline=90s','--max-retry-attempts=0','--quiet')
}
Cloud @('scheduler','jobs','pause','captain-planner',"--project=$project","--location=$scheduleRegion",'--quiet')
Cloud @('scheduler','jobs','update','http','captain-planner',"--project=$project","--location=$scheduleRegion",'--schedule=*/1 * * * *',"--uri=$origin/captain/control/tick","--oidc-token-audience=$origin",'--quiet')
Write-Output 'Captain controller deployed. Queue and planner remain PAUSED. No X publisher exists.'
