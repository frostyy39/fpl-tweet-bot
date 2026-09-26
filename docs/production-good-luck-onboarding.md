# Disabled production Good Luck boundary

This deployment reuses the proven Good Luck planner, deadline revalidation, event classification,
canonical tweet rendering and posting-state code. It changes only configuration and infrastructure
boundaries. It creates no post and remains disabled until a separate GW6 arming review.

## Fixed resources

| Purpose | Resource |
| --- | --- |
| Cloud Run service | `good-luck-production` in `europe-west1` |
| Runtime identity | `good-luck-production-runtime@fpl-frosty-bot-v1.iam.gserviceaccount.com` |
| Sole task/Scheduler invoker | `good-luck-production-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com` |
| Business state | `production-good-luck-state` in `europe-west2` |
| Deadline queue | `production-good-luck-deadline` in `europe-west2` |
| Daily checker | `good-luck-production-checker`, `0 6 * * *`, `Europe/London` |
| OAuth metadata | `production-shared-x-oauth` in `europe-west2` |
| OAuth token state | `production-x-oauth-token-state` |
| X destination | immutable user ID `1249335464571650048` |

The production Good Luck and Captain consumers share exactly one schema-2 production OAuth
authority. They never copy or pin its token version. Their posting/idempotency state remains
separate: Good Luck uses `production-good-luck-state`; Captain uses `captain-state`.

## Disabled application gate

`fpl_bot.production_good_luck_runtime` validates every fixed resource and destination before the
shared application graph can be composed. While `X_POSTING_ENABLED=false`, its checker, preflight
and deadline routes return only `{"status":"disabled"}`. Firestore, Cloud Tasks, OAuth and X
adapters are not constructed, so an accidental invocation cannot create a task, claim or write.

The production queue and Scheduler are provisioned separately and left paused. The existing
FPLBotTest service, `(default)` state, queue, Scheduler, test OAuth authority and destination are
not mutated.

## Disabled deployment evidence (2026-09-26)

The disabled production boundary was deployed as Cloud Run revision
`good-luck-production-00001-mmn` in `europe-west1`, using image digest
`sha256:99299d18a7bda92427736fc66abc69d02abc4464de389139954d16ae51e2c010` and the dedicated
runtime identity. The production queue was empty and `PAUSED`; the production Scheduler was
`PAUSED` with the reviewed `0 6 * * *` `Europe/London` cadence.

Temporary jobs exercised the effective IAM and invocation boundaries and were deleted after their
logs were retained:

- the production runtime created/read/deleted an isolated probe document in
  `production-good-luck-state`, read the production OAuth metadata, accessed the referenced
  production secret version without emitting it, and fetched fresh official FPL event 6 with its
  2026-10-10T10:00:00Z deadline and 10 fixtures;
- the same identity was denied `(default)`, `shared-x-oauth`, `captain-state`, the test token
  secret, and Compute instance inspection;
- the existing FPLBotTest runtime identity was denied `production-good-luck-state`,
  `production-shared-x-oauth`, and the production token secret;
- anonymous and Captain-worker calls were denied by Cloud Run, while the sole production invoker
  received `{"status":"disabled"}` from all three application routes;
- disabled calls created no task, posting record, claim, `write_started` state, or X write.

A read-only `/2/users/me` proof returned the immutable production identity
`1249335464571650048`. The original access token had genuinely expired, so the proof performed one
coordinated refresh through the shared schema-2 production authority. Authority revision advanced
from 1 to 2 and the authoritative Secret Manager version from 1 to 2 (previous version 1); the
attempt ended `committed`, the lease cleared, and no uncertainty remained. Token values were never
logged. Both production Good Luck and production Captain dynamically reference the same database
and secret ID rather than pinning either revision or version.

## Future arming review

Before GW6, a separate review must verify the official event/deadline, empty production queue,
unclaimed production event state, healthy shared production OAuth authority, immutable X identity,
and enabled configuration for both production services. The review must then enable only the
intended production Good Luck and Captain paths and retain the independent FPLBotTest environment.
