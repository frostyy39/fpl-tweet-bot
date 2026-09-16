# Captain Firestore persistence (Milestone 6)

No resources are provisioned by this milestone. Construct
`FirestoreCaptainRepository(project=..., database=...)` and
`FirestoreCaptainVmOperations(project=..., database=...)` only in future approved
runtime composition. Both arguments are required; there is no implicit project or
database fallback. Injected clients must match both identities. The physical choice
of shared versus separate database and its IAM remain deferred. Distinct names are
logical isolation, **not an IAM boundary**.

## Durable layout

All root collections have the fixed `captain_v1_` prefix. Good Luck collections are
neither configurable targets nor accessed by these adapters. Document IDs are
SHA-256 of the canonical typed key, checked on every read.

| Collection suffix | Key / contents |
| --- | --- |
| generations | generation UUID / immutable assignment, version, status history |
| current | destination + Captain type + event / current generation UUID |
| attempts | attempt UUID / claim, status, accepted immutable handoff and digest |
| generation_attempt | generation UUID / intended acquisition attempt UUID |
| posts | destination + Captain type + event / posting attempts, uncertainty, Post ID |
| intents | generation UUID + task kind / scheduled action and acknowledgement |
| vm | active singleton / current use or cleanup fence, or null |
| retired_vm | lease UUID / retained cleanup fence evidence |
| operations | lease UUID + START/STOP / asynchronous operation phase |
| health | generation UUID / append-only non-secret observations |
| assignment_index | assignment UUID / globally unique generation binding |
| claim_index | posting claim UUID / globally unique event-post key binding |

Posting keys never include a deadline generation. No audit records are deleted.
Candidates are not persisted: the accepted repository interfaces do not require
candidate storage. Likewise, no invented error/credential fields are added.

## Transactions and reference semantics

Every public operation executes inside the real Python Firestore `transactional`
wrapper. A fresh transaction-local reference repository/VM ledger is rebuilt on
each retry. Read-through maps preserve the existing domain transitions, buffering
writes until all domain reads and strict serialization succeed. There are no
external tasks, clocks, random IDs, browser actions, VM requests or X calls inside
retryable callbacks. IDs and timestamps are caller supplied.

The two uniqueness indexes are read and written transactionally. Most operations
use point reads. Supersession queries only the old generation's intents; outbox
discovery queries pending intents. No whole-history scan or aggregate whole-system
document is used. These equality queries use ordinary single-field indexes.

Firestore retries rerun the domain operation against current durable state. A
lost commit acknowledgement can be retried using the same IDs/payload: replay
returns `applied=False`, never permission to repeat an external side effect.
`write_started`, `uncertain` and `succeeded` retain the existing posting barriers.

Outbox intent is committed before any later external task operation. Acknowledging
task creation is a separate transaction; a crash between them leaves reconcilable
pending work. Task names supplement this state, not replace it.

VM operation acknowledgement never releases the cleanup fence. As in the accepted
Milestone 2/3 contracts, the controller must confirm `stop_is_settled` and actual
termination before calling `complete_cleanup`. The latter retains exact lease/
generation fencing and retired-lease replay behaviour; it does not itself call or
observe Compute Engine. Outstanding START must settle before STOP acknowledgement.

## Serialization and operational bounds

Version 1 uses an explicit class/enum allowlist, exact fields/types, canonical UTC
timestamps with microseconds, UUIDs, Decimal strings, immutable tuples and verified
handoff digests. Typed map wrappers avoid nested Firestore arrays. Unknown versions,
extra fields, malformed identities, inconsistent histories/bindings, invalid
success IDs and mismatched routing metadata fail closed with sanitized errors.
No dynamic imports/object construction from arbitrary persisted class names occurs.

Records have a conservative 750,000-byte JSON encoding guard below Firestore's
document limit. Event-specific retry/audit histories and per-generation health
histories must remain bounded operationally; exceeding the guard fails closed,
never truncates audit or removes a posting barrier. Retained successful event keys
must not be expired by future retention policy. Before deployment, review Firestore
index exemptions for the `entry` payload (query only `routing` fields), IAM and
backup/retention policy. No index or resource configuration is changed here.

Tests use an atomic versioned Firestore fake with rollback, contention/retry and
lost-ack simulation, run the existing repository contract suite against both
backends, and exercise the installed SDK retry decorator and wire encoder without
RPCs. A real local-emulator concurrency rehearsal remains recommended before
deployment; normal pytest requires neither an emulator nor GCP credentials.

Reference: [Firestore transaction ordering and retry requirements](https://firebase.google.com/docs/firestore/manage-data/transactions).
