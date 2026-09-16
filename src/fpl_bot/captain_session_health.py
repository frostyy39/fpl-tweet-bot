"""Non-secret observations; no cookie, browser-storage or credential access."""

from dataclasses import dataclass
from datetime import datetime

from fpl_bot.captain_handoff import AuthenticationStatus
from fpl_bot.captain_state import RefreshObservation, SessionExpiryKind, SessionHealthEvidence


@dataclass(frozen=True, slots=True)
class SessionObservation:
    observed_at: datetime
    authentication: AuthenticationStatus
    expiry_kind: SessionExpiryKind = SessionExpiryKind.UNKNOWN
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        SessionHealthEvidence(
            self.observed_at, self.authentication, self.expiry_kind, self.expires_at
        )


def summarize_session(
    observations: tuple[SessionObservation, ...], next_target: datetime | None
) -> SessionHealthEvidence | None:
    """Only a comparable expiry increase is an observed metadata refresh, not session proof.

    The real browser currently supplies UNKNOWN expiry, so its refresh stays UNKNOWN.
    No value/token comparison is performed. Metadata-only producers may supply known
    expiry classifications in future; absent or incomparable data never implies renewal.
    """
    if type(observations) is not tuple or not all(
        isinstance(o, SessionObservation) for o in observations
    ):
        raise ValueError("invalid session observations")
    if not observations:
        return None
    if any(
        b.observed_at < a.observed_at for a, b in zip(observations, observations[1:], strict=False)
    ):
        raise ValueError("session observation chronology")
    before, after = observations[0], observations[-1]
    refresh = RefreshObservation.UNKNOWN
    if (
        len(observations) > 1
        and before.expiry_kind == after.expiry_kind != SessionExpiryKind.UNKNOWN
    ):
        if before.expires_at == after.expires_at:
            refresh = RefreshObservation.NOT_OBSERVED
        elif before.expiry_kind == SessionExpiryKind.FIXED and after.expires_at > before.expires_at:
            refresh = RefreshObservation.OBSERVED
    return SessionHealthEvidence(
        after.observed_at,
        after.authentication,
        after.expiry_kind,
        after.expires_at,
        next_target,
        refresh,
        after.authentication == AuthenticationStatus.REQUIRED,
    )
