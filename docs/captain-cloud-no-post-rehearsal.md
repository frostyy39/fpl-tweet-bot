# Captain cloud NO-POST rehearsal

This composition has no X client, credentials, secret mounts or publisher. It is
restricted to project `fpl-frosty-bot-v1`, database `captain-state`, the fixed London
VM, and diagnostic destination ID `1`. These generations are not production
publisher assignments. A later publisher must not consume this diagnostic state.

## Reproducible resources

Run `deploy/provision-captain.ps1` without switches for its plan, and with `-Apply`
only after reviewing the additive changes. It creates a London named database,
paused `captain-orchestration` queue, `captain-images` Docker repository, private
build-source bucket and five independent Captain identities. It attaches the
worker identity while the VM is **TERMINATED**, without changing Windows accounts,
disks, browser data, networking or the existing four-hour STOP limit.

Controller Firestore authority is conditioned on the exact `captain-state`
database; Good Luck's existing `(default)` condition remains unchanged. VM
get/start/stop authority is granted on the Captain VM only. Zone-operation get/list
is project-scoped read-only evidence (not arbitrary instance control). Queue
enqueue/full-view permissions are queue-scoped; controller actAs is limited to
the Captain Tasks identity. Build permissions cover only its source bucket,
image repository and project log writing. The worker has no direct Firestore,
Tasks, Compute, Secret Manager or X authority. Service invocation IAM is scoped to
`captain-controller`; application authorization further separates worker, Tasks
and planner endpoint identities.

Build using `deploy/captain-build.yaml`, an explicit
`--ignore-file=deploy/captain.gcloudignore`, the Captain build identity and its
private source bucket. Tag images with the full Git commit and record the image
digest. Deploy with `deploy/deploy-captain.ps1 -Image <commit-tagged-image>`.
The controller now runs in `europe-west1`; its configured audience/origin and
worker configuration use that region. State, queue, Scheduler, images and the
Windows VM remain in London. The paused London Scheduler's URI/audience are
updated explicitly. Small cross-region state/task calls do not require a NAT,
proxy or static IP. The earlier London controller remains private and inactive;
no scheduler/task/client targets it. Good Luck stays unchanged.
Queue and minute-cadence planner are intentionally **PAUSED** pending rehearsal.
No secret environment variables or Secret Manager versions are configured.

The controller combines existing domain/adapters without changing timing policy.
Each request performs one bounded VM reconciliation step. START must settle before
STOP dispatch; STOP acknowledgement retains ownership until TERMINATED evidence.
Periodic planner ticks repair asynchronous operations and durable task intents.
Successful accepted handoff triggers cleanup through the same fencing path.
Validated candidates are retained as immutable `captain_no_post_audits` documents
with `postable=false`, canonical UTF-8 tweet text, source ordinals/ranks, fresh
ownership/fixtures and acquisition evidence. This diagnostic collection is not a
publisher input. No accepted handoff is modified or reinterpreted as credentials.

## Windows private setup boundary

Reuse the installed Python and dependencies, but place the new source/config
outside both Chrome profiles. The startup action is:

```text
<venv>\Scripts\python.exe -m fpl_bot.captain_worker_cli --config <private-config.json>
```

The working directory must contain the new `fpl_bot` source. The configuration
contains exactly `controller_origin`, `profile_dir`, `expected_user`, `audit_parent`:
the fixed HTTPS origin, the existing dedicated profile path, `captaintrial`, and
a precreated private diagnostic parent. No password, token or cookie is included.
The CLI writes exclusively created current-user-private UTF-8 diagnostic audits;
the structured authenticated controller submission remains authoritative.
`deploy/package-captain-worker.ps1` builds a worker-only ZIP from the static local
import closure and prints its SHA256. No X publisher or Google persistence/control
client is included. The package does not install or modify the Chrome profile.

Task Scheduler registration is a **private manual Windows GUI step**. Use the
existing captaintrial identity, Password logon, Limited privileges, run whether
logged on or not, one instance, network required, no automatic restart, and a
bounded startup trigger. Enter the existing password only in the native Windows
dialog. Do not use SYSTEM, S4U, auto-login or password resets.

The bundle includes `prepare-captain-worker-remote.ps1`. Run its reviewed contents
inside the existing captaintrial desktop, supplying the verified ZIP and SHA256.
It requires the existing runtime, closed Chrome and free profile ownership; refuses
an existing application directory; and grants only the actual current-user SID,
SYSTEM and Administrators access. It builds `Captain-Worker-NoPost.xml` in memory
and writes it as UTF-8. It does **not** register or run the task. Import the XML in
Task Scheduler as `Captain-Worker-NoPost`, check Password/Limited logon, then save
with the existing password only in the native dialog. Its trigger is boot (not
logon), delayed 30 seconds, with a 25-minute limit, IgnoreNew and no demand start.

