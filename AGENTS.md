# Repository Guidelines

## Mission and V1 Scope

Build and maintain a reliable, low-cost FPL automation platform. V1 has one objective: publish this predefined X post at every official Fantasy Premier League deadline:

```text
Good luck everyone 🔒🥳

#FPL #FPLCommunity #{EVENT_CODE}
```

Derive `EVENT_CODE` automatically as `GWx`, `BGWx`, `DGWx`, or `BDGWx`. Never hardcode the Gameweek number. V1 contains no other tweet types; proposed additions require explicit scope approval.

## Authoritative Data and Time Handling

Treat live FPL data as authoritative for the event ID, official deadline, and event fixtures. Use the official FPL deadline when available; never infer it from Premier League kickoff times. Use timezone-aware UTC datetimes internally; never use naive datetimes for scheduling or deadline comparisons. Convert to `Europe/London` only for human-readable deadlines and same-calendar-day scheduling, including correct GMT/BST transitions.

Classify an event from its fixtures:

- **GW:** all 20 teams have exactly one fixture.
- **BGW:** at least one team has zero fixtures and no team has multiple fixtures.
- **DGW:** at least one team has multiple fixtures and no team is blank.
- **BDGW:** both blank-team and multiple-fixture-team conditions exist.

## Scheduling and Posting Safety

Automatically detect the next official FPL deadline. Arm the final posting task only when that deadline falls on the current `Europe/London` calendar day, and target the official deadline timestamp itself. Never intentionally post early. A preflight or warmup may run shortly beforehand, but it must publish nothing.

A scheduled posting task must carry at minimum the expected FPL event ID and expected official deadline timestamp. Immediately before publishing, re-fetch authoritative FPL data and require both values to match the currently authoritative event and deadline. If either differs, the task is stale and must fail closed without publishing. Provide a safe dry-run mode that exercises decision-making and rendering without publishing or recording a false success.

Use the official X API wherever economically viable. Production architecture must not use Selenium, browser automation, X cookies, or automated browser login.

Posting must be idempotent, with successful-post duplicate prevention keyed primarily by FPL event ID. Once an event ID is recorded as successfully posted, never deliberately publish the V1 deadline tweet for that event again, even if its deadline later differs. Persist enough state to identify the successful event and retain the resulting X Post ID. Task naming or versioning may include both event ID and expected deadline, but it must not weaken event-ID-based successful-post duplicate prevention. Handle ambiguous API or network failures by reconciling outcome safely; do not blindly retry when the first request may have succeeded.

## Architecture, Cost, and Secrets

The preferred V1 stack is Python, GitHub, Google Cloud Run, Google Cloud Scheduler, Google Cloud Tasks, Google Secret Manager, the official X API, and Firestore only where persistent audit or idempotency state requires it. Prefer scale-to-zero services and minimal infrastructure. Target £0–£5 per month; the V1 hard ceiling is £10 per month unless explicitly approved.

Do not add an OpenAI or other LLM runtime dependency when deterministic Python can solve the task. Never commit credentials, tokens, cookies, or `.env` files. Store production secrets in Google Secret Manager or an equivalent managed secret store, and provide only redacted configuration examples.

## Project Structure and Local Commands

The repository currently contains only `README.md` and this guide. Place Python packages under `src/`, tests under `tests/`, and deployment configuration in clearly named root-level or `deploy/` files. Separate FPL ingestion, event classification, scheduling, X publishing, and persistence behind focused interfaces.

No build or test tooling exists yet. When adding it, expose and document root-level commands for local execution, formatting/linting, the full test suite, and dry-run operation. Keep `README.md` and deployment documentation synchronized with actual production behaviour; GitHub is the canonical source for application code and deployment documentation.

## Coding and Testing Discipline

Use standard Python conventions: four-space indentation, `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants and environment variables. Adopt committed formatter, linter, and test configuration with the first implementation. Keep integrations mockable and tests deterministic; tests must never send a real post.

Every change affecting scheduling, posting, or duplicate prevention requires tests. At minimum, cover:

- regular GW, BGW, DGW, and BDGW classification;
- deadline parsing and GMT/BST handling;
- same-day scheduling;
- duplicate prevention and changed-deadline rejection;
- exact tweet rendering.

## Commits and Pull Requests

Make small, auditable changes. Use short imperative commit subjects, optionally Conventional Commits (for example, `feat: classify double gameweeks`). Pull requests must describe behaviour and motivation, include test evidence, identify configuration or deployment effects, and show sample output when tweet rendering changes. Do not mix unrelated infrastructure, behaviour, or documentation changes.
