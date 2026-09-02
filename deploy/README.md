# Google Cloud V1 foundation and private deployment

This guide reproduces the Google Cloud foundation and first private, posting-disabled Cloud Run
deployment for FPL Bot V1. It intentionally stops before Cloud Scheduler creation, mutable OAuth
token bootstrap, or enabling X posting. Run each command from an authenticated `gcloud` session
after separately confirming the target project and billing account. Never reuse the retired
project.

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
$StaticClientIdSecret = "x-oauth-client-id"
$StaticClientSecretSecret = "x-oauth-client-secret"
$Service = "fpl-bot"
$ExpectedXUserId = "<approved-test-account-numeric-id>"
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
| Private Cloud Run service | `fpl-bot` | `europe-west2` |

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

## Private posting-disabled Cloud Run deployment

Create separate regional secrets for the static OAuth client configuration. Add exactly one
version to each using an approved local secure-input process and `--data-file=-`; do not put either
value on a command line, in shell history, or in a tracked file. Ensure the secure-input process
writes the exact value without adding a newline. The mutable `$TokenSecret` remains at zero
versions until the separately reviewed user-token bootstrap.

```powershell
gcloud secrets create $StaticClientIdSecret --project=$ProjectId `
  --replication-policy=user-managed --locations=$Region
gcloud secrets create $StaticClientSecretSecret --project=$ProjectId `
  --replication-policy=user-managed --locations=$Region

# Supply each value through a hidden, non-logging stdin process:
gcloud secrets versions add $StaticClientIdSecret --project=$ProjectId --data-file=-
gcloud secrets versions add $StaticClientSecretSecret --project=$ProjectId --data-file=-

gcloud secrets add-iam-policy-binding $StaticClientIdSecret --project=$ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding $StaticClientSecretSecret --project=$ProjectId `
  --member="serviceAccount:$RuntimeEmail" --role="roles/secretmanager.secretAccessor"
```

Resolve the reviewed image to its full digest. Because the application validates its own future
task target and audience at startup, use a syntactically valid non-operational HTTPS sentinel only
for the initial private revision. Do not invoke any workflow route on that revision.

```powershell
$ImageDigest = "<$Region-docker.pkg.dev/$ProjectId/$Repository/fpl-bot@sha256:reviewed-digest>"
$SentinelOrigin = "https://not-configured.invalid"

gcloud run deploy $Service --project=$ProjectId --region=$Region --platform=managed `
  --image=$ImageDigest --service-account=$RuntimeEmail --no-allow-unauthenticated `
  --min-instances=0 --max-instances=2 --concurrency=2 --cpu=1 --memory=512Mi `
  --port=8080 --ingress=all `
  --set-env-vars="GCP_PROJECT_ID=$ProjectId,GCP_PROJECT_NUMBER=$ProjectNumber,FIRESTORE_DATABASE_ID=(default),CLOUD_TASKS_LOCATION_ID=$Region,CLOUD_TASKS_QUEUE_ID=$Queue,CLOUD_RUN_BASE_URL=$SentinelOrigin,CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL=$InvokerEmail,CLOUD_TASKS_OIDC_AUDIENCE=$SentinelOrigin,X_ENVIRONMENT=test,X_POSTING_ENABLED=false,X_EXPECTED_USER_ID=$ExpectedXUserId,X_TOKEN_SECRET_ID=$TokenSecret" `
  --set-secrets="X_OAUTH_CLIENT_ID=${StaticClientIdSecret}:1,X_OAUTH_CLIENT_SECRET=${StaticClientSecretSecret}:1"
```

Read the service URL returned by Cloud Run; do not derive or predict it. Replace both sentinels
with that exact service origin, wait for the corrected revision, and ensure only it receives
traffic.

```powershell
gcloud run services describe $Service --project=$ProjectId --region=$Region
$ServiceUrl = "<exact-service-url-returned-by-cloud-run>"

gcloud run services update $Service --project=$ProjectId --region=$Region `
  --update-env-vars="CLOUD_RUN_BASE_URL=$ServiceUrl,CLOUD_TASKS_OIDC_AUDIENCE=$ServiceUrl"