Pending release polling is cancellable, at most 120 bounded checks with at most
10 seconds between pending replies, covering the 15-minute warmup. It does not
open Chrome during readiness. Real acquisition still requires fresh controller
FPL validation and `[T,L]`, with `T=D-2h`, `L=T+5m`, inclusive.

## Evidence and cost

Before T, a real worker can prove metadata identity authentication and safe
no-assignment exit without browser acquisition. Do not move a real release earlier
to obtain a dataset. Any fixture rehearsal must be explicitly separated from
production assignments and incapable of obtaining posting authority.

Budget assumes about four aggregate VM-running hours/month: Windows 2-vCPU licence
is $0.092/hour; add E2 compute and ephemeral IPv4. The existing 50-GiB pd-standard
disk dominates retained cost (approximately $2–3/month in London). Cloud Run is
request-billed with minimum zero, Tasks volume is tiny, named Firestore loses the
default-database free quota but expected operations/storage remain small. Allow
approximately £2–£4/month incremental Captain operating cost at this usage, with
VAT/exchange rates and existing retained Belgium disk billed separately. Monitor
actual billing; do not assume the four-hour emergency stop is the normal runtime.

No Good Luck service, queue, job, state, OAuth or runtime identity is modified.
Record actual provider evidence separately; local tests are not cloud proof.

## 17 September 2026 partial cloud evidence

Phase A release correction is checkpointed at
`a87971e4ee17b474450cc3d5a1b5322d49d3b184`. Captain-only resources were created
additively: named London database `captain-state`, the paused queue/planner,
private controller, image/source repositories and five service identities.
The existing Windows VM was never started; its worker identity was attached while
TERMINATED and its four-hour STOP safeguard retained. No Windows account or
Chrome profile was accessed or modified.

The authenticated temporary planner invocation reached the controller at
19:46:51 UTC, but fresh official FPL bootstrap retrieval returned HTTP 403 before
generation creation or any VM work. A separate body-free public HTTP matrix at
19:50:58 UTC returned 403 for bootstrap, fixtures and site root with both existing
diagnostic header profiles. This does **not** prove IP blocking. The temporary
Scheduler probe and HTTP-probe Cloud Run job were removed. No cached official data,
fake release clock, timing bypass, proxy or FPL Review acquisition was substituted.
Both real planner and queue remain PAUSED. The full cloud rehearsal is NOT passed.

`python -m fpl_bot.captain_persistence_probe` is an independent, explicitly
non-postable persistence check, intended to run under the controller identity.
It checks database metadata isolation without requesting any Good Luck document,
performs read-only repository operations, and writes/replays only an immutable
`captain_integration_probes` document. It does not create a generation, assignment,
task intent, VM lease, candidate or posting record. Its output is evidence only
after an actual successful execution; mocked tests are not live IAM proof.

The startup task still needs private native-GUI registration. Cloud Tasks delivery,
real Compute dispatch/reservation reconciliation, VM metadata-token transport,
handoff/candidate generation and final VM cleanup have not yet been exercised
end-to-end. Fresh official FPL reachability and the private registration step must
be resolved before claiming that chain or enabling the genuine deadline run.

## Controlled regional connectivity comparison

`deploy/captain-fpl-connectivity-probe.yaml` runs two read-only requests using the
existing `FplApiClient` and the exact immutable `d438d398` image. It records only
allowlisted response metadata, exception classes, JSON shape/chronology success,
runtime versions, DNS address families and whether a proxy is configured. It
does not save bodies, inspect cookies, create state or control the VM. Failed
bootstrap is followed by the unfiltered fixtures endpoint; successful bootstrap
selects the event fresh and queries its fixtures without a hardcoded event ID.
Temporary jobs should be deleted after collecting their safe Cloud Logging output.

On 17 September at 20:51:18 UTC, London returned two empty 403 responses from
Varnish through LCY edges. At 20:52:17 UTC, Belgium returned valid bootstrap JSON
and event fixtures with two HTTP 200 responses through BRU edges. Both used
Python 3.12.14, OpenSSL 3.5.7, IPv4 DNS results, the same GET requests/headers and
no proxy. This supports a regional egress/CDN difference; it does not identify
the underlying filtering policy or prove a general Google Cloud IP block.
The repository Good Luck factory uses the identical default `FplApiClient`; both
Dockerfiles use `python:3.12-slim`, and its existing service has no VPC-egress
override. No Good Luck request, deployment or secret access was performed.

