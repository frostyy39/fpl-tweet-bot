from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from test_captain_firestore import adapters
from test_captain_validation import KEY, D, T, build

from fpl_bot.captain_http import CallerIdentity
from fpl_bot.captain_publisher import (
    FPLBOTTEST_USER_ID,
    CaptainPublisher,
    PublicationInstruction,
    PublishStatus,
)
from fpl_bot.captain_publisher_http import PublisherAuthConfig, create_publisher_app
from fpl_bot.captain_state import GenerationStatus as G
from fpl_bot.captain_state import PostingStatus, StateConflict, candidate_content_digest
from fpl_bot.captain_validation import candidate_record
from fpl_bot.x_api import AuthenticatedXUser, CreatedXPost

PUBLISH_KEY = replace(KEY, destination_user_id=FPLBOTTEST_USER_ID)


class X:
    def __init__(self, user_id=FPLBOTTEST_USER_ID, failure=None):
        self.user_id, self.failure = user_id, failure
        self.identity_calls = self.write_calls = 0

    def get_authenticated_user(self):
        self.identity_calls += 1
        return AuthenticatedXUser(self.user_id, "FPLBotTest")

    def create_text_post(self, text):
        self.write_calls += 1
        if self.failure:
            raise self.failure
        return CreatedXPost("123456789", text)


def prepared(*, enabled=True, x=None):
    validator, repo, source, clock, handoff = build()
    # The production key is fixed; tests preserve the accepted assignment while
    # replacing only its event-level destination identity.
    generation = repo.generation(handoff.assignment.generation_id)
    production_key = PUBLISH_KEY
    repo._generations[generation.assignment.generation_id] = replace(generation, key=production_key)
    del repo._current[KEY]
    repo._current[production_key] = generation.assignment.generation_id
    candidate = validator.validate(production_key, handoff)
    record = candidate_record(candidate)
    repo.accept_candidate(record, T)
    x = x or X()
    publisher = CaptainPublisher(repo, validator, x, x, clock, posting_enabled=enabled)
    instruction = PublicationInstruction(
        handoff.assignment.generation_id,
        handoff.attempt_id,
        handoff.payload_digest,
        record.candidate_digest,
        UUID(int=40),
    )
    return publisher, instruction, repo, source, clock, x, record


def test_disabled_gate_precedes_state_oauth_and_x():
    class Never:
        def __getattr__(self, name):
            raise AssertionError(name)

    publisher = CaptainPublisher(Never(), Never(), Never(), Never(), Never(), posting_enabled=False)
    instruction = PublicationInstruction(UUID(int=1), UUID(int=2), "a" * 64, "b" * 64, UUID(int=3))
    result = publisher.publish(instruction)
    assert result.status == PublishStatus.DISABLED


def test_exact_fplbottest_success_revalidates_and_commits_write_barrier():
    publisher, instruction, repo, source, _, x, _ = prepared()
    result = publisher.publish(instruction)
    assert result.status == PublishStatus.SUCCEEDED
    assert result.event_id == 5 and result.post_id == "123456789"
    assert source.reads == ["bootstrap", 5, "bootstrap", 5, "bootstrap", 5]
    assert x.identity_calls == 1 and x.write_calls == 1
    attempt = repo.posting(PUBLISH_KEY).attempts[-1]
    assert [status for status, _ in attempt.history] == [
        PostingStatus.CLAIMED,
        PostingStatus.WRITE_STARTED,
        PostingStatus.SUCCEEDED,
    ]


@pytest.mark.parametrize("user", ["1", "9999999999999999999"])
def test_wrong_or_arbitrary_x_identity_never_writes(user):
    publisher, instruction, repo, _, _, x, _ = prepared(x=X(user))
    assert publisher.publish(instruction).status == PublishStatus.REJECTED
    assert x.write_calls == 0
    assert repo.posting(PUBLISH_KEY).attempts[-1].status == PostingStatus.FAILED_BEFORE_WRITE


