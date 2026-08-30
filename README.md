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

## Milestone 2B: Local Test-Account Authorization Helper

Milestone 2B adds a local OAuth 2.0 Authorization Code with PKCE helper. It requests exactly
`tweet.read users.read tweet.write offline.access`, generates a fresh cryptographic state and S256
PKCE challenge, and listens only at the registered callback
`http://127.0.0.1:8765/callback`. It validates state before exchanging the short-lived code and
handles denial callbacks without echoing provider-supplied details.

For the configured confidential client, the helper follows X's current
[user access-token guide](https://docs.x.com/fundamentals/authentication/oauth-2-0/user-access-token):
it authenticates the token request with HTTP Basic Client ID/Client Secret and posts the code,
`authorization_code` grant, exact redirect URI, and PKCE verifier to
`https://api.x.com/2/oauth2/token`. It requires both an access token and refresh token, then calls
only `GET /2/users/me`. It never enables posting or calls `POST /2/tweets`.

The helper reads these names from the process environment. Do not put their values in Git, a
`.env` file, command arguments, shell history, or screenshots:

| Name | Purpose |
| --- | --- |
| `X_OAUTH_CLIENT_ID` | Confidential OAuth 2.0 Client ID. |
| `X_OAUTH_CLIENT_SECRET` | Confidential OAuth 2.0 Client Secret. |
| `X_OAUTH_TOKEN_OUTPUT_FILE` | New absolute encrypted handoff file path outside this repository. |

The output file must not already exist. For the real Windows run, the complete JSON handoff is
encrypted in memory with Windows DPAPI before the file is created. DPAPI uses the current Windows
user context; the helper never writes a plaintext token file. The file contains only a format
marker and ciphertext, cannot be moved to another Windows user as a portable secret, and remains
outside Git. Encryption or persistence failure creates no usable handoff and fails closed.

Non-Windows development retains an exclusively created plaintext handoff with POSIX mode `0600`,
but that path is not used for the actual Windows Milestone 2B authorization. Neither path is
production persistence.

When explicitly approved for a real test-account authorization, run one of:

```bash
python -m fpl_bot.x_oauth_cli
# Or, after installation:
fpl-bot-x-authorize
```

The browser opens automatically; the authorization URL is not printed. Successful terminal output
contains only the authenticated numeric user ID, username, generic handoff confirmation, and a
reminder that posting remains disabled. **Do not run this helper until the controlled real OAuth
authorization is explicitly approved.**

The first authorization does not require a pre-known numeric account ID. After token acquisition,
the helper performs only the read-only `/2/users/me` request and prints its numeric user ID and
username. Manually confirm that identity is the dedicated private test account. Its numeric ID then
becomes the canonical `X_EXPECTED_USER_ID` for the later controlled Post; obtaining it does not
enable posting.

## One-Shot X Test Post

The one-shot runner reuses the existing guarded X API client; it adds no authentication or HTTP
implementation. Its fixed message is `FPL Bot API integration test — TEST ACCOUNT ONLY`, which is
deliberately separate from the FPL deadline renderer. By default it only previews that message and
makes no X API request:

```bash
python -m fpl_bot.test_post
# Or, after installation:
fpl-bot-x-test-post
```

An approved manual test against the dedicated private test account requires both the explicit
`--live` option and every existing write guard:

```bash
python -m fpl_bot.test_post --live
# Or: fpl-bot-x-test-post --live
```

Configure `X_ENVIRONMENT=test`, `X_POSTING_ENABLED=true`, `X_EXPECTED_USER_ID`, and
`X_USER_ACCESS_TOKEN` in the process environment. Never place their values in Git, `.env` files,
command arguments, screenshots, or documentation. The expected ID must be the numeric ID manually
verified during the OAuth helper's `/2/users/me` bootstrap. The access token must have
`tweet.read users.read tweet.write offline.access` authorization.

The OAuth helper remains the sole local token-acquisition path, and its DPAPI file remains the
encrypted at-rest handoff on Windows. The runner neither reimplements OAuth nor persists tokens;
for an approved Windows test, load the access token from that handoff into the current process
environment without printing it.

Live mode validates configuration before creating a client, re-verifies `/2/users/me`, and sends
exactly one `POST /2/tweets` request without retries. On success it prints only the resulting Post
ID. Ambiguous outcomes must not be retried manually until reconciled. This runner has no production
mode and is not connected to FPL event selection, deadline tweets, scheduling, persistence, cloud
infrastructure, or deployment. **Do not use `--live` until the controlled test is separately
approved.**

## Requirements and Installation

- Python 3.11 or newer
- Internet access only for the live FPL dry run, an explicitly initiated OAuth helper, or the
  separately approved one-shot X test

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
