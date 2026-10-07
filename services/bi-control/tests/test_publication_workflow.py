from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_bi_control import (
    DashboardAnswerAuthority,
    DashboardAuthorityUnavailable,
    DashboardCompositionError,
    DashboardNoValidPlan,
    DashboardProductGenerationReference,
    DashboardProviderReceipt,
    DashboardPublicationAttempt,
    DashboardPublicationConflict,
    DashboardPublicationIntent,
    DashboardPublicationNotPending,
    DashboardPublicationRecord,
    DashboardPublicationWorkflow,
    DashboardStaleRevision,
    DeclareDashboardPublicationCommand,
    PublishDashboardCommand,
    SQLiteDashboardPublicationRepository,
    classify_publication_failure,
)
from heinzel_contract_model import ArtifactReference
from heinzel_provider_sdk import ProviderError
from heinzel_request_management import GovernedAnswerVerificationError

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
RETENTION = timedelta(hours=1)
PRODUCT_REF = ArtifactReference(artifact_id="product:orders", version=2, digest="a" * 64)
METRIC_REF = ArtifactReference(artifact_id="metric:revenue", version=3, digest="b" * 64)


def _answer(
    *,
    tenant_id: str = "tenant-a",
    request_id: str = "request-1",
    answer_id: str = "answer-1",
    result_expires_at: datetime = NOW + RETENTION,
) -> DashboardAnswerAuthority:
    return DashboardAnswerAuthority(
        tenant_id=tenant_id,
        request_id=request_id,
        request_revision=6,
        answer_id=answer_id,
        title="Revenue by region",
        execution_receipt_ref="execution-1",
        result_ref="result-1",
        result_digest="c" * 64,
        product_generation_refs=(
            DashboardProductGenerationReference(product_ref=PRODUCT_REF, generation=7),
        ),
        metric_version_refs=(METRIC_REF,),
        as_of=NOW - timedelta(minutes=5),
        freshness_disposition="current",
        delivered_at=NOW - timedelta(minutes=1),
        result_expires_at=result_expires_at,
    )


def _receipt(*, revision: int = 1) -> DashboardProviderReceipt:
    return DashboardProviderReceipt(
        tenant_id="tenant-a",
        dashboard_id="dashboard:revenue",
        version=1,
        revision=revision,
        stable_external_key="pm-dashboard-" + "d" * 24,
        desired_digest="e" * 64,
        lifecycle_state="active",
        external_url="https://superset.internal/dashboard/7",
        provider_version="4.1.1",
        applied_at=NOW,
    )


class _Answers:
    """Stand in for the request-management reader, returning the real authority model."""

    def __init__(self, authority: DashboardAnswerAuthority | None, *, raises: bool = False) -> None:
        self.authority = authority
        self.raises = raises
        self.calls = 0

    def read_exact(
        self, *, tenant_id: str, request_id: str, answer_id: str
    ) -> DashboardAnswerAuthority | None:
        self.calls += 1
        if self.raises:
            raise GovernedAnswerVerificationError("dashboard answer result evidence has expired")
        return self.authority


class _Publisher:
    """Stand in for composition, returning the real receipt model or the real refusal types."""

    def __init__(self, *outcomes: DashboardProviderReceipt | Exception) -> None:
        self.outcomes = list(outcomes)
        self.commands: list[PublishDashboardCommand] = []

    def publish(self, command: PublishDashboardCommand) -> DashboardProviderReceipt:
        self.commands.append(command)
        outcome = self.outcomes.pop(0) if self.outcomes else _receipt()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _command(
    *, intent_id: str = "intent-1", **overrides: object
) -> DeclareDashboardPublicationCommand:
    fields: dict[str, object] = {
        "intent_id": intent_id,
        "tenant_id": "tenant-a",
        "dashboard_id": "dashboard:revenue",
        "dashboard_version": 1,
        "request_id": "request-1",
        "answer_id": "answer-1",
        "expected_revision": 1,
    }
    fields.update(overrides)
    return DeclareDashboardPublicationCommand.model_validate(fields)