@pytest.mark.parametrize("field", ["candidate_digest", "handoff_digest", "attempt_id"])
def test_missing_or_conflicting_candidate_evidence_rejected(field):
    publisher, instruction, repo, _, _, x, _ = prepared()
    value = "f" * 64 if field.endswith("digest") else UUID(int=99)
    with pytest.raises(StateConflict):
        publisher.publish(replace(instruction, **{field: value}))
    assert x.write_calls == 0 and not repo.posting(PUBLISH_KEY).attempts


def test_absent_candidate_never_reaches_x():
    publisher, instruction, repo, _, _, x, _ = prepared()
    del repo._candidates[instruction.generation_id]
    with pytest.raises(StateConflict):
        publisher.publish(instruction)
    assert x.identity_calls == 0 and x.write_calls == 0


@pytest.mark.parametrize(
    "offset,expected",
    [
        (timedelta(microseconds=-1), PublishStatus.REJECTED),
        (timedelta(0), PublishStatus.SUCCEEDED),
        (timedelta(minutes=5), PublishStatus.SUCCEEDED),
        (timedelta(minutes=5, microseconds=1), PublishStatus.REJECTED),
    ],
)
def test_exact_release_window_boundaries(offset, expected):
    publisher, instruction, _, _, clock, x, _ = prepared()
    clock.value = T + offset
    assert publisher.publish(instruction).status == expected
    assert x.write_calls == (1 if expected == PublishStatus.SUCCEEDED else 0)


@pytest.mark.parametrize("change", ["deadline", "event", "fetch"])
def test_fresh_fpl_change_or_failure_rejected_before_x(change):
    publisher, instruction, repo, source, _, x, _ = prepared()
    if change == "deadline":
        source.bootstrap["events"][0]["deadline_time"] = (D + timedelta(hours=1)).isoformat()
    elif change == "event":
        source.bootstrap["events"][0]["id"] = 6
    else:
        source.fetch_bootstrap_static = lambda: (_ for _ in ()).throw(OSError("offline"))
    assert publisher.publish(instruction).status == PublishStatus.REJECTED
    assert x.write_calls == 0 and not repo.posting(PUBLISH_KEY).attempts


def test_immutable_candidate_content_change_rejected():
    publisher, instruction, repo, source, _, x, _ = prepared()
    source.bootstrap["elements"][3]["selected_by_percent"] = "8.8"
    assert publisher.publish(instruction).status == PublishStatus.REJECTED
    assert x.write_calls == 0
    assert (
        repo.candidate(instruction.generation_id).candidate_digest == instruction.candidate_digest
    )


def test_firestore_persists_candidate_idempotently_and_rejects_conflict():
    _, instruction, source_repo, _, _, _, record = prepared()
    assignment = source_repo.generation(instruction.generation_id).assignment
    handoff = source_repo.generation_acquisition(instruction.generation_id).handoff
    durable, _, _ = adapters()
    durable.plan(PUBLISH_KEY, assignment, None, T)
    for old, new in [(G.PLANNED, G.WARMING), (G.WARMING, G.READY), (G.READY, G.RELEASED)]:
        durable.transition(assignment.generation_id, old, new, T)
    durable.claim_acquisition(assignment.generation_id, handoff.attempt_id, T)
    durable.accept(handoff, T)
    assert durable.accept_candidate(record, T).applied
    assert not durable.accept_candidate(record, T).applied
    assert durable.candidate(assignment.generation_id) == record
    tweet = record.tweet + "!"
    args = (
        record.key,
        record.generation_id,
        record.attempt_id,
        record.handoff_digest,
        record.event_code,
        record.deadline_utc,
        record.top_three,
        record.differential,
        tweet,
        record.weighted_length,
    )
    conflicting = replace(record, tweet=tweet, candidate_digest=candidate_content_digest(*args))
    with pytest.raises(StateConflict):
        durable.accept_candidate(conflicting, T)