Belgium repeated the two successful requests at 20:55:49 UTC. Controller image
`9e434fbe64b45cf09754c8d1bd7f67ba8d11adee` has digest
`sha256:bc6bf9eb2ea05061d063ab03bf45bddf86285cd65450a4af0eac3b604d0cc603`.
The Belgium controller revision is `captain-controller-00001-565`. The paused
London planner's URI/audience now reference Belgium, with its original Captain
planner identity explicitly preserved. Deployment compares fully qualified
Scheduler names from JSON because the CLI's display formatter shortens names.

The temporary authenticated planner probe completed at 21:02:28 UTC with two
identical successful controller ticks and a denied attempt to use the planner
identity at the worker endpoint. Fresh official FPL selected event 5 / GW5 with
deadline `2026-09-18T17:30:00Z`; the derived target is `15:30:00Z`, warmup
`15:15:00Z`, expiry `15:35:00Z`. Generation
`3c114305-3fa1-5fa3-860e-a56783156a5a` belongs only to diagnostic destination `1`.
Three deterministic tasks (warmup, release, cleanup) are intentionally retained
in the PAUSED Captain queue. No VM lease was created; both ticks reported idle.

The real `WorkerControllerService.release`/Firestore probe completed at
21:03:34 UTC after fresh bootstrap and fixture retrieval. It checked the stored
generation/deadline, returned `pending_before_T` and confirmed no acquisition
claim existed. This proves the pre-T gate, not a live at-T browser acquisition.
At 21:04:58 UTC, the existing worker HTTP transport and metadata-token source
successfully called the Belgium controller under the worker service identity
and returned no assignment. A wrong-audience token was denied. This probe ran
in a Cloud Run job; the Windows VM metadata/startup path remains unexercised.
No bearer token or response body was logged. All temporary connectivity/planner/
release/auth jobs must be removed after preserving their Cloud Logging evidence.

`captain-planner-validation-probe.yaml` contains a pre-warmup time guard and
requires no existing VM lease before invoking the real planner. Its source and
controller calls use fresh official data. `captain-release-validation-probe.yaml`
requires the stored PLANNED generation and pre-T time, and never grants release.
`captain-worker-auth-probe.yaml` obtains no assignment and opens no browser.
These are manual, bounded diagnostic definitions, not recurring production jobs.

Regional placement adds no static-egress/NAT resource or minimum-instance cost.
Expected cross-region state/task traffic is small and should add pennies monthly
at normal Gameweek volume. The prior approximately £2–£4/month operating estimate
remains the budget assumption, subject to actual quotas, VAT, rates and billing.
The older private London controller remains idle with minimum zero until a later
explicit cleanup decision; it is not the target of active clients/tasks/Scheduler.

## Private worker registration procedure

Do not enable the paused queue/planner or run the startup task during preparation.
The operator uses their established local IAP/RDP channel. After the verified
worker ZIP is copied to the remote Downloads folder, the following interactive
PowerShell procedure runs under the real `captaintrial` identity. Substitute the
exact bundle basename and SHA256 printed by `package-captain-worker.ps1`:

```powershell
$Bundle = Join-Path $env:USERPROFILE 'Downloads\worker-<commit>.zip'
$ExpectedSha256 = '<verified SHA256>'
if ((Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'STOP: bundle hash mismatch'
}
$SetupDirectory = Join-Path $env:TEMP ('CaptainSetup-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $SetupDirectory | Out-Null
Expand-Archive -LiteralPath $Bundle -DestinationPath $SetupDirectory
$Preparation = Get-Content -LiteralPath (Join-Path $SetupDirectory 'prepare-captain-worker-remote.ps1') -Raw
& ([scriptblock]::Create($Preparation)) -Bundle $Bundle -ExpectedSha256 $ExpectedSha256
```

This executes the verified, bundled preparation code interactively without
changing execution policy. It checks existing dependencies and closed/free
Chrome, creates a fresh private application directory and XML, and registers
nothing. The files copied into the temporary folder are application source only.
Never replace the prepared app directory if it unexpectedly already exists.

In Task Scheduler:

1. Select Task Scheduler Library, then **Import Task**. Open
   `C:\Users\captaintrial\AppData\Local\FPLBot\CaptainCloudWorker01\Captain-Worker-NoPost.xml`.
2. Set Name to `Captain-Worker-NoPost`. On General, verify the existing
   `captaintrial` account, **Run whether user is logged on or not**, **Do not store
   password** unchecked, and **Run with highest privileges** unchecked.
3. Check the single trigger is **At startup**, delayed 30 seconds; the action uses
   the existing trial venv Python, `fpl_bot.captain_worker_cli`, the new config,
   and the private application directory as Start in.
4. Check the imported settings: network required, IgnoreNew, no restart, 25-minute
   limit, demand start disabled. Save with OK. Enter the existing password only
   in the native Windows dialog. Never put it in PowerShell or chat.
