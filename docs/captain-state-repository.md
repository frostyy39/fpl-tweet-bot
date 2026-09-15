# Captain milestone 2: state and atomic repository contracts

This milestone adds no cloud, HTTP, browser, X or scheduling implementation.
Milestone 1 contracts/timing and all Good Luck code are unchanged. The in-memory
repository is a thread-safe reference for local tests, **not production persistence**.
There is no physical Firestore database/collection choice. Future adapters must
implement these Captain-only ports without access to Good Luck state.

## State and atomicity

Every repository operation is linearizable. Compound changes must commit together
or not at all. Immutable reads cannot mutate the repository. Caller-supplied UTC
times and UUIDs make retries deterministic; there are no internal clocks/UUID
generators. Generation and posting histories retain their transitions. A repository
adapter must validate deserialized state and preserve these invariants.

`CaptainRepository` composes narrow generation/acquisition, posting, outbox,
VM-lease and session-evidence ports. They are views of the **same transactional
store**, not independent implementations that weaken cross-record atomicity.

`Mutation.applied=False` means an observation/replay, never permission to repeat
an external side effect. Callers must distinguish this from a newly granted claim.
Lost responses favor safety: do not infer that an operation failed to commit.

### Generations and acquisitions

Normal lifecycle:

`planned -> warming -> ready -> released -> acquiring -> accepted`

Active states can terminate as `failed`, `authentication_required`, or `missed`.
Missed requires strictly after L; warming/readiness may complete late, but no earlier
than W or later than L. Release/acquisition remain governed by inclusive [T,L].
Acquiring/accepted transitions are only available through atomic claim/accept, not
the generic transition operation. Accepted cannot be reset to an active state.

`plan` binds a PostKey to an immutable Milestone 1 assignment and a monotonically
increasing generation version. The expected-current UUID is a compare-and-swap
guard. Reusing a generation UUID with different contents conflicts. An identical
creation replay returns the stored snapshot without reviving it. Assignment UUIDs
cannot be reused for another generation.

Replacing a generation atomically fences old work, records `cancelled_stale`,
cancels old outbox intents, and cancels any still-active acquisition. Old handoffs,
release operations, posting starts and dispatch acknowledgements fail closed.
Historical accepted payloads and posting outcomes are retained. Current official
FPL revalidation remains a later controller responsibility; this store cannot know
about a deadline change until the controller submits a new plan.

V1 allows **one acquisition attempt per generation**, with no automatic takeover
or retry after a crashed/failed browser claim. Replayed identical claims report
the existing attempt without authorizing another browser run. Attempts retain
their IDs, claim/update timestamps, terminal classification and accepted handoff.
Recovery must first establish that the previous worker is stopped; any future
multi-attempt policy requires explicit review rather than silently stealing a lease.

`accept` requires the intended active claim, acquisition start at/after its claim,
the current immutable assignment, and Milestone 1 eligibility. It atomically stores
the payload, marks acceptance, and creates the publish intent. Same attempt/digest
is a read-only replay (even after L, but never after generation supersession).
Different digest conflicts. Digest is content identity, not authentication. No
payload is accepted for posting based on session-health evidence alone.

### Posting barrier

The key is **destination numeric X user ID + `captain` + FPL event ID**. It never
contains deadline/generation. Missing record means unclaimed. Each immutable claim
identity has a retained transition history:

`claimed -> write_started -> succeeded | uncertain`

`claimed -> failed_before_write` permits a new claim with a new UUID. There is no
automatic claim expiry/reclamation. A recovery operation may mark a claim failed
before write only if it is still claimed; a concurrent old worker must still win
`start_write` before doing anything external and will be rejected after failure.

`start_write` checks current generation, accepted handoff and [T,L] again. Only its
first successful mutation can proceed toward later guarded X integration. Replays
do not authorize X. Persisting `write_started` then crashing **before** the actual
request is deliberately treated conservatively, just like a potentially sent request.

Neither write_started nor uncertain can return to retryable failure or obtain a
new automatic claim, including after deadline replanning. A positive, verified
outcome can move uncertain to succeeded; there is no automatic reconciliation or
"not found, retry" operation. Post IDs are immutable. Successful events are permanently
blocked from another claim for that destination/type, regardless of later generation.
Outcome recording remains permitted after L or supersession so a late X response
cannot be discarded. Fresh FPL, X identity checks and exact before-request timing
remain mandatory responsibilities of the future guarded posting integration.

### Durable outbox

Plan atomically creates warmup (W), release (T), cleanup/watchdog (L) intents.
Acceptance atomically creates publish-at-acceptance intent; acquisition must not
have begun before T. Intents carry only generation, kind, deterministic identity,
scheduled time and pending/dispatched/cancelled status. No arbitrary URL or task
payload is stored. Cleanup timing is an intent, not authority to kill a process:
the later controller must account for the inclusive L boundary and in-flight work.

Reconciliation enumerates pending intents, creates the external task using that
identity, verifies matching create/already-exists, then acknowledges. A crash before
ack leaves the intent pending; retry uses the same identity. Acknowledgement means
external creation, not execution. If supersession races external creation, the task
may exist but its handler must reject the stale generation. Task-name deduplication
alone is never the posting/acquisition guard. No external reconciliation code exists here.

### VM-use fence

One exclusive lease binds a caller-supplied token to a generation. It never expires
automatically and cannot be stolen by a new generation. A superseded generation
may clean up **only its own still-active lease**, without changing generation state.

`begin_cleanup` atomically moves that lease to `stopping`, blocking any new owner
before the external stop is issued. `complete_cleanup` releases it only after the
future controller verifies completion of its stop operation and actual termination.
Checking a token then releasing it before stopping would be unsafe and is forbidden.
Uncertain/in-flight stop retains the fence. Retired-token replay is a no-op; an old
cleanup cannot fence or stop a newer owner. Retired tokens can never be reused.

Later integration must serialize external start/stop operations and reconcile their
operation identities: **no old outstanding start/stop request may survive lease
release**. These pure state methods do not magically make provider requests atomic.
The VM fence and posting barrier intentionally favor safety over automatic recovery.

### Future session-health evidence

Typed, append-only observations allow authentication classification, observation
time, unknown/session/fixed expiry classification and optional expiry timestamp,
next target, observed/not-observed/unknown legitimate refresh, and manual-reauth flag.
There are no cookie names/values, URLs, credential fields or free-form metadata maps.
An observation timestamp cannot be overwritten with different contents.

The agreed policy remains: use genuine acquisition as the primary health opportunity;
standalone checks are evidence-driven backstops. Warn early for foreseeable risk.
Expiry is not proof of future validity. Do not alter cookies, automate login/CAPTCHA,
copy sessions or expose secrets. Browser inspection and warning logic are deferred.

## Before the next milestone

Implement authenticated controller inputs, fresh FPL validation, durable adapter
transactions, outbox reconciliation and external-operation fencing in separately
reviewed steps. Decide physical IAM/storage isolation later. No interface exposes
Good Luck repositories, and no Captain consumer should receive one. Contract tests
should be reused against each future persistence adapter, including concurrent races.
