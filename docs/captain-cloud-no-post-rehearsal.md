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
