# GW7 Captain release-rejection diagnostics (not deployed)

GW6's original Windows audit (worker build `3a71ad6e78f8f54789b51be8bdbaef1b2b0222cc`)
ended `release_rejected` at `2026-10-10T08:00:04.315759Z`, before browser launch.
The controller had returned HTTP 200 and durably claimed the attempt at
`08:00:04.144328Z`. Neither the HTTP response body nor the individual worker
predicate was recorded, so subsecond clock skew is a hypothesis, not a finding.

This checkpoint changes **diagnostics only**. The worker retains the same
assignment/run binding, inclusive `[T,T+5 minutes]`, and `issued_at <= local_now`
checks in the same order. Browser acquisition remains unreachable after any
failed check. The new worker audit is schema 3; schema-2 historical audits are
untouched. Its `release_rejection` object contains only enum values and bounded
millisecond measurements. No raw grant, timestamp pair, URL, exception text,
browser state, credential or token is recorded.

The controller adds `server_sent_at` to an otherwise unchanged HTTP 200 release
response. The worker records the local request-start and response-receipt times.
Together these produce a bounded interval for `server_clock - worker_clock`:

- Entire interval positive: worker behind controller.
- Entire interval negative: worker ahead of controller.
- Interval crosses zero: indeterminate; network latency alone is not called skew.
- Older controller without `server_sent_at`: unknown, with no policy fallback.

Offsets are capped at ±60,000 ms. The separate `issued_minus_worker_ms` shows
whether the grant appeared slightly in the worker's future. Window positions
distinguish genuine before-T and after-L rejection. Every rejection has a typed
subcode, including malformed/revoked responses; malformed transport still has
the original high-level `controller_unavailable` status.

An authenticated worker failure report may create one immutable
`captain_v1_release_failures` Firestore record keyed by run ID. The transaction
checks the current assignment and, where present, its acquisition owner.
Identical reports replay without rewriting the timestamp; conflicting reports
fail closed. The record is worker-reported evidence, **never posting authority**.
It cannot create a candidate, task or posting claim. Reports without a current
assignment (including a locally reused run ID) remain in the private audit only.

## Behavioural review, not implemented

The strict `issued_at > local_now` comparison can reject a valid controller
claim for a subsecond clock offset. A controller claim inside `[T,L]` does not
make the Windows clock authoritative. If a later diagnostic confirms that
specific predicate and clock relation, the smallest candidate correction is
to use the controller's durable claim time as the server-authoritative release
decision, while retaining assignment/run binding, fresh official FPL validation,
atomic one-attempt claim, and the worker's independent no-early/out-of-window
checks. Review the exact clock-skew policy and tests separately before changing
behaviour. Do not simply extend `[T,L]` or allow early acquisition.

## Separate deployment and validation gate

Nothing in this checkpoint is deployed. With separate authorization, deploy the
backward-compatible controller first and verify the production publisher stays
disabled and planner/queue paused. Build a new immutable worker-only bundle from
this commit, verify its hash/import closure, and install it under `captaintrial`
using the reviewed same-user atomic replacement, preserving config, audit history,
profile, password and rollback. After RDP sign-out and confirmed TERMINATED,
perform **one** fresh `non_postable_rehearsal` with diagnostic destination `1`,
no X/OAuth access and no production assignment. If it fails, inspect the central
typed record before any retry; independently stop/verify the VM. Only a separate
review may authorize a behavioural fix or production rearming.
