# FPL Tweet Bot

A deterministic, low-cost automation project for the official Fantasy Premier League (FPL)
deadline tweet:

```text
Good luck everyone 🔒🥳

#FPL #FPLCommunity #{EVENT_CODE}
```

`EVENT_CODE` is derived from live FPL event and fixture data, producing `GWx`, `BGWx`,
`DGWx`, or `BDGWx`. The Gameweek number is never hardcoded.

## Milestone 1

Milestone 1 is a read-only Python core and local dry run. It:

- retrieves events, official `deadline_time` values, and current teams from FPL's
  `bootstrap-static/` endpoint;
- retrieves fixtures assigned to the selected event from `fixtures/?event={id}`;
- selects the next relevant event, accepting a unique future `is_next` only when it does not skip
  an earlier unpassed official deadline, then using a unique future `is_current` or the earliest
  future deadline;
- parses timezone-aware UTC deadlines and converts them to `Europe/London` with `zoneinfo`;
- requires exactly 20 unique current teams and at least one fixture assigned to the event;
- counts every team's fixtures and classifies the event as GW, BGW, DGW, or BDGW;
- renders the exact V1 tweet and a human-readable diagnostic report.

If FPL exposes no current or future deadline, contradicts its `is_next` chronology, returns a team
count other than 20, or returns zero event fixtures, the command exits cleanly with an explanatory
error instead of guessing. Fixture kickoff times are never used as deadline substitutes.

The Milestone 1 command cannot post to X. It includes no scheduler, cloud infrastructure,
task queue, Firestore, Secret Manager, persistence, or deployment implementation.

## Milestone 2A: X API Safety Foundation

