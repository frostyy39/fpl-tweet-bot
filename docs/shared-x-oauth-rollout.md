# Post-GW5 coordinated OAuth rollout

Integration base: `2332c6b`, which directly contains accepted Captain `52f5f84`.
Only shared OAuth code changes Good Luck runtime behaviour. No Captain M9.

The deployment uses `deploy/OAuthRollout.Dockerfile`: the immutable successful GW5
Good Luck image is the base, with only the three corrected shared OAuth modules
and privileged migration/read-only tools overlaid. Good Luck business modules and
installed dependency versions remain byte-for-byte unchanged. This avoids rolling
Captain's later shared fixture-parser additions into the Good Luck application.
The Git integration branch still preserves all accepted Captain source code.

## Reviewed legacy authority

GW5 deadline: 2026-09-18 17:30:00 UTC. Cloud Tasks delivery: 17:30:00.132483;
durable claim: 17:30:00.565881; write-start: 17:30:00.763719. Successful post:
`2101000815150239804`; completion persisted 17:30:03.973761. Task HTTP 200.
No queued Good Luck tasks, duplicate delivery or uncertainty was observed.

The completed runtime advanced authority to revision 5, exact Secret Manager
`projects/fpl-frosty-bot-v1/secrets/x-oauth-token-state/versions/5`, previous 4,
updated 17:30:02.333299 UTC. The successful subsequent post and normal completed
refresh, unchanged metadata and drained consumers are the reviewed evidence.
**Absence/expiry of a lease alone is never migration authorization.**

Migration is a privileged metadata-only command, NOT runtime auto-migration.
`python -m fpl_bot.x_oauth_migration_cli` requires exact project/database/account,
revision, current/previous versions and credential update timestamp, plus explicit
quiescence/history-review attestations. One Firestore transaction compares all
legacy fields and adds schema 2, attempt generation 0 and no active attempt.
Credential revision, explicit secret versions and credential timestamp are retained.
There is no Secret Manager or X client in migration. No secret version is created.
Missing, changed, leased (even expired), malformed or uncertain authority is rejected.
Lost commit acknowledgement: retain quiescence, inspect metadata, repeat the exact
expectation. Already converted exact state returns `already_migrated`; progressed
schema-2 state is never overwritten. Do not roll back to a schema-1 binary.

## Exact operational order

1. Audit GW5 and all consumers; retain non-secret baseline. Build/test a clean
   reviewed commit. Use `deploy/oauth-rollout-build.yaml` and
   `--ignore-file=deploy/oauth-rollout.gcloudignore`. Check upload list for secrets.
2. Pause Scheduler `fpl-checker` and queue `fpl-deadline` (both europe-west2).
   They were ENABLED/RUNNING; Scheduler cadence `0 6 * * *`, Europe/London.
   Confirm no active requests/jobs/refresh/posting claim. Captain stays paused.
3. Update service `fpl-bot` in europe-west1 to the reviewed digest and
   `X_POSTING_ENABLED=false`. Preserve identity, every other environment setting,
   pinned client secrets (client ID v1, client secret v2), timeout 300s, concurrency
   2, CPU 1, memory 512Mi and max instances 2. Drain old requests and confirm no
   tagged old-revision endpoint. Never invoke a posting route during maintenance.
   Also update the dormant `fpl-bot` europe-west2 service to the same corrected
   digest, retaining its existing posting false flag and all regional settings.
   It shares the authority and must not remain an incompatible old consumer.
4. Run a temporary one-attempt/max-retries-0 migration Cloud Run Job as
   `fpl-bot-runtime`, no secret mounts. Exact migration arguments:

   ```text
   --project=fpl-frosty-bot-v1 --database=(default)
   --secret-id=x-oauth-token-state --expected-user-id=1732468005336907776
   --revision=5
   --version=projects/fpl-frosty-bot-v1/secrets/x-oauth-token-state/versions/5
   --previous-version=projects/fpl-frosty-bot-v1/secrets/x-oauth-token-state/versions/4
   --authority-updated-at=2026-09-18T17:30:02.333299Z
   --consumers-quiesced --legacy-history-reviewed
   ```

5. Confirm metadata schema 2, revision 5/version 5, no attempt/lease. Run a
   one-attempt read-only verification Job with posting false and original pinned
   client secret references. `python -m fpl_bot.x_oauth_rollout_probe
   --post-id=2101000815150239804 --event-code=GW5` reuses the coordinated identity
   verifier and GETs only the existing post; exact author/text are checked. It
   never instantiates a create-post client. Only a genuinely expired/near-expiry
   access credential causes coordinator refresh; migration never does.
6. Audit safe schema-2 transitions and final pointer/attempt. Unknown provider
   result => retain consumers paused and recover as documented; never reuse R0.
   Read-only fresh FPL probe confirms next-event planning. Probe has no task/post
   capabilities. Remove temporary Jobs only after logs are preserved.
7. Restore only posting flag true on the corrected image, queue RUNNING and
   Scheduler ENABLED. Compare all other runtime/settings against baseline; verify
   healthy service and no unintended tasks/posts. Captain remains paused/no-post,
   Windows VM TERMINATED. Retain operational evidence in this document.

The existing corrected refresh coordinator commits the dispatch barrier before X,
binds replacement version before authority promotion, and commits pointer/revision/
terminal attempt/lease clearing together. An abandoned post-dispatch attempt never
becomes reusable through lease expiry. Reconciliation promotes only a durably bound
replacement; unbound/lost response requires explicit operator reauthorization.

## Captain coexistence gate

After successful rollout, coordinator compatibility is cleared, not Captain IAM,
publisher enablement or arming. M9 must still review least-privilege access to this
single authority, immutable FPLBotTest ID and guarded event-level posting state.
No independent refresh-token store is allowed.
