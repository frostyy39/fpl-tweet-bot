# Google Cloud V1 foundation

This guide reproduces the non-posting Google Cloud foundation for FPL Bot V1. It intentionally
stops before Cloud Run deployment, Cloud Scheduler creation, OAuth token bootstrap, or enabling X
posting. Run each command from an authenticated `gcloud` session after separately confirming the
target project and billing account. Never reuse the retired project.

## Parameters and stable resource names

The billing account and operator identity are deliberately not stored in Git.

```powershell
$ProjectId = "<globally-unique-project-id>"
$BillingAccountId = "<approved-billing-account-id>"
$Region = "europe-west2"
$RuntimeServiceAccount = "fpl-bot-runtime"
$InvokerServiceAccount = "fpl-bot-invoker"
$Repository = "fpl-bot"
$Queue = "fpl-deadline"
$TokenSecret = "x-oauth-token-state"
$SourceCommit = "<reviewed-git-commit>"
```

Stable V1 resource names are:

| Resource | Name | Location |
| --- | --- | --- |
| Artifact Registry Docker repository | `fpl-bot` | `europe-west2` |
| Firestore database | `(default)` | `europe-west2` |
| Secret Manager OAuth token-state secret | `x-oauth-token-state` | user-managed `europe-west2` replica |
| Cloud Tasks queue | `fpl-deadline` | `europe-west2` |
| Runtime service account | `fpl-bot-runtime` | global IAM resource |
| OIDC caller service account | `fpl-bot-invoker` | global IAM resource |
| Future private Cloud Run service | `fpl-bot` | `europe-west2` |

## Project and APIs

Create the project only after confirming that `$ProjectId` is the approved unused identifier and
that `$BillingAccountId` is the approved open billing account.

```powershell
gcloud projects create $ProjectId --name="FPL Frosty Bot"
gcloud billing projects link $ProjectId --billing-account=$BillingAccountId
gcloud config set project $ProjectId
gcloud billing projects describe $ProjectId
```

Enable only the APIs directly used by this architecture. Google Cloud can also enable platform
dependencies automatically; do not force-disable a service when `serviceusage` reports that a core
platform service depends on it.

```powershell
$Services = @(
  "run.googleapis.com",
  "artifactregistry.googleapis.com",
  "cloudbuild.googleapis.com",
  "firestore.googleapis.com",
  "secretmanager.googleapis.com",
  "cloudtasks.googleapis.com",
  "cloudscheduler.googleapis.com",
  "iam.googleapis.com"
)
gcloud services enable $Services --project=$ProjectId
```

## Foundation resources

```powershell
gcloud iam service-accounts create $RuntimeServiceAccount `
  --project=$ProjectId --display-name="FPL Bot runtime"
gcloud iam service-accounts create $InvokerServiceAccount `
  --project=$ProjectId --display-name="FPL Bot authenticated invoker"

gcloud artifacts repositories create $Repository --project=$ProjectId `
  --location=$Region --repository-format=docker --description="FPL Bot V1 container images"

gcloud firestore databases create --project=$ProjectId `
  --database="(default)" --location=$Region --type=firestore-native --edition=standard

gcloud secrets create $TokenSecret --project=$ProjectId `
  --replication-policy=user-managed --locations=$Region

gcloud tasks queues create $Queue --project=$ProjectId --location=$Region `
  --max-attempts=10 --min-backoff=30s --max-backoff=300s --max-doublings=3 `
  --max-dispatches-per-second=1 --max-concurrent-dispatches=1
```

The queue deliberately has bounded attempts, conservative backoff, and serial dispatch. The
application acknowledges terminal stale, duplicate, failed, uncertain, and invalid-payload results
with `2xx`; only pre-write failures that may make progress return a retryable response. Queue
redelivery therefore cannot become an application-level X Post retry. No task is created during
foundation provisioning.

## Least-privilege IAM

The application uses Application Default Credentials from the future Cloud Run runtime identity.
No service-account key is created or downloaded.

```powershell
$RuntimeEmail = "$RuntimeServiceAccount@$ProjectId.iam.gserviceaccount.com"
$InvokerEmail = "$InvokerServiceAccount@$ProjectId.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/datastore.user" `
  --condition="expression=resource.name=='projects/$ProjectId/databases/(default)',title=fpl_default_database_only"

gcloud tasks queues add-iam-policy-binding $Queue --project=$ProjectId --location=$Region `
  --member="serviceAccount:$RuntimeEmail" --role="roles/cloudtasks.enqueuer"
