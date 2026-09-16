# Captain worker orchestration (Milestone 4)

Pure composition: `CaptainWorker` receives a controller client, UTC clock and
`ProvenBrowserAcquisition` configured with the existing dedicated profile.
It is intended to run under the already-proven password-backed Windows identity.
No task registration, transport, credential provisioning or deployment is included.
The configured expected-user check is a guard, not authentication: the future
transport must authenticate the worker independently. The historical trial runner
and its local audit format are unchanged.

## Lifecycle and trust boundary

1. Obtain a typed versioned assignment and attempt; absence exits without Chrome.
2. Ask the authenticated controller for a separate release bound to the complete
   assignment, attempt and this invocation's unique run ID. Stale/revoked releases
   fail closed. The worker never repairs an assignment.
3. Wait through at most 120 bounded release checks (at most ten seconds each), with
   cancellation and expiry checks. Client implementations must enforce request
   timeouts, cancellation and long-poll waiting; no busy retry or implicit takeover.
4. Both release issuance and actual acquisition start must be within inclusive
   `[T, T+5 minutes]`. Early release is rejected, not used as permission to wait
   locally and acquire later. Warmup never supplies a posting dataset.
5. Acquire once through the existing stable-Chrome lifecycle and canonical complete
   table parser. Clean closure/profile release precedes handoff construction.
6. Submit M1 immutable projections, source ordinals, counts, timestamps, digest,
   authentication and cleanup status. Official IDs are omitted for cloud-side
   authoritative resolution. No ranking or tweet is authoritative on this worker.
7. Accept only a matching accepted/replay receipt. A lost acknowledgement is
   `handoff_uncertain`; retain the identical payload for later reconciliation, never
   acquire or submit a different dataset automatically. Other failures report only
   allowlisted categories, not arbitrary browser/transport exception strings.

Acquisition at exact L is permitted, but a handoff finishing after L is rejected
under the existing M1 acceptance policy. This is not extra time for posting.
Browser cleanup remains inside the proven acquirer, including on exceptions.

The future controller adapter must atomically bind release to ONE run ID per
attempt, backed by durable M2 claim state, and revalidate current generation and
official deadline. A new process cannot automatically take over an active or
consumed attempt. Local run-ID replay rejection supplements, not replaces, that
durable guarantee. Digest is content identity, not authentication. Submission must
freshly validate eligibility and use immutable M2 acceptance/replay semantics.
These are adapter obligations; no production endpoint is supplied here.

## Session health: evidence and limitation

The real path observes public authenticated Projections readiness while Chrome is
already open, with UTC timestamps. It returns typed M2 health evidence separately
from the projection payload, including the next target if provided. No additional
browser health run is scheduled. Authentication failure remains fail-closed.

The existing browser interface has no metadata-only expiry API. Playwright's
cookie-list operation would also retrieve secret values, so it is deliberately
not used. Real expiry classification and refresh remain **unknown**. Public table
readiness is not a claim to have inspected an account identifier, nor a guarantee
of future server-side session validity. Unknown horizon must not be interpreted
by later planning as assurance that no warning/backstop is needed.

Pure `SessionObservation` supports persistent (`fixed`), session-only and unknown
metadata from a future safe producer. Tests compare before/after expiry only:
increase means observed metadata change, equal known metadata means no change
observed, and incomparable/decreasing/unknown metadata means unknown. This does
not establish renewal causality or continued server validity. There is no secret
value field, cookie inspection/export/mutation, login automation or security change.
Normal clean Chrome closure preserves legitimate browser-managed session updates.

Before live integration, implement and test authenticated transport, durable
single-run release binding, payload acknowledgement/reconciliation and Windows
composition. Resolve a metadata-only source if expiry-aware warnings are required;
do not fabricate expiry or introduce secret-reading merely to populate fields.
