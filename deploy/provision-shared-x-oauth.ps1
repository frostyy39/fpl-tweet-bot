# Additive, explicit database isolation. Never grants project-wide datastore access.
[CmdletBinding(SupportsShouldProcess)]
param()
$ErrorActionPreference = 'Stop'
$project = 'fpl-frosty-bot-v1'
$database = 'shared-x-oauth'
$location = 'europe-west2'

function Invoke-GcloudChecked {
    param([string[]]$Arguments)
    & gcloud.cmd @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Gcloud operation failed; inspect before proceeding.' }
}

if (-not $PSCmdlet.ShouldProcess($project, 'Provision shared OAuth database and scoped IAM')) { return }
$databases = Invoke-GcloudChecked @('firestore','databases','list',"--project=$project",'--format=json') | ConvertFrom-Json
$existing = $databases | Where-Object { $_.name -eq "projects/$project/databases/$database" }
if ($existing) {
    if ($existing.locationId -ne $location -or $existing.type -ne 'FIRESTORE_NATIVE') {
        throw 'Existing database location/mode differs; do not replace it.'
    }
} else {
    Invoke-GcloudChecked @('firestore','databases','create',"--project=$project", "--database=$database", "--location=$location", '--type=firestore-native', '--delete-protection')
}
$publisher = "captain-publisher@$project.iam.gserviceaccount.com"
$accounts = Invoke-GcloudChecked @('iam','service-accounts','list',"--project=$project",'--format=json') | ConvertFrom-Json
if (-not ($accounts | Where-Object { $_.email -eq $publisher })) {
    Invoke-GcloudChecked @('iam','service-accounts','create','captain-publisher',"--project=$project",'--display-name=Captain publisher (not deployed or armed)')
}
foreach ($member in @("serviceAccount:fpl-bot-runtime@$project.iam.gserviceaccount.com", "serviceAccount:$publisher")) {
    Invoke-GcloudChecked @('projects','add-iam-policy-binding',$project,"--member=$member",'--role=roles/datastore.user',"--condition=expression=resource.name=='projects/$project/databases/$database',title=shared_oauth_database_only")
}
Invoke-GcloudChecked @('projects','add-iam-policy-binding',$project,"--member=serviceAccount:$publisher",'--role=roles/datastore.user',"--condition=expression=resource.name=='projects/$project/databases/captain-state',title=captain_publisher_state_only")
# No publisher Secret Manager, Compute, Run invocation or posting permission here.
# Effective project/inherited IAM and real denied probes are mandatory separately.
