"""Read-only cloud-side candidate validation. A candidate is NEVER posting authority."""

from dataclasses import dataclass
from datetime import datetime
from decimal import InvalidOperation
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from fpl_bot.captain_controller import Clock, OfficialFplSource
from fpl_bot.captain_handoff import CaptainAssignment, CompletenessStatus, ProjectionHandoff
from fpl_bot.captain_models import CaptainProjection, CaptainSelection
from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_review_browser import ReviewLogicalProjectionRow, resolve_review_rows
from fpl_bot.captain_service import build_captain_report
from fpl_bot.captain_state import (
    AcquisitionAttempt,
    AttemptStatus,
    Generation,
    GenerationStatus,
    PostingRecord,
    PostingStatus,
    PostKey,
    StateConflict,
)
from fpl_bot.captain_tweet import x_weighted_text_length
from fpl_bot.errors import (
    CaptainFixtureError,
    CaptainPlayerResolutionError,
    CaptainProjectionError,
    CaptainRenderingError,
    CaptainReviewBrowserError,
    DataValidationError,
    NoSuitableEventError,
)
from fpl_bot.events import parse_events, select_next_event
from fpl_bot.models import Fixture
from fpl_bot.parsing import parse_fixtures, parse_players, parse_teams
from fpl_bot.service import build_event_report


class ValidationStatus(StrEnum):
    STALE_GENERATION = "stale_generation"
    DEADLINE_CHANGED = "deadline_changed"
    EVENT_CHANGED = "event_changed"
    CLASSIFICATION_CHANGED = "classification_changed"
    INVALID_HANDOFF = "invalid_handoff"
    IDENTITY_MISSING = "identity_missing"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    IDENTITY_MISMATCH = "identity_mismatch"
    DUPLICATE_PLAYER = "duplicate_player_resolution"
    INCOMPLETE = "incomplete_projection_dataset"
    INVALID_FIXTURES = "invalid_fixtures"
    NO_DIFFERENTIAL = "no_differential"
    EARLY = "early_candidate"
    LATE = "late_candidate"
    ALREADY_POSTED = "already_posted"
    POSTING_BLOCKED = "posting_blocked"
    OVERLENGTH = "tweet_overlength"
    INVALID_FPL = "invalid_official_data"
    FPL_UNAVAILABLE = "official_data_unavailable"


class CandidateRejected(ValueError):
    def __init__(self, status: ValidationStatus):
        self.status = status
        super().__init__(status.value)


class CandidateStateReader(Protocol):
    """Read-only Captain state; authenticated accepted state, not worker assertions."""

    def current(self, key: PostKey) -> Generation | None: ...
    def generation_acquisition(self, generation_id: UUID) -> AcquisitionAttempt | None: ...
    def posting(self, key: PostKey) -> PostingRecord: ...


@dataclass(frozen=True, slots=True)
class ValidatedSelection:
    source_ordinal: int
    projection_rank: int
    official: CaptainSelection


@dataclass(frozen=True, slots=True)
class ValidatedCaptainCandidate:
    key: PostKey
    assignment: CaptainAssignment
    attempt_id: UUID
    accepted_handoff_digest: str
    top_three: tuple[ValidatedSelection, ValidatedSelection, ValidatedSelection]
    differential: ValidatedSelection
    official_event_fixtures: tuple[Fixture, ...]
    tweet: str
    weighted_length: int
    validated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class _ResolvedProjections:
    records: tuple[CaptainProjection, ...]

    def fetch_event_projections(self, event_id: int) -> tuple[CaptainProjection, ...]:
        return self.records


