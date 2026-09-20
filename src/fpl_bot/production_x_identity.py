"""Reviewed immutable production X identity; populated only after no-post authorization."""

from typing import Final

# This deliberately prevents production publisher composition and deployment until
# a separately reviewed /2/users/me result identifies the intended account.
PRODUCTION_X_USER_ID: Final[str | None] = None
