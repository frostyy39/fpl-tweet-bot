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

## Executed audit — 18 September 2026 (all times UTC)

- Code/tools checkpoint: `270f283c45d68000d07c269c7b35887baf470951`.
  Build `dbbcef44-4bd4-403b-b319-9ba658339382` succeeded. Deployed immutable
  image: `europe-west2-docker.pkg.dev/fpl-frosty-bot-v1/fpl-bot/fpl-bot@sha256:b365ecf5e8d24547a411b1ce87448fb67c8a61722d61623fddc028efb09a0c39`.
- Scheduler pause completed 20:17:25.504; empty queue paused 20:17:31.041.
  No active posting/refresh attempt, running job, recent dormant-service request,
  old tagged endpoint or other configured OAuth consumer was found.
- Non-writing corrected revisions: europe-west1 `fpl-bot-00004-7bz` and
  europe-west2 `fpl-bot-00006-2cg`. Only shared OAuth modules/operator tools
  differ from the immutable successful GW5 application base.
- Metadata-only migration execution `fpl-bot-oauth-schema-migrate-7jscf`:
  transaction committed 20:20:40.165866; command reported `migrated` at
  20:20:40.174918. Revision 5/current 5/previous 4 preserved; schema 2,
  attempt generation 0, no attempt/lease. No X or Secret Manager operation.
- Read-only identity execution `fpl-bot-oauth-readonly-zl6w2` succeeded.
  Its genuinely needed refresh claimed 20:22:30.017789, durably dispatched
  20:22:30.439466, created version 6 at 20:22:31.905958 and committed authority
  20:22:32.097305. Credential revision advanced exactly once, 5 to 6;
  attempt generation 1, state `committed`, current version 6, previous version 5,
  lease cleared, no uncertainty. Identity matched `1732468005336907776` at
  20:22:33.469685. Tokens were never printed. Final durable timestamps/version
  metadata prove the barriers and completed persistence; intermediate states were
  not individually captured by polling, so no fabricated observation times are used.
- Existing-post audit execution `fpl-bot-oauth-readonly-8kjjs` succeeded at
  20:25:24.285514. Reused revision 6, no additional refresh. GET existing post
  `2101000815150239804` confirmed exact author, GW5 tweet and created timestamp
  17:30:03 UTC. Unicode-safe Cloud Logging API retrieval independently confirmed
  the lock/celebration emoji; gcloud's Windows console rendering is not evidence
  of corrupted X text.
- Unchanged Good Luck read-only planning probe succeeded 20:25:21.255289:
  today's London-calendar-day event remains GW5, deadline 17:30 UTC, already
  succeeded. No checker/posting/task route was invoked. A separate fresh
  chronology probe confirmed next unpassed event 6, deadline
  `2026-10-10T10:00:00Z`, at 20:31:29.639283. The first optional chronology
  command failed due to Windows command-line quote stripping (no state/X effect);
  corrected single-quoted Python literals succeeded in execution
  `fpl-bot-oauth-fpl-proof-bjfgx`. Future planning logic was not changed.
- Restored europe-west1 revision `fpl-bot-00005-l7m`, posting true, same image.
  London service remains posting false. Queue resumed 20:29:23.555 and Scheduler
  resumed 20:29:32.785. Runtime identity, all other environment values, pinned
  static secret refs, CPU/memory/concurrency/timeout/max instances, URLs/OIDC
  audience, queue rates/retries, Scheduler cadence/timezone/retries all match
  captured intended configuration. Service Ready and authenticated GET `/`
  returned the expected application 404 without invoking any mutable route.
- No new Good Luck task or posting attempt/post was produced; GW4/GW5 durable
  audit timestamps remain unchanged. Captain was never enabled, its queue and
  planner remain paused, VM TERMINATED. No IAM, destination or Captain resource
  change. Temporary migration/identity/FPL Jobs are removed only after their
  completed audit was preserved in Cloud Logging.

Final code review additionally hardened the operator migration's actual persisted
type checks (Python boolean/integer equality must not normalize malformed records).
Three regression cases were added. This is not a change to the deployed runtime
coordinator; the actual migrated document's numeric types were verified. There is
no need to rerun migration, replace credentials, or redeploy business logic.

Validation: 276 OAuth/migration/audit-focused, 629 Captain, 233 Good Luck relevant,
1,405 full-suite tests passed; Ruff, format, dependency and diff checks passed.

Next step: separately review disabled FPLBotTest-only Captain M9 publisher and its
least-privilege route to this single shared authority. Do not grant Captain broad
Good Luck state access, create another refresh-token authority, arm a run, or post
as a consequence of this OAuth rollout.
