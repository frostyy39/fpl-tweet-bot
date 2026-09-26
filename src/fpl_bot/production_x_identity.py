"""Reviewed immutable production X identity; populated only after no-post authorization."""

from typing import Final

# Established by the separately reviewed no-post OAuth authorization and read-only
# /2/users/me verification on 2026-09-26.  This is an identity guard, never a token.
PRODUCTION_X_USER_ID: Final[str | None] = "1249335464571650048"
