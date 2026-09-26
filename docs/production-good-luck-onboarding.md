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

## Future arming review

Before GW6, a separate review must verify the official event/deadline, empty production queue,
unclaimed production event state, healthy shared production OAuth authority, immutable X identity,
and enabled configuration for both production services. The review must then enable only the
intended production Good Luck and Captain paths and retain the independent FPLBotTest environment.