Milestone 2A adds a focused, programmatic client for the official X API v2 endpoints
[`GET /2/users/me`](https://docs.x.com/x-api/users/get-my-user) and
[`POST /2/tweets`](https://docs.x.com/x-api/posts/create-post). It adds no X CLI, credentials,
automatic token acquisition or refresh, persistent idempotency, scheduler, or real-post
validation. **Do not attempt a real Post during Milestone 2A.**

The chosen authentication design is OAuth 2.0 Authorization Code with PKCE. X documents both
OAuth 1.0a User Context and OAuth 2.0 PKCE for these endpoints, and
[recommends OAuth 2.0 for new integrations](https://docs.x.com/x-api/getting-started/getting-access)
that need granular scopes. The eventual unattended flow will request
`tweet.read`, `users.read`, `tweet.write`, and `offline.access`: `offline.access` supplies a
refresh token so an access token can be renewed without repeating interactive authorization at
every FPL deadline. App-only authentication cannot call `/2/users/me` or create a Post on behalf
of a user. See X's
[authentication mapping](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping)
and [OAuth 2.0 PKCE guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code).

Configuration is read through these environment-variable names; no values or secret files belong
in Git:

| Name | Purpose |
| --- | --- |
| `X_ENVIRONMENT` | Must be the only supported write environment, `test`; there is no production mode. |
| `X_POSTING_ENABLED` | Must be explicitly `true`; absence defaults to disabled. |
| `X_EXPECTED_USER_ID` | Expected immutable numeric ID of the dedicated test account. |
| `X_USER_ACCESS_TOKEN` | OAuth 2.0 user-context access token; its presence alone never enables posting. |

Before a write, the client requires every configuration guard, calls `/2/users/me`, validates the
response, and compares its numeric user ID with `X_EXPECTED_USER_ID`. A mismatch fails closed;
username is validated but is not the identity key because it can change. Test and future
production accounts must use different IDs and secrets. Production mode and production
credentials remain absent.

The future Milestone 2B controlled test will use the private zero-follower account, explicit write
enablement, its configured numeric ID, and an unmistakable caller-supplied message such as
`FPL Bot API integration test — TEST ACCOUNT ONLY — <timestamp>`. It will not use the deterministic
V1 deadline tweet. A create-Post timeout, connection failure, server error, or malformed HTTP 201
is surfaced as an ambiguous write outcome and is never retried automatically. Credentialed X
requests never follow HTTP redirects, so bearer credentials cannot be forwarded to a redirected
destination. An unexpected read redirect is a typed API failure; an unexpected create-Post
redirect or HTTP 408 is an ambiguous write outcome.

## Milestone 2B: Local Test-Account Authorization Helper

Milestone 2B adds a local OAuth 2.0 Authorization Code with PKCE helper. It requests exactly
`tweet.read users.read tweet.write offline.access`, generates a fresh cryptographic state and S256
PKCE challenge, and listens only at the registered callback
`http://127.0.0.1:8765/callback`. It validates state before exchanging the short-lived code and
handles denial callbacks without echoing provider-supplied details.

For the configured confidential client, the helper follows X's current
[user access-token guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/user-access-token):
it authenticates the token request with HTTP Basic Client ID/Client Secret and posts the code,
`authorization_code` grant, exact redirect URI, and PKCE verifier to
`https://api.x.com/2/oauth2/token`. It requires both an access token and refresh token, then calls
only `GET /2/users/me`. It never enables posting or calls `POST /2/tweets`.

The helper reads these names from the process environment. Do not put their values in Git, a
`.env` file, command arguments, shell history, or screenshots:

| Name | Purpose |
| --- | --- |
| `X_OAUTH_CLIENT_ID` | Confidential OAuth 2.0 Client ID. |
| `X_OAUTH_CLIENT_SECRET` | Confidential OAuth 2.0 Client Secret. |
| `X_OAUTH_TOKEN_OUTPUT_FILE` | New absolute encrypted handoff file path outside this repository. |

The output file must not already exist. For the real Windows run, the complete JSON handoff is
encrypted in memory with Windows DPAPI before the file is created. DPAPI uses the current Windows
user context; the helper never writes a plaintext token file. The file contains only a format
marker and ciphertext, cannot be moved to another Windows user as a portable secret, and remains
outside Git. Encryption or persistence failure creates no usable handoff and fails closed.

Non-Windows development retains an exclusively created plaintext handoff with POSIX mode `0600`,
but that path is not used for the actual Windows Milestone 2B authorization. Neither path is
production persistence.

When explicitly approved for a real test-account authorization, run one of:

```bash
python -m fpl_bot.x_oauth_cli
# Or, after installation:
fpl-bot-x-authorize
```

The browser opens automatically; the authorization URL is not printed. Successful terminal output
contains only the authenticated numeric user ID, username, generic handoff confirmation, and a
reminder that posting remains disabled. **Do not run this helper until the controlled real OAuth
authorization is explicitly approved.**

The first authorization does not require a pre-known numeric account ID. After token acquisition,
the helper performs only the read-only `/2/users/me` request and prints its numeric user ID and
username. Manually confirm that identity is the dedicated private test account. Its numeric ID then
becomes the canonical `X_EXPECTED_USER_ID` for the later controlled Post; obtaining it does not
enable posting.

## One-Shot X Test Post

The one-shot runner reuses the existing guarded X API client; it adds no authentication or HTTP
implementation. Its fixed message is `FPL Bot API integration test — TEST ACCOUNT ONLY`, which is
deliberately separate from the FPL deadline renderer. By default it only previews that message and
makes no X API request:

```bash
python -m fpl_bot.test_post
# Or, after installation:
fpl-bot-x-test-post
```

An approved manual test against the dedicated private test account requires both the explicit
`--live` option and every existing write guard:

```bash
python -m fpl_bot.test_post --live
# Or: fpl-bot-x-test-post --live
```

Configure `X_ENVIRONMENT=test`, `X_POSTING_ENABLED=true`, `X_EXPECTED_USER_ID`, and
`X_USER_ACCESS_TOKEN` in the process environment. Never place their values in Git, `.env` files,
command arguments, screenshots, or documentation. The expected ID must be the numeric ID manually
verified during the OAuth helper's `/2/users/me` bootstrap. The access token must have
`tweet.read users.read tweet.write offline.access` authorization.

The OAuth helper remains the sole local token-acquisition path, and its DPAPI file remains the
encrypted at-rest handoff on Windows. The runner neither reimplements OAuth nor persists tokens;
for an approved Windows test, load the access token from that handoff into the current process
environment without printing it.

Live mode validates configuration before creating a client, re-verifies `/2/users/me`, and sends
exactly one `POST /2/tweets` request without retries. On success it prints only the resulting Post
ID. Ambiguous outcomes must not be retried manually until reconciled. This runner has no production
mode and is not connected to FPL event selection, deadline tweets, scheduling, persistence, cloud
infrastructure, or deployment. **Do not use `--live` until the controlled test is separately
approved.**

## Durable Posting State

The persistence layer stores one document per immutable FPL event at
`fpl_event_posts/{event_id}`. Its explicit posting states are:

```text
unclaimed -> posting_claimed -> posting_in_progress -> succeeded | failed | uncertain
```

An event document may exist before posting with null posting fields while scheduling, deadline, and
preflight metadata are reconciled. Reconciliation may update that metadata only while the event is
unclaimed. A Firestore claim transaction either creates a claimed document when none exists or
updates an existing unclaimed document whose event code and deadline metadata match. Concurrent
transactions cannot both claim it. Once any posting status exists, another claim is denied; a
mismatched deadline cannot mutate the claimed operation. The claim holder must transactionally
persist `posting_in_progress` and a timezone-aware UTC attempt timestamp immediately before a
future X request. Only that claim may record a terminal outcome.

Each document retains the FPL event ID, event code, official UTC deadline, claim and attempt
timestamps, posting status, successful X Post ID or safe failure details, plus nullable
`scheduled_task_id`, `scheduled_task_status`, and `preflight_status` fields reserved for later
milestones. This milestone does not populate those placeholders through scheduling behaviour.

Firestore may retry a state transaction to resolve database contention; this never retries an X
write. If claim, attempt, or outcome persistence is uncertain, callers fail closed and do not Post
or reclaim automatically. The in-memory implementation provides the same transitions for
deterministic tests, while production uses the `google-cloud-firestore` adapter. Unit tests require
no Google credentials and make no Firestore requests. Error details are normalized to one line,
limited to 2,000 characters, and rejected if they resemble authorization or credential material;
callers should still supply concise, non-secret application errors.

## Deadline Post Execution Coordinator

The injected `DeadlinePostExecutionCoordinator` joins one already-resolved `EventReport` to the
durable posting-state store and guarded X client. It deterministically revalidates the event code
and V1 tweet before claiming, persists `posting_in_progress` before invoking X, and then records
`succeeded`, `failed`, or `uncertain`. The X client still enforces explicit test-mode posting,
credentials, and `/2/users/me` identity verification before its single create-Post attempt.

The coordinator contains no retry loop. A duplicate claim performs no X operation. Known
pre-write and definite rejection failures become `failed`; only the X client's explicit ambiguous
write error becomes `uncertain`. An otherwise unclassifiable boundary bug leaves the event closed
in `posting_in_progress` rather than guessing that a Post may exist. If X succeeds but the
success-state write cannot be confirmed, the typed application error retains the known numeric X
Post ID for manual reconciliation and explicitly prohibits another Post attempt.

This module has no CLI, HTTP endpoint, scheduler, or automatic trigger. Tests inject in-memory
state and fake X transports; running the test suite cannot contact X, Firestore, or FPL.

## Live Deadline Revalidation

`DeadlineExecutionRevalidator` accepts only an expected FPL event ID and timezone-aware UTC
deadline. At execution it fetches fresh bootstrap data, locates that exact event, requires the live
official deadline to match exactly, and refuses execution before that deadline. It then fetches
fresh event fixtures and reuses the deterministic report builder to derive the current
GW/BGW/DGW/BDGW code and V1 tweet before invoking the existing posting coordinator.

Missing events, changed deadlines, early execution, unavailable FPL data, and malformed teams or
fixtures fail before any posting-state or X activity. After successful live validation, an existing
unclaimed audit record is reconciled to the fresh event code while retaining task/preflight audit
fields. Existing claimed or terminal records skip reconciliation and reach the coordinator's
duplicate no-op without mutation or X access. A concurrent claim during reconciliation is handled
the same way; claimed metadata remains immutable and fail-closed.

This layer receives no cached event code or tweet and has no task creation, scheduler, HTTP
endpoint, retry, cloud provisioning, or deployment behavior.

## Deadline Planning

`DeadlinePlanner` fetches fresh FPL bootstrap data and reuses the Milestone 1 event parser and
chronology-safe selector. To avoid inventing a minute-based lateness cutoff, a single authoritative
event whose official deadline is on the current London date remains the planning candidate even
after its deadline time; when there is no current-day event, the existing selector chooses the
current/future event unchanged. Multiple current-day deadlines are treated as contradictory. The
planner converts both the injected current time and selected deadline to `Europe/London` with the
standard timezone database, then compares only their local calendar dates. A matching date returns
the existing immutable `ScheduledDeadlineInstruction`; a different date returns an explicit
no-arm decision.

The instruction contains only the authoritative event ID and exact timezone-aware UTC deadline.
It carries no event code or tweet, and downstream execution still re-fetches and revalidates both
identity values before any posting activity. Invalid, unavailable, or contradictory FPL data
raises the existing typed failures rather than manufacturing an instruction. The planner adds no
maximum-lateness rule: its date decision alone does not reject a selected deadline merely because
the time has passed.

This pure planning layer reads bootstrap data only. It does not fetch fixtures, create tasks,
claim posting state, call X, import Cloud Tasks, or implement Scheduler, Cloud Run, HTTP, retry, or
deployment behavior.

## Cloud Task Arming

`DeadlineTaskArmer` accepts an already-approved `ScheduledDeadlineInstruction`; it does not fetch
FPL or repeat the planner. When the deadline is still future, it first reconciles an unclaimed
audit record to `arming`, confirms that posting has not concurrently been claimed, submits one
named HTTP task, and records the result. At or after the deadline it records and returns
`overdue_same_day` without calling Cloud Tasks, because Cloud Tasks replaces a past
`schedule_time` with the current time rather than honestly retaining the official deadline.

Task IDs use `fpl-` followed by the first 40 hexadecimal characters of SHA-256 over the canonical
ASCII identity `fpl-deadline|event_id|official_deadline_utc`. The fixed domain separator is
independent of the payload schema, so the same instruction keeps the same identity across
processes and payload-version changes. The hash changes when either immutable value changes, is
uniformly distributed, and uses only supported task-ID characters. The JSON body is independently
versioned and contains exactly `version`, `expected_event_id`, and `expected_deadline_utc`. It has
no event code, tweet, fixture data, posting state, credentials, or secrets. The submitted
`schedule_time` equals the instruction's official UTC deadline exactly.

The production adapter requires a project ID, region, queue ID, HTTPS execution URL, same-project
task-caller service-account email, and optional HTTPS OIDC audience. It uses an authenticated HTTP
POST target and the official Google Cloud Tasks client. No project-specific value is hardcoded.
See Google's documentation for
[HTTP task OIDC authentication](https://cloud.google.com/tasks/docs/creating-http-target-tasks)
and [task naming, scheduling, and de-duplication](https://cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2#createtaskrequest).

The adapter disables client retries for each call. On `ALREADY_EXISTS`, it performs one `GetTask`
with `FULL` view for the exact deterministic name and compares the name, schedule time, HTTP
method, URL, payload, OIDC service-account email, and OIDC audience. Only an exact match is
`already_armed`. `NOT_FOUND` means the name is retained but no task is currently confirmed, so the
result is `task_name_reserved`; any definition mismatch is a fail-closed conflict. Neither case
causes a random replacement name.

A transport-ambiguous create performs the same single read-only reconciliation. An exact task is
`reconciled_armed`; `NOT_FOUND` remains ambiguous, and a mismatch remains a conflict. A later
checker invocation may retry only the same deterministic name. Definite failure is audited without
an in-invocation retry. If a task is known to exist but final audit persistence fails, a typed
error retains its non-secret task name and requires reconciliation—no second logical task is
created.

The eventual arming identity needs only narrowly scoped create and read access. Reconciliation
requires `cloudtasks.tasks.get`; retrieving the payload with `FULL` view additionally requires
`cloudtasks.tasks.fullView` on the queue, as documented for
[`GetTask`](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/get).
Deployment must grant these alongside task creation and service-account impersonation permissions
without granting queue-administration permissions.

Scheduling metadata may exist before fixture classification, so an unclaimed audit record may
temporarily have no event code. Live deadline execution still derives and reconciles the fresh
event code before acquiring the posting claim. Once posting has any state, arming cannot mutate the
record. This layer does not claim posting, call X, create queues, implement task handlers,
Scheduler, Cloud Run, overdue recovery, cancellation, deployment, or provisioning.

## Deadline Task HTTP Handler

The deadline-task surface exposes only `POST /tasks/deadline`. A framework-neutral handler parses
the existing versioned Cloud Tasks payload and invokes the injected `DeadlineExecutionRevalidator`;
a thin Flask application factory adapts that result to HTTP. The factory constructs no FPL,
Firestore, X, or Google Cloud client, so tests and imports have no network or credential side
effects. A later deployment composition root will inject the existing production adapters rather
than duplicating them. Flask is the only dependency added for this surface because it provides a
small conventional Cloud Run-compatible request adapter and deterministic local test client.

Authentication remains outside application code. The intended Cloud Run service is private, the
Cloud Task carries its configured Google-signed OIDC identity, and only the designated task-caller
service account receives `roles/run.invoker`. Cloud Run and IAM reject unauthorized traffic before
the request reaches the container; the application does not parse or verify JWTs and never treats
`X-CloudTasks-*` headers as event identity. This milestone documents but does not provision that
boundary. See Google's guidance for
[authenticated Cloud Run service-to-service requests](https://cloud.google.com/run/docs/authenticating/service-to-service).

Cloud Tasks removes an HTTP task after a `2xx` response and retries non-`2xx` or missing responses.
The handler deliberately uses only these statuses:

| HTTP | Result | Meaning |
| --- | --- | --- |
| `200` | `posted` | The Post succeeded and durable success was recorded. |
| `200` | `duplicate` | Posting state already exists; no new X write occurred. |
| `200` | `stale` | Live FPL proves the event is missing or its deadline changed. |
| `200` | `invalid_task_payload` | Malformed, unsupported, non-UTC, or extra payload data is permanently invalid. |
| `200` | `failed_closed` | A definite/ambiguous X outcome or post-claim persistence problem left the event closed. |
| `503` | `retryable` | Early delivery, unavailable/invalid live FPL data, an unconfirmed atomic claim outcome, or an unknown failure may safely be delivered again. |

The JSON response contains only the `result` value—never exception text, credentials, headers, or
OAuth material. A valid body contains exactly `version`, `expected_event_id`, and
`expected_deadline_utc`; the event code and tweet remain live-derived. Invalid bodies never invoke
the revalidator. Known X failures are acknowledged only after the existing coordinator has closed
durable state, so Cloud Tasks cannot become an X retry loop. If a successful HTTP response is lost,
redelivery reaches the same event-level atomic claim and returns `duplicate` without a second X
write. An unconfirmed claim transaction is not described as cleanly pre-claim: redelivery either
makes progress if no claim committed or observes the same event's committed claim and terminates as
a duplicate. Google documents the acknowledgement behavior in the
[Cloud Tasks HTTP request reference](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks).

This is an injectable execution surface only. It has no global production app, deployment files,
custom OIDC verification, Scheduler, overdue recovery, queue provisioning, or live-call command.

## One-Run Deadline Checker

`DeadlineChecker` represents one future periodic-checker invocation. It obtains a timezone-aware
UTC check time, asks the existing `DeadlinePlanner` for the authoritative London-day decision, and
passes the exact returned `ScheduledDeadlineInstruction` to the existing `DeadlineTaskArmer`.
Neither the checker result nor the instruction contains an event code, tweet, fixture data, or
classification. Production composition must inject the same UTC clock into the checker and its
time-sensitive lower services; each lower layer continues to validate its own safety boundary.

The orchestration outcomes are deliberately small:

| Planning/arming outcome | Checker action/result |
| --- | --- |
| Deadline is not on the current London day | `no_action_not_today`; no arming or execution |
| Task created | `task_armed`; no direct execution |
| Existing task verified | `task_already_armed`; no direct execution |
| Ambiguous creation reconciled to the same task | `task_reconciled_armed`; no direct execution |
| Posting state already exists | `already_handled` or overdue `overdue_duplicate`; no direct execution |
| Exact deadline reached or passed today | invoke the existing revalidator once |
| Overdue Post succeeds | `overdue_executed` |
| Overdue claim is denied | `overdue_duplicate` |
| Overdue revalidation finds a changed/missing event | `stale`; no Post |
| Temporary live-FPL or unconfirmed claim failure | `retryable_failure`; no in-run retry |
| Known closed X/posting-state failure | `failed_closed`; no in-run retry |

Only the armer's explicit `overdue_same_day` result selects direct recovery. Task creation
rejection, a reserved name, definition conflict, unresolved create/reconciliation outcome, or audit
persistence failure propagates its existing typed error and never invokes posting directly. A
later checker run may re-enter the same deterministic task identity; this checker adds no loop.

An exact-deadline task and overdue recovery may race safely. Both paths use the same immutable
instruction, re-fetch live FPL data, and meet at the existing event-level atomic posting claim. The
winner can make the one X write; the other receives the durable duplicate result. Repeated overdue
runs after success therefore make zero additional X requests. This module adds no Scheduler,
checker endpoint, process loop, Cloud Run deployment, task creation implementation, or cloud
provisioning.

## Private Checker HTTP Surface

The injectable Flask app can expose `POST /checker/run` alongside `POST /tasks/deadline` when a
`DeadlineChecker` is supplied to `create_app`. The checker route accepts no authoritative request
input: request bodies and query parameters are ignored, and the existing planner always obtains the
event and deadline from fresh FPL data. The handler invokes the checker once and returns only a
small JSON result label.

Terminal outcomes return HTTP 200: no action today, task armed/already armed/reconciled, duplicate,
overdue success, stale recovery, and known fail-closed outcomes. Explicitly retryable checker
results and unknown exceptions return HTTP 503 so a later delivery may try again; the handler adds
no retry loop. It never returns exception text, event data, credentials, tokens, or internal state.

Production authentication remains outside Python: the future private Cloud Run service will rely
on IAM, with only the future Scheduler caller granted invocation permission. The intended Scheduler
cadence is once daily at `06:00 Europe/London`; no Scheduler resource, cron/process loop, IAM rule,
deployment, or live dependency composition is included yet.

## Five-Minute Preflight

For a future same-day deadline, the checker first ensures the exact-deadline task and then asks the
preflight armer to create a second task at exactly `official_deadline - 5 minutes`. Its ID is
`fpl-preflight-<sha256-prefix>`, derived deterministically from the event ID and official UTC
deadline, while the final task retains its separate `fpl-<sha256-prefix>` identity. Both carry only
the existing versioned event-ID/deadline payload. If the preflight time is reached or passed, no
past task is created; the final task remains valid. A preflight arming failure does not undo a
confirmed final task and is reported as the retryable, non-secret `preflight_failed` checker result.

`DeadlinePreflight` is structurally read-only with respect to X. It receives only the FPL read
boundary, an audit-only state surface, and final/preflight task armers. It has no posting claim,
post-execution coordinator, deadline execution revalidator, X creator, or dry-run switch. It fetches
fresh bootstrap events, validates the exact event and official deadline, records audit status, and
never fetches fixtures or renders a tweet.

The strict `POST /tasks/preflight` route uses the same immutable payload parser as
`POST /tasks/deadline`; invalid payloads are terminal and invoke no preflight logic. An unchanged
deadline records `preflight_passed` only before that deadline. A delayed delivery at or after an
unchanged deadline records terminal `preflight_too_late` without changing or inhibiting the final
deadline task; late final-task execution retains its independent live revalidation semantics. A
changed deadline later or earlier on the same London day is re-armed only while still future, with
a new deterministic final task and a new preflight where its five-minute time remains schedulable.
An already-reached changed deadline never invokes overdue posting recovery. A deadline moved to
another London day is marked stale for the normal daily checker. Claimed posting records remain
immutable. Cloud Run/IAM remains the authentication boundary; no Scheduler, deployment, IAM,
queue, or Firestore provisioning is included.

## Production Runtime Composition

`fpl_bot.production.create_production_app()` is the explicit composition path for the existing V1
services. Importing the module reads no configuration, creates no SDK client, starts no server, and
makes no external request. Calling the factory validates configuration, constructs the existing FPL,
Firestore, Cloud Tasks, X, planning, preflight, revalidation, and posting components, and returns one
Flask app with these private IAM-protected routes:

- `POST /checker/run` — intended for Cloud Scheduler once daily at `06:00 Europe/London`;
- `POST /tasks/preflight` — deterministic warmup five minutes before the official deadline;
- `POST /tasks/deadline` — live revalidation and guarded execution at the official FPL deadline.

Required runtime environment variables contain configuration names only; do not commit their
values:

| Name | Purpose | Secret |
| --- | --- | --- |
| `GCP_PROJECT_ID` | Google Cloud project used by Firestore and Cloud Tasks. | No |
| `FIRESTORE_DATABASE_ID` | Firestore database ID; optional, defaults to `(default)`. | No |
| `CLOUD_TASKS_LOCATION_ID` | Cloud Tasks region, such as the chosen UK-compatible region. | No |
| `CLOUD_TASKS_QUEUE_ID` | Existing queue that will hold deadline and preflight tasks. | No |
| `CLOUD_RUN_BASE_URL` | HTTPS origin of the future private Cloud Run service. | No |
| `CLOUD_TASKS_CALLER_SERVICE_ACCOUNT_EMAIL` | Service identity attached to task OIDC tokens. | No |
| `CLOUD_TASKS_OIDC_AUDIENCE` | Optional OIDC audience; defaults to the Cloud Run origin. | No |
| `X_ENVIRONMENT` | Existing guarded X environment; currently only `test` is accepted. | No |
| `X_POSTING_ENABLED` | Use `false` for safe deployment/smoke testing; every X write still requires explicit `true`. | No |
| `X_EXPECTED_USER_ID` | Immutable numeric ID verified through `/2/users/me` before every write. | No |
| `X_TOKEN_SECRET_ID` | Existing Secret Manager secret that holds versioned OAuth token-state payloads. | No |
| `X_OAUTH_CLIENT_ID` | Confidential OAuth client ID used for token refresh. | **Yes** |
| `X_OAUTH_CLIENT_SECRET` | Confidential OAuth client secret used for token-endpoint authentication. | **Yes** |

Firestore, Cloud Tasks, and Secret Manager use Application Default Credentials and the future Cloud
Run service identity. No service-account key file is loaded by the application. The static OAuth
client secret may be injected from Secret Manager as an environment variable. Mutable access and
refresh tokens instead use the distributed token-state store described below. The application does
not create the secret, initialize Firestore, provision IAM, or deploy cloud resources.

Production-account deployment remains deliberately blocked by the write guard, which still accepts
only `X_ENVIRONMENT=test`. Cloud resources and initial token authority also remain unprovisioned.

This milestone adds composition only: it does not change the 06:00 planning decision, five-minute
preflight, exact-deadline task identity, live revalidation, event-level idempotency, X identity
guard, or retry behaviour.

## Unattended X Token Refresh

X documents that Authorization Code with PKCE access tokens normally last two hours and that
requesting `offline.access` yields a refresh token. The confidential-client refresh request is a
form-encoded `POST https://api.x.com/2/oauth2/token` with `grant_type=refresh_token`, the current
refresh token, and HTTP Basic Client ID/Client Secret authentication. See X's current
[authorization-code guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
and [user-token guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/user-access-token).

`RefreshingXAccessTokenProvider` reads immutable token state containing the access token, refresh
token, required scopes, and timezone-aware UTC expiry. It refreshes before the credential is expired
or within the fixed five-minute safety margin. A refresh response must contain a Bearer access token,
a positive lifetime, and all V1 scopes: `tweet.read`, `users.read`, `tweet.write`, and
`offline.access`. Under OAuth 2.0 refresh semantics, a returned replacement refresh token becomes
authoritative; when no replacement is returned, the previous refresh token remains in the new state.
Validated replacement state must be durably stored before its access token reaches the X client.

Token persistence is behind a versioned compare-and-swap interface. A concurrent loser cannot
overwrite a newer token generation: it re-reads and may use the already-valid authoritative state,
without an internal retry loop. The included in-memory store remains deterministic test support
only. Ordinary Secret Manager environment injection is insufficient for rotating token state
because an environment value is fixed for the lifetime of a Cloud Run instance or revision; Google
documents that
[secret environment variables are resolved at instance startup](https://cloud.google.com/run/docs/configuring/services/secrets).
Do not store refresh tokens in Firestore.

### Distributed cloud token state

`GoogleCloudXTokenStateStore` stores each complete sensitive OAuth generation as a version of the
configured `X_TOKEN_SECRET_ID`. Firestore collection `x_oauth_token_authority` contains one
non-secret document named `x-user-{X_EXPECTED_USER_ID}`. That document records the schema,
monotonic revision, exact authoritative Secret Manager version resource name, previous version,
UTC update time, and optional refresh lease owner/expiry. It never contains tokens or OAuth client
credentials.

Readers access only the explicit numeric Secret Manager version selected by Firestore. They never
use `latest` or a version alias: Google documents that an added version is strongly consistent when
read by its [explicit version number](https://cloud.google.com/secret-manager/docs/reference/consistency),
but not through `latest` or aliases.

When refresh is required, a Firestore transaction grants a one-minute lease for the current
revision. The OAuth request then occurs outside the transaction. The validated response is written
immediately as a new Secret Manager version, after which a second Firestore transaction checks the
revision and lease owner, advances authority, and clears the lease. Only a confirmed authoritative
generation may supply an access token to `/2/users/me` and the posting client. Transaction callback
retries therefore repeat metadata operations only; they cannot repeat OAuth or X requests.

If Secret Manager storage fails, Firestore authority is unchanged. If Secret Manager succeeds but
Firestore definitely rejects the transition, the candidate remains non-authoritative. If the
Firestore outcome is uncertain, the store re-reads authority and accepts the candidate only when
that exact version and revision are confirmed; otherwise it fails closed and retains the orphan for
manual reconciliation. A crashed refresher's lease expires, allowing a later invocation to recover.
After a successful rotation the current and immediately previous versions remain enabled; an older
superseded version is disabled best-effort. Cleanup failure cannot affect current authority, and no
potentially authoritative candidate is destroyed.

The initial deployment bootstrap remains manual: authorize the approved test account with the
existing local DPAPI helper, add the complete serialized token state securely as the first version
of the pre-created secret without printing it, then initialize the Firestore authority document to
that explicit version and revision. No bootstrap transfer or provisioning command is included.

The eventual Cloud Run identity needs narrowly scoped Firestore document read/transaction writes,
plus `secretmanager.versions.access` and `secretmanager.versions.add` on the configured secret.
Best-effort old-version disabling additionally needs `secretmanager.versions.disable`; use a
secret-scoped custom role or the narrowest suitable predefined roles, not project-wide Secret
Manager Admin.

The posting order remains: durable event claim, persisted `posting_in_progress`, valid-token
acquisition or refresh, `/2/users/me`, exact numeric account-ID verification, and at most one
`POST /2/tweets`. Refresh failures are definite pre-write failures, never ambiguous X writes. The
standalone test-post runner remains explicitly gated and continues accepting its deliberately
supplied static `X_USER_ACCESS_TOKEN`; it does not silently opt into unattended refresh.

## Container Runtime

The production container serves the existing `create_production_app()` graph through Gunicorn; it
does not use Flask's development server or add another application factory. Gunicorn binds to
`0.0.0.0` on Cloud Run's `PORT`, with `8080` only as a local fallback. V1 uses one worker with two
threads: this is deliberately modest for a small I/O-bound service, while Cloud Run can scale
horizontally. Correctness never depends on this process layout; Firestore posting claims,
deterministic Cloud Task identities, and the distributed OAuth refresh lease/CAS remain the safety
boundaries across threads and instances.

Build and run the image locally with runtime configuration supplied from outside the repository:

```bash
docker build -t fpl-tweet-bot:local .
docker run --rm -p 8080:8080 -e PORT=8080 --env-file /absolute/path/outside/repo/runtime.env fpl-tweet-bot:local
```

The required variable names and meanings are listed in **Production Runtime Composition** above.
Do not keep the referenced environment file in this repository. A future Cloud Run deployment will
inject configuration and secrets and will use its service identity through Application Default
Credentials; the image contains no service-account key, OAuth token, or environment-specific
value. Importing `fpl_bot.wsgi` performs no composition or external operation. Gunicorn calls the
factory once when starting the web application, and missing required configuration fails startup
without printing secret values. No custom health or admin route is added; successful server startup
is the runtime health signal.

The parameterized, non-secret Google Cloud foundation commands, IAM scopes, image-build procedure,
verification stop point, and teardown guidance are documented in [deploy/README.md](deploy/README.md).

## Requirements and Installation

- Python 3.11 or newer
- Internet access only for the live FPL dry run, an explicitly initiated OAuth helper, or the
  separately approved one-shot X test

Create a virtual environment and install the package with development tools:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

FPL and X HTTP access use the Python standard library. The conditional `tzdata` dependency is the
standard timezone database fallback on Windows. Firestore and Cloud Tasks production adapters use
Google's official Python clients, Flask supplies the injectable HTTP route adapter, and Gunicorn is
the Linux production WSGI server. Creating the Google clients will eventually require a Google
Cloud project and Application Default Credentials, plus a Firestore database or Cloud Tasks queue
respectively. No cloud resources or credentials are required or provisioned in this milestone.

## Development Commands

Run the complete deterministic test suite (all external responses are mocked):

```bash
python -m pytest
```

Check lint and formatting without changing files:

```bash
python -m ruff check .
python -m ruff format --check .
```

Apply formatting locally when needed:

```bash
python -m ruff format .
```

## Live FPL Dry Run

Run either command from the repository root:

```bash
python -m fpl_bot
# Or, after installation:
fpl-bot
```

The command queries only FPL-hosted JSON endpoints and prints the event ID, official deadline in
UTC and London time, classification, event code, blank/double teams, fixture count for every team,
and rendered tweet. It has no publishing side effect and requires no credentials.

## Classification Rules

- **GW:** every current Premier League team has exactly one fixture.
- **BGW:** at least one team is blank and no team has multiple fixtures.
- **DGW:** at least one team has multiple fixtures and no team is blank.
- **BDGW:** at least one team is blank and at least one has multiple fixtures.

See `AGENTS.md` for permanent engineering, reliability, cost, and scope constraints.
