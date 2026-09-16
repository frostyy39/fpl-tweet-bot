from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

import fpl_bot.captain_validation as validation
from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_state import GenerationStatus as G
from fpl_bot.captain_state import PostingStatus, PostKey
from fpl_bot.captain_validation import CandidateRejected, CaptainCandidateValidator
from fpl_bot.captain_validation import ValidationStatus as V

T = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)
D = T + timedelta(hours=2)
KEY = PostKey("12345", 5)


class Clock:
    value = T

    def now(self):
        return self.value


class Source:
    def __init__(self, count=4, kind="GW", names=None):
        names = names or ["João", "Second", "Third", "Fourth"]
        self.bootstrap = {
            "events": [
                {"id": 5, "name": "Gameweek 5", "deadline_time": D.isoformat(), "is_next": True}
            ],
            "teams": [
                {"id": i, "name": f"Team {i}", "short_name": chr(64 + i) * 2} for i in range(1, 21)
            ],
            "elements": [
                {
                    "id": i,
                    "web_name": names[i - 1] if i <= len(names) else f"Player{i}",
                    "team": 1,
                    "selected_by_percent": "9.9" if i == count else "10.0",
                }
                for i in range(1, count + 1)
            ],
        }
        self.fixtures = [
            {
                "id": i,
                "event": 5,
                "team_h": i,
                "team_a": i + 1,
                "kickoff_time": (D + timedelta(hours=1)).isoformat(),
            }
            for i in range(1, 21, 2)
        ]
        if kind in {"BGW", "BDGW"}:
            self.fixtures.pop()
        if kind in {"DGW", "BDGW"}:
            self.fixtures.insert(
                0,
                {
                    "id": 99,
                    "event": 5,
                    "team_h": 3,
                    "team_a": 1,
                    "kickoff_time": (D + timedelta(days=2)).isoformat(),
                },
            )
        self.reads = []
        self.after_fetch = lambda: None

    def fetch_bootstrap_static(self):
        self.reads.append("bootstrap")
        return deepcopy(self.bootstrap)

    def fetch_event_fixtures(self, event_id):
        self.reads.append(event_id)
        self.after_fetch()
        return deepcopy(self.fixtures)


def build(count=4, kind="GW", names=None, official_id=None):
    source, clock, repo = Source(count, kind, names), Clock(), InMemoryCaptainRepository()
    assignment = CaptainAssignment(UUID(int=1), UUID(int=2), 5, f"{kind}5", CaptainTiming(D))
    repo.plan(KEY, assignment, None, T)
    for old, new in [(G.PLANNED, G.WARMING), (G.WARMING, G.READY), (G.READY, G.RELEASED)]:
        repo.transition(assignment.generation_id, old, new, T)
    repo.claim_acquisition(assignment.generation_id, UUID(int=3), T)
    handoff = ProjectionHandoff(
        1,
        assignment,
        UUID(int=3),
        T,
        T,
        tuple(
            ProjectionRecord(
                i,
                player["web_name"],
                "AA",
                Decimal(10) - Decimal(i) / 10,
                official_id if i == 1 else None,
            )
            for i, player in enumerate(source.bootstrap["elements"], start=1)
        ),
        RowCounts(count, count, 0, count),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )
    repo.accept(handoff, T)
    return CaptainCandidateValidator(repo, source, clock), repo, source, clock, handoff


def rejects(validator, handoff, expected):
    with pytest.raises(CandidateRejected) as error:
        validator.validate(KEY, handoff)
    assert error.value.status == expected


def test_exact_gw_candidate_and_immutability():
    validator, repo, source, _, handoff = build()
    candidate = validator.validate(KEY, handoff)
    expected = (
        "🧢 CAPTAIN PICKS 🧢\n\n#GW5 Projected Points:\n\n"
        "🥇 João v BB (H) - 9.90\n🥈 Second v BB (H) - 9.80\n🥉 Third v BB (H) - 9.70\n\n"
        "Differential:\n\n🐴 Fourth v BB (H) - 9.60\n\nGood luck!\n\n#FPL #FPLCommunity"
    )
    assert candidate.tweet == expected
    assert candidate.weighted_length == validation.x_weighted_text_length(expected)
    assert candidate.key == KEY and candidate.assignment == handoff.assignment
    assert candidate.attempt_id == handoff.attempt_id
    assert candidate.accepted_handoff_digest == handoff.payload_digest
    assert candidate.validated_at_utc == T
    assert candidate.top_three[0].official.player.selected_by_percent == Decimal("10.0")
    assert candidate.differential.projection_rank == 4
    assert source.reads == ["bootstrap", 5]
    assert not repo.posting(KEY).attempts  # Validation has no posting/state side effects.
    with pytest.raises(FrozenInstanceError):
        candidate.tweet = "worker tweet"