5. Do not select Run, reboot, or enable the queue/planner. Report successful
   registration so the subsequent bounded boot/no-assignment proof can be planned.

At checkpoint `0d195cef76520e714411338e772893c54788f0c9`, registration was
the private Windows interaction pause point. The subsequent evidence below
supersedes that pause; it does not claim a complete end-to-end rehearsal.

## Registered-worker cold boot and early Tasks delivery: 17 September

The operator confirmed successful native password-backed registration of
`Captain-Worker-NoPost`, without a manual Run. They then closed their RDP window
and user-managed IAP tunnel, and agreed not to reconnect during the proof.

The setup VM was stopped and independently observed as TERMINATED. Compute
reported its subsequent cold start at `2026-09-17T22:33:49.306Z`; Windows reported
boot at `22:33:57.500Z`. The temporary read-only SYSTEM observer verified:

- principal `captaintrial`, Password logon, Limited privileges;
- one Boot trigger, 30-second delay, exact prepared Python/action/working directory;
- network required, IgnoreNew, no restart, demand start disabled, 25-minute bound;
- no current-boot interactive/unlock/RDP/cached-interactive logons for `captaintrial`
  in the enabled Security log, corroborating the operator's no-login confirmation;
- the actual worker's strict UTF-8 audit: `no_assignment`, exit 0, no handoff/digest;
- zero observed Chrome processes and no profile lifecycle markers.

The Python audit spans `22:35:53.119721Z`–`22:35:53.580305Z`. Its SHA256 is
`F705ED505ED5B86CA20CD7A949F8FA8230316AACDB0241200A7899C9E6E0A0EC`.
Task history contains action/start events at `22:35:03.888Z` and completion/result
events at `22:35:53.646Z`; Task Scheduler separately reported LastRunTime
`22:35:35Z`, LastTaskResult 0, Ready. These are recorded observations, not a
claim that all Windows startup clock timestamps are identical. The controller
logged the Windows worker's authenticated assignment request with HTTP 204 at
`22:35:53.418509Z`. No identity token or request Authorization header was captured.

`observe-captain-worker-boot.ps1` reads only Task Scheduler, logon-event metadata,
process counts, lifecycle marker existence and whitelisted worker-audit fields.
SYSTEM never executes the acquisition, impersonates the user, reads cookies or
opens Chrome. `install-captain-boot-observer.ps1` preserves the existing baseline
startup script verbatim, prepends a bounded background observer, and saves only
the original startup/guest-attributes settings in ignored local artifacts.
Restoration refuses a concurrent script change. The original settings were
restored after observation; the observer exited and will not run on later boots.

Compute independently reported the VM TERMINATED, with lastStopTimestamp
`2026-09-17T22:38:20.149Z`. The four-hour maximum-run-duration STOP safeguard
remains intact. This boot used operator Compute commands, **not** the durable
controller dispatch path; it does not prove real provider reservation fencing.

The guarded fresh planner probe succeeded before and after the Tasks test at
`22:35:38.888Z` and `22:43:44.329Z`. Both pairs of ticks were idempotent, used
fresh official FPL data, reported the same generation and an idle VM path, and
denied the planner identity at the worker endpoint.

`probe-captain-early-task.ps1` copied the already-durable warmup task's exact body
into temporary task `captain-rehearsal-early-2bac31d841134741979db24856b81dac`.
It enforced the paused queue, expected URL/OIDC identity/audience, diagnostic
destination, warmup kind and a 30-minute pre-warmup margin. RunTask dispatched
that diagnostic once while the real scheduled task remained untouched.
The real Captain warmup handler logged HTTP 200 at `22:42:06.473300Z`. Its
unchanged time/state gate allowed no early VM ownership/acquisition. The
diagnostic task was removed; only the three intended future tasks remain.
Invariant state is checked separately by `captain-state-inspection-probe.yaml`,
which performs Captain-only reads and asserts PLANNED/current generation, no VM
lease, no acquisition, no posting attempts and no pending outbox intents.
The real state probe confirmed all these invariants at `22:47:49.608264Z`.

Temporary planner/state-inspection jobs are deleted after retaining Cloud
Logging evidence. The Captain queue and planner remain PAUSED. The startup
worker remains intentionally registered; there is no Codex-owned IAP tunnel.
No X implementation/credentials were enabled, and no Good Luck resource changed.

### Timing-gated remainder

The accepted repository rejects VM-use claims before the official generation's
warmup time, not only projection acquisitions before release. For the currently
freshly validated generation, warmup is 18 September 15:15 UTC / 16:15 BST;
release is 15:30 UTC / 16:30 BST, with inclusive expiry 15:35 UTC / 16:35 BST.
There is no accepted rehearsal-purpose release contract or timing bypass.

