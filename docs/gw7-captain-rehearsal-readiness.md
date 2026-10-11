# GW7 Captain diagnostic rehearsal readiness (not an arming instruction)

This checkpoint changes repository code and the reviewed worker-replacement
procedure only. It does **not** deploy a controller, start the VM, install a
worker, schedule a rehearsal, enable a publisher, or resume production controls.
Good Luck and both X OAuth authorities are out of scope.

## Why the September proof cannot run on the current production controller

The successful September rehearsal (`eeda4ea1c2890f0681821ef19093fb873ebf4995`)
used the no-post controller composition: destination `1`, no publisher router,
and a worker service reading that same destination. `/captain/control/rehearsal`
created an immutable `non_postable_rehearsal` generation and the ordinary worker
assignment/release/handoff routes saw it. The worker validated the release,
acquired 50 authenticated FPL Review rows, handed them off, and cloud validation
persisted a non-postable candidate. No X route was present.

The later production composition uses destination `1249335464571650048` and a
separate production publisher router. It intentionally denied `/rehearsal` and
its worker service read only the production destination. A destination-`1`
generation was therefore unavailable to the installed worker. That is a route
composition gap, not evidence that the worker's GW6 release predicate changed.

## Restored diagnostic path and safety boundary

The production controller still defaults to **rehearsal disabled**. An explicit
`CAPTAIN_NONPOST_REHEARSAL_ENABLED=true` at the separately reviewed deployment
enables a planner-only `/captain/control/rehearsal` request. The controller
creates a destination-`1` generation with an immutable rehearsal binding. The
binding stores the fresh official FPL event/deadline separately from a release
within the next 30 minutes. It does not change the official deadline or the
production `[T,T+5m]` window. The worker's existing assignment, release-grant,
`issued_at`, run-ID and handoff validations execute unchanged. Release failures
go through the authenticated worker failure route to the central durable audit.

The dual-purpose worker selector returns a diagnostic or production assignment
only when exactly one is live. It verifies the immutable generation and binding;
two simultaneous assignments yield no work. Normal production tick/routing and
the production destination remain unchanged. The `inspect` route defaults to
production; planner-only `{"purpose":"non_postable_rehearsal"}` selects the
diagnostic record. Diagnostic planning, task/handoff follow-up and the dedicated
`/captain/control/rehearsal/reconcile` route reconcile only destination-`1`
task intents; they cannot enqueue a production publication task. A validated
diagnostic candidate returns
`validated_non_postable_rehearsal` and never calls the publisher router.
Repository `claim_post` and the publisher independently reject any rehearsal
binding; destination `1` is a further fence. Neither worker nor controller
accepts arbitrary tweet text for publication. This restore does **not** apply
the proposed clock-skew behavioural change.

## Worker rollback inventory and preservation

The installed worker remains build `3a71ad6e78f8f54789b51be8bdbaef1b2b0222cc`.
The diagnostic bundle is build `98a09c5278deeff289b30fdb7e5123315f116abf`,
ZIP SHA-256 `FC3E498305FF285D21E844DE548DA61FDC665157841F8A401DF216E02D9EC02B`.
The two occupied rollback slots reported by the operator are a versioned
`CaptainCloudWorker01.rollback` and
`CaptainCloudWorker01.rollback-legacy-unversioned`. Their exact on-VM
manifests, ownership, and hashes must be checked before replacement; this
checkpoint does not claim to have read them.

The revised same-user script validates the versioned rollback's manifest,
private configuration, source closure, owner/ACL and tree hashes, then renames
it in the same parent to `CaptainCloudWorker01.rollback-versioned-<commit>`.
It verifies the hashes after the rename. It neither overwrites nor deletes the
existing legacy archive. It then stages the new bundle, preserving the current
private config and historical audits, and swaps directories using the fixed
rollback slot. A target collision, malformed archive or hash mismatch stops
the replacement for operator review. If a post-rename hash check fails, the
preserved archive is retained; no automatic deletion or speculative retry is
allowed. The currently installed worker remains intact until the staged bundle
is verified and swapped. The password-backed Scheduled Task is checked but
never re-registered, and the sibling FPL Review browser profile is not moved,
copied or opened.

A restore is a separate, supervised same-user operation: first verify the
target archive manifest/hash and that no worker or Chrome process is running;
retain the current root under a new unoccupied quarantine name; rename the
chosen archived directory to the original root; verify config/audit hashes,
ACL, worker import and Scheduled Task contract before boot. Never overwrite
another generation or remove an archive merely to make room. If any check
fails, stop with the VM shut down and obtain an explicit recovery review.

## Next authorized operation — bounded sequence, not executed here

1. Read-only preflight: verify the exact Git/image/bundle hashes, fresh official
   event/deadline, Captain publisher disabled, planner/queue paused, no live
   production assignment/publication, VM `TERMINATED`, four-hour STOP safeguard,
   and Good Luck unchanged. Reject simultaneous diagnostic/production work.
2. Build an immutable controller image from this reviewed checkpoint and deploy
   with `deploy-captain-production-controller.ps1 -EnableNonPostRehearsal` while
   all production controls remain paused and publisher disabled. Verify the
   controller revision, OIDC route and default production inspection. Do not
   enable posting or recurring planning.
3. With a separate maintenance approval, boot the VM once. Stop for the
   operator to connect over the approved IAP/RDP path as `captaintrial`; never
   request the Windows password in chat or command arguments. Before running
   replacement, inspect both occupied rollback generations and the actual
   Scheduled Task. Transfer only the reviewed ZIP and replacement script.
4. Under `captaintrial`, run the reviewed replacement with the exact build and
   ZIP hash above. Verify `replacement_verified`, installed build, both
   archived generations, config/audit preservation, unchanged browser profile,
   owner/ACL and password-backed boot trigger. Do not launch Chrome or run the
   worker manually. Operator signs out and closes RDP/IAP; stop the VM and
   independently confirm `TERMINATED`.
5. Only after that proof, under separate rehearsal approval, create exactly one
   fresh bounded destination-`1` generation via the planner-only route. Keep
   the recurring planner paused; resume only the Captain queue for the three
   explicit diagnostic tasks and restore it to paused afterward. A genuine
   cold boot with no RDP must exercise the installed worker release predicates.
   Inspect local/central release-rejection subcode and bounded clock evidence.
   If rejected, stop and diagnose; do not blind-retry or apply a timing bypass.
6. If accepted, allow the single non-postable run to proceed through FPL Review,
   handoff and fresh cloud validation. Confirm no production publication task,
   claim, `write_started`, OAuth access or X request. Reconcile STOP and require
   independent Compute `TERMINATED` evidence. Remove temporary tasks/jobs,
   pause the Captain queue, leave planner/publisher disabled, and retain audits.

The controller change is small and the diagnostic release path is testable, so
GW7 readiness remains technically plausible. It is **not** yet established:
the compatible controller has not been deployed, the diagnostic worker has not
been installed, and no new release diagnostic has been observed. Any GW7
production rearming requires separate evidence review and authorization.
