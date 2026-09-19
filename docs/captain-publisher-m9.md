# Captain publisher M9 boundary

The Captain publisher is a separate private Cloud Run service in `europe-west1`. Its runtime
identity is `captain-publisher@fpl-frosty-bot-v1.iam.gserviceaccount.com`; the only workload
invoker is `captain-publisher-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com`.

The service is hard-bound in code and deployment configuration to numeric X identity
`1732468005336907776` (FPLBotTest), `captain-state`, and `shared-x-oauth`. It has no fallback to
`(default)` and no Compute dependency. The request contains only immutable generation, attempt,
handoff, candidate and claim identities. It cannot supply a destination or tweet.

Cloud validation first persists an immutable candidate record in `captain-state`. Publication
reloads that record and its accepted handoff, then reconstructs the candidate twice from fresh
official FPL data: before the event-level claim and after the claim. The reconstructed content
digest must equal the persisted candidate digest. A fresh `/2/users/me` response must identify
FPLBotTest before `write_started` is durably recorded. Only the request that atomically advances
`claimed` to `write_started` may call X.

The M9 deployment sets `X_POSTING_ENABLED=false`. This server-side gate runs before Captain state,
OAuth or X access. No Scheduler or Cloud Task targets the service. Enabling a later reviewed proof
requires a new revision and a deterministic publication task; deployment alone cannot arm it.

After `write_started`, any non-validated X outcome is recorded as `uncertain` and no automatic
second write is allowed. A promotion to `succeeded` requires an exact independently verified X
Post ID; text/time similarity is not evidence.
