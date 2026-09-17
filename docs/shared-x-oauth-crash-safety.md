# Shared X OAuth crash/ambiguity safety — code checkpoint, not deployed

This correction is isolated to shared OAuth coordination. It does not change Good
Luck FPL chronology, scheduling, rendering, posting idempotency, Cloud Tasks,
destination or runtime configuration. Captain M9 remains blocked/unarmed.

## Provider guarantees actually established

The current [official X refresh documentation](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
and [user-token guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/user-access-token)
specify POST `/2/oauth2/token`, `grant_type=refresh_token`, and confidential-client
authentication. They do **not** establish a safe lost-response lookup, an
idempotency key, indefinite old-token validity, or a retry guarantee. We therefore
do not claim every refresh necessarily rotates, or assume R0 remains reusable
after a request may have reached X. No provider call is needed for the tests.

## Authority schema 2

Secret payload schema 1 is unchanged: tokens live only in Secret Manager.
Firestore authority schema 2 retains the exact numeric current/previous secret
version pointers and monotonically increasing credential revision. It adds a
refresh attempt generation and typed last-attempt evidence: immutable attempt ID,
source credential revision/generation, state, UTC claim/dispatch/update timestamps,
exact candidate version when established, and a fixed non-secret classification.
It retains the current/last attempt, not an unbounded attempt-history map.

Lease owner is the random attempt identity. A lease provides mutual exclusion,
not proof of token validity. Claim and dispatch each conditionally transact on
the same authority document; they cannot cross credential generations. A duplicate
dispatch or stale owner cannot dispatch. No X or Secret Manager calls occur inside
Firestore transaction callbacks.

| Durable state | Permitted recovery |
| --- | --- |
| No attempt / committed / operator reauthorized / aborted before dispatch | Read the current explicit version; claim a new refresh only if needed. |
| Claimed, still live | One owner; other consumers fail closed. |
| Claimed, expired, no dispatch barrier | A new claim is safe. The old owner is fenced. |
| Dispatched | Old refresh-token reuse forbidden, even if the HTTP call never actually ran. |
| Persisting | Successful response was obtained; old refresh-token reuse forbidden. |
| Uncertain | Both consumers blocked regardless of lease age. Reconcile exact replacement or recover manually. |

The irreversible dispatch barrier commits **before** entering the HTTP client.
All errors after that barrier, including timeout/reset, HTTP rejection, invalid
response, unusable replacement and persistence failure, are treated conservatively.
We have no documented provider pre-rotation guarantee that would justify resetting
authority for these errors. If recording `uncertain` also fails, retained
`dispatched`/`persisting` still blocks reuse. Expired post-dispatch reads durably
mark the attempt uncertain; expiry never grants a new refresh.

## Crash boundaries

| Crash boundary | Result |
| --- | --- |
| Before claim commit | No provider permission; retry may claim. |
| Claim committed, before dispatch barrier | No HTTP authorized; after expiry, recovery may claim with a new identity. |
| Dispatch commit acknowledgement lost | Do not call HTTP. A committed barrier cannot be released; require recovery. |
| Barrier committed, immediately before/during/after HTTP | Retain no-reuse barrier; abandoned attempt becomes uncertain. No retry of R0. |
| Successful response, before replacement storage | Persisting/uncertain. Lost replacement requires manual reauthorization. |
| Secret version created, before exact candidate binding | Orphan is not inferred from `latest`, listing or timing; require manual recovery. |
| Candidate bound, before authority pointer commit | `reconcile_refresh_attempt()` reads only that exact version and conditionally promotes it. No X call/new secret. |
| Pointer commit acknowledgement lost | Confirm exact version plus revision by authoritative reread; otherwise remain fail closed. |
| Authority committed, before process return/cleanup | Revision, pointer, terminal attempt and lease clearing commit together. Other consumers read the new authority. |

Successful rotation advances credential revision once. Secret Manager storage,
Firestore and X are not a distributed atomic transaction. Failed candidate binding
or unconfirmed pointer updates never permit an old-token refresh. Current and
previous authoritative versions are retained; older confirmed history is disabled
best-effort, without invalidating current authority. Never use an alias as authority.

`recover_uncertain_if_revision()` is a privileged library operation, not a runtime
endpoint or automatic retry. The operator must first quiesce all consumers and
obtain a fresh manually authorized, identity-verified grant via the existing
private DPAPI handoff. It binds exact expected revision/attempt, expected numeric
user and a usable new grant; restoring the uncertain old refresh token is rejected.
The caller's explicit quiescence attestation is not proof of deployment/IAM safety.
The existing reseed CLI does not bypass uncertainty or perform schema migration.

## Multi-consumer and rollout gate

Both Good Luck and Captain must use the same configured authority and the same
corrected coordinator. Each reads the current explicit version when needed; no
independent refresh-token copy is permitted. A lease surrounds only refresh and
durable replacement, never browser acquisition. A distributed store cannot be
used with the development-only CAS fallback or an old coordinator interface.

**Not backward compatible with the deployed Good Luck runtime.** Its schema-1
parser requires exact fields/version; it rejects schema 2. The corrected runtime
likewise rejects schema 1 instead of inventing safe dispatch evidence. Leaving an
old consumer running against schema 1 retains the unsafe expiry behavior. Do not
deploy Captain sharing credentials, or mutate the live authority, while that
consumer remains enabled. Never rewrite a schema-2 uncertain record as schema 1
or roll back an old binary against migrated state.

Smallest separately reviewed coordinated rollout (not authorized/executed here):

1. Preserve the imminent GW5 Good Luck deployment unchanged; Captain stays disabled.
   The current refresh risk remains, but this checkpoint introduces no live risk.
2. After the deadline post/outcome is established, schedule a quiet maintenance
   window. Record current runtime revision/config and non-secret authority metadata.
3. Pause all posting/refresh consumers and drain in-flight requests; verify no old
   revision/job/tool can refresh. Pause alone is not sufficient without draining.
4. Deploy corrected Good Luck code with posting disabled, preserving all FPL/task/
   identity configuration. Validate configuration and failed-closed legacy decoding.
5. Use a separately reviewed privileged migration procedure, fresh manual
   authorization/identity proof where legacy dispatch history cannot be established,
   and exact expected-revision CAS to create schema-2 idle/current authority. Do not
   treat an expired old lease or its absence as proof of no prior rotation. This
   checkpoint deliberately supplies no unattended/live schema migration command.
6. Read-only current-credential/identity verification, followed by isolated
   synthetic contention/fault tests. No disposable post. Ensure every consumer is
   corrected before restoring Good Luck's original intended posting configuration.
7. Only then resume disabled/test-only M9, with separate publisher/state permissions.
   Default-database OAuth access isolation still requires its own least-privilege
   design review; this correction does not grant Captain any access.

No deployment or live migration in this checkpoint. It is not safe to rush this
coordinated change before GW5 solely to obtain the Captain test.