Therefore this run proves the no-assignment Windows metadata-identity transport
and real early Tasks delivery, **not** durable Compute START/STOP dispatch,
released browser acquisition, handoff acceptance or cloud candidate validation.
Those require a deliberately activated genuine-window NO-POST rehearsal.
No browser was opened, projection fixture was represented as live acquisition,
handoff fabricated, candidate generated, or full Milestone 8 pass claimed.
The retained paused tasks do not arm that future rehearsal by themselves.
X publisher integration remains excluded.

## Explicit out-of-window non-postable rehearsal contract

The later full browser rehearsal uses `POST /captain/control/rehearsal`, callable only by the
existing Captain planner identity. Its versioned request contains a UUID rehearsal identity and a
bounded release timestamp no more than 30 minutes ahead. The controller fetches the current
official event, deadline and fixtures, derives the event code, and atomically writes both the
diagnostic generation and an immutable `non_postable_rehearsal` binding. The binding retains the
real official deadline separately from the synthetic worker execution window.

This is not a production timing override. Normal generations still use `T=D-2h` and `[T,T+5m]`
unchanged. Assignment, release, handoff and candidate validation all re-fetch official FPL and
compare event/deadline with the immutable rehearsal binding. The worker still gets one bounded
five-minute acquisition window and the ordinary immutable handoff contract.

Independent publication fences are deliberately redundant:

- rehearsal state uses only diagnostic destination `1`, never FPLBotTest;
- the binding is persisted transactionally with the generation in `captain-state`;
- `claim_post` rejects a rehearsal generation before creating any posting attempt;
- the publisher independently rejects any generation carrying a rehearsal binding even if its
  server-side enablement were accidentally changed;
- the no-post controller has no X client, credentials, OAuth access, publisher invocation route or
  arbitrary tweet endpoint;
- diagnostic candidate audits are schema 2, explicitly `postable=false` and
  `purpose=non_postable_rehearsal`.

The controlled procedure keeps the recurring planner paused, removes obsolete prior-event tasks,
creates one rehearsal while the queue is paused, reviews the deterministic task set, resumes the
queue only for that set, and uses the planner-authenticated
`POST /captain/control/reconcile` endpoint for bounded asynchronous VM reconciliation without
planning another generation. The temporary reconciler is removed and the queue paused again after
authoritative Compute termination. It never turns a rehearsal candidate into a publication
instruction.

## Full cloud/Windows rehearsal attempts: 19 September

The first live rehearsal exposed a release-boundary race without opening the browser. The worker's
poll at `2026-09-19T18:50:26.519Z` was classified stale immediately before the durable release
handler completed. Generation `d698cdf5-7136-56ab-864d-003aa69c21e2` therefore failed closed with
no acquisition claim, handoff, candidate or posting attempt. Compute independently confirmed
termination at `18:55:53.285Z`.

Checkpoint `b63809fdb5669fa90459689a4e2a42444acfa2db` corrects only that classification: a fresh,
current, in-window `WARMING` or `READY` generation remains pending until the release task's durable
`RELEASED` transition is visible. The atomic acquisition claim is still impossible before that
transition. The correction passed 18 focused controller tests, 674 Captain tests and the complete
1,479-test suite, plus Ruff lint/format, dependency-integrity and diff checks. Controller revision
`captain-controller-00003-cwp` serves the commit-tagged image digest
`sha256:c135e468764ca694479c5eec8d61039bfb0afcf1b4bc7e8212d8cdd52044ab09`.

The second immutable rehearsal used generation `bf8f1dc6-1986-596a-bda2-677f8df33583`, assignment
`6db3b19f-721b-577a-9ccd-a0db077b87b8`, attempt
`2e64a2c9-2cca-4c13-b2f6-71d5fcf9cdd0`, Event 6 / `GW6`, and the fresh official deadline
`2026-10-10T11:00:00+01:00`. Its synthetic non-postable release was
`2026-09-19T19:25:12Z`; the official deadline remained separately bound and authoritative.

Cloud Tasks delivered warmup, and the durable START used lease
`b8de7033-b661-5200-9f1d-0764a2b75f0a`, reservation
`8078ccfd-7824-5cc1-bad4-421fe24ca747`, stable Compute request ID
`a7ba3b36-0ac0-59d0-9324-1473e47688a5`, and provider operation
`operation-1789845216356-65bdad052e759-4867ec21-61fb19a0`. The unattended Windows worker obtained
its assignment at `19:15:38.670Z`; all pre-release polls returned 202. The release task committed at
`19:25:12.454Z`, and the worker received its valid grant at `19:25:16.456Z`.

