# GW6 integrated production readiness

This checkpoint prepares, but does not execute, the first integrated production run. Both
production posting gates remain false, all production queues and recurring controls remain paused,
and the Captain VM remains terminated.

## Authoritative target reviewed on 2026-09-26

Fresh official FPL bootstrap and event fixtures selected event 6 with deadline
`2026-10-10T10:00:00Z`. All 20 current teams have one event fixture, so the deterministic event
code is `GW6`.

| Boundary | UTC | Europe/London |
| --- | --- | --- |
| Captain warmup | 2026-10-10 07:45 | 08:45 BST |
| Captain T | 2026-10-10 08:00 | 09:00 BST |
| Captain L (inclusive) | 2026-10-10 08:05 | 09:05 BST |
| Good Luck deadline | 2026-10-10 10:00 | 11:00 BST |

These values are review evidence, not runtime constants. Every enablement command requires an
expected event/deadline and calls `fpl_bot.production_readiness` to compare them with a new official
FPL fetch. Any difference stops the procedure for explicit re-planning.

## Captain production route

The existing `captain-controller` service remains the worker/controller origin, so the proven
password-backed worker and metadata-token audience do not change. The reviewed production
controller image changes its immutable destination from diagnostic `1` to production user
`1249335464571650048` and disables the rehearsal endpoint.

After a handoff, the existing durable `PUBLISH` intent delivers to the controller. The controller:

1. revalidates and persists the immutable candidate;
2. derives a versioned `PublicationInstruction` containing only generation, attempt, handoff and
   candidate digests plus a deterministic claim ID;
3. creates/reconciles one deterministic Cloud Task addressed to
   `captain-production-publisher` with OIDC from `captain-prod-pub-invoker`;
4. acknowledges the controller task only after exact task creation/reconciliation succeeds.

The worker cannot invoke the publisher and has no X credentials. The controller never accepts or
routes arbitrary tweet text. Rehearsal bindings and destination `1` are rejected before publisher
routing. The publisher independently reconstructs the candidate, repeats fresh FPL/state/time/X
identity checks, and retains the event-level posting key `(destination, captain, event ID)`.

## FPLBotTest Captain write proof decision

A separate Captain FPLBotTest write is not a prerequisite for GW6. There is no existing safe way
to create one outside the genuine `[T,L]` window: doing so would require a timing bypass or
proof-only posting authority. The official X create transport is the same guarded `XApiClient`
already used successfully by Good Luck; production `/2/users/me`, real schema-2 refresh, the full
Captain non-postable browser/candidate chain, and publisher state transitions are independently
proven.

What remains unproven until the first real Captain write is the final live composition of the
Captain-specific task delivery, durable `write_started`, one `/2/tweets` response, and X Post ID
persistence. The deterministic task, duplicate, crash and ambiguous-response tests cover those
boundaries without weakening production semantics.

## Arming order (do not execute from this checkpoint)

1. Confirm a clean reviewed Git commit and use its immutable production-controller, publisher and
   Good Luck image tags.
2. Run `fpl-bot-production-target --expected-event-id <id> --expected-deadline-utc <UTC>` and stop
   on any mismatch.
3. Confirm `production-shared-x-oauth` is schema 2, committed, lease-clear and not uncertain; verify
   `/2/users/me` equals `1249335464571650048` without intentionally forcing refresh.
4. Read `captain-state` and `production-good-luck-state` through their runtime identities. Require
   no success, claim, `write_started`, uncertainty or conflicting current generation for the event.
5. Require Captain queue/planner and production Good Luck queue/Scheduler paused, queues empty,
   the VM terminated, and the password-backed worker build/profile prerequisites unchanged.
6. Run `provision-captain-production-routing.ps1 -Apply`; its only mutation is the controller's
   `actAs` grant on the dedicated production publisher invoker.
7. Deploy the reviewed production controller image with
   `deploy-captain-production-controller.ps1`. Controls remain paused and the publisher remains
   disabled.
8. Deploy the existing reviewed production Captain publisher image with
   `deploy-captain-production-publisher.ps1 -EnablePosting -ExpectedEventId <id>
   -ExpectedDeadlineUtc <UTC>`. Reconfirm its immutable destination and sole invoker.
9. Deploy the reviewed production Good Luck image with
   `deploy-production-good-luck.ps1 -EnablePosting -ExpectedEventId <id>
   -ExpectedDeadlineUtc <UTC>`. Its queue and Scheduler remain paused.
10. Re-run fresh target, identity, OAuth, state and FPLBotTest-isolation checks after all revisions
    are ready.
11. Resume `captain-orchestration`, then `captain-planner`; inspect deterministic warmup, release,
    publish and cleanup intents for the reviewed generation before their execution times.
12. Resume `production-good-luck-deadline`, then `good-luck-production-checker`; verify its first
    checker can only arm the exact official-deadline task on the same London calendar day.
13. Confirm test Good Luck, test Captain publisher and test OAuth configuration/revisions did not
    change. Record revision names, task identities and audit timestamps.

Any failed check stops the sequence; it never causes a partial bypass or state reset.

## First-run monitoring

Captain monitoring follows durable evidence for warmup/START reservation and Compute request ID,
VM boot, worker authentication, release, acquisition/handoff, candidate digest, publisher task,
claim, `write_started`, X outcome/Post ID, cleanup/STOP and independently confirmed `TERMINATED`.

Good Luck monitoring records the 06:00 Europe/London checker, deterministic deadline task, exact
deadline delivery, claim, posting attempt, X outcome and Post ID.

OAuth monitoring records authority revision, current version pointer, attempt state, lease and any
uncertainty. Monitoring is observational: every component fails closed without a human watcher.

## Emergency fail-closed procedure

Run `deploy/emergency-disable-production.ps1 -Apply`. It disables both server-side posting gates,
pauses both recurring planners and the Good Luck queue, and preserves all business/OAuth state. If
the Captain VM is active it deliberately leaves the Captain queue available only for the existing
durable fenced cleanup; reconcile that cleanup to authoritative `TERMINATED` and rerun the command
to pause the Captain queue. Never raw-stop an unfenced newer lease, reset a posting record, or
change OAuth authority to perform an emergency disable.
