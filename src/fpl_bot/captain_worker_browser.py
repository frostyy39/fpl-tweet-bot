"""Projection-only bridge to the proven browser lifecycle and canonical table parser."""

from pathlib import Path

from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CleanupStatus,
    CompletenessStatus,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_review_browser import (
    PlaywrightReviewBrowserAcquirer,
    parse_review_projection_table,
)
from fpl_bot.captain_worker import AcquiredDataset
from fpl_bot.errors import CaptainReviewBrowserError


class ProvenBrowserAcquisition:
    def __init__(self, profile_directory: Path) -> None:
        self.profile_directory = profile_directory

    def acquire(self, event_id: int) -> AcquiredDataset:
        observations = []
        acquirer = PlaywrightReviewBrowserAcquirer(
            self.profile_directory, session_observer=observations.append
        )
        snapshot = acquirer.acquire(event_id)  # Owns/closes Chrome and releases the profile.
        if (
            acquirer.last_lifecycle.get("authentication") != "authenticated"
            or acquirer.last_lifecycle.get("ownership") != "released"
        ):
            raise CaptainReviewBrowserError("browser_profile_unclean")
        table = parse_review_projection_table(snapshot, event_id)
        return AcquiredDataset(
            tuple(
                ProjectionRecord(
                    row.source_row_ordinal,
                    row.display_name,
                    row.team_short_code,
                    row.projected_points,
                )
                for row in table.rows
            ),
            RowCounts(
                table.raw_body_row_count,
                table.genuine_player_row_count,
                table.auxiliary_row_count,
                len(table.rows),
            ),
            CompletenessStatus.COMPLETE,
            AuthenticationStatus.AUTHENTICATED,
            CleanupStatus.RELEASED,
            tuple(observations),
        )
