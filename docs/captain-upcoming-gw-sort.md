# Captain upcoming-GW ranked-prefix acquisition

## Status and evidence (17 October 2026)

Repository-only correction; not deployed or installed. The installed Windows
worker remains `0085c3e036ff977466418356993c5c8638f73605`. Production control,
publisher, ranking, renderer, OAuth and timing configuration are unchanged.

GW7 acquired 50 rows, omitting De Cuyper. Ranking those retained rows was correct.
The historical acquisition did not record its sort state, so today's inspection
does not prove his historical projection or precise historical horizon rank.

During the authorized interactive inspection, the operator observed:

- Initial Total-descending table: 50 DOM rows, Haaland through Murillo.
- Scrolling the inner list appended rows to 679, Haaland through Mack Allan.
- Clicking GW8 descending replaced the initial prefix: 50 rows, B.Fernandes
  through Enciso. No fixed-page/50-player source-universe limit was established.
- The operator's independent analysis of a private 679-unique-player CSV reproduced
  both prefixes' endpoints. Konsa moved from horizon rank 234 to GW8 rank 17;
  Enciso from 55 to 50; Enzo from 21 to 27. This supports global GW sorting,
  rather than sorting only the original 50 rendered players.
- The active GW8 `TH` had `after:bg-fpl-accent` and an SVG class
  `lucide-chevron-down`; inactive Total had `lucide-arrow-up-down`. No `aria-sort`
  attribute was present. Screenshots showed blank search, All players,
  unrestricted price and Points metric.

These are current UI observations and operator-supplied CSV comparison results,
not an independently recovered GW7 full-source snapshot. No private CSV or full
projection dataset is committed. Maintenance ended with RDP signed out, IAP
closed, and Compute independently reporting TERMINATED; its native four-hour
STOP safeguard remained intact.

## Narrow extraction change

`PlaywrightReviewBrowserAcquirer` retains existing authenticated navigation and
profile ownership/cleanup. After projection readiness it:

1. Requires exactly one supported projection table and the exact assignment
   event's GW header, not a positional leftmost-column assumption. Assignment
   authority and fresh official event/deadline validation remain unchanged.
2. Requires visible All players, blank search, unrestricted price and Points
   controls. Unsupported, ambiguous or filtered scope fails closed; it does not
   silently change the user's filters.
3. Inspects the header underline and chevron independently. It clicks the exact
   header at most twice, observing state changes, until descending is verified.
   One click is never presumed to mean descending. Unknown markers fail closed.
4. Requires the start of the list (only a bounded return-to-top, not full scrolling
   or pagination). Rejects unexpected auxiliary/hidden rows, duplicate identities,
   malformed rows and a changed header layout.
5. Requires non-increasing GW values and an unchanged SHA-256 fingerprint of
   every captured ordinal/name/team/value for at least 800 ms. Constant row count
   alone is not readiness. There is no hardcoded 50- or 679-row success threshold.
6. Rechecks the proof against the exact rows in `ProvenBrowserAcquisition` after
   Chrome closes and ownership is released. No verified proof means no handoff.

The existing canonical parser, exact official identity resolver,
`build_captain_report`, stable points ranking, strict official ownership `<10.0`,
fixture enrichment and renderer are reused unchanged. All captured rows must
resolve; unmatched rows are not silently discarded. No position filter is added.

## Selection sufficiency, not full-source completeness

Under the inspected global-sort/contiguous-prefix contract, all omitted players
follow the captured prefix in GW points order. The unchanged cloud selector
requires at least three players and finds the first official-ownership-eligible
differential **outside** those three. If it finds one in the prefix, no later row
can outrank it. Equal projections retain source order. If there is no eligible
differential in the prefix, cloud validation rejects the candidate. It does not
guess, publish a partial selection, or fall back to Total ordering.