The worker then reported `acquisition_failed` at `19:25:16.974Z`, before a handoff or session-health
observation existed. The current deliberately redacted Windows audit does not retain the underlying
safe browser error category, so authentication health, row counts and Captain selections cannot be
claimed. No candidate or `captain_no_post_audits` record was produced. This is the remaining
Windows-local diagnostic blocker; no blind retry was attempted.

Cleanup used STOP reservation `88b3c6a9-790c-5d6f-8de5-4c9479a6ad3f`, stable request ID
`9ca33df9-87eb-56b5-9842-93f7dec8801e`, and provider operation
`operation-1789845927954-65bdafabd067b-891d2067-cb9b865e`. Compute reported TERMINATED at
`19:25:47.952Z`; the durable VM lease and operations then cleared. Both rehearsal posting records
contain zero attempts. The publisher remained revision `captain-publisher-00001-tkl` with
`X_POSTING_ENABLED=false`, received no POST request, and was never given a rehearsal candidate.
The recurring Captain planner and queue were returned to PAUSED, temporary tasks/jobs were removed,
and Good Luck remained revision `fpl-bot-00007-c4n` and healthy.

### Windows diagnostic boundary and next-run gate

The installed startup action loads source directly from the fixed private directory
`CaptainCloudWorker01`; deploying a controller image does not update that directory. Registration
evidence ties the prepared worker to checkpoint `0d195cef76520e714411338e772893c54788f0c9`, but that
legacy bundle predates an embedded build manifest. Its exact on-disk source version therefore cannot
be re-proven while the VM remains terminated. The original preparation script deliberately refuses
to overwrite an existing application directory, and there is no reviewed non-interactive updater.
Consequently another boot would still execute the same generic-diagnostic worker and would be a
blind retry. No further live rehearsal is permitted until the diagnostic-capable package is installed
and its immutable build identity is verified.

New worker bundles contain `worker-build.json`, binding the ZIP to its 40-character Git commit. Audit
schema 2 records that build ID and, on acquisition failure, one allowlisted diagnostic object. It does
not contain exception messages, paths, environment values, cookie/session data, passwords, bearer
tokens or X/OAuth material. The diagnostic distinguishes:

- worker configuration, profile validation and exclusive profile ownership;
- Chrome executable resolution, Playwright/runtime initialization and persistent-context launch;
- a created browser that exits before first-page readiness;
- navigation begun versus FPL Review/session/table processing;
- authentication required, bounded timeout, filesystem denial and sanitized unexpected failure.

The safe evidence is limited to the typed code/stage, four booleans (executable resolved, profile
directory existed, browser process created, navigation began), bounded duration, and an allowlisted
exception class. The temporary boot observer independently validates this schema and copies only
those fields to guest attributes. Invalid or unknown diagnostic data makes the observer fail closed.
The controller still receives only the existing typed acquisition failure and follows the same
terminal cleanup path; the diagnostic does not create a handoff, candidate or posting authority.

Updating the fixed application directory currently requires a controlled `captaintrial` maintenance
session: verify the new ZIP SHA256, ensure the task is not running, Chrome is closed and all profile
lifecycle markers are absent, then install through a separately reviewed replacement procedure. The
browser profile must not be copied, replaced or inspected. A SYSTEM startup-script overlay is not an
accepted update mechanism because it would change ownership/DPAPI assumptions. If the task action or
registration must change, the existing password must again be supplied only in Task Scheduler's
native credential dialog. This maintenance step, followed by one cold boot with no RDP, is the next
safe diagnostic task; it is not performed by this checkpoint.

### Reviewed same-user diagnostic worker replacement

The exact diagnostic bundle built from `e27331ef7cda4dbf1a3d8747deb09dfade8bfc49` is
`worker-e27331ef7cda4dbf1a3d8747deb09dfade8bfc49.zip`, with SHA256
`437AF8CA4AD9E3EE8272BEEFAA2A82A28AC3D3D08CE092AE2EA271BF1EC53AF0`. Its manifest is exactly
schema 1 plus that commit. Its import closure contains only the worker, browser, transport and pure
Captain domain modules; it contains no publisher/X module, credentials, browser profile or secret
material.

The installed layout has three separate lifetimes:

- `CaptainCloudWorker01/fpl_bot`, the preparation helper and `worker-build.json` are replaceable
  application files;
- `CaptainCloudWorker01/worker-config.json`, `audits/` and the retained Task XML are private
  persistent worker state;
- `CaptainControlledTrial01/venv`, the registered Task and `FPLReviewCaptainProfile` are external to
  the application directory and are never moved by a package replacement.

