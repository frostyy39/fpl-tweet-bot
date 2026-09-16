# Captain milestone 3: planner, deliveries and outbox reconciliation

This is application logic with injected ports and local fakes, not a deployed
service. There are no concrete FPL HTTP, Cloud Tasks, Firestore, Compute Engine,
browser or X adapters here. Existing shared FPL parsers/classification are reused
without modification. Milestone 1/2 contracts and Good Luck behaviour are unchanged.

## Planning and authoritative data

`CaptainController` takes `OfficialFplSource`, `Clock`, `CaptainRepository` and
`VmOperationRepository`. FPL ports have uncached fetch semantics. The clock must
return aware UTC. No implicit machine clock, service client or credential is used.
The real adapter's transport authentication/freshness remain future work.

Each planner invocation fetches official bootstrap data, validates exactly 20
unique teams and reuses chronology-safe event selection. Fixture parsing and
classification supply GW/BGW/DGW/BDGW. Empty/malformed fixture data fails closed.

The rolling horizon is event-based, not a fixed number of London calendar days:

- Plan the nearest unpassed official deadline, even if its Captain window is missed.
- Also plan the first still-open/upcoming Captain window if the nearest has expired.
  Search past multiple expired windows when necessary, without skipping contradictory
  official chronology. This normally creates at most two horizon entries.
- Reconcile changed deadlines of already-known events still present in bootstrap,
  including a deadline moved into the past. If no future event remains, known
  deadline corrections can still be recorded; otherwise an empty horizon is valid.

All arithmetic uses the existing timing policy: D; T=D-2h; W=T-15m; L=T+5m.
London date/offset conversion is for interpretation, never a deadline-day arming
filter. A target or warmup on the previous London date is therefore schedulable.

Stable UUID identities derive from destination, event/code, deadline and previous
generation identity. Unchanged plans reuse their generation. Returning to an old
deadline creates a new generation rather than resurrecting an obsolete UUID.
The pre-classification generation snapshot is retained for compare-and-swap; a
concurrent conflicting plan requires a new fetch/retry, not overwriting its state.
There is no atomic FPL snapshot spanning network calls: deliveries must revalidate.

All selected fixture inputs are validated before writes. Multi-event planning is
not one transaction; interruption after one event is repaired by replaying planning.
Generation replacement uses Milestone 2 atomic supersession, retaining audit and
fencing old intents. Existing event-level posting barriers suppress new work even
when the deadline changes. No posting claims or writes occur in this controller.

## Task outbox

Milestone 2 already creates warmup/release/watchdog intents atomically with planning,
and publish intent with accepted handoff. This controller never creates task intent
independently of those repository operations.

`TaskEnvelope` contains only destination, immutable assignment, task kind and intended
time. Its deterministic name comes from generation + kind. A canonical digest binds
the full envelope; it is content identity, not authentication. The future adapter
must create or confirm the **exact** envelope under that name, never accept an
unverified AlreadyExists as success.

`reconcile_tasks` reads durable pending intents, invokes the injected scheduler's
`ensure`, verifies returned name/digest, rechecks current generation, then records
acknowledgement. Tests simulate lost create responses and partial progress. A retry
confirms the same logical task; it does not create another intent. Supersession
racing creation can leave an external stale task, but its delivery is rejected.
Successful/uncertain/claimed posting records suppress non-cleanup dispatch. Stale
task names alone are never trusted as the duplicate-post guard.

## Delivery eligibility

Every delivery must exactly match the current stored intent/assignment. Warmup,
release and publish fetch/revalidate official event, deadline and classification;
clock and posting barriers are checked again after the potentially slow fetch.
Runtime-ready callbacks and handoff acceptance also revalidate FPL. No controller
method automates acquisition, renders a new tweet or calls X.

- Warmup requests a durable start operation only during [W,T). Replays confirm it.
- Release is possible only during inclusive [T,L] and from ready state. It does not
  itself claim or start browser acquisition; the later worker must use the existing
  acquisition claim and immutable handoff rules.
- Publish delivery checks an already accepted handoff and returns eligibility only.
  This is not a posting authorization; later integration still needs event-level
  claim, fresh FPL/X identity checks and the atomic write-start barrier.
- Before the release boundary, delivery returns `early` with an explicit retry time.
  After L it returns `missed` and marks active generations missed where allowed.
  Accepted audit state is retained. L itself remains eligible.
- Watchdog intent is scheduled at L by Milestone 2; delivery at exactly L returns
  an early result with L+1 microsecond. Stop requests occur strictly after L, or
  via owned-lease reconciliation after accepted handoff/failure/supersession.
- `not_ready` is not success. The future transport must arrange a bounded retry
  within the window. It must not acknowledge early/not-ready delivery as completed.

Cleanup does not depend on FPL availability: it has no posting authority and must
remain usable to stop an owned VM when data acquisition is unavailable. Old task
deliveries fail closed; separate lease reconciliation can clean their old owner.

## Asynchronous VM-operation record

`VmOperationRepository` is an additional durable contract; `InMemoryVmOperations`
is only a thread-safe reference fake. It records a deterministic operation identity
per lease/action and `requested -> in_progress -> completed | failed`. No Compute
Engine request is made. The future adapter must persist/resolve its provider operation
reference against this identity and verify lease/generation before submission.

Start and stop request creation are retry-safe. Stop permanently seals a lease
against a new start record. An already-requested start must reach a confirmed terminal
outcome before stop can enter in-progress state. The later adapter must check this
prerequisite **before issuing stop**, not merely before recording its acknowledgement.
Unknown/time-out responses remain requested/in-progress, never guessed as failed.
Failed means definitive terminal provider failure with no outstanding operation.

A stop acknowledgement is not termination. Successful stop completion requires
explicit termination evidence. `complete_cleanup` refuses release until start has
settled, stop has completed, and termination is confirmed. Failed/ambiguous stop
holds the fence; no automatic fence-stealing or failed-operation retry is provided.
An `applied=False` operation replay never authorizes reissuing a side effect.

`reconcile_vm` uses the durable active lease, independently of task intents. It
repairs a crash between lease acquisition and start intent, and between the stopping
fence and stop intent. It requests cleanup for superseded/terminal generations,
expired windows and failed starts; accepted handoff permits prompt cleanup. It
releases only after settled stop evidence. Thus cancelled old task intents cannot
strand the old lease indefinitely. If recovery finds no start intent after T, it
seals/stops rather than initiating a late warmup. A newer lease is never affected
by stale cleanup tokens or old completion replay.

Provider operations cannot be made atomic merely by these records. Before real
integration, verify provider idempotency and operation lookup semantics, serialise
requests per lease, retain tombstones, and prove no old in-flight request can survive
lease release. Use both repository ports consistently; do not bypass the controller
and directly release cleanup after issuing a stop. No physical storage decision is made.

## Session-health hook and next milestone

Planner results optionally surface a warning from existing typed evidence: manual
reauthentication required, authentication unconfirmed, or expiry at/before the next
known target. Missing evidence means unknown, not confirmed health. No new health
intent, routine browser boot, cookie field, inspection or login behaviour is added.
Real acquisition remains the primary future observation opportunity; standalone
checks are evidence-driven backstops. Expiry is not proof of server-side validity.

Before Milestone 4: define authenticated adapter/handler boundaries, durable physical
storage/IAM, delivery retry mapping, and externally verified VM-operation receipts.
Keep existing in-memory implementations out of production. Re-run these pure tests
against future durable adapters. No browser, VM, X or deployment test occurred here.
