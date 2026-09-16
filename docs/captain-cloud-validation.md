# Captain cloud candidate validation (Milestone 5)

`CaptainCandidateValidator` takes a read-only Captain state port, the existing
uncached official-FPL source port and an injected UTC clock. No concrete network,
database, browser, task, VM or X adapter is constructed. The caller supplies a
typed handoff already accepted by authenticated controller logic; a digest is
content identity only, never authentication.

Validation checks current accepted generation, assignment, attempt, exact stored
payload/digest, durable release/claim chronology, completeness, authentication,
cleanup and the event/destination/type posting barrier. Both validation and
acquisition must obey the existing inclusive `[T, T+5m]` policy. It fetches fresh
bootstrap and event fixtures on every call, then repeats state/time checks after
building the candidate to catch supersession, posting barriers or elapsed time.
No persistent state is written and no posting lease is claimed.

Official chronology, deadline and classification must match the assignment.
Exactly 20 unique teams and nonempty valid event fixtures are required through
the existing parsers/classifier. Every Review row is resolved using the shared
exact Unicode/whitespace-normalized name plus team resolver. Optional worker IDs
are cross-checks, not identity authority. Missing/ambiguous/conflicting identity
or duplicate resolved IDs fails closed; unresolved low-ranked rows are not dropped.

The existing Captain report builder supplies stable projection ranking, full
remainder Differential scan below 10.0% fresh ownership, official names, fixture
enrichment and canonical rendering. It retains GW/BGW/DGW/BDGW rules, chronological
multi-fixture formatting, uppercase home/lowercase away opponents, two-decimal
projections and weighted-X-length rejection. Selected players without fixtures
fail closed. No worker tweet, historical ownership or historical selection is used.

The frozen `ValidatedCaptainCandidate` contains:

- `PostKey`: destination numeric user ID, Captain type and event ID;
- immutable assignment: assignment/generation IDs, event code and deadline/timing;
- accepted attempt ID and handoff digest;
- top three and Differential, each with source ordinal, rank and immutable official
  selection (ID, web name, ownership, team, projection and rendered-fixture inputs);
- official event fixture records used, including kickoff timestamps;
- exact canonical tweet, weighted length and UTC validation timestamp.

`CandidateRejected` exposes a deterministic allowlisted classification rather than
raw transport errors. Ambiguous/started/claimed posting states also block candidate
creation; failed-before-write history alone does not.

## Publisher boundary

A candidate is evidence, not authorization, and the frozen type is not a security
capability. Separate repository reads and the two FPL requests are not a distributed
transaction. The later publisher must authenticate its input, freshly validate FPL
and current state/time again, atomically claim through Captain's event-level barrier,
and perform the guarded final checks immediately before any X write. It must retain
uncertainty after a possible write rather than replaying automatically.

No Milestone 1–4 semantics are changed. Session expiry/refresh remains unknown;
authenticated readiness evidence is unchanged and no cookie API is introduced.