def _workflow(
    *,
    publisher: _Publisher | None = None,
    answers: _Answers | None = None,
    clock: _Clock | None = None,
    database_path: str = ":memory:",
) -> tuple[DashboardPublicationWorkflow, _Publisher, _Clock, SQLiteDashboardPublicationRepository]:
    repository = SQLiteDashboardPublicationRepository(database_path)
    resolved_publisher = publisher if publisher is not None else _Publisher()
    resolved_clock = clock if clock is not None else _Clock()
    workflow = DashboardPublicationWorkflow(
        repository=repository,
        composition=resolved_publisher,
        answers=answers if answers is not None else _Answers(_answer()),
        clock=resolved_clock,
    )
    return workflow, resolved_publisher, resolved_clock, repository


def test_a_declared_intent_takes_its_deadline_from_the_answers_result_snapshot() -> None:
    workflow, _, _, _ = _workflow(answers=_Answers(_answer(result_expires_at=NOW + RETENTION)))

    record = workflow.declare(_command())

    assert record.state == "pending"
    assert record.attempts == ()
    assert record.intent.result_expires_at == NOW + RETENTION
    assert record.intent.declared_at == NOW


def test_an_answer_that_is_not_delivered_opens_no_publication_window() -> None:
    workflow, publisher, _, _ = _workflow(answers=_Answers(None))

    with pytest.raises(DashboardAuthorityUnavailable):
        workflow.declare(_command())

    assert publisher.commands == []
    with pytest.raises(LookupError):
        workflow.read(tenant_id="tenant-a", intent_id="intent-1")


def test_an_answer_whose_evidence_failed_verification_opens_no_publication_window() -> None:
    workflow, _, _, _ = _workflow(answers=_Answers(None, raises=True))

    with pytest.raises(DashboardCompositionError):
        workflow.declare(_command())


def test_an_answer_for_another_request_cannot_be_declared_under_this_command() -> None:
    workflow, _, _, _ = _workflow(answers=_Answers(_answer(request_id="request-2")))

    with pytest.raises(DashboardCompositionError):
        workflow.declare(_command())


def test_declaring_one_intent_twice_returns_the_first_window_rather_than_extending_it() -> None:
    answers = _Answers(_answer())
    workflow, _, clock, _ = _workflow(answers=answers)
    first = workflow.declare(_command())

    clock.now = NOW + timedelta(minutes=30)
    answers.authority = _answer(result_expires_at=NOW + timedelta(days=30))
    replay = workflow.declare(_command())

    assert replay == first
    assert replay.intent.result_expires_at == NOW + RETENTION


def test_a_different_publication_under_one_intent_id_is_refused() -> None:
    answers = _Answers(_answer())
    workflow, _, _, _ = _workflow(answers=answers)
    workflow.declare(_command())

    answers.authority = _answer(answer_id="answer-2")
    with pytest.raises(DashboardPublicationConflict):
        workflow.declare(_command(answer_id="answer-2"))


def test_an_intent_past_its_deadline_expires_without_reaching_the_provider() -> None:
    workflow, publisher, clock, _ = _workflow()
    workflow.declare(_command())

    clock.now = NOW + RETENTION
    record = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert record.state == "expired"
    assert publisher.commands == []
    assert record.attempts[-1].outcome == "expired"
    assert record.attempts[-1].failure_code is None
    assert record.attempts[-1].dashboard_revision is None


def test_an_intent_inside_its_deadline_publishes_and_records_what_was_applied() -> None:
    workflow, publisher, _, _ = _workflow(publisher=_Publisher(_receipt(revision=1)))
    workflow.declare(_command())

    record = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert record.state == "published"
    assert len(publisher.commands) == 1
    assert publisher.commands[0].expected_revision == 1
    assert record.attempts[-1].outcome == "published"
    assert record.attempts[-1].dashboard_revision == 1
    assert record.attempts[-1].desired_digest == "e" * 64


def test_a_retryable_failure_leaves_the_intent_pending_and_a_later_attempt_publishes() -> None:
    publisher = _Publisher(
        DashboardAuthorityUnavailable("dashboard connection authority is unavailable"),
        _receipt(revision=1),
    )
    workflow, _, clock, _ = _workflow(publisher=publisher)
    workflow.declare(_command())

    failed = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    assert failed.state == "pending"
    assert failed.attempts[-1].failure_code == "authority_unavailable"
    assert failed.attempts[-1].retryable is True

    clock.now = NOW + timedelta(minutes=5)
    published = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert published.state == "published"
    assert len(published.attempts) == 2
    assert len(publisher.commands) == 2


