param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$region = 'europe-west2'
$zone = 'europe-west2-b'
$instance = 'captain-browser-london-windows-trial'
$database = 'captain-state'
$projectNumber = '524790767721'

function Cloud {
    & gcloud.cmd @args
    if ($LASTEXITCODE -ne 0) { throw "Captain provisioning stopped: gcloud exit $LASTEXITCODE" }
}
if (-not $Apply) {
    Write-Output 'PLAN ONLY: captain-state database; captain-orchestration queue; captain-images repository; captain-controller/tasks/worker/planner/build identities; narrowly scoped IAM; stopped VM worker identity. No Good Luck modifications.'
    exit 0
}
$state = Cloud @('compute','instances','describe',$instance,"--project=$project","--zone=$zone",'--format=value(status)')
if ($state -ne 'TERMINATED') { throw 'VM must be stopped before changing its workload identity' }
$databases = Cloud @('firestore','databases','list',"--project=$project",'--format=value(name)')
if ($databases -notcontains "projects/$project/databases/$database") {
    Cloud @('firestore','databases','create',"--project=$project","--database=$database","--location=$region",'--type=firestore-native','--delete-protection','--quiet')
}
$accounts = Cloud @('iam','service-accounts','list',"--project=$project",'--format=value(email)')
foreach ($name in @('captain-controller','captain-tasks','captain-worker','captain-planner','captain-build')) {
    if ($accounts -notcontains "$name@$project.iam.gserviceaccount.com") {
        Cloud @('iam','service-accounts','create',$name,"--project=$project","--display-name=$name",'--quiet')
    }
}
$controller = "captain-controller@$project.iam.gserviceaccount.com"
$tasks = "captain-tasks@$project.iam.gserviceaccount.com"
$worker = "captain-worker@$project.iam.gserviceaccount.com"
$build = "captain-build@$project.iam.gserviceaccount.com"
Cloud @('projects','add-iam-policy-binding',$project,"--member=serviceAccount:$controller",'--role=roles/datastore.user',"--condition=expression=resource.name=='projects/$project/databases/$database',title=captain_database_only",'--quiet')
$roles = Cloud @('iam','roles','list',"--project=$project",'--format=value(name)')
if ($roles -notcontains "projects/$project/roles/captainVmControl") {
    Cloud @('iam','roles','create','captainVmControl',"--project=$project",'--title=Captain fixed VM control','--permissions=compute.instances.get,compute.instances.start,compute.instances.stop','--stage=GA','--quiet')
}
if ($roles -notcontains "projects/$project/roles/captainVmOperationsRead") {
    Cloud @('iam','roles','create','captainVmOperationsRead',"--project=$project",'--title=Captain operation reconciliation','--permissions=compute.zoneOperations.get,compute.zoneOperations.list','--stage=GA','--quiet')
}
Cloud @('compute','instances','add-iam-policy-binding',$instance,"--project=$project","--zone=$zone","--member=serviceAccount:$controller","--role=projects/$project/roles/captainVmControl",'--quiet')
Cloud @('projects','add-iam-policy-binding',$project,"--member=serviceAccount:$controller","--role=projects/$project/roles/captainVmOperationsRead",'--condition=None','--quiet')
Cloud @('iam','service-accounts','add-iam-policy-binding',$tasks,"--project=$project","--member=serviceAccount:$controller",'--role=roles/iam.serviceAccountUser','--quiet')
$queues = Cloud @('tasks','queues','list',"--project=$project","--location=$region",'--format=value(name)')
$queuePath = "projects/$project/locations/$region/queues/captain-orchestration"
if ($queues -notcontains $queuePath) {
    Cloud @('tasks','queues','create','captain-orchestration',"--project=$project","--location=$region",'--max-dispatches-per-second=1','--max-concurrent-dispatches=2','--max-attempts=3','--min-backoff=10s','--max-backoff=60s','--quiet')
}
# Queue-level IAM avoids granting authority over the Good Luck queue.
foreach ($role in @('roles/cloudtasks.enqueuer','roles/cloudtasks.viewer')) {
    Cloud @('tasks','queues','add-iam-policy-binding','captain-orchestration',"--project=$project","--location=$region","--member=serviceAccount:$controller","--role=$role",'--quiet')
}
Cloud @('tasks','queues','pause','captain-orchestration',"--project=$project","--location=$region",'--quiet')
$repositories = Cloud @('artifacts','repositories','list',"--project=$project","--location=$region",'--format=value(name)')
if ($repositories -notcontains "projects/$project/locations/$region/repositories/captain-images") {
    Cloud @('artifacts','repositories','create','captain-images',"--project=$project","--location=$region",'--repository-format=docker','--quiet')
}
Cloud @('artifacts','repositories','add-iam-policy-binding','captain-images',"--project=$project","--location=$region","--member=serviceAccount:$build",'--role=roles/artifactregistry.writer','--quiet')
Cloud @('projects','add-iam-policy-binding',$project,"--member=serviceAccount:$build",'--role=roles/logging.logWriter','--condition=None','--quiet')
$bucket = "gs://captain-build-source-$projectNumber"
$buckets = Cloud @('storage','buckets','list',"--project=$project",'--format=value(name)')
if (($buckets -notcontains "captain-build-source-$projectNumber") -and ($buckets -notcontains $bucket)) {
    Cloud @('storage','buckets','create',$bucket,"--project=$project","--location=$region",'--uniform-bucket-level-access','--public-access-prevention','--quiet')
}
Cloud @('storage','buckets','add-iam-policy-binding',$bucket,"--member=serviceAccount:$build",'--role=roles/storage.objectViewer','--quiet')
Cloud @('storage','buckets','update',$bucket,'--lifecycle-file=deploy/captain-build-retention.json','--quiet')
Cloud @('compute','instances','set-service-account',$instance,"--project=$project","--zone=$zone","--service-account=$worker",'--scopes=https://www.googleapis.com/auth/cloud-platform','--quiet')
Write-Output 'Captain baseline provisioned. VM remains stopped. Cloud Run and Scheduler are not yet deployed/armed.'
