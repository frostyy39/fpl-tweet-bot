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
   The reviewed Job definition is `deploy/shared-oauth-isolation-probe.yaml`.
   On Windows `deploy/prepare-shared-oauth-isolation-probe.ps1` loads its exact code
   through the working Jobs create API, preserving quotes without adding APIs or
   changing execution policy. Its encoded command contains no secret data.
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

## Executed evidence — 18 September 2026, UTC

Code checkpoint `9afaa92ce1eeb0e90b3365bde5522661062fef90` descends directly from
accepted `cd0c0c7fc32f43287618b499a27285d8450c6fee` (and Captain `52f5f841...`).
Build `6836077d-f01e-493c-b83b-4504845c7b69` succeeded with immutable image
`europe-west2-docker.pkg.dev/fpl-frosty-bot-v1/fpl-bot/shared-oauth-database@sha256:093b9800dd37b3943c9ca94123f647682f2b2ad76ae0b84e59d3f3b3f797578b`.

- Preflight: both existing databases in `europe-west2`; source schema 2, revision
  6/current secret version 6/previous 5, committed attempt generation 1, no lease
  or uncertainty. These are audit facts, **not pinned runtime versions**.
  Fresh official next deadline was event 6, `2026-10-10T10:00:00Z`, observed at
  21:21:50. No pending Good Luck task, recent POST, active refresh/posting attempt
  or OAuth job consumer. The dormant London `fpl-bot-fpl-probe` is FPL-only,
  last completed 4 September and was not invoked/changed.
- `shared-x-oauth` created 21:30:04.848013: Native/Standard, London, pessimistic
  concurrency, deletion protection enabled. No custom indexes/TTL needed.
- Good Luck Scheduler pause updated 21:31:25.968594; empty queue paused.
  Non-writing EU1 `fpl-bot-00006-j5r` Ready 21:31:52.510923; EU2
  `fpl-bot-00007-6fz` Ready 21:32:01.347084. Both 100% latest with no tags,
  posting false, OAuth DB `shared-x-oauth`, business DB `(default)`. Migration
  did not dispatch until after the 300-second request drain bound.
- Inspection `shared-oauth-database-migrate-5wfgm` at 21:33:53.445274 reviewed
  revision 6 and digest
  `4272e6e07d3ad173cfc72a9a923b4b42142ccc07dfead5a0c1dddcb13392dfc6`.
- Migration `shared-oauth-database-migrate-fwbfx`: source retirement audited at
  21:38:49.003241, source commit 21:38:49.365661; destination creation commit
  21:38:49.871319; `relocated` at 21:38:49.879334. Exact schema-2 record copied:
  revision 6/current 6/previous 5/attempt generation 1 and all original timestamps
  retained. Source is a schema-3 retired tombstone; strict runtime readers reject
  it before secrets/X. No secret access or X request in the migration job.
- Read-only identity `shared-oauth-database-readonly-g6s5c` at 21:40:26.953142:
  `/2/users/me` matched `1732468005336907776` through new authority under Good
  Luck runtime. No refresh occurred: destination update time/attempt/pointers
  unchanged and Secret Manager newest version remains the original 6 created at
  20:22:31.905958. Lease clear, no uncertainty.
- `captain-oauth-isolation-probe-5zrwh` passed at 21:40:37.697421 under real
  `captain-publisher` attached identity: OAuth metadata read/parse, isolated
  read/transaction/create in `shared-x-oauth` and `captain-state`; `(default)`
  document read/create denied; fixed Captain VM Compute inspection denied (403).
  Probe records deleted in finally blocks. No real Good Luck posting mutation.
- Exact replay `shared-oauth-database-migrate-p49r6` returned `already_relocated`
  at 21:41:58.650190 without changing either record.
- IAM: project has no folder/organization ancestors; full project bindings have
  no group/public/unconditional datastore grant to publisher. Publisher has only
  two `datastore.user` grants conditioned on exact OAuth/Captain DB resource names,
  no Compute/Secret Manager/Run grant and no user-managed keys. Good Luck retains
  its exact default-DB grant and existing secret accessor/version-manager grants;
  additional OAuth-DB grant is exact. Shared authority exists only in the new DB;
  default retains retirement evidence, not usable coordination state.
- Restored EU1 `fpl-bot-00007-c4n`, posting true, same new image; queue RUNNING,
  Scheduler ENABLED, same `0 6 * * *` Europe/London, URI/OIDC/retries preserved.
  EU2 stays posting false. CPU/memory/concurrency/timeout/max-scale, identities,
  all unrelated environment and pinned static client references unchanged.
  Authenticated GET `/` returned expected application 404; no checker/task route
  was called. Good Luck tasks remain empty; GW5 posting record still succeeded,
  Post ID `2101000815150239804`, update time `17:30:03.973761Z` unchanged.
- Only three temporary jobs are deleted after preserving their Cloud Logging
  evidence: `shared-oauth-database-migrate`, `shared-oauth-database-readonly`,
  `captain-oauth-isolation-probe`. No permanent infrastructure/audit deletion.
  Captain queue/planner remain paused and Windows VM TERMINATED; no M9 publisher,
  arming, FPL Review acquisition or X write.

The gcloud YAML replace path requested a disabled Resource Manager API; it was
not enabled. The working Jobs create path loaded the exact reviewed YAML code
through the Windows-safe loader. Initial CLI quoting failures made no provider
job call or authority mutation; no execution-policy or permission workaround.

Validation after cutover: 32 new migration/configuration/security tests; full suite
1,437 passed; OAuth 353, Captain 629, Good Luck relevant 399; Ruff/format/pip/diff
checks passed. The IAM prerequisite for a separately reviewed M9 is cleared, not
publisher deployment/enablement or the complete live browser-pipeline proof.
