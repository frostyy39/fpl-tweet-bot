# Captain cloud adapter boundary (Milestone 7)

The Captain adapter layer is provider-facing but provider-independent at its
domain boundary. `CaptainCloudTasksAdapter` creates deterministic, OIDC-authenticated
HTTPS tasks only after a durable outbox intent exists. `CaptainComputeAdapter`
targets one explicitly configured Captain instance and reconciles asynchronous
operations; the durable VM lease remains fenced until terminal stop evidence.

`CaptainWorkerHttpClient` obtains a short-lived audience-bound identity token
from the Compute metadata service and refuses credentialed redirects. The Flask
factory exposes only Captain worker and task routes, with separate expected
workload identities. No route has an X-writing surface.

The normal test suite uses injected fakes. An emulator rehearsal can be added by
setting the Firestore emulator environment variables and constructing the existing
`FirestoreCaptainRepository`; no emulator or cloud resource is started by tests.

The physical Captain database remains a deployment decision. A separate named
database gives the strongest IAM boundary but adds operational configuration;
Captain-only collections in the existing database are cheaper and can still be
isolated by collection-level application/IAM controls where supported. Either
choice needs indexes for current-generation lookup, pending intents and operation
reconciliation, plus TTL/retention review for audit records. At current scale,
Firestore storage/operations are expected to remain within the existing low-cost
budget; provision only after IAM and cost review.