gcloud run services add-iam-policy-binding $Service --project=$ProjectId --region=$Region `
  --member="serviceAccount:$InvokerEmail" --role="roles/run.invoker"
```

Keep the service private: there must be no `allUsers` invoker binding. Test only a harmless
nonexistent path. The anonymous request must be rejected by IAM, while an authorized caller should
pass IAM and receive Flask's ordinary `404`. Never use a workflow route for deployment smoke
testing.

```powershell
$SmokeUrl = "$ServiceUrl/__smoke/not-found"
Invoke-WebRequest $SmokeUrl -SkipHttpErrorCheck  # expected 403

$IdentityToken = gcloud auth print-identity-token
Invoke-WebRequest $SmokeUrl -Headers @{Authorization="Bearer $IdentityToken"} `
  -SkipHttpErrorCheck  # expected 404 for an authorized operator
$IdentityToken = $null
```

The deployed service uses ADC through `$RuntimeEmail`, one CPU, `512Mi`, concurrency `2`, minimum
instances `0`, and maximum instances `2`. It has no VPC connector, Cloud NAT, static outbound IP,
or service-account key. Static secret environment references are pinned to explicit numeric
versions because Cloud Run resolves environment-variable secrets when an instance starts.

## Verification

Verify the relevant resource state before deployment and repeat these checks afterward:

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

After the private deployment, expected state is one empty Tasks queue, no token-authority document,
zero mutable token-secret versions, one enabled version on each static OAuth client secret, no
user-managed service-account keys, one scale-to-zero private Cloud Run service, and no Scheduler
job. `X_POSTING_ENABLED=false`; no OAuth refresh, X identity request, or X Post has occurred.

## One-time test-account token bootstrap

Keep `X_POSTING_ENABLED=false` throughout this operation. The approved source is the complete
`x_test_oauth_tokens.dpapi` handoff created by the local OAuth helper and stored outside the
repository. Never paste access or refresh tokens into a command, environment variable, tracked
file, log, or terminal output.

From a clean reviewed checkout, first confirm that the configured mutable secret has zero versions
and the Firestore authority document is absent. Then run the dedicated create-only utility with the
external encrypted file path:

```powershell
$TokenHandoff = "<absolute-path-outside-repository-to-x_test_oauth_tokens.dpapi>"

fpl-bot-x-bootstrap `
  --project-id=$ProjectId `
  --project-number=$ProjectNumber `
  --database-id="(default)" `
  --secret-id=$TokenSecret `
  --expected-user-id=$ExpectedXUserId `
  --token-file=$TokenHandoff
```

The utility decrypts the handoff only in the current Windows user's process, validates the exact
local schema, expected user ID, bearer type, timezone-aware UTC expiry, refresh token, and required
V1 scopes, then serializes through the production cloud-token-store contract. It creates one
explicit Secret Manager version before transactionally creating revision `1` of
`x_oauth_token_authority/x-user-{X_EXPECTED_USER_ID}`. Firestore receives only schema, revision,
explicit version name, UTC update time, and empty lease fields—never OAuth token values.

The command verifies the new state through `GoogleCloudXTokenStateStore.read()` and reports only
non-secret metadata. An expired access token is permitted when the refresh state is otherwise
valid; bootstrap deliberately performs no refresh and no X request. If authority already exists,
the command performs no mutation. If a candidate secret version exists without authority, it fails
for explicit reconciliation rather than uploading a duplicate. Rerunning bootstrap is not the
normal rotation mechanism; future runtime refresh uses the distributed lease/CAS flow.

Afterward, verify that the authority points to the exact numeric enabled version, its initial
revision is `1`, no lease is active, the queue remains empty, Scheduler remains absent, and Cloud
Run still has `X_POSTING_ENABLED=false`. Controlled refresh and identity verification are separate
later reviews.

## Teardown

For teardown, first disable any Scheduler job, then remove the Cloud Run service, queue, image
repository, token secret, Firestore database, service accounts, and finally billing/project only
after separately exporting any audit data that must be retained. Resolve every target by explicit
project, region, and resource name before deletion. Teardown commands are intentionally omitted to
prevent accidental execution against the wrong project.