`replace-captain-worker-remote.ps1` (SHA256
`98FB0CB603FFEA74B6ABCF4F0AEA417D43A800E000535675AB9A5697AD0AA2CA`) performs the reviewed
replacement under the existing
`captaintrial` identity. It requires the expected ZIP hash and commit, validates the exact worker-only
archive closure, the current task contract, private config, owner/ACL, zero worker/Chrome processes
and absence of lifecycle markers. It stages under a private sibling directory, copies the existing
config byte-for-byte and audit tree hash-for-hash, optionally preserves the Task XML, then renames the
old root to the single `CaptainCloudWorker01.rollback` directory and the staged root into place. If
the second rename or installed validation fails, it restores the old root; it never deletes either
copy. It verifies the installed manifest, imports the worker without running it, checks dependencies,
and rechecks the unchanged Task.

The Task does not need temporary disablement during this maintenance procedure. It has one boot
trigger, demand start is disabled, and the replacement refuses to proceed until the startup instance
has completed and the Task is `Ready`. The queue and planner must remain paused, so the legacy worker
can only take its already-proven no-assignment exit during the maintenance boot. The script never
edits/re-registers the password-backed Task and therefore does not request or handle the password.
It also never copies, moves, hashes or opens browser-profile contents. After verification, the Task
remains enabled with its original password-backed principal, boot trigger and fixed application path.

Diagnostic-checkpoint validation: 84 worker/observer-focused tests, 693 Captain tests and the full
1,501-test suite passed. Ruff lint and format checks, dependency integrity, static worker import
closure and Git diff checks also passed.

Earlier infrastructure-checkpoint validation: nine offline Windows helper tests, 46 focused
runtime/HTTP/adapter/helper tests, 629 Captain tests and 1,338 full-suite tests
passed. Ruff lint, formatting, dependency integrity and diff checks passed.
The normal tests use fake CLI/browser/transport inputs and no live Google calls.

## Corrected-worker installation and completed rehearsal: 20 September

The first diagnostic-worker run used build
`e27331ef7cda4dbf1a3d8747deb09dfade8bfc49`. It failed in two milliseconds at
`profile_validation` with `profile_missing_or_inaccessible`, despite the dedicated profile existing,
no Chrome process or lock marker being present, and no browser process or navigation having begun.
The profile was not changed. The failure exposed a repository-boundary defect rather than an FPL
Review authentication problem: the flattened worker install made
`Path(__file__).resolve().parents[2]` resolve to the common
`C:\Users\captaintrial\AppData\Local\FPLBot` directory, falsely treating the sibling
`FPLReviewCaptainProfile` as if it were inside a source repository.

Checkpoint `55b5e6aef912b23a0c22e62f595560e2c4d45243` replaces that positional-parent
inference. A source repository is now recognized only by explicit repository markers (`.git` and
`pyproject.toml`), while the installed worker application directory is fenced independently. The
dedicated-profile and provenance-marker requirements remain unchanged. Tests cover source-checkout
profiles inside and outside the repository, the flattened sibling-profile layout, profiles inside
the installed application directory, provenance validation order, unchanged lock checks and the
absence of browser launch after a genuine path failure.

The corrected immutable worker was built from
`3a71ad6e78f8f54789b51be8bdbaef1b2b0222cc`. Its reviewed ZIP SHA256 was
`A17609FA7EAED36BE29BF2CF81D373253CCA52D218478CE3CFE9CC2C8B90096F`; the
reviewed replacement script SHA256 was
`EA752E07F80A7A6EF8B8F29862DF6BBDAD16CB8E1B873D762B6831D0BA23BF19`.
The worker-only closure contained 19 modules and no publisher/X module, credential or profile data.
Checkpoint `3a71ad6e78f8f54789b51be8bdbaef1b2b0222cc` also makes an occupied legacy
rollback slot fail-safe: the hash-verified legacy rollback is preserved under its own immutable
name before the fixed rollback slot is used. No rollback state is deleted or overwritten.

The same-user `captaintrial` replacement reported `replacement_verified`, previous build
`e27331ef7cda4dbf1a3d8747deb09dfade8bfc49`, installed build `3a71ad6...`, both the
immediate and legacy rollback copies retained, and the browser profile unmodified. The private
configuration and nine audit files were preserved. The existing password-backed Task remained
enabled with its 30-second boot delay; it was not re-registered and did not require temporary
disablement. After verification, the operator signed out, closed RDP and terminated the
user-managed IAP tunnel. The rehearsal then used a separate cold boot with no RDP connection.

### Successful immutable non-postable rehearsal