@pytest.mark.parametrize("kind", ["GW", "BGW", "DGW", "BDGW"])
def test_classification_and_chronological_multi_fixture_rendering(kind):
    validator, _, _, _, handoff = build(kind=kind)
    candidate = validator.validate(KEY, handoff)
    assert candidate.assignment.event_code == f"{kind}5"
    fixture_text = "BB (H) & cc (a)" if kind in {"DGW", "BDGW"} else "BB (H)"
    assert f"João v {fixture_text} - 9.90" in candidate.tweet


def test_tie_preserves_source_order(monkeypatch):
    validator, repo, _, _, handoff = build()
    tied = replace(
        handoff, records=tuple(replace(r, projected_points=Decimal("8")) for r in handoff.records)
    )
    attempt = replace(repo.generation_acquisition(handoff.assignment.generation_id), handoff=tied)
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: attempt)
    result = validator.validate(KEY, tied)
    assert [s.source_ordinal for s in result.top_three] == [1, 2, 3]
    assert result.differential.source_ordinal == 4
    assert "- 8.00" in result.tweet


def test_full_ranking_differential_and_fresh_ownership():
    validator, _, source, _, handoff = build(count=26)
    assert validator.validate(KEY, handoff).differential.projection_rank == 26
    source.bootstrap["elements"][3]["selected_by_percent"] = "9.9"
    candidate = validator.validate(KEY, handoff)
    assert candidate.differential.projection_rank == 4
    assert candidate.differential.official.player.selected_by_percent == Decimal("9.9")
    source.bootstrap["elements"][3]["selected_by_percent"] = "10.0"
    source.bootstrap["elements"][-1]["selected_by_percent"] = "10.0"
    rejects(validator, handoff, V.NO_DIFFERENTIAL)


@pytest.mark.parametrize("official_id,valid", [(1, True), (99, False)])
def test_worker_id_crosscheck(official_id, valid):
    validator, _, _, _, handoff = build(official_id=official_id)
    if valid:
        assert validator.validate(KEY, handoff).top_three[0].official.player.element_id == 1
    else:
        rejects(validator, handoff, V.IDENTITY_MISMATCH)


@pytest.mark.parametrize(
    "mode,status",
    [
        ("missing", V.IDENTITY_MISSING),
        ("ambiguous", V.IDENTITY_AMBIGUOUS),
        ("wrong_team", V.IDENTITY_MISMATCH),
    ],
)
def test_identity_failures(mode, status):
    validator, _, source, _, handoff = build()
    if mode == "missing":
        source.bootstrap["elements"].pop(0)
    elif mode == "ambiguous":
        source.bootstrap["elements"].append(dict(source.bootstrap["elements"][0], id=99))
    else:
        source.bootstrap["elements"][0]["team"] = 2
    rejects(validator, handoff, status)


def test_duplicate_resolved_ids_defensive_check(monkeypatch):
    validator, _, _, _, handoff = build()
    resolve = validation.resolve_review_rows

    def duplicate(*args, **kwargs):
        records, diagnostic = resolve(*args, **kwargs)
        return (records[0], records[0], *records[2:]), diagnostic

    monkeypatch.setattr(validation, "resolve_review_rows", duplicate)
    rejects(validator, handoff, V.DUPLICATE_PLAYER)


@pytest.mark.parametrize(
    "mode,status", [("deadline", V.DEADLINE_CHANGED), ("event", V.EVENT_CHANGED)]
)
def test_changed_official_event(mode, status):
    validator, _, source, _, handoff = build()
    if mode == "deadline":
        source.bootstrap["events"][0]["deadline_time"] = (D + timedelta(hours=1)).isoformat()
    else:
        source.bootstrap["events"][0]["id"] = 6
    rejects(validator, handoff, status)