def test_a_terminal_failure_settles_the_publication_without_a_further_attempt() -> None:
    publisher = _Publisher(DashboardNoValidPlan("dashboard publication requires a visual intent"))
    workflow, _, _, _ = _workflow(publisher=publisher)
    workflow.declare(_command())

    failed = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    assert failed.state == "failed"
    assert failed.attempts[-1].failure_code == "no_valid_plan"
    assert failed.attempts[-1].retryable is False

    replay = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert replay == failed
    assert len(publisher.commands) == 1


def test_an_ambiguous_provider_outcome_is_not_retried_automatically() -> None:
    publisher = _Publisher(ProviderError("dashboard apply outcome unknown", "ambiguous"))
    workflow, _, _, _ = _workflow(publisher=publisher)
    workflow.declare(_command())

    record = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert record.state == "failed"
    assert record.attempts[-1].failure_code == "provider_ambiguous"
    assert record.attempts[-1].retryable is False


def test_the_retention_is_the_whole_retry_budget_for_a_failed_publication() -> None:
    publisher = _Publisher(
        ProviderError("superset is unreachable", "transient_unavailable"),
        _receipt(revision=1),
    )
    workflow, _, clock, _ = _workflow(publisher=publisher)
    workflow.declare(_command())

    pending = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    assert pending.state == "pending"
    assert pending.attempts[-1].failure_code == "provider_unavailable"

    clock.now = NOW + RETENTION + timedelta(seconds=1)
    expired = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert expired.state == "expired"
    assert len(publisher.commands) == 1
    assert [attempt.outcome for attempt in expired.attempts] == ["failed", "expired"]


def test_advancing_a_published_publication_repeats_no_provider_effect() -> None:
    workflow, publisher, _, _ = _workflow()
    workflow.declare(_command())
    published = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    replay = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    assert replay == published
    assert len(publisher.commands) == 1


def test_an_unexpected_publication_error_propagates_and_leaves_the_intent_pending() -> None:
    publisher = _Publisher(ValueError("dashboard desired revision is stale"))
    workflow, _, _, _ = _workflow(publisher=publisher)
    workflow.declare(_command())

    with pytest.raises(ValueError, match="stale"):
        workflow.advance(tenant_id="tenant-a", intent_id="intent-1")

    record = workflow.read(tenant_id="tenant-a", intent_id="intent-1")
    assert record.state == "pending"
    assert record.attempts == ()


def test_a_settled_publication_survives_a_repository_reopen(tmp_path: Path) -> None:
    database_path = str(tmp_path / "publications.sqlite3")
    workflow, _, _, repository = _workflow(database_path=database_path)
    workflow.declare(_command())
    published = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    repository.close()

    reopened = SQLiteDashboardPublicationRepository(database_path)
    try:
        assert reopened.load("tenant-a", "intent-1") == published
        assert reopened.list_pending("tenant-a") == ()
    finally:
        reopened.close()


def test_draining_advances_only_the_pending_intents_of_one_tenant() -> None:
    answers = _Answers(_answer())
    workflow, publisher, _, repository = _workflow(answers=answers)
    workflow.declare(_command(intent_id="intent-1"))
    workflow.declare(_command(intent_id="intent-2"))
    answers.authority = _answer(tenant_id="tenant-b")
    workflow.declare(_command(intent_id="intent-3", tenant_id="tenant-b"))

    workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    records = workflow.drain("tenant-a")

    assert [record.intent.intent_id for record in records] == ["intent-2"]
    assert len(publisher.commands) == 2
    assert [intent.intent_id for intent in repository.list_pending("tenant-b")] == ["intent-3"]


def test_a_publication_is_never_read_across_a_tenant_boundary() -> None:
    workflow, _, _, _ = _workflow()
    workflow.declare(_command())

    with pytest.raises(LookupError):
        workflow.read(tenant_id="tenant-b", intent_id="intent-1")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DashboardAuthorityUnavailable("unavailable"), "authority_unavailable"),
        (DashboardCompositionError("does not match"), "authority_invalid"),
        (DashboardNoValidPlan("no plan"), "no_valid_plan"),
        (DashboardStaleRevision("stale"), "stale_revision"),
        (ProviderError("throttled", "throttled"), "provider_unavailable"),
        (ProviderError("transport", "transient_transport"), "provider_unavailable"),
        (ProviderError("unknown", "ambiguous_outcome"), "provider_ambiguous"),
        (ProviderError("refused", "permanent"), "provider_rejected"),
        (ProviderError("denied", "authorization"), "provider_rejected"),
    ],
)
def test_each_publication_failure_is_classified_without_carrying_its_message(
    error: DashboardCompositionError | ProviderError, expected: str
) -> None:
    assert classify_publication_failure(error) == expected


