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

## Private worker registration: next required interaction

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

The password/private Windows interaction is the current pause point. Full Tasks
delivery, Compute reservations/start/stop reconciliation, Windows unattended
transport, fresh projection handoff and canonical candidate remain unproven by
this partial cloud rehearsal. X publisher integration is still excluded.
