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
- selects the next relevant event, preferring a unique future `is_next`, then a unique future
  `is_current`, then the earliest future deadline;
- parses timezone-aware UTC deadlines and converts them to `Europe/London` with `zoneinfo`;
- counts every team's fixtures and classifies the event as GW, BGW, DGW, or BDGW;
- renders the exact V1 tweet and a human-readable diagnostic report.

If FPL exposes no current or future deadline, the command exits cleanly with an explanatory
error instead of guessing. Fixture kickoff times are never used as deadline substitutes.

This milestone cannot post to X. It includes no X client, scheduler, cloud infrastructure,
task queue, Firestore, Secret Manager, persistence, or deployment implementation.

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
