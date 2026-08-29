# FPL Tweet Bot

A deterministic, low-cost automation project for the official Fantasy Premier League (FPL)
deadline tweet:

```text
Good luck everyone 🔒🥳

#FPL #FPLCommunity #{EVENT_CODE}
```

`EVENT_CODE` is derived from live FPL event and fixture data, producing `GWx`, `BGWx`,
`DGWx`, or `BDGWx`. The Gameweek number is never hardcoded.

## Milestone 1

Milestone 1 is a read-only Python core and local dry run. It:

- retrieves events, official `deadline_time` values, and current teams from FPL's
  `bootstrap-static/` endpoint;
- retrieves fixtures assigned to the selected event from `fixtures/?event={id}`;
- selects the next relevant event, accepting a unique future `is_next` only when it does not skip
  an earlier unpassed official deadline, then using a unique future `is_current` or the earliest
  future deadline;
- parses timezone-aware UTC deadlines and converts them to `Europe/London` with `zoneinfo`;
- requires exactly 20 unique current teams and at least one fixture assigned to the event;
- counts every team's fixtures and classifies the event as GW, BGW, DGW, or BDGW;
- renders the exact V1 tweet and a human-readable diagnostic report.

If FPL exposes no current or future deadline, contradicts its `is_next` chronology, returns a team
count other than 20, or returns zero event fixtures, the command exits cleanly with an explanatory
error instead of guessing. Fixture kickoff times are never used as deadline substitutes.

The Milestone 1 command cannot post to X. It includes no scheduler, cloud infrastructure,
task queue, Firestore, Secret Manager, persistence, or deployment implementation.

## Milestone 2A: X API Safety Foundation

Milestone 2A adds a focused, programmatic client for the official X API v2 endpoints
[`GET /2/users/me`](https://docs.x.com/x-api/users/get-my-user) and
[`POST /2/tweets`](https://docs.x.com/x-api/posts/create-post). It adds no X CLI, credentials,
automatic token acquisition or refresh, persistent idempotency, scheduler, or real-post
validation. **Do not attempt a real Post during Milestone 2A.**

The chosen authentication design is OAuth 2.0 Authorization Code with PKCE. X documents both
OAuth 1.0a User Context and OAuth 2.0 PKCE for these endpoints, and
[recommends OAuth 2.0 for new integrations](https://docs.x.com/x-api/getting-started/getting-access)
that need granular scopes. The eventual unattended flow will request
`tweet.read`, `users.read`, `tweet.write`, and `offline.access`: `offline.access` supplies a
refresh token so an access token can be renewed without repeating interactive authorization at
every FPL deadline. App-only authentication cannot call `/2/users/me` or create a Post on behalf
of a user. See X's
[authentication mapping](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping)
and [OAuth 2.0 PKCE guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code).

Configuration is read through these environment-variable names; no values or secret files belong
in Git:

| Name | Purpose |
| --- | --- |
| `X_ENVIRONMENT` | Must be the only supported write environment, `test`; there is no production mode. |
| `X_POSTING_ENABLED` | Must be explicitly `true`; absence defaults to disabled. |
| `X_EXPECTED_USER_ID` | Expected immutable numeric ID of the dedicated test account. |
| `X_USER_ACCESS_TOKEN` | OAuth 2.0 user-context access token; its presence alone never enables posting. |

Before a write, the client requires every configuration guard, calls `/2/users/me`, validates the
response, and compares its numeric user ID with `X_EXPECTED_USER_ID`. A mismatch fails closed;
username is validated but is not the identity key because it can change. Test and future
production accounts must use different IDs and secrets. Production mode and production
credentials remain absent.

The future Milestone 2B controlled test will use the private zero-follower account, explicit write
enablement, its configured numeric ID, and an unmistakable caller-supplied message such as
`FPL Bot API integration test — TEST ACCOUNT ONLY — <timestamp>`. It will not use the deterministic
V1 deadline tweet. A create-Post timeout, connection failure, server error, or malformed HTTP 201
is surfaced as an ambiguous write outcome and is never retried automatically. Credentialed X
requests never follow HTTP redirects, so bearer credentials cannot be forwarded to a redirected
destination. An unexpected read redirect is a typed API failure; an unexpected create-Post
redirect or HTTP 408 is an ambiguous write outcome.

## Requirements and Installation

- Python 3.11 or newer
- Internet access only for the live dry run

Create a virtual environment and install the package with development tools:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Runtime HTTP access uses the Python standard library. The only conditional runtime dependency
is `tzdata`, used as the standard timezone database fallback on Windows.

## Development Commands

Run the complete deterministic test suite (all external responses are mocked):

```bash
python -m pytest
```

Check lint and formatting without changing files:

```bash
python -m ruff check .
python -m ruff format --check .
```

Apply formatting locally when needed:

```bash
python -m ruff format .
```

## Live FPL Dry Run

Run either command from the repository root:

```bash
python -m fpl_bot
# Or, after installation:
fpl-bot
```

The command queries only FPL-hosted JSON endpoints and prints the event ID, official deadline in
UTC and London time, classification, event code, blank/double teams, fixture count for every team,
and rendered tweet. It has no publishing side effect and requires no credentials.

## Classification Rules

- **GW:** every current Premier League team has exactly one fixture.
- **BGW:** at least one team is blank and no team has multiple fixtures.
- **DGW:** at least one team has multiple fixtures and no team is blank.
- **BDGW:** at least one team is blank and at least one has multiple fixtures.

See `AGENTS.md` for permanent engineering, reliability, cost, and scope constraints.
