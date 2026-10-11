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

The reported installed worker is build
`98a09c5278deeff289b30fdb7e5123315f116abf`. The corrected clock worker
bundle is build `0085c3e036ff977466418356993c5c8638f73605`, ZIP SHA-256
`C06EB2D1B53D0920BF63528E9DB9640E6C6888AAF010A74520DAEEAB25337561`.
The reported rollback slot contains `3a71ad6e78f8f54789b51be8bdbaef1b2b0222cc`;
an older `e27331ef7cda4dbf1a3d8747deb09dfade8bfc49` versioned archive and
the legacy archive already coexist. These are historical reports, not a new
read of the stopped VM: inspect their actual names, manifests, owner/ACL and
hashes during the next approved maintenance session before replacement.

The same-user installer now validates **every** existing versioned archive,
including its build-named directory, manifest, private configuration, worker
entry point, owner/ACL, non-redirected content and SHA-256 tree. Distinct
builds coexist as `CaptainCloudWorker01.rollback-versioned-<commit>`. The
current versioned rollback is moved into a *new* build-named archive only after
all prior archives are validated. If the same build is already archived, the
trees must be byte-identical; the rollback is retained under
`...rollback-versioned-<commit>-duplicate-<tree-SHA256>`. A content mismatch or
occupied target stops without overwriting either copy. Multiple archives are
append-only: no retention timer or automatic deletion is applied. Existing
archive directories are never recursively archived. The rollback is renamed,
not copied, so its historical audit files are not duplicated by archiving.

Only after rollback preservation does the installer stage the new worker,
preserve the current private config and audits, and atomically rename the
current root to the rollback slot and the staged root into service. A verified
repeat invocation with the same installed bundle is a no-op; code drift fails
closed. A target collision, malformed archive or hash mismatch stops for
operator review. If a post-rename verification fails, the original worker is
restored and the failed stage retained; never delete a failed stage or archive
just to retry. The password-backed Scheduled Task is checked but never
re-registered, and the sibling FPL Review browser profile is not moved, copied
or opened.

A restore is a separate, supervised same-user operation: first verify the
chosen canonical or duplicate archive's exact manifest, name/digest, complete
tree hash and owner/ACL, and confirm no worker or Chrome process is running.
Retain the current root under a new unoccupied quarantine name; rename the
chosen archived directory to the original root; verify config/audit hashes,
ACL, worker import and Scheduled Task contract before boot. A restore consumes
that archive name by rename, so record its identity in the audit; never copy
browser data, overwrite another generation or remove an archive merely to
make room. If any check fails, stop with the VM shut down and obtain an
explicit recovery review.

### Next corrected-worker installation (separate authorization required)

1. With the VM still `TERMINATED`, verify the Captain publisher is disabled,
   planner/queue paused, no postable assignment or dispatchable publication,
   and four-hour Compute `STOP` protection active. Do not touch Good Luck.
2. Boot only for interactive `captaintrial` maintenance. Before replacement,
   inspect the current root, fixed rollback, every versioned archive and legacy
   archive read-only; verify build IDs, config/audit hashes, owner/ACL, profile
   separation, and the password-backed boot task. Stop if this differs from
   the inventory above or any archive differs from its manifest/name.
3. Transfer the existing verified `0085c3e...` ZIP (SHA-256 above) and the
   newly reviewed replacement script as separate files. Recheck both hashes on
   Windows; do not use an older script with the same filename. Run the script
   once under `captaintrial` with the expected commit and ZIP hash. Confirm
   `replacement_verified`, current installed build, append-only archives,
   retained `98a09c...` rollback, audit hashes and unchanged task/profile.
4. Sign out, close RDP/IAP, stop the VM and independently confirm `TERMINATED`.
   Keep publisher/planner/queue disabled/paused. A later, separately authorized
   non-postable cold-boot rehearsal is required before any arming decision.

## Status after the first GW7 diagnostic rehearsal

The compatible controller is already deployed as `captain-controller-00006-js7`.
Build `98a09c5278deeff289b30fdb7e5123315f116abf` passed one genuine
non-postable cold-boot rehearsal, including worker release, FPL Review,
handoff, candidate validation and confirmed VM termination. That success did
not identify GW6's historical release-rejection predicate. The separately
reviewed clock correction is in build `0085c3e036ff977466418356993c5c8638f73605`,
which is **not installed** as of this repository checkpoint. The next action is
the corrected-worker installation above; another rehearsal and any production
rearming each require their own authorization. Do not redeploy the unchanged
controller merely to install the corrected worker.