Rehearsal `c1a6aa7e-b7dd-47e7-a024-a090a4126e1c` created fresh generation
`647883ac-ab89-5c49-9ed1-6c70e7ec40d1`, assignment
`aacebf83-8048-52df-8cc6-9c3a17ba40b5` and acquisition attempt
`764a44ad-0aa1-4e7c-aa56-dbcea3a91339`. Fresh official FPL data identified Event 6 / `GW6`
with deadline `2026-10-10T10:00:00Z`. That official deadline remained authoritative and separate
from the bounded synthetic execution window. The immutable purpose was
`non_postable_rehearsal`, and the destination was the independent diagnostic ID `1`.

The explicit warmup task delivered at `2026-09-20T16:31:02Z`. Durable START used lease
`6506830c-d2d8-5a6f-be6d-bf96143044cf`, reservation
`722ad169-b953-559a-9a0d-229e38f80f39`, stable Compute request ID
`ff3ebfda-6b1a-5897-a56d-9c722cfa9d97`, and provider operation
`operation-1789921864485-65beca8e88964-7c70758c-85517d4b`. Compute confirmed RUNNING and
the generation became READY at `16:33:10.153733Z`. The password-backed unattended worker obtained
its assignment at `16:33:16.901993Z`; every pre-release poll returned HTTP 202.

The release task committed the durable RELEASED transition at `16:46:02.566666Z`. The worker
received HTTP 200 at `16:46:03.660660Z`, and the atomic acquisition claim was recorded at
`16:46:03.543502Z`. Profile boundary and provenance validation therefore passed in the corrected
build; Chrome resolution, persistent browser launch and navigation were reached. FPL Review was
authenticated, and acquisition ran from `16:46:03.607006Z` through `16:46:44.637971Z`. The handoff
contained 50 raw, 50 genuine and 50 extracted rows, preserved source ordinals and reported complete
cleanup. Safe session evidence records `authenticated` at `16:46:43.712663Z`; real cookie expiry
and refresh remain unknown, and no cookie values were read or persisted.

The authenticated worker handoff returned HTTP 200 at `16:46:48.093987Z`. Fresh cloud validation
then accepted the immutable handoff digest
`89c9ea434fcf188a5849e7410e29982bbc9c51518384597ead66e1a120616f53` and persisted candidate
digest `e3083e1c3e50331750c0169a854bf4d6f225a61893b9b7f648c434feca95d438` at
`16:46:49.131951Z`. The official selection was:

- B.Fernandes, 6.64, TOT (H), fresh ownership 39.6%;
- Saka, 6.32, LEE (H), fresh ownership 13.3%;
- Palmer, 6.30, BOU (H), fresh ownership 26.7%;
- Differential Havertz, 5.07, LEE (H), fresh ownership 8.5%.

The canonical 204-weighted-character non-postable candidate was:

```text
🧢 CAPTAIN PICKS 🧢

#GW6 Projected Points:

🥇 B.Fernandes v TOT (H) - 6.64
🥈 Saka v LEE (H) - 6.32
🥉 Palmer v BOU (H) - 6.30

Differential:

🐴 Havertz v LEE (H) - 5.07

Good luck!

#FPL #FPLCommunity
```

The candidate is permanently marked `postable=false` through the rehearsal binding and diagnostic
destination. Its event-level posting record has zero attempts; there is no claim, `write_started`
or X Post ID. The publisher remained revision `captain-publisher-00001-tkl` with
`X_POSTING_ENABLED=false` and received zero requests during the rehearsal. The controller/worker
path has no X or shared-OAuth dependency, so the shared OAuth authority was not accessed or
refreshed.

Cleanup reserved exactly one STOP using reservation
`0aa90b66-9344-581d-a635-949b3b3e9363`, stable request ID
`58e1f4c2-e76f-51d9-85ab-2df48e736917`, and provider operation
`operation-1789922806791-65bece112fc2b-9b680be0-44315ec6`. Acknowledgement left the VM-use lease
in `stopping`; it did not release ownership. Compute independently recorded TERMINATED at
`2026-09-20T16:47:13.587Z`, after which reconciliation removed the VM-use lease and terminalised the
operation. The queue is PAUSED and empty, the planner is PAUSED, and the temporary rehearsal job is
deleted. The four-hour Compute STOP safeguard remains configured. Good Luck remains healthy at
revision `fpl-bot-00007-c4n` and was not changed.

This completes the previously outstanding non-postable chain: authenticated Cloud Tasks,
crash-safe Compute START, cold password-backed Windows execution without RDP, metadata-authenticated
assignment/release, real FPL Review acquisition, immutable handoff, fresh official-FPL candidate
validation, fenced STOP and independently confirmed termination. It does not prove an X write. The
next step, under a separate review, is a controlled FPLBotTest-only publisher proof with the existing
fresh validation, event-level idempotency, shared-OAuth and uncertainty safeguards.