The wire schema remains v1: `complete` means all records of the verified ranked
prefix were captured, not all source players. No controller deployment is needed.
Local audit schema 4 adds `projection_coverage`: fixed contract/scope, event/GW,
descending direction, timestamp, count, row digest, first/last points, bounded
click count and stable-observation count. It explicitly records
`full_source_coverage=false` and
`selection_sufficiency=requires_cloud_top_three_and_differential`. The controller
does not receive or independently certify these new local proof fields; it still
reconciles every supplied row and reconstructs the selection from fresh FPL data.
New safe local failure codes are `review_sort_not_verified`,
`review_scope_not_verified`, and `review_prefix_not_verified`. The optional
read-only boot observer recognizes these allowlisted codes without forwarding
raw browser, session, or projection content.

## Limits and future preview

This depends on FPL Review preserving its observed global sort and contiguous
prefix semantics. A silent provider change to subset-only sorting with identical
UI markers cannot be mathematically detected from a prefix alone. The current
CSV/UI cross-check is the evidence for that contract, not a per-request proof of
the entire universe. Known scope, header, marker and ordering changes fail closed.
DOM control types were not fully inventoried during manual inspection; the
semantic scope selectors must be verified in a separately authorized rehearsal.
The 800-ms stability check reduces asynchronous rendering risk, not a guarantee
against a provider updating data immediately afterward. If that contract cannot
be reconfirmed, stop and review supported export/full enumeration, not a higher
row threshold or silent fallback.

A future non-postable Live Captain Preview should invoke this same acquirer,
canonical identity resolver, selector and renderer. It can display prefix rows,
official matching/ownership and precise differential eligibility, alongside the
coverage proof. It must label the remaining universe as unobserved, not excluded.
Central preview reporting and its explicit non-postable trigger are separate work;
this patch does not add a preview endpoint or any posting authority.

## Separately authorized deployment and validation

1. Recheck official FPL event/deadline and production task/VM ownership. Review
   an explicit maintenance and isolated rehearsal plan without dispatching
   production tasks. Preserve Good Luck and both OAuth authorities.
2. Build a **new** worker-only immutable bundle from this correction's committed
   source. Record its manifest and ZIP SHA-256; never relabel the old 0085c3e ZIP.
   Validate the worker import closure and existing collision-safe installer hash.
3. With operator approval and safe no-assignment maintenance conditions, perform
   the reviewed interactive `captaintrial` atomic replacement. Verify all archive
   identities/hashes first; retain the installed worker as rollback and preserve
   configuration, historical audits, Chrome profile and password-backed Task.
4. Sign out, close RDP/IAP, stop and independently verify TERMINATED. Do not
   manually launch the worker or Chrome as an installation test.
5. Separately authorize exactly one destination-1 immutable non-postable
   cold-boot rehearsal using the existing isolated diagnostic task path. Never
   resume production tasks for convenience; no publisher call or X authority.
6. Require authenticated release, correct GW-descending/scope proof, stable prefix,
   official identity reconciliation, sufficient top three/differential, handoff,
   immutable non-postable candidate, STOP and independent TERMINATED confirmation.
   Review the local coverage allowlist using the maintenance/observer procedure
   only if separately approved. Do not expose raw audit/handoff or session data.
7. Any unsupported UI/sufficiency failure stops without automatic retry or
   publication. Production activation remains a separate operator decision.

Deterministic coverage includes global reordering from outside the old top 50,
exact header selection, both sort directions, delayed and unstable snapshots,
filter/metric changes, duplicate/missing rows, no eligible differential, `<10.0`
boundary, source-order ties, exact prefix/full-source tweet equivalence, unmatched
identities, audit redaction, observer compatibility and no handoff on failure.

Validation at this checkpoint: 1,674 full-suite tests passed, including the offline
JavaScript DOM-reader and executable PowerShell observer tests. Ruff lint and
format checks, `pip check`, the static worker-only import closure and
`git diff --check` passed. The closure remains free of X publishing and cloud
credential-authority adapters. No controller, publisher, selection/ranking,
renderer, release-validation, Good Luck or OAuth implementation was changed.
