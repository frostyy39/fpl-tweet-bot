# Captain orchestration milestone 1: pure contracts

No controller, repository implementation, cloud client, HTTP endpoint, browser
inspection or posting capability is added. Good Luck code is unchanged. Captain
state must be accessed through Captain-only repository interfaces in milestone 2;
no Firestore database/collection choice is encoded here. Physical isolation awaits
the IAM/cost milestone. Never pass a Good Luck state repository into Captain.

## Timing policy

`CaptainTiming` accepts aware UTC datetimes only. D is the current official FPL
deadline; T = D minus exactly two elapsed hours; release = T; W = T minus fifteen
minutes; L = T plus five minutes. Warmup is permitted in [W,T); posting acquisition
and initiation of a new X attempt are permitted in **[T,L], inclusive**. One
microsecond before T or after L is rejected. Acquisition starting at L is technically
permitted, but cannot yield an eligible later handoff outside L. An X response may
arrive after L without authorizing a retry or invalidating an attempt begun in time.

UTC subtraction precedes London conversion. `target_london` and `warmup_london`
provide their own calendar dates/offsets/folds; do not filter on the deadline's
London date. Target may be the previous day; warmup may precede midnight even
when target does not. There is no scheduler clock read or hardcoded Gameweek.

The policy is a pure prerequisite, not an X authorization. Future callers must
use trusted current time and fresh authoritative FPL data immediately before a
write. Recheck after any expensive work, refresh or claim. A handoff timestamp or
digest is not proof of freshness, provenance or session validity.

## Immutable schema v1

- `CaptainAssignment`: nonzero UUID assignment and generation identities, positive
  official event ID, matching GW/BGW/DGW/BDGW label, derived timing. A changed plan
  needs a new generation; the later repository must enforce this. Event code here
  is an expectation, not a substitute for fresh fixture classification.
- `ProjectionRecord`: positive source ordinal, preserved Unicode Review name,
  uppercase team code, finite Decimal projection, optional positive worker-resolved
  official element ID. No fuzzy identity matching or aliases. Cloud FPL resolution
  remains required. Missing optional ID is represented explicitly as null.
- `RowCounts`: raw = genuine + auxiliary; extracted equals record count and cannot
  exceed genuine count. Complete datasets require every genuine row. There is no
  50-row assumption, top-N cap, or requirement that ordinals be contiguous.
- `ProjectionHandoff`: schema version, assignment, worker attempt UUID, UTC
  acquisition start/end, immutable ordered record tuple, counts, completeness,
  authentication and cleanup enums. Records must have strictly increasing unique
  source ordinals. Duplicate optional official IDs and exact NFC/whitespace-normalized
  name+team identities are rejected. Case is not folded and accents are not removed.

Wire conversion uses exact allowlisted keys at every level; missing/extra fields
are errors. In particular, no tweet, URL, credential or browser-state fields are
accepted. Public names remain untrusted public data, never executable instructions.
UTC wire timestamps require RFC3339 `Z` or `+00:00`, with at most microsecond
precision; canonical output uses six fractional digits and Z. Floats, exponent
notation, nonfinite values and malformed decimal strings are rejected on the wire.
Decimal resource bounds are 64 coefficient digits, exponent magnitude at most 64,
and at most 64 integer digits; no rounding or ranking cutoff is introduced.

`require_eligible(current_assignment, now_utc)` rejects mismatched generations or
expectations, pre-T acquisition, future/inverted acquisition intervals, receipt/
attempt checks after L, empty/partial/unknown sets, unconfirmed authentication and
unreleased/unclean browser state. Ineligible handoffs can still be decoded for
audit; parsing alone must never be treated as acceptance or permission to post.

## Payload identity

Canonical JSON is UTF-8, sorted keys, compact separators, original record order,
canonical UUIDs/UTC timestamps and decimal strings without insignificant trailing
zeros. SHA-256 covers the whole normalized payload including generation and attempt.
`payload_digest` is derived, not mutable caller state. Verify a separately carried
digest after decoding; this is content identity, **not a signature or authentication**.
The later transport must reject duplicate JSON object keys and bound request sizes
before calling `from_payload`; this milestone accepts decoded dictionaries only.
Repository acceptance must enforce same attempt + same digest = idempotent replay;
same attempt + different digest = conflict. Event-level successful-post prevention
must not be keyed by generation or payload digest.

## Deferred session-health requirement

Every genuine authenticated Captain acquisition should serve as the primary
session-health opportunity while Chrome is already open. Later VM lifecycle work
must capture only safe session metadata: authenticated-account confirmation,
expiry/session classification, observation/acquisition time, next known Captain
target, and evidence of legitimate server-issued metadata refresh during normal
activity. Never expose or persist secret cookie values. Compare the observed
horizon to the next target; use standalone checks as an evidence-driven backstop
for long gaps or insufficient/risky evidence, not routine extra boots. Warn early
where evidence already predicts risk; do not first discover predictable expiry at T.

Expiry alone is not proof of server-side validity. Do not fabricate, extend or
rewrite cookies, automate login/CAPTCHA, export/copy session state, use Sync or
credential workarounds. Clean browser closure must persist legitimate server
updates. Authentication failure remains fail-closed.

No metadata inspection mechanism or speculative cookie fields are added here.
The existing pure authentication classification, acquisition times and assignment
target provide the required foundation. A typed session-health evidence record
and its persistence belong to the later lifecycle milestone once the observable
metadata contract is defined; it must not be used as posting authorization.