class CaptainCandidateValidator:
    def __init__(self, repository: CandidateStateReader, source: OfficialFplSource, clock: Clock):
        self.repository, self.source, self.clock = repository, source, clock

    def _state(self, key: PostKey, handoff: ProjectionHandoff) -> datetime:
        now = self.clock.now()
        require_utc(now)
        if (
            not isinstance(key, PostKey)
            or not isinstance(handoff, ProjectionHandoff)
            or key.event_id != handoff.assignment.event_id
        ):
            raise CandidateRejected(ValidationStatus.INVALID_HANDOFF)
        timing = handoff.assignment.timing
        if now < timing.target_utc:
            raise CandidateRejected(ValidationStatus.EARLY)
        if now > timing.expiry_utc:
            raise CandidateRejected(ValidationStatus.LATE)
        try:
            generation = self.repository.current(key)
            if (
                generation is None
                or generation.assignment != handoff.assignment
                or generation.key != key
                or generation.status != GenerationStatus.ACCEPTED
            ):
                raise CandidateRejected(ValidationStatus.STALE_GENERATION)
            attempt = self.repository.generation_acquisition(handoff.assignment.generation_id)
            if (
                attempt is None
                or attempt.status != AttemptStatus.ACCEPTED
                or attempt.attempt_id != handoff.attempt_id
                or attempt.generation_id != handoff.assignment.generation_id
                or attempt.handoff != handoff
                or attempt.handoff.payload_digest != handoff.payload_digest
            ):
                raise CandidateRejected(ValidationStatus.INVALID_HANDOFF)
            releases = tuple(
                at for status, at in generation.history if status == GenerationStatus.RELEASED
            )
            if (
                len(releases) != 1
                or not timing.permits_new_attempt(releases[0])
                or not releases[0] <= attempt.claimed_at <= handoff.acquisition_started_utc
                or not handoff.acquisition_ended_utc <= attempt.updated_at <= now
            ):
                raise CandidateRejected(ValidationStatus.INVALID_HANDOFF)
            if handoff.completeness != CompletenessStatus.COMPLETE or len(handoff.records) < 4:
                raise CandidateRejected(ValidationStatus.INCOMPLETE)
            handoff.require_eligible(generation.assignment, now)
            posting = self.repository.posting(key)
            if posting.key != key:
                raise CandidateRejected(ValidationStatus.INVALID_HANDOFF)
            if any(a.status == PostingStatus.SUCCEEDED for a in posting.attempts):
                raise CandidateRejected(ValidationStatus.ALREADY_POSTED)
            if any(a.status != PostingStatus.FAILED_BEFORE_WRITE for a in posting.attempts):
                raise CandidateRejected(ValidationStatus.POSTING_BLOCKED)
        except CandidateRejected:
            raise
        except (StateConflict, ValueError, TypeError):
            raise CandidateRejected(ValidationStatus.INVALID_HANDOFF) from None
        return now

    def validate(self, key: PostKey, handoff: ProjectionHandoff) -> ValidatedCaptainCandidate:
        now = self._state(key, handoff)
        try:
            bootstrap = self.source.fetch_bootstrap_static()
            event = select_next_event(parse_events(bootstrap["events"]), now=now)
            teams = parse_teams(bootstrap["teams"])
            players = parse_players(bootstrap["elements"])
        except (DataValidationError, NoSuitableEventError, KeyError, TypeError, ValueError):
            raise CandidateRejected(ValidationStatus.INVALID_FPL) from None
        except Exception:
            raise CandidateRejected(ValidationStatus.FPL_UNAVAILABLE) from None
        if event.event_id != key.event_id:
            raise CandidateRejected(ValidationStatus.EVENT_CHANGED)
        if event.deadline_utc != handoff.assignment.timing.deadline_utc:
            raise CandidateRejected(ValidationStatus.DEADLINE_CHANGED)
        try:
            payload = self.source.fetch_event_fixtures(event.event_id)
        except Exception:
            raise CandidateRejected(ValidationStatus.FPL_UNAVAILABLE) from None
        try:
            fixtures = parse_fixtures(list(payload), event.event_id)
            event_report = build_event_report(event, teams, fixtures)
        except (DataValidationError, TypeError, ValueError):
            raise CandidateRejected(ValidationStatus.INVALID_FIXTURES) from None
        if event_report.event_code != handoff.assignment.event_code:
            raise CandidateRejected(ValidationStatus.CLASSIFICATION_CHANGED)
        rows = tuple(
            ReviewLogicalProjectionRow(
                r.source_ordinal, r.review_name, r.review_team, r.projected_points
            )
            for r in handoff.records
        )
        try:
            resolved, diagnostic = resolve_review_rows(
                rows,
                players,
                teams,
                event_id=event.event_id,
                acquired_at_utc=handoff.acquisition_ended_utc,
            )
        except CaptainReviewBrowserError as error:
            category = (
                ValidationStatus.DUPLICATE_PLAYER
                if error.category == "invalid_projection_table"
                else ValidationStatus.IDENTITY_MISMATCH
            )
            raise CandidateRejected(category) from None
        if diagnostic.failures:
            category = diagnostic.failures[0].category
            status = {
                "duplicate_exact_match": ValidationStatus.IDENTITY_AMBIGUOUS,
                "exact_name_wrong_team": ValidationStatus.IDENTITY_MISMATCH,
            }.get(category, ValidationStatus.IDENTITY_MISSING)
            raise CandidateRejected(status)
        if len(resolved) != len(handoff.records):
            raise CandidateRejected(ValidationStatus.INCOMPLETE)
        if len({p.element_id for p in resolved}) != len(resolved):
            raise CandidateRejected(ValidationStatus.DUPLICATE_PLAYER)
        for record, projection in zip(handoff.records, resolved, strict=True):
            if (
                record.official_element_id is not None
                and record.official_element_id != projection.element_id
            ):
                raise CandidateRejected(ValidationStatus.IDENTITY_MISMATCH)
        try:
            report = build_captain_report(
                event_report, _ResolvedProjections(resolved), players, teams, fixtures
            )
        except CaptainFixtureError:
            raise CandidateRejected(ValidationStatus.INVALID_FIXTURES) from None
        except CaptainPlayerResolutionError:
            raise CandidateRejected(ValidationStatus.IDENTITY_MISMATCH) from None
        except CaptainRenderingError:
            raise CandidateRejected(ValidationStatus.OVERLENGTH) from None
        except CaptainProjectionError:
            raise CandidateRejected(ValidationStatus.NO_DIFFERENTIAL) from None
        except (ValueError, InvalidOperation):
            raise CandidateRejected(ValidationStatus.INVALID_HANDOFF) from None
        ordinal_by_id = {
            p.element_id: r.source_ordinal for r, p in zip(handoff.records, resolved, strict=True)
        }
        # Ranking evidence mirrors the stable order used by the proven report builder.
        ranks = {
            p.element_id: i
            for i, p in enumerate(
                sorted(resolved, key=lambda p: p.projected_points, reverse=True), start=1
            )
        }

        def evidence(selection: CaptainSelection) -> ValidatedSelection:
            element = selection.player.element_id
            return ValidatedSelection(ordinal_by_id[element], ranks[element], selection)

        validated_at = self._state(
            key, handoff
        )  # Catch supersession, barrier or expiry during fetch.
        return ValidatedCaptainCandidate(
            key,
            handoff.assignment,
            handoff.attempt_id,
            handoff.payload_digest,
            tuple(evidence(s) for s in report.top_three),
            evidence(report.differential),
            fixtures,
            report.tweet,
            x_weighted_text_length(report.tweet),
            validated_at,
        )
