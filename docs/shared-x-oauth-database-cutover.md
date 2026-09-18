# Dedicated shared OAuth database cutover

This prerequisite is separate from Captain M9. No publisher, arming or X post.

## Security and configuration

`shared-x-oauth` is Native Firestore in `europe-west2`, matching both existing
business databases. Location is permanent. Actual credentials remain in the same
Secret Manager secret; only schema-2 authority metadata is relocated.
`X_OAUTH_FIRESTORE_DATABASE_ID` is required and cannot be `(default)`. Business
`FIRESTORE_DATABASE_ID` remains independent: Good Luck `(default)`, Captain
`captain-state`. The normal runtime never falls back or initializes an authority.

`deploy/provision-shared-x-oauth.ps1` grants Good Luck its additional exact-database
datastore role and prepares `captain-publisher` with only exact OAuth/Captain
database roles. It grants the publisher no Secret Manager, Compute, invocation or
posting capability. Project/ancestor/group grants must also be inspected and real
effective reads/writes tested. No Firestore collection-level IAM fiction is used.
No additional indexes, TTL or deletion of authority/audit evidence is required.
Named databases have no free quota but no creation fee. A kilobyte-scale authority
and tens/hundreds of transactions monthly add pennies, not a fixed instance cost.

## Reviewed operational sequence

1. Read safe live metadata; require schema 2, terminal reusable attempt and no
   lease. Verify official next deadline, queues empty and no active posting.
   Record both Good Luck services, schedulers, queues, IAM and posting audit times.
2. Validate all local suites. Build `deploy/shared-oauth-database-build.yaml`.
   Its Dockerfile overlays only runtime database wiring and operator tools on the
   immutable deployed Good Luck image; dependencies/business modules are frozen.
3. Create dedicated database/scoped bindings. Keep Captain paused/VM TERMINATED.
4. Pause Good Luck Scheduler and empty queue. Deploy both Good Luck regions on
   the corrected image with posting false, business database unchanged and OAuth
   database explicitly `shared-x-oauth`. Route 100% to corrected revisions, no old
   tags, and allow existing bounded requests to drain. No temporary jobs/consumer
   may still refresh from the old store. Re-read source exact metadata.
5. Operator CLI `python -m fpl_bot.x_oauth_database_migration --inspect` reads only
   safe source metadata and prints exact revision/content digest/version pointer.
   Pass that freshly reviewed digest/revision with `--consumers-quiesced` to the
   same CLI; use explicit project/source/destination/secret/user identifiers.
6. Source transaction CAS-replaces exact authority with a schema-3 **retired
   tombstone**, retaining the reviewed metadata, migration identity and UTC time.
   All schema-2 readers reject it before secrets/X. Only after this fence does a
   separate destination transaction create the exact unmodified schema-2 record.
   These are NOT falsely described as a cross-database atomic transaction.
7. Verify pointers, revision, attempt/lease, strict serialization and idempotent
   replay. Read-only X identity through the new store; refresh only if needed,
   exclusively through the corrected coordinator. Check expected numeric identity.
8. Probe `captain-publisher` real effective metadata reads/transactions in its
   databases, denied default-document read/create and denied Compute inspection.
   Probe only isolated records, never Good Luck posting data. Remove probe records
   and temporary jobs after preserving logs. Inspect project/ancestor IAM too.
9. Restore intended Good Luck posting flag, queue/Scheduler configuration exactly.
   Dormant London service stays disabled. Compare all unrelated configuration and
   posting audit timestamps. Captain stays paused, VM TERMINATED. No M9 deployment.

## Interruption/recovery

Before source CAS, the old source alone remains authoritative. After source CAS
and before destination commit, **neither** store is usable; keep consumers quiet.
After destination commit, only the new store is authoritative. Lost acknowledgements
are recovered by replaying the exact reviewed digest/revision: source retirement
and identical destination creation are idempotent. Different source/destination,
active lease, uncertainty, malformed metadata or advanced destination aborts; no
overwrites, token refresh, source unretirement or guessing a `latest` secret.
After a subsequent legitimate refresh, do not rerun the old migration to replace
new authority; audit the tombstone and current destination instead.

Runtime rollback must retain the dedicated database wiring/schema-2 coordinator.
Never restore an old image expecting `(default)` or resurrect the retired source.

## Executed evidence

To be appended after live verification; contextual version numbers must never be
used as pinned runtime configuration.