gcloud tasks queues add-iam-policy-binding $Queue --project=$ProjectId --location=$Region `
  --member="serviceAccount:$RuntimeEmail" --role="roles/cloudtasks.viewer"

gcloud secrets add-iam-policy-binding $TokenSecret --project=$ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding $TokenSecret --project=$ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/secretmanager.secretVersionManager"

gcloud iam service-accounts add-iam-policy-binding $InvokerEmail --project=$ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/iam.serviceAccountUser"
```

`roles/cloudtasks.enqueuer` supplies task creation and FULL response view; queue-scoped
`roles/cloudtasks.viewer` supplies `tasks.get` for deterministic-name reconciliation. Secret roles
are bound on the one token secret, not the whole project. `iam.serviceAccounts.actAs` is bound on
the invoker identity itself. Grant `roles/run.invoker` to `$InvokerEmail` on only the future
`fpl-bot` Cloud Run service after that service exists.

Do not grant Owner, Editor, project-wide Secret Manager Admin, Cloud Tasks Admin, or project-wide
Service Account User/Token Creator to either application identity.

## Immutable container build

Build only a reviewed clean commit. Confirm the upload list contains no environment file, key,
certificate, OAuth handoff, token, credential JSON, or Git metadata before submitting.

```powershell
git status --short
git rev-parse HEAD
gcloud meta list-files-for-upload

$ProjectNumber = gcloud projects describe $ProjectId --format="value(projectNumber)"
$BuildServiceAccount = "$ProjectNumber-compute@developer.gserviceaccount.com"
$Image = "$Region-docker.pkg.dev/$ProjectId/$Repository/fpl-bot:$($SourceCommit.Substring(0, 8))"

gcloud artifacts repositories add-iam-policy-binding $Repository `
  --project=$ProjectId --location=$Region `
  --member="serviceAccount:$BuildServiceAccount" --role="roles/artifactregistry.writer"

gcloud builds submit . --project=$ProjectId --region=$Region --tag=$Image --timeout=1200s
```

New projects use the Compute Engine default service account for Cloud Build. It must not retain the
project Editor role. Its Artifact Registry Writer binding is scoped to this repository. Cloud
Build's generated source bucket also needs a bucket-scoped `roles/storage.objectViewer` binding for
that identity; if the first source submission creates the bucket before rejecting source access,
apply the binding to `gs://${ProjectId}_cloudbuild` and resubmit the identical deterministic tag.
Do not replace these narrow grants with Editor.

Capture and deploy by the immutable digest, not just the mutable tag:

```powershell
gcloud artifacts docker images describe $Image --project=$ProjectId `
  --format="value(image_summary.fully_qualified_digest)"
```

## Verification stop point

Before deployment, verify:

```powershell
gcloud config get-value project
gcloud billing projects describe $ProjectId
gcloud services list --enabled --project=$ProjectId
gcloud artifacts repositories describe $Repository --project=$ProjectId --location=$Region
gcloud firestore databases describe --project=$ProjectId --database="(default)"
gcloud secrets versions list $TokenSecret --project=$ProjectId
gcloud tasks queues describe $Queue --project=$ProjectId --location=$Region
gcloud tasks list --queue=$Queue --project=$ProjectId --location=$Region
gcloud iam service-accounts keys list --iam-account=$RuntimeEmail --managed-by=user
gcloud iam service-accounts keys list --iam-account=$InvokerEmail --managed-by=user
gcloud run services list --project=$ProjectId --region=$Region
gcloud scheduler jobs list --project=$ProjectId --location=$Region
```

Expected foundation state is one empty Tasks queue, an empty Firestore database, zero token-secret
versions, no user-managed service-account keys, no Cloud Run service, and no Scheduler job. X
posting remains disabled and no OAuth credential is loaded.

## Later deployment and teardown

The first deployment must use the captured image digest, the existing `create_production_app()`
container, minimum instances `0`, the runtime service account, private ingress/authentication, and
the environment-variable contract in the root README. Bootstrap the approved test-account OAuth
state only in a separately reviewed milestone; never place token state or OAuth client secrets in
tracked files or the image.

For teardown, first disable any Scheduler job, then remove the Cloud Run service, queue, image
repository, token secret, Firestore database, service accounts, and finally billing/project only
after separately exporting any audit data that must be retained. Resolve every target by explicit
project, region, and resource name before deletion. Teardown commands are intentionally omitted to
prevent accidental execution against the wrong project.