def test_stale_generation_including_change_during_fetch():
    validator, repo, source, _, handoff = build()

    def supersede():
        replacement = replace(
            handoff.assignment,
            assignment_id=UUID(int=11),
            generation_id=UUID(int=12),
            timing=CaptainTiming(D + timedelta(hours=1)),
        )
        repo.plan(KEY, replacement, handoff.assignment.generation_id, T)

    source.after_fetch = supersede
    rejects(validator, handoff, V.STALE_GENERATION)
    rejects(validator, handoff, V.STALE_GENERATION)


def test_mismatched_accepted_digest():
    validator, _, _, _, handoff = build()
    altered = replace(
        handoff,
        records=(replace(handoff.records[0], projected_points=Decimal("99")), *handoff.records[1:]),
    )
    rejects(validator, altered, V.INVALID_HANDOFF)


def test_acquisition_before_durable_release_rejected(monkeypatch):
    validator, repo, _, _, handoff = build()
    attempt = repo.generation_acquisition(handoff.assignment.generation_id)
    monkeypatch.setattr(
        repo,
        "generation_acquisition",
        lambda _: replace(attempt, claimed_at=T + timedelta(seconds=1)),
    )
    rejects(validator, handoff, V.INVALID_HANDOFF)


@pytest.mark.parametrize(
    "seconds,status", [(-0.000001, V.EARLY), (0, None), (300, None), (300.000001, V.LATE)]
)
def test_validation_window(seconds, status):
    validator, _, _, clock, handoff = build()
    clock.value = T + timedelta(seconds=seconds)
    if status:
        rejects(validator, handoff, status)
    else:
        assert validator.validate(KEY, handoff).validated_at_utc == clock.value


def test_lateness_during_fetch():
    validator, _, source, clock, handoff = build()
    source.after_fetch = lambda: setattr(clock, "value", T + timedelta(minutes=6))
    rejects(validator, handoff, V.LATE)


@pytest.mark.parametrize(
    "outcome,status",
    [(PostingStatus.SUCCEEDED, V.ALREADY_POSTED), (PostingStatus.UNCERTAIN, V.POSTING_BLOCKED)],
)
def test_event_posting_barrier(outcome, status):
    validator, repo, _, _, handoff = build()
    repo.claim_post(handoff.assignment.generation_id, UUID(int=8), T)
    repo.start_write(KEY, UUID(int=8), T)
    repo.finish_post(
        KEY, UUID(int=8), outcome, T, "123" if outcome == PostingStatus.SUCCEEDED else None
    )
    rejects(validator, handoff, status)


def test_selected_player_without_fixture():
    validator, _, source, _, handoff = build(kind="BGW")
    for player in source.bootstrap["elements"]:
        player["team"] = 19
    # Exact identities still match; only fixtures fail.
    altered = replace(handoff, records=tuple(replace(r, review_team="SS") for r in handoff.records))
    attempt = validator.repository.generation_acquisition(handoff.assignment.generation_id)
    validator.repository.generation_acquisition = lambda _: replace(attempt, handoff=altered)
    rejects(validator, altered, V.INVALID_FIXTURES)


def test_overlength():
    validator, _, _, _, handoff = build(names=["A" * 100, "B" * 100, "C" * 100, "D" * 100])
    rejects(validator, handoff, V.OVERLENGTH)


def test_worker_tweet_is_not_an_input():
    validator, _, source, _, handoff = build()
    source.bootstrap["tweet"] = "UNTRUSTED_WORKER_TWEET"
    assert "UNTRUSTED" not in validator.validate(KEY, handoff).tweet
    payload = handoff.to_payload()
    payload["tweet"] = "UNTRUSTED_WORKER_TWEET"
    with pytest.raises(ValueError):
        ProjectionHandoff.from_payload(payload)


def test_incomplete_accepted_state_rejected(monkeypatch):
    validator, repo, _, _, handoff = build()
    partial = replace(handoff, completeness=CompletenessStatus.PARTIAL)
    attempt = repo.generation_acquisition(handoff.assignment.generation_id)
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: replace(attempt, handoff=partial))
    rejects(validator, partial, V.INCOMPLETE)