def test_ambiguous_x_result_is_uncertain_and_never_retried():
    publisher, instruction, repo, _, _, x, _ = prepared(x=X(failure=TimeoutError("unknown")))
    assert publisher.publish(instruction).status == PublishStatus.UNCERTAIN
    assert publisher.publish(instruction).status == PublishStatus.REJECTED
    assert x.write_calls == 1
    assert repo.posting(PUBLISH_KEY).attempts[-1].status == PostingStatus.UNCERTAIN


def test_crash_after_claim_can_resume_only_same_claim_before_write_started():
    publisher, instruction, repo, _, _, x, _ = prepared()
    repo.claim_post(instruction.generation_id, instruction.claim_id, T)
    assert publisher.publish(instruction).status == PublishStatus.SUCCEEDED
    assert x.write_calls == 1


def test_crash_after_write_started_never_reissues_x():
    publisher, instruction, repo, _, _, x, _ = prepared()
    repo.claim_post(instruction.generation_id, instruction.claim_id, T)
    repo.start_write(PUBLISH_KEY, instruction.claim_id, T)
    assert publisher.publish(instruction).status == PublishStatus.REJECTED
    assert x.write_calls == 0


def test_concurrent_duplicate_deliveries_produce_one_x_write():
    publisher, instruction, repo, _, _, x, _ = prepared()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: publisher.publish(instruction), range(2)))
    assert sum(result.status == PublishStatus.SUCCEEDED for result in results) == 1
    assert x.write_calls == 1
    assert repo.posting(PUBLISH_KEY).attempts[-1].status == PostingStatus.SUCCEEDED


def test_success_and_exact_reconciliation_are_permanent_event_barriers():
    publisher, instruction, repo, _, _, x, _ = prepared(x=X(failure=TimeoutError()))
    assert publisher.publish(instruction).status == PublishStatus.UNCERTAIN
    assert publisher.reconcile_exact_post(instruction, "987654321").post_id == "987654321"
    assert publisher.publish(instruction).status == PublishStatus.ALREADY_SUCCEEDED
    assert x.write_calls == 1
    assert repo.posting(PUBLISH_KEY).attempts[-1].status == PostingStatus.SUCCEEDED


class Authorizer:
    def __init__(self, email):
        self.email = email

    def authorize(self, authorization, audience):
        if authorization != "Bearer token":
            raise PermissionError
        return CallerIdentity("subject", self.email)


def test_http_boundary_has_one_strict_non_text_operation_and_exact_caller():
    publisher, instruction, _, _, _, x, _ = prepared(enabled=False)
    email = "captain-publisher-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com"
    auth = PublisherAuthConfig("https://publisher.example", email)
    app = create_publisher_app(publisher, Authorizer(email), auth)
    payload = {
        "version": 1,
        "generation_id": str(instruction.generation_id),
        "attempt_id": str(instruction.attempt_id),
        "handoff_digest": instruction.handoff_digest,
        "candidate_digest": instruction.candidate_digest,
        "claim_id": str(instruction.claim_id),
    }
    response = app.test_client().post(
        "/captain/publisher/execute", json=payload, headers={"Authorization": "Bearer token"}
    )
    assert response.status_code == 200 and response.json == {
        "event_id": None,
        "post_id": None,
        "status": "disabled",
    }
    assert x.write_calls == 0
    assert app.test_client().post("/captain/publisher/execute", json=payload).status_code == 403
    wrong = create_publisher_app(publisher, Authorizer("captain-worker@example.com"), auth)
    assert (
        wrong.test_client()
        .post("/captain/publisher/execute", json=payload, headers={"Authorization": "Bearer token"})
        .status_code
        == 403
    )
    assert (
        app.test_client()
        .post(
            "/captain/publisher/execute",
            json={**payload, "tweet": "arbitrary"},
            headers={"Authorization": "Bearer token"},
        )
        .status_code
        == 409
    )