def test_an_intent_whose_deadline_has_already_passed_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="deadline"):
        DashboardPublicationIntent(
            intent_id="intent-1",
            tenant_id="tenant-a",
            dashboard_id="dashboard:revenue",
            dashboard_version=1,
            request_id="request-1",
            answer_id="answer-1",
            expected_revision=1,
            command_digest="1" * 64,
            result_expires_at=NOW,
            declared_at=NOW,
        )


def test_a_published_attempt_that_names_nothing_it_applied_is_unconstructable() -> None:
    with pytest.raises(ValueError, match="revision"):
        DashboardPublicationAttempt(
            intent_id="intent-1",
            intent_digest="f" * 64,
            attempt=1,
            outcome="published",
            observed_at=NOW,
        )


def test_an_expired_attempt_that_names_a_failure_is_unconstructable() -> None:
    with pytest.raises(ValueError, match="expired attempt"):
        DashboardPublicationAttempt(
            intent_id="intent-1",
            intent_digest="f" * 64,
            attempt=1,
            outcome="expired",
            failure_code="provider_rejected",
            observed_at=NOW,
        )


def test_a_pending_record_cannot_carry_a_terminal_failure_as_its_last_attempt() -> None:
    intent = DashboardPublicationIntent(
        intent_id="intent-1",
        tenant_id="tenant-a",
        dashboard_id="dashboard:revenue",
        dashboard_version=1,
        request_id="request-1",
        answer_id="answer-1",
        expected_revision=1,
        command_digest="1" * 64,
        result_expires_at=NOW + RETENTION,
        declared_at=NOW,
    )
    terminal = DashboardPublicationAttempt(
        intent_id="intent-1",
        intent_digest=intent.intent_digest,
        attempt=1,
        outcome="failed",
        failure_code="no_valid_plan",
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="retryable failure"):
        DashboardPublicationRecord(intent=intent, state="pending", attempts=(terminal,))

    with pytest.raises(ValueError, match="does not belong"):
        DashboardPublicationRecord(
            intent=intent,
            state="failed",
            attempts=(terminal.model_copy(update={"intent_digest": "0" * 64}),),
        )


def test_an_attempt_against_an_already_settled_publication_is_refused() -> None:
    workflow, _, _, repository = _workflow()
    declared = workflow.declare(_command())
    settled = workflow.advance(tenant_id="tenant-a", intent_id="intent-1")
    assert settled.state == "published"

    with pytest.raises(DashboardPublicationNotPending):
        repository.record_attempt(
            DashboardPublicationAttempt(
                intent_id="intent-1",
                intent_digest=declared.intent.intent_digest,
                attempt=2,
                outcome="expired",
                observed_at=NOW + timedelta(minutes=1),
            ),
            tenant_id="tenant-a",
            state="expired",
        )

    assert repository.load("tenant-a", "intent-1") == settled


def test_a_record_cannot_continue_past_the_attempt_that_settled_it() -> None:
    intent = DashboardPublicationIntent(
        intent_id="intent-1",
        tenant_id="tenant-a",
        dashboard_id="dashboard:revenue",
        dashboard_version=1,
        request_id="request-1",
        answer_id="answer-1",
        expected_revision=1,
        command_digest="1" * 64,
        result_expires_at=NOW + RETENTION,
        declared_at=NOW,
    )
    expired = DashboardPublicationAttempt(
        intent_id="intent-1",
        intent_digest=intent.intent_digest,
        attempt=1,
        outcome="expired",
        observed_at=NOW,
    )
    published = DashboardPublicationAttempt(
        intent_id="intent-1",
        intent_digest=intent.intent_digest,
        attempt=2,
        outcome="published",
        dashboard_revision=1,
        desired_digest="e" * 64,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="further attempt"):
        DashboardPublicationRecord(intent=intent, state="published", attempts=(expired, published))
