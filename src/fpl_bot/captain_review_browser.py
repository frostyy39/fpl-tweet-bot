"""Local-only FPL Review acquisition through a dedicated authenticated browser profile."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from fpl_bot.captain_browser_runtime import (
    chrome_version,
    discover_chrome,
    own_profile,
    reject_personal_profile,
    require_keyring,
    stable_candidates,
)
from fpl_bot.captain_models import CaptainProjection
from fpl_bot.captain_session_health import AuthenticationStatus, SessionObservation
from fpl_bot.errors import CaptainReviewBrowserError
from fpl_bot.models import FplPlayer, Team

FPL_REVIEW_APP_URL = "https://app.fplreview.com/"
DEDICATED_PROFILE_MARKER = ".fpl-review-captain-profile"
PLAYWRIGHT_CHROME_CHANNEL = "chrome"
_LEGACY_PROFILE_MARKER = "Dedicated FPL Review Captain browser profile.\n"
_STABLE_PROFILE_MARKER = "Dedicated FPL Review Captain browser profile.\nbrowser_channel=chrome\n"
_POINTS_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_GW_HEADER_PATTERN = re.compile(
    r"^(?:GW\s*(?P<prefixed>\d+)(?:\s*(?:PTS|POINTS|EV))?|"
    r"(?P<suffixed>\d+)\s*(?:PTS|POINTS|EV))$",
    re.IGNORECASE,
)
_TEAM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,4}$")
_TEAM_POSITION_PATTERN = re.compile(
    r"^(?P<team>[A-Z0-9]{2,4})\s*•\s*(?:GK|GKP|DEF|MID|FWD)$",
    re.IGNORECASE,
)
_MAX_DIAGNOSTIC_TEXT = 120
_MAX_DIAGNOSTIC_CLASS = 160
_MAX_STRUCTURAL_SAMPLE_ROWS = 3
_MAX_READINESS_OBSERVATIONS = 40
_MAX_NAV_CONTROL_TEXTS = 8
_READINESS_POLL_MILLISECONDS = 200
_REQUIRED_STABLE_OBSERVATIONS = 2


@dataclass(frozen=True, slots=True)
class ReviewDomCell:
    text: str
    visible: bool = True
    tag_name: str = ""
    class_name: str = ""
    aria_hidden: str | None = None
    hidden: bool = False
    display: str = ""
    visibility: str = ""


@dataclass(frozen=True, slots=True)
class ReviewDomTable:
    header_rows: tuple[tuple[ReviewDomCell, ...], ...]
    body_rows: tuple[tuple[ReviewDomCell, ...], ...]


@dataclass(frozen=True, slots=True)
class ReviewPageSnapshot:
    tables: tuple[ReviewDomTable, ...]
    login_required: bool = False
    premium_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class ReviewBrowserAcquisition:
    acquired_at_utc: datetime
    total_projection_rows: int
    resolved_candidate_count: int


@dataclass(frozen=True, slots=True)
class ReviewOfficialIdentity:
    web_name: str
    team_short_code: str
    element_id: int


@dataclass(frozen=True, slots=True)
class ReviewIdentityDiagnostic:
    source_row_ordinal: int
    review_display_name: str
    review_team_short_code: str
    normalized_review_name: str
    normalized_review_team_code: str
    exact_official_match_count: int
    exact_name_exists: bool
    exact_name_official_matches: tuple[ReviewOfficialIdentity, ...]
    review_team_code_exists: bool
    same_team_official_players: tuple[ReviewOfficialIdentity, ...]
    category: str


@dataclass(frozen=True, slots=True)
class ReviewIdentityDiagnosticSummary:
    event_id: int
    matched_gameweek_column: str
    acquired_at_utc: datetime
    total_projection_rows: int
    resolved_candidate_count: int
    failures: tuple[ReviewIdentityDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ReviewLogicalProjectionRow:
    source_row_ordinal: int
    display_name: str
    team_short_code: str
    projected_points: Decimal


@dataclass(frozen=True, slots=True)
class ReviewCanonicalProjectionTable:
    event_id: int
    matched_gameweek_column: str
    raw_header_count: int
    logical_headers: tuple[str, ...]
    logical_header_raw_indexes: tuple[int, ...]
    representative_raw_row_cell_count: int
    logical_row_cell_count: int
    raw_body_row_count: int
    genuine_player_row_count: int
    auxiliary_row_count: int
    auxiliary_reason: str
    rows: tuple[ReviewLogicalProjectionRow, ...]


@dataclass(frozen=True, slots=True)
class ReviewRowExtractionFailure:
    source_row_ordinal: int
    player_cell_text: str
    selected_gameweek_cell_text: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewRowExtractionDiagnostic:
    raw_body_row_count: int
    genuine_player_row_count: int
    auxiliary_row_count: int
    auxiliary_reason: str
    failures: tuple[ReviewRowExtractionFailure, ...]


@dataclass(frozen=True, slots=True)
class ReviewStructuralCell:
    index: int
    text: str
    tag_name: str
    class_name: str
    aria_hidden: str | None
    hidden: bool
    display: str
    visibility: str


@dataclass(frozen=True, slots=True)
class ReviewStructuralRow:
    source_row_ordinal: int
    cell_count: int
    cells: tuple[ReviewStructuralCell, ...]


@dataclass(frozen=True, slots=True)
class ReviewTableStructuralInspection:
    table_index: int
    header_row_index: int
    total_header_count: int
    headers: tuple[ReviewStructuralCell, ...]
    sample_rows: tuple[ReviewStructuralRow, ...]


@dataclass(frozen=True, slots=True)
class ReviewHeaderSignature:
    table_index: int
    header_row_index: int
    headers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewReadinessObservation:
    observed_at_utc: datetime
    elapsed_milliseconds: int
    visible_projections_control_count: int
    semantic_block_count: int
    player_present: bool
    price_present: bool
    expected_gameweek_present: bool
    total_present: bool
    elite_ownership_present: bool
    header_signatures: tuple[ReviewHeaderSignature, ...]
    relevant_control_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewNavigationDiagnostic:
    app_loaded_at_utc: datetime
    initial_public_url: str
    final_public_url: str
    initial_semantic_table_present: bool
    visible_projections_control_count: int
    before_click_at_utc: datetime | None
    click_completed: bool
    after_click_at_utc: datetime | None
    first_player_at_utc: datetime | None
    first_price_at_utc: datetime | None
    first_expected_gameweek_at_utc: datetime | None
    first_total_at_utc: datetime | None
    first_elite_ownership_at_utc: datetime | None
    all_required_same_table_at_utc: datetime | None
    ready_at_utc: datetime | None
    observations: tuple[ReviewReadinessObservation, ...]
    final_header_signatures: tuple[ReviewHeaderSignature, ...]


@dataclass(frozen=True, slots=True)
class _ProjectionViewResult:
    snapshot: ReviewPageSnapshot
    diagnostic: ReviewNavigationDiagnostic


class _ProjectionViewFailure(Exception):
    def __init__(self, category: str, diagnostic: ReviewNavigationDiagnostic) -> None:
        self.category = category
        self.diagnostic = diagnostic
        super().__init__(category)


class _RowExtractionFailure(Exception):
    def __init__(self, diagnostic: ReviewRowExtractionDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__("invalid_projection_table")


@dataclass(slots=True)
class _NavigationTrace:
    app_loaded_at_utc: datetime
    initial_public_url: str
    initial_semantic_table_present: bool = False
    visible_projections_control_count: int = 0
    before_click_at_utc: datetime | None = None
    click_completed: bool = False
    after_click_at_utc: datetime | None = None
    first_player_at_utc: datetime | None = None
    first_price_at_utc: datetime | None = None
    first_expected_gameweek_at_utc: datetime | None = None
    first_total_at_utc: datetime | None = None
    first_elite_ownership_at_utc: datetime | None = None
    all_required_same_table_at_utc: datetime | None = None
    observations: list[ReviewReadinessObservation] | None = None
    final_header_signatures: tuple[ReviewHeaderSignature, ...] = ()

    def build(
        self,
        *,
        final_public_url: str,
        ready_at_utc: datetime | None,
    ) -> ReviewNavigationDiagnostic:
        return ReviewNavigationDiagnostic(
            app_loaded_at_utc=self.app_loaded_at_utc,
            initial_public_url=self.initial_public_url,
            final_public_url=final_public_url,
            initial_semantic_table_present=self.initial_semantic_table_present,
            visible_projections_control_count=self.visible_projections_control_count,
            before_click_at_utc=self.before_click_at_utc,
            click_completed=self.click_completed,
            after_click_at_utc=self.after_click_at_utc,
            first_player_at_utc=self.first_player_at_utc,
            first_price_at_utc=self.first_price_at_utc,
            first_expected_gameweek_at_utc=self.first_expected_gameweek_at_utc,
            first_total_at_utc=self.first_total_at_utc,
            first_elite_ownership_at_utc=self.first_elite_ownership_at_utc,
            all_required_same_table_at_utc=self.all_required_same_table_at_utc,
            ready_at_utc=ready_at_utc,
            observations=tuple(self.observations or ()),
            final_header_signatures=self.final_header_signatures,
        )


class ReviewBrowserAcquirer(Protocol):
    def acquire(self, event_id: int) -> ReviewPageSnapshot: ...


class FplReviewBrowserProjectionSource:
    """Resolve a freshly rendered Review table to authoritative FPL element IDs."""

    def __init__(
        self,
        acquirer: ReviewBrowserAcquirer,
        players: Sequence[FplPlayer],
        teams: Sequence[Team],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._acquirer = acquirer
        self._players = tuple(players)
        self._teams = tuple(teams)
        self._clock = clock
        self.last_acquisition: ReviewBrowserAcquisition | None = None
        self.last_projections: tuple[CaptainProjection, ...] | None = None
        self.last_identity_diagnostic: ReviewIdentityDiagnosticSummary | None = None
        self.last_canonical_table: ReviewCanonicalProjectionTable | None = None
        self.last_row_extraction_diagnostic: ReviewRowExtractionDiagnostic | None = None
        self.last_structural_inspections: tuple[ReviewTableStructuralInspection, ...] = ()

    def fetch_event_projections(self, event_id: int) -> tuple[CaptainProjection, ...]:
        _require_event_id(event_id)
        snapshot = self._acquirer.acquire(event_id)
        if snapshot.login_required:
            raise CaptainReviewBrowserError("reauthentication_required")
        if snapshot.premium_unavailable:
            raise CaptainReviewBrowserError("premium_unavailable")

        self.last_structural_inspections = inspect_review_table_structure(snapshot, event_id)

        acquired_at = self._clock()
        if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
            raise CaptainReviewBrowserError("invalid_projection_table")
        acquired_at_utc = acquired_at.astimezone(UTC)
        try:
            canonical = _parse_review_projection_table(snapshot, event_id)
        except _RowExtractionFailure as exc:
            self.last_row_extraction_diagnostic = exc.diagnostic
            raise CaptainReviewBrowserError("invalid_projection_table") from None
        self.last_canonical_table = canonical
        projections, diagnostic = _resolve_canonical_projection_table(
            canonical,
            self._players,
            self._teams,
            acquired_at_utc=acquired_at_utc,
        )
        if diagnostic.failures:
            self.last_identity_diagnostic = diagnostic
            self.last_acquisition = ReviewBrowserAcquisition(
                acquired_at_utc=acquired_at_utc,
                total_projection_rows=diagnostic.total_projection_rows,
                resolved_candidate_count=diagnostic.resolved_candidate_count,
            )
            raise CaptainReviewBrowserError("identity_resolution_failed")
        self.last_acquisition = ReviewBrowserAcquisition(
            acquired_at_utc=acquired_at_utc,
            total_projection_rows=len(projections),
            resolved_candidate_count=len(projections),
        )
        self.last_projections = projections
        return projections


class PlaywrightReviewBrowserAcquirer:
    """Freshly load Review in an isolated persistent Chromium profile."""

    def __init__(
        self,
        profile_directory: Path,
        *,
        timeout_seconds: float = 30.0,
        session_observer: Callable[[SessionObservation], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._profile_directory = require_dedicated_profile(profile_directory)
        self._timeout_milliseconds = int(timeout_seconds * 1000)
        self.last_navigation_diagnostic: ReviewNavigationDiagnostic | None = None
        self.last_lifecycle: dict[str, str] = {}
        self._session_observer = session_observer

    def _observe_session(self, authentication: AuthenticationStatus) -> None:
        if self._session_observer is not None:
            # Public readiness evidence only. No expiry/renewal inference or secret APIs.
            try:
                self._session_observer(SessionObservation(datetime.now(UTC), authentication))
            except Exception:
                raise CaptainReviewBrowserError("session_observation_failed") from None

    def acquire(self, event_id: int) -> ReviewPageSnapshot:
        self.last_lifecycle = {
            "platform": sys.platform,
            "profile_classification": "captain_dedicated_external",
            "started_at_utc": datetime.now(UTC).isoformat(),
            "authentication": "not_checked",
            "ownership": "not_acquired",
        }
        try:
            executable = find_stable_chrome_executable()
            self.last_lifecycle.update(
                chrome_executable=str(executable),
                chrome_version=chrome_version(executable),
                playwright_version=version("playwright"),
                keyring=require_keyring(),
            )
            with own_profile(self._profile_directory):
                self.last_lifecycle["ownership"] = "exclusive"
                failure = None
                try:
                    snapshot = self._acquire(event_id, executable)
                except CaptainReviewBrowserError as exc:
                    if exc.category in {"browser_launch_failed", "browser_profile_unclean"}:
                        raise
                    failure = exc
            self.last_lifecycle.update(ownership="released", authentication="authenticated")
            if failure is not None:
                self.last_lifecycle["authentication"] = (
                    "reauthentication_required"
                    if failure.category == "reauthentication_required"
                    else "not_confirmed"
                )
                raise failure
            return snapshot
        except PackageNotFoundError:
            raise CaptainReviewBrowserError("browser_dependency_unavailable") from None
        finally:
            self.last_lifecycle["ended_at_utc"] = datetime.now(UTC).isoformat()

    def _acquire(self, event_id: int, executable: Path) -> ReviewPageSnapshot:
        _require_event_id(event_id)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CaptainReviewBrowserError("browser_dependency_unavailable") from None

        try:
            with sync_playwright() as playwright:
                context = _launch_stable_chrome_context(
                    playwright.chromium,
                    self._profile_directory,
                    headless=True,
                    executable=executable,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        FPL_REVIEW_APP_URL,
                        wait_until="domcontentloaded",
                        timeout=self._timeout_milliseconds,
                    )
                    self._observe_session(AuthenticationStatus.NOT_CONFIRMED)
                    try:
                        result = _wait_for_projections_view(
                            page,
                            event_id,
                            self._timeout_milliseconds,
                            app_loaded_at_utc=datetime.now(UTC),
                        )
                    except _ProjectionViewFailure as exc:
                        self.last_navigation_diagnostic = exc.diagnostic
                        if exc.category == "reauthentication_required":
                            self._observe_session(AuthenticationStatus.REQUIRED)
                        raise CaptainReviewBrowserError(exc.category) from None
                    self.last_navigation_diagnostic = result.diagnostic
                    self._observe_session(AuthenticationStatus.AUTHENTICATED)
                    return result.snapshot
                finally:
                    try:
                        context.close()
                    except Exception:
                        raise CaptainReviewBrowserError("browser_profile_unclean") from None
        except CaptainReviewBrowserError:
            raise
        except PlaywrightTimeoutError:
            raise CaptainReviewBrowserError("table_load_timeout") from None
        except Exception:
            raise CaptainReviewBrowserError("browser_launch_failed") from None


def extract_review_projections(
    snapshot: ReviewPageSnapshot,
    event_id: int,
    players: Sequence[FplPlayer],
    teams: Sequence[Team],
) -> tuple[CaptainProjection, ...]:
    """Parse and resolve the same canonical semantic table used by diagnostics."""
    canonical = parse_review_projection_table(snapshot, event_id)
    projections, diagnostic = _resolve_canonical_projection_table(
        canonical,
        players,
        teams,
        acquired_at_utc=datetime.now(UTC),
    )
    if diagnostic.failures:
        raise CaptainReviewBrowserError("identity_resolution_failed")
    return projections


def collect_review_identity_diagnostics(
    snapshot: ReviewPageSnapshot,
    event_id: int,
    players: Sequence[FplPlayer],
    teams: Sequence[Team],
    *,
    acquired_at_utc: datetime,
) -> ReviewIdentityDiagnosticSummary:
    """Collect every exact-identity failure from the canonical semantic table."""
    canonical = parse_review_projection_table(snapshot, event_id)
    _, diagnostic = _resolve_canonical_projection_table(
        canonical,
        players,
        teams,
        acquired_at_utc=acquired_at_utc,
    )
    return diagnostic


def parse_review_projection_table(
    snapshot: ReviewPageSnapshot,
    event_id: int,
) -> ReviewCanonicalProjectionTable:
    """Convert one raw rendered table into one unambiguous logical projection table."""
    try:
        return _parse_review_projection_table(snapshot, event_id)
    except _RowExtractionFailure:
        raise CaptainReviewBrowserError("invalid_projection_table") from None


def _parse_review_projection_table(
    snapshot: ReviewPageSnapshot,
    event_id: int,
) -> ReviewCanonicalProjectionTable:
    _require_event_id(event_id)
    if not snapshot.tables:
        raise CaptainReviewBrowserError("invalid_projection_table")

    saw_event_header = any(
        _header_event_id(cell.text) == event_id
        for table in snapshot.tables
        for headers in table.header_rows
        for cell in headers
        if cell.visible
    )
    if not saw_event_header:
        raise CaptainReviewBrowserError("upcoming_event_column_missing")
    alignments = _semantic_projection_alignments(snapshot, event_id)
    if len(alignments) != 1:
        raise CaptainReviewBrowserError("invalid_projection_table")
    table_index, header_row_index, start, stop = alignments[0]
    table = snapshot.tables[table_index]
    headers = table.header_rows[header_row_index]
    logical_headers = headers[start:stop]
    parsed_rows, diagnostic = _extract_projection_rows(
        table.body_rows,
        logical_headers,
        event_id,
    )
    if diagnostic.failures or not parsed_rows:
        raise _RowExtractionFailure(diagnostic)
    return ReviewCanonicalProjectionTable(
        event_id=event_id,
        matched_gameweek_column=f"GW{event_id}",
        raw_header_count=len(headers),
        logical_headers=tuple(_canonical_header_name(cell.text) for cell in logical_headers),
        logical_header_raw_indexes=tuple(range(start, stop)),
        representative_raw_row_cell_count=max(len(row) for row in table.body_rows),
        logical_row_cell_count=len(logical_headers),
        raw_body_row_count=diagnostic.raw_body_row_count,
        genuine_player_row_count=diagnostic.genuine_player_row_count,
        auxiliary_row_count=diagnostic.auxiliary_row_count,
        auxiliary_reason=diagnostic.auxiliary_reason,
        rows=parsed_rows,
    )


def inspect_review_table_structure(
    snapshot: ReviewPageSnapshot,
    event_id: int,
    *,
    max_sample_rows: int = _MAX_STRUCTURAL_SAMPLE_ROWS,
) -> tuple[ReviewTableStructuralInspection, ...]:
    """Capture bounded, allowlisted public DOM structure for matched Review tables."""
    _require_event_id(event_id)
    semantic_rows = {
        (table_index, header_row_index)
        for table_index, header_row_index, _, _ in _semantic_projection_alignments(
            snapshot, event_id
        )
    }
    inspections: list[ReviewTableStructuralInspection] = []
    for table_index, table in enumerate(snapshot.tables):
        for header_row_index, headers in enumerate(table.header_rows):
            if semantic_rows:
                include = (table_index, header_row_index) in semantic_rows
            else:
                include = any(
                    cell.visible and _header_event_id(cell.text) == event_id for cell in headers
                )
            if not include:
                continue
            inspections.append(
                ReviewTableStructuralInspection(
                    table_index=table_index,
                    header_row_index=header_row_index,
                    total_header_count=len(headers),
                    headers=tuple(
                        _structural_cell(index, cell) for index, cell in enumerate(headers)
                    ),
                    sample_rows=tuple(
                        ReviewStructuralRow(
                            source_row_ordinal=ordinal,
                            cell_count=len(row),
                            cells=tuple(
                                _structural_cell(index, cell) for index, cell in enumerate(row)
                            ),
                        )
                        for ordinal, row in enumerate(table.body_rows[:max_sample_rows], start=1)
                    ),
                )
            )
    return tuple(inspections)


def _resolve_canonical_projection_table(
    canonical: ReviewCanonicalProjectionTable,
    players: Sequence[FplPlayer],
    teams: Sequence[Team],
    *,
    acquired_at_utc: datetime,
) -> tuple[tuple[CaptainProjection, ...], ReviewIdentityDiagnosticSummary]:
    return resolve_review_rows(
        canonical.rows,
        players,
        teams,
        event_id=canonical.event_id,
        acquired_at_utc=acquired_at_utc,
    )


def resolve_review_rows(
    rows: Sequence[ReviewLogicalProjectionRow],
    players: Sequence[FplPlayer],
    teams: Sequence[Team],
    *,
    event_id: int,
    acquired_at_utc: datetime,
) -> tuple[tuple[CaptainProjection, ...], ReviewIdentityDiagnosticSummary]:
    """Pure exact identity resolution shared by browser diagnostics and cloud validation."""
    if acquired_at_utc.tzinfo is None or acquired_at_utc.utcoffset() is None:
        raise CaptainReviewBrowserError("invalid_projection_table")
    team_by_id = {team.team_id: team for team in teams}
    if len(team_by_id) != len(teams) or any(player.team_id not in team_by_id for player in players):
        raise CaptainReviewBrowserError("identity_resolution_failed")
    official = tuple(
        ReviewOfficialIdentity(
            web_name=player.web_name,
            team_short_code=team_by_id[player.team_id].short_name.strip().upper(),
            element_id=player.element_id,
        )
        for player in players
    )

    diagnostics: list[ReviewIdentityDiagnostic] = []
    projections: list[CaptainProjection] = []
    resolved_ids: set[int] = set()
    for row in rows:
        normalized_name = _normalize_name(row.display_name)
        normalized_team = row.team_short_code.strip().upper()
        name_matches = tuple(
            item for item in official if _normalize_name(item.web_name) == normalized_name
        )
        team_matches = tuple(item for item in official if item.team_short_code == normalized_team)
        exact_matches = tuple(
            item for item in name_matches if item.team_short_code == normalized_team
        )
        if len(exact_matches) == 1:
            element_id = exact_matches[0].element_id
            if element_id in resolved_ids:
                raise CaptainReviewBrowserError("invalid_projection_table")
            resolved_ids.add(element_id)
            projections.append(CaptainProjection(element_id, row.projected_points))
            continue
        if len(exact_matches) > 1:
            category = "duplicate_exact_match"
        elif not team_matches:
            category = "unknown_team_code"
        elif name_matches:
            category = "exact_name_wrong_team"
        else:
            category = "zero_exact_match"
        diagnostics.append(
            ReviewIdentityDiagnostic(
                source_row_ordinal=row.source_row_ordinal,
                review_display_name=row.display_name,
                review_team_short_code=row.team_short_code,
                normalized_review_name=normalized_name,
                normalized_review_team_code=normalized_team,
                exact_official_match_count=len(exact_matches),
                exact_name_exists=bool(name_matches),
                exact_name_official_matches=name_matches,
                review_team_code_exists=bool(team_matches),
                same_team_official_players=team_matches,
                category=category,
            )
        )

    summary = ReviewIdentityDiagnosticSummary(
        event_id=event_id,
        matched_gameweek_column=f"GW{event_id}",
        acquired_at_utc=acquired_at_utc.astimezone(UTC),
        total_projection_rows=len(rows),
        resolved_candidate_count=len(projections),
        failures=tuple(diagnostics),
    )
    return tuple(projections), summary


def _semantic_header_blocks(
    headers: tuple[ReviewDomCell, ...],
    event_id: int | None,
) -> tuple[tuple[int, int], ...]:
    labels = tuple(_normalize_header(cell.text) for cell in headers)
    blocks: list[tuple[int, int]] = []
    for start, label in enumerate(labels):
        if label != "PLAYER" or start + 4 >= len(labels) or labels[start + 1] != "PRICE":
            continue
        cursor = start + 2
        gameweeks: list[int] = []
        while cursor < len(labels):
            gameweek = _header_event_id(labels[cursor])
            if gameweek is None:
                break
            gameweeks.append(gameweek)
            cursor += 1
        if not gameweeks or (event_id is not None and gameweeks.count(event_id) != 1):
            continue
        if cursor + 1 >= len(labels):
            continue
        if labels[cursor] != "TOTAL" or not _is_elite_ownership_header(labels[cursor + 1]):
            continue
        stop = cursor + 2
        if all(cell.visible for cell in headers[start:stop]):
            blocks.append((start, stop))
    return tuple(blocks)


def _semantic_projection_alignments(
    snapshot: ReviewPageSnapshot,
    event_id: int | None,
) -> tuple[tuple[int, int, int, int], ...]:
    return tuple(
        (table_index, header_row_index, start, stop)
        for table_index, table in enumerate(snapshot.tables)
        if table.body_rows
        for header_row_index, headers in enumerate(table.header_rows)
        for start, stop in _semantic_header_blocks(headers, event_id)
    )


def _extract_projection_rows(
    rows: tuple[tuple[ReviewDomCell, ...], ...],
    logical_headers: tuple[ReviewDomCell, ...],
    event_id: int,
) -> tuple[tuple[ReviewLogicalProjectionRow, ...], ReviewRowExtractionDiagnostic]:
    logical_count = len(logical_headers)
    selected_index = next(
        index
        for index, header in enumerate(logical_headers)
        if _header_event_id(header.text) == event_id
    )
    parsed: list[ReviewLogicalProjectionRow] = []
    failures: list[ReviewRowExtractionFailure] = []
    auxiliary_count = 0
    genuine_count = 0
    for ordinal, row in enumerate(rows, start=1):
        windows = tuple(
            row[start : start + logical_count]
            for start in range(0, len(row) - logical_count + 1)
            if row[start].visible and row[start + selected_index].visible
        )
        if not windows:
            auxiliary_count += 1
            continue
        genuine_count += 1
        if len(windows) != 1:
            failures.append(_row_extraction_failure(ordinal, "ambiguous_logical_alignment"))
            continue
        candidate = windows[0]
        identity = _parse_player_cell(candidate[0].text)
        if identity is None:
            failures.append(
                _row_extraction_failure(
                    ordinal,
                    "invalid_player_cell",
                    player_cell_text=candidate[0].text,
                    selected_gameweek_cell_text=candidate[selected_index].text,
                )
            )
            continue
        projected_points = _try_parse_points(candidate[selected_index].text)
        if projected_points is None:
            failures.append(
                _row_extraction_failure(
                    ordinal,
                    "invalid_selected_gameweek_projection",
                    player_cell_text=candidate[0].text,
                    selected_gameweek_cell_text=candidate[selected_index].text,
                )
            )
            continue
        display_name, team_short_code = identity
        parsed.append(
            ReviewLogicalProjectionRow(
                source_row_ordinal=ordinal,
                display_name=display_name,
                team_short_code=team_short_code,
                projected_points=projected_points,
            )
        )
    diagnostic = ReviewRowExtractionDiagnostic(
        raw_body_row_count=len(rows),
        genuine_player_row_count=genuine_count,
        auxiliary_row_count=auxiliary_count,
        auxiliary_reason="no_visible_logical_column_block",
        failures=tuple(failures),
    )
    return tuple(parsed), diagnostic


def _parse_player_cell(value: str) -> tuple[str, str] | None:
    lines = tuple(_normalize_name(line) for line in value.splitlines() if line.strip())
    if len(lines) < 2 or not lines[0]:
        return None
    team_lines = tuple(
        match for line in lines[1:] if (match := _TEAM_POSITION_PATTERN.fullmatch(line)) is not None
    )
    if len(team_lines) != 1:
        return None
    return lines[0], team_lines[0].group("team").upper()


def _row_extraction_failure(
    source_row_ordinal: int,
    reason: str,
    *,
    player_cell_text: str = "",
    selected_gameweek_cell_text: str = "",
) -> ReviewRowExtractionFailure:
    return ReviewRowExtractionFailure(
        source_row_ordinal=source_row_ordinal,
        player_cell_text=player_cell_text[:_MAX_DIAGNOSTIC_TEXT],
        selected_gameweek_cell_text=(selected_gameweek_cell_text[:_MAX_DIAGNOSTIC_TEXT]),
        reason=reason,
    )


def _is_elite_ownership_header(value: str) -> bool:
    return re.sub(r"\s+", "", value) in {"ELITEOWN%", "ELITEOWNERSHIP%"}


def _canonical_header_name(value: str) -> str:
    event_id = _header_event_id(value)
    if event_id is not None:
        return f"GW{event_id}"
    normalized = _normalize_header(value)
    return "ELITE OWN%" if _is_elite_ownership_header(normalized) else normalized


def _structural_cell(index: int, cell: ReviewDomCell) -> ReviewStructuralCell:
    return ReviewStructuralCell(
        index=index,
        text=cell.text[:_MAX_DIAGNOSTIC_TEXT],
        tag_name=cell.tag_name[:16],
        class_name=cell.class_name[:_MAX_DIAGNOSTIC_CLASS],
        aria_hidden=cell.aria_hidden[:16] if cell.aria_hidden is not None else None,
        hidden=cell.hidden,
        display=cell.display[:32],
        visibility=cell.visibility[:32],
    )


def prepare_dedicated_profile(profile_directory: Path) -> Path:
    """Prepare the stable-Chrome profile without adopting ordinary browser state."""
    resolved = _require_profile_outside_repository(profile_directory)
    marker = resolved / DEDICATED_PROFILE_MARKER
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise CaptainReviewBrowserError("dedicated_profile_required")
        if any(resolved.iterdir()) and not marker.is_file():
            raise CaptainReviewBrowserError("dedicated_profile_required")
        if marker.is_file():
            marker_text = marker.read_text(encoding="utf-8")
            if marker_text == _LEGACY_PROFILE_MARKER:
                _preserve_legacy_chromium_profile(resolved)
                resolved.mkdir()
            elif marker_text != _STABLE_PROFILE_MARKER:
                raise CaptainReviewBrowserError("dedicated_profile_required")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    marker.write_text(_STABLE_PROFILE_MARKER, encoding="utf-8")
    return resolved


def require_dedicated_profile(profile_directory: Path) -> Path:
    resolved = _require_profile_outside_repository(profile_directory)
    marker = resolved / DEDICATED_PROFILE_MARKER
    if not marker.is_file() or marker.read_text(encoding="utf-8") != _STABLE_PROFILE_MARKER:
        raise CaptainReviewBrowserError("dedicated_profile_required")
    return resolved


def find_stable_chrome_executable(candidates: Sequence[Path] | None = None) -> Path:
    """Locate exactly one installed stable Chrome; never download or fall back."""
    paths = tuple(candidates) if candidates is not None else _stable_chrome_candidates()
    return discover_chrome(paths)


def stable_chrome_login_command(
    chrome_executable: Path, profile_directory: Path
) -> tuple[str, ...]:
    """Build the fixed manual-login command without remote debugging or credential arguments."""
    return (
        str(chrome_executable),
        f"--user-data-dir={profile_directory}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        *(("--password-store=gnome-libsecret",) if sys.platform == "linux" else ()),
        FPL_REVIEW_APP_URL,
    )


def open_manual_review_login(
    profile_directory: Path,
    *,
    chrome_executable: Path | None = None,
    process_factory: Callable[[Sequence[str]], Any] = subprocess.Popen,
) -> None:
    """Open stable Chrome for manual login without reading browser session state."""
    try:
        executable = chrome_executable or find_stable_chrome_executable()
        require_keyring()
        profile = prepare_dedicated_profile(profile_directory)
        with own_profile(profile):
            process = process_factory(stable_chrome_login_command(executable, profile))
            print(
                "Complete FPL Review authentication in the dedicated stable Chrome window, "
                "verify Projections, then close that window cleanly."
            )
            return_code = process.wait()
            if return_code != 0:
                raise CaptainReviewBrowserError("browser_profile_unclean")
    except CaptainReviewBrowserError:
        raise
    except Exception:
        raise CaptainReviewBrowserError("browser_launch_failed") from None
    if return_code != 0:
        raise CaptainReviewBrowserError("browser_launch_failed")


def _launch_stable_chrome_context(
    chromium: Any,
    profile_directory: Path,
    *,
    headless: bool,
    executable: Path | None = None,
) -> Any:
    options = {"executable_path": str(executable)} if executable is not None else {}
    # Preserve the Chromium sandbox and OS credential store on every platform.
    options["ignore_default_args"] = ["--password-store=basic", "--use-mock-keychain"]
    options["chromium_sandbox"] = True
    if sys.platform == "linux":
        options["args"] = ["--password-store=gnome-libsecret"]
    return chromium.launch_persistent_context(
        user_data_dir=str(profile_directory),
        channel=PLAYWRIGHT_CHROME_CHANNEL,
        headless=headless,
        viewport={"width": 1440, "height": 1000},
        **options,
    )


def _stable_chrome_candidates() -> tuple[Path, ...]:
    return stable_candidates()


def _preserve_legacy_chromium_profile(profile_directory: Path) -> None:
    backup = profile_directory.with_name(f"{profile_directory.name}.playwright-chromium-backup")
    if backup.parent != profile_directory.parent or backup.exists():
        raise CaptainReviewBrowserError("dedicated_profile_required")
    profile_directory.rename(backup)


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _normalize_header(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).upper().split())


def _header_event_id(value: str) -> int | None:
    compact = " ".join(value.replace("_", " ").split())
    match = _GW_HEADER_PATTERN.fullmatch(compact)
    if match is None:
        return None
    return int(match.group("prefixed") or match.group("suffixed"))


def _try_parse_points(value: str) -> Decimal | None:
    compact = value.strip()
    if not _POINTS_PATTERN.fullmatch(compact):
        return None
    try:
        points = Decimal(compact)
    except InvalidOperation:
        return None
    return points if points.is_finite() else None


def _require_event_id(event_id: int) -> None:
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise CaptainReviewBrowserError("upcoming_event_column_missing")


def _require_profile_outside_repository(profile_directory: Path) -> Path:
    if profile_directory.is_symlink():
        raise CaptainReviewBrowserError("dedicated_profile_required")
    resolved = profile_directory.expanduser().resolve()
    reject_personal_profile(resolved)
    repository = Path(__file__).resolve().parents[2]
    if resolved == repository or repository in resolved.parents:
        raise CaptainReviewBrowserError("dedicated_profile_required")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        ordinary_chrome_root = (Path(local_app_data) / "Google" / "Chrome" / "User Data").resolve()
        if resolved == ordinary_chrome_root or ordinary_chrome_root in resolved.parents:
            raise CaptainReviewBrowserError("dedicated_profile_required")
    return resolved


def _wait_for_projections_view(
    page: Any,
    event_id: int,
    timeout_milliseconds: int,
    *,
    app_loaded_at_utc: datetime,
    utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = monotonic,
    poll_milliseconds: int = _READINESS_POLL_MILLISECONDS,
) -> _ProjectionViewResult:
    """Click once if needed and poll fresh snapshots through one semantic predicate."""
    _require_event_id(event_id)
    if timeout_milliseconds <= 0 or poll_milliseconds <= 0:
        raise ValueError("readiness timing values must be greater than zero")
    if app_loaded_at_utc.tzinfo is None or app_loaded_at_utc.utcoffset() is None:
        raise ValueError("app_loaded_at_utc must be timezone-aware")

    started = monotonic_clock()
    trace = _NavigationTrace(
        app_loaded_at_utc=app_loaded_at_utc.astimezone(UTC),
        initial_public_url=_safe_public_url(page.url),
        observations=[],
    )
    clicked = False
    stable_key: tuple[object, ...] | None = None
    stable_observations = 0
    first_observation = True
    last_snapshot = ReviewPageSnapshot(())

    while True:
        observed_at = _aware_utc_now(utc_clock)
        elapsed_milliseconds = max(0, int((monotonic_clock() - started) * 1000))
        last_snapshot = _snapshot_page(page)
        alignments = _semantic_projection_alignments(last_snapshot, event_id)
        header_signatures = _visible_header_signatures(last_snapshot)
        markers = _semantic_header_markers(last_snapshot, event_id)
        visible_controls, relevant_control_texts = (
            ((), ()) if clicked else _visible_projection_controls(page)
        )
        control_count = len(visible_controls)
        if first_observation:
            trace.initial_semantic_table_present = len(alignments) == 1
            trace.visible_projections_control_count = control_count
            first_observation = False
        _record_readiness_observation(
            trace,
            observed_at=observed_at,
            elapsed_milliseconds=elapsed_milliseconds,
            visible_control_count=control_count,
            semantic_block_count=len(alignments),
            markers=markers,
            header_signatures=header_signatures,
            relevant_control_texts=relevant_control_texts,
        )

        if last_snapshot.login_required:
            _fail_projection_view("reauthentication_required", trace, page)
        if last_snapshot.premium_unavailable:
            _fail_projection_view("premium_unavailable", trace, page)
        if len(alignments) > 1:
            _fail_projection_view("projection_table_unavailable", trace, page)

        if len(alignments) == 1:
            alignment = alignments[0]
            table = last_snapshot.tables[alignment[0]]
            current_key = (
                alignment,
                tuple(cell.text for cell in table.header_rows[alignment[1]]),
                len(table.body_rows),
            )
            if current_key == stable_key:
                stable_observations += 1
            else:
                stable_key = current_key
                stable_observations = 1
            if stable_observations >= _REQUIRED_STABLE_OBSERVATIONS:
                ready_at = _aware_utc_now(utc_clock)
                exact_snapshot = ReviewPageSnapshot(
                    tables=(table,),
                    login_required=False,
                    premium_unavailable=False,
                )
                return _ProjectionViewResult(
                    snapshot=exact_snapshot,
                    diagnostic=trace.build(
                        final_public_url=_safe_public_url(page.url),
                        ready_at_utc=ready_at,
                    ),
                )
        else:
            stable_key = None
            stable_observations = 0
            if not clicked:
                if control_count > 1:
                    _fail_projection_view("projection_table_unavailable", trace, page)
                if control_count == 1:
                    trace.before_click_at_utc = _aware_utc_now(utc_clock)
                    try:
                        visible_controls[0].click(
                            timeout=max(1, timeout_milliseconds - elapsed_milliseconds)
                        )
                    except Exception:
                        _fail_projection_view("projection_table_unavailable", trace, page)
                    trace.click_completed = True
                    trace.after_click_at_utc = _aware_utc_now(utc_clock)
                    clicked = True

        if elapsed_milliseconds >= timeout_milliseconds:
            _fail_projection_view(
                _projection_timeout_category(last_snapshot, event_id),
                trace,
                page,
            )
        remaining = timeout_milliseconds - elapsed_milliseconds
        page.wait_for_timeout(min(poll_milliseconds, max(1, remaining)))


def _visible_projection_controls(page: Any) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    buttons = page.locator("button")
    matches: list[Any] = []
    relevant_texts: list[str] = []
    try:
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible():
                continue
            text = _normalize_header(button.inner_text())
            if "PROJECTION" in text and len(relevant_texts) < _MAX_NAV_CONTROL_TEXTS:
                relevant_texts.append(text[:_MAX_DIAGNOSTIC_TEXT])
            if text == "PROJECTIONS":
                matches.append(button)
    except Exception:
        return (), tuple(relevant_texts)
    return tuple(matches), tuple(relevant_texts)


def _projection_timeout_category(snapshot: ReviewPageSnapshot, event_id: int) -> str:
    if snapshot.login_required:
        return "reauthentication_required"
    if snapshot.premium_unavailable:
        return "premium_unavailable"
    any_projection_block = bool(_semantic_projection_alignments(snapshot, None))
    matching_projection_block = bool(_semantic_projection_alignments(snapshot, event_id))
    if any_projection_block and not matching_projection_block:
        return "upcoming_event_column_missing"
    return "projection_table_unavailable"


def _fail_projection_view(
    category: str,
    trace: _NavigationTrace,
    page: Any,
) -> None:
    raise _ProjectionViewFailure(
        category,
        trace.build(final_public_url=_safe_public_url(page.url), ready_at_utc=None),
    )


def _aware_utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("readiness clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _safe_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "app.fplreview.com":
        return "outside_fpl_review"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _visible_header_signatures(
    snapshot: ReviewPageSnapshot,
) -> tuple[ReviewHeaderSignature, ...]:
    return tuple(
        ReviewHeaderSignature(
            table_index=table_index,
            header_row_index=header_row_index,
            headers=tuple(cell.text[:_MAX_DIAGNOSTIC_TEXT] for cell in headers if cell.visible),
        )
        for table_index, table in enumerate(snapshot.tables[:8])
        for header_row_index, headers in enumerate(table.header_rows[:4])
    )


def _semantic_header_markers(
    snapshot: ReviewPageSnapshot,
    event_id: int,
) -> tuple[bool, bool, bool, bool, bool]:
    labels = tuple(
        _normalize_header(cell.text)
        for table in snapshot.tables
        for headers in table.header_rows
        for cell in headers
        if cell.visible
    )
    return (
        "PLAYER" in labels,
        "PRICE" in labels,
        any(_header_event_id(label) == event_id for label in labels),
        "TOTAL" in labels,
        any(_is_elite_ownership_header(label) for label in labels),
    )


def _record_readiness_observation(
    trace: _NavigationTrace,
    *,
    observed_at: datetime,
    elapsed_milliseconds: int,
    visible_control_count: int,
    semantic_block_count: int,
    markers: tuple[bool, bool, bool, bool, bool],
    header_signatures: tuple[ReviewHeaderSignature, ...],
    relevant_control_texts: tuple[str, ...],
) -> None:
    player, price, expected_gameweek, total, elite_ownership = markers
    if player and trace.first_player_at_utc is None:
        trace.first_player_at_utc = observed_at
    if price and trace.first_price_at_utc is None:
        trace.first_price_at_utc = observed_at
    if expected_gameweek and trace.first_expected_gameweek_at_utc is None:
        trace.first_expected_gameweek_at_utc = observed_at
    if total and trace.first_total_at_utc is None:
        trace.first_total_at_utc = observed_at
    if elite_ownership and trace.first_elite_ownership_at_utc is None:
        trace.first_elite_ownership_at_utc = observed_at
    if semantic_block_count == 1 and trace.all_required_same_table_at_utc is None:
        trace.all_required_same_table_at_utc = observed_at

    observation = ReviewReadinessObservation(
        observed_at_utc=observed_at,
        elapsed_milliseconds=elapsed_milliseconds,
        visible_projections_control_count=visible_control_count,
        semantic_block_count=semantic_block_count,
        player_present=player,
        price_present=price,
        expected_gameweek_present=expected_gameweek,
        total_present=total,
        elite_ownership_present=elite_ownership,
        header_signatures=header_signatures,
        relevant_control_texts=relevant_control_texts,
    )
    observations = trace.observations
    if observations is None:
        observations = []
        trace.observations = observations
    if observations:
        previous = observations[-1]
        unchanged = (
            previous.visible_projections_control_count == visible_control_count
            and previous.semantic_block_count == semantic_block_count
            and previous.player_present == player
            and previous.price_present == price
            and previous.expected_gameweek_present == expected_gameweek
            and previous.total_present == total
            and previous.elite_ownership_present == elite_ownership
            and previous.header_signatures == header_signatures
            and previous.relevant_control_texts == relevant_control_texts
        )
        if unchanged and elapsed_milliseconds - previous.elapsed_milliseconds < 1000:
            trace.final_header_signatures = header_signatures
            return
    if len(observations) < _MAX_READINESS_OBSERVATIONS:
        observations.append(observation)
    else:
        observations[-1] = observation
    trace.final_header_signatures = header_signatures


def _snapshot_page(page: Any) -> ReviewPageSnapshot:
    result = page.evaluate(_SNAPSHOT_JAVASCRIPT)
    tables = tuple(
        ReviewDomTable(
            header_rows=tuple(
                tuple(_snapshot_dom_cell(cell) for cell in row) for row in table["headerRows"]
            ),
            body_rows=tuple(
                tuple(_snapshot_dom_cell(cell) for cell in row) for row in table["bodyRows"]
            ),
        )
        for table in result["tables"]
    )
    return ReviewPageSnapshot(
        tables=tables,
        login_required=bool(result["loginRequired"]),
        premium_unavailable=bool(result["premiumUnavailable"]),
    )


def _snapshot_dom_cell(value: dict[str, Any]) -> ReviewDomCell:
    return ReviewDomCell(
        text=str(value["text"]),
        visible=bool(value["visible"]),
        tag_name=str(value["tagName"]),
        class_name=str(value["className"]),
        aria_hidden=value["ariaHidden"],
        hidden=bool(value["hidden"]),
        display=str(value["display"]),
        visibility=str(value["visibility"]),
    )


_SNAPSHOT_JAVASCRIPT = """
() => {
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      element.getClientRects().length > 0;
  };
  const cells = (row, selector) => Array.from(row.querySelectorAll(selector)).map((cell) => {
    const style = window.getComputedStyle(cell);
    return {
      text: cell.innerText.trim().slice(0, 120),
      visible: visible(cell),
      tagName: cell.tagName.toLowerCase().slice(0, 16),
      className: String(cell.className || '').slice(0, 160),
      ariaHidden: cell.getAttribute('aria-hidden'),
      hidden: Boolean(cell.hidden),
      display: style.display.slice(0, 32),
      visibility: style.visibility.slice(0, 32),
    };
  });
  const pageText = document.body ? document.body.innerText : '';
  return {
    tables: Array.from(document.querySelectorAll('table')).map((table) => ({
      headerRows: Array.from(table.querySelectorAll('thead tr')).map((row) => cells(row, 'th,td')),
      bodyRows: Array.from(table.querySelectorAll('tbody tr')).map((row) => cells(row, 'th,td')),
    })),
    loginRequired: /(?:log in|sign in|continue with patreon)/i.test(pageText),
    premiumUnavailable: /(?:premium required|upgrade|become a patron|subscribe)/i.test(pageText),
  };
}
"""