def test_successful_post_during_fpl_fetch_blocks_candidate():
    validator, repo, source, _, handoff = build()

    def posted():
        repo.claim_post(handoff.assignment.generation_id, UUID(int=8), T)
        repo.start_write(KEY, UUID(int=8), T)
        repo.finish_post(KEY, UUID(int=8), PostingStatus.SUCCEEDED, T, "123")

    source.after_fetch = posted
    rejects(validator, handoff, V.ALREADY_POSTED)


@pytest.mark.parametrize("at", [T, T + timedelta(minutes=5)])
def test_acquisition_release_acceptance_at_exact_boundary(monkeypatch, at):
    validator, repo, _, clock, handoff = build()
    boundary = replace(handoff, acquisition_started_utc=at, acquisition_ended_utc=at)
    generation = repo.current(KEY)
    generation = replace(
        generation,
        history=tuple(
            (status, at if status in {G.RELEASED, G.ACQUIRING, G.ACCEPTED} else when)
            for status, when in generation.history
        ),
    )
    attempt = replace(
        repo.generation_acquisition(handoff.assignment.generation_id),
        handoff=boundary,
        claimed_at=at,
        updated_at=at,
    )
    monkeypatch.setattr(repo, "current", lambda _: generation)
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: attempt)
    clock.value = at
    assert validator.validate(KEY, boundary).validated_at_utc == at


def test_acquisition_before_target_rejected_even_if_stored(monkeypatch):
    validator, repo, _, _, handoff = build()
    early = replace(handoff, acquisition_started_utc=T - timedelta(microseconds=1))
    attempt = replace(repo.generation_acquisition(handoff.assignment.generation_id), handoff=early)
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: attempt)
    rejects(validator, early, V.INVALID_HANDOFF)


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_id": UUID(int=99)},
        {"generation_id": UUID(int=99)},
        {"handoff": None},
    ],
)
def test_accepted_attempt_binding(monkeypatch, change):
    validator, repo, _, _, handoff = build()
    attempt = replace(repo.generation_acquisition(handoff.assignment.generation_id), **change)
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: attempt)
    rejects(validator, handoff, V.INVALID_HANDOFF)


@pytest.mark.parametrize(
    "change",
    [
        {"authentication": AuthenticationStatus.REQUIRED},
        {"cleanup": CleanupStatus.UNCLEAN},
    ],
)
def test_ineligible_payload_even_if_reported_accepted(monkeypatch, change):
    validator, repo, _, _, handoff = build()
    invalid = replace(handoff, **change)
    attempt = replace(
        repo.generation_acquisition(handoff.assignment.generation_id), handoff=invalid
    )
    monkeypatch.setattr(repo, "generation_acquisition", lambda _: attempt)
    rejects(validator, invalid, V.INVALID_HANDOFF)


def test_changed_classification_rejected():
    validator, _, source, _, handoff = build()
    source.fixtures.pop()
    rejects(validator, handoff, V.CLASSIFICATION_CHANGED)


@pytest.mark.parametrize("fixtures", [[], [{"id": 1, "event": 5, "team_h": 1, "team_a": 99}]])
def test_invalid_official_fixtures(fixtures):
    validator, _, source, _, handoff = build()
    source.fixtures = fixtures
    rejects(validator, handoff, V.INVALID_FIXTURES)


def test_unresolved_nonselected_row_still_rejects():
    validator, _, source, _, handoff = build(count=26)
    source.bootstrap["elements"].pop(20)
    rejects(validator, handoff, V.IDENTITY_MISSING)


def test_transport_failure_does_not_leak_details():
    validator, _, source, _, handoff = build()

    def unavailable():
        raise RuntimeError("SECRET_SENTINEL")

    source.fetch_bootstrap_static = unavailable
    rejects(validator, handoff, V.FPL_UNAVAILABLE)


def test_official_web_name_is_rendered_after_exact_normalization():
    validator, _, source, _, handoff = build(names=["  João  ", "Second", "Third", "Fourth"])
    source.bootstrap["elements"][0]["web_name"] = "João"
    candidate = validator.validate(KEY, handoff)
    assert candidate.top_three[0].official.player.web_name == "João"
    assert "🥇 João v" in candidate.tweet
