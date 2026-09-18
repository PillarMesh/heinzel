from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    AnswerIntentValidation,
    AnswerPolicyAdmissionService,
    AnswerQuestionIntent,
    AnswerQuestionValidationResult,
    AnswerScopePolicy,
    PolicyAdmissionResult,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteRequestRepository,
    TransitionEvent,
)
from heinzel_request_management.answer_investigation import (
    AnswerInvestigationDenied,
    ConfirmAnswerInvestigation,
    SQLiteAnswerInvestigationAuthority,
)
from heinzel_request_management.answer_validation_repository import (
    SQLiteAnswerValidationRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


class _DefinitionAuthorities:
    def read(self, tenant_id: str, plan_digest: str) -> None:
        raise AssertionError("definition admission must not read a query plan")

    def list_breached_statement_digests(
        self, *, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[str, ...]:
        raise AssertionError("definition admission must not read statement usage")


def _policy(*, restatement_confirmation: str = "never") -> AnswerScopePolicy:
    reference = ArtifactReference(artifact_id="semantic-a", version=1, digest="1" * 64)
    return AnswerScopePolicy.model_validate(
        {
            "policy_id": "policy-a",
            "tenant_id": "tenant-a",
            "revision": 1,
            "prior_policy_digest": None,
            "principal_scope": ("requester-a",),
            "purposes": ("operations",),
            "semantic_version_ref": reference,
            "data_product_version_refs": (reference,),
            "metric_version_refs": (),
            "dimension_refs": (),
            "filter_domains": (),
            "max_time_window": 3600,
            "max_staleness": 60,
            "quality_disposition": "block",
            "disclosure_classifications": (),
            "disclosure_entity": "customer",
            "minimum_group_size": 1,
            "restatement_confirmation": restatement_confirmation,
            "row_ceiling": 10,
            "byte_ceiling": 1000,
            "scan_ceiling": 1000,
            "period_scan_budget": 10000,
            "statement_timeout": 10,
            "result_retention": 3600,
            "agent_access": "denied",
            "model_disclosure": "metadata",
            "valid_from": NOW.replace(hour=11),
            "valid_until": NOW.replace(hour=13),
            "created_at": NOW.replace(hour=11),
            "approval_ids": ("approval-a",),
        }
    )


def _fixture(
    *,
    question_digest: str | None = None,
    confirmation_required: bool = False,
    policy: AnswerScopePolicy | None = None,
) -> tuple[
    sqlite3.Connection,
    SQLiteRequestRepository,
    SQLiteAnswerInvestigationAuthority,
    AnswerQuestionValidationResult,
]:
    connection = sqlite3.connect(":memory:")
    requests = SQLiteRequestRepository(connection)
    request = RequestManagementService(requests, clock=lambda: NOW).submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="operations",
        question="What is revenue?",
    )
    intent = AnswerQuestionIntent(
        intent_id="intent-a",
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        question_digest=question_digest or digest(request.payload),
        intent_kind="definition",
        metric_refs=(),
        dimension_refs=(),
        filters=(),
        time_window=None,
        ordering=(),
        row_limit=10,
        interpreter="form",
        interpreter_ref="answer-form-v1",
        created_at=NOW,
    )
    resolved_policy = policy or _policy()
    validation = AnswerIntentValidation(
        validation_id="validation-a",
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        intent_digest=digest(intent),
        semantic_version_digest=resolved_policy.semantic_version_ref.digest,
        policy_id=resolved_policy.policy_id,
        policy_revision=resolved_policy.revision,
        policy_digest=resolved_policy.canonical_digest(),
        entitlement_snapshot_digest="3" * 64,
        bound_metric_versions=(),
        bound_dimensions=(),
        bound_filters=(),
        restatement="Explain revenue",
        product_generation_refs=(),
        outcome="admitted",
        reason_codes=(),
        created_at=NOW,
    )
    result = SQLiteAnswerValidationRepository(connection).save(
        AnswerQuestionValidationResult(
            intent=intent,
            validation=validation,
            restatement_confirmation_required=confirmation_required,
        )
    )
    authority = SQLiteAnswerInvestigationAuthority(connection, clock=lambda: NOW)
    return connection, requests, authority, result


def _command(
    result: AnswerQuestionValidationResult, **changes: object
) -> ConfirmAnswerInvestigation:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "request_id": result.intent.request_id,
        "expected_revision": result.intent.request_revision,
        "requester_id": "requester-a",
        "validation_digest": digest(result.validation),
    }
    values.update(changes)
    return ConfirmAnswerInvestigation.model_validate(values)


def test_confirmation_atomically_advances_the_exact_submitted_revision_and_replays() -> None:
    connection, requests, authority, result = _fixture()

    first = authority.confirm(_command(result))
    replay = authority.confirm(_command(result))

    assert replay == first
    assert first.validation_request_revision == 1
    assert first.confirmed_revision == 2
    current = requests.load_owned_request("tenant-a", result.intent.request_id)
    assert current.state is RequestState.INVESTIGATING
    assert current.revision == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM answer_investigation_confirmations"
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "changes",
    (
        {"tenant_id": "tenant-b"},
        {"requester_id": "requester-b"},
        {"expected_revision": 2},
        {"validation_digest": "f" * 64},
    ),
)
def test_unbound_confirmation_never_advances_the_request(changes: dict[str, object]) -> None:
    connection, requests, authority, result = _fixture()

    with pytest.raises(AnswerInvestigationDenied):
        authority.confirm(_command(result, **changes))

    current = requests.load_owned_request("tenant-a", result.intent.request_id)
    assert current.state is RequestState.SUBMITTED
    assert current.revision == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM answer_investigation_confirmations"
    ).fetchone() == (0,)


def test_confirmation_rejects_a_changed_question() -> None:
    _, requests, authority, result = _fixture(question_digest="f" * 64)

    with pytest.raises(AnswerInvestigationDenied, match="question has changed"):
        authority.confirm(_command(result))

    assert (
        requests.load_owned_request("tenant-a", result.intent.request_id).state
        is RequestState.SUBMITTED
    )


def test_confirmation_required_is_left_for_the_restatement_authority() -> None:
    _, requests, authority, result = _fixture(confirmation_required=True)

    with pytest.raises(AnswerInvestigationDenied, match="requires confirmation"):
        authority.confirm(_command(result))

    assert (
        requests.load_owned_request("tenant-a", result.intent.request_id).state
        is RequestState.SUBMITTED
    )


def test_confirmation_rejects_a_non_submitted_current_revision() -> None:
    connection, requests, authority, result = _fixture()
    request = requests.load_owned_request("tenant-a", result.intent.request_id).model_copy(
        update={"state": RequestState.CLARIFYING}
    )
    connection.execute(
        "UPDATE request_revisions SET payload = ? WHERE request_id = ? AND revision = 1",
        (canonical_bytes(request), result.intent.request_id),
    )
    connection.commit()

    with pytest.raises(AnswerInvestigationDenied, match="stale"):
        authority.confirm(_command(result))


def test_missing_durable_revision_is_typed_corruption_denial() -> None:
    connection, _, authority, result = _fixture()
    confirmation = authority.confirm(_command(result))
    connection.execute(
        "DELETE FROM request_revisions WHERE request_id = ? AND revision = 1",
        (result.intent.request_id,),
    )
    connection.commit()

    with pytest.raises(AnswerInvestigationDenied, match="continuity"):
        authority.read(tenant_id="tenant-a", confirmation_id=confirmation.confirmation_id)


def test_missing_transition_event_is_typed_corruption_denial() -> None:
    connection, _, authority, result = _fixture()
    confirmation = authority.confirm(_command(result))
    connection.execute(
        "DELETE FROM transition_events WHERE request_id = ? AND request_revision = 2",
        (result.intent.request_id,),
    )
    connection.commit()

    with pytest.raises(AnswerInvestigationDenied, match="continuity"):
        authority.read(tenant_id="tenant-a", confirmation_id=confirmation.confirmation_id)


def test_changed_transition_actor_is_typed_corruption_denial() -> None:
    connection, _, authority, result = _fixture()
    confirmation = authority.confirm(_command(result))
    event = connection.execute(
        "SELECT event_id, payload FROM transition_events WHERE request_id = ?",
        (result.intent.request_id,),
    ).fetchone()
    assert event is not None
    changed = TransitionEvent.model_validate_json(event[1], strict=True).model_copy(
        update={"actor_id": "requester-a"}
    )
    connection.execute(
        "UPDATE transition_events SET payload = ? WHERE event_id = ?",
        (canonical_bytes(changed), event[0]),
    )
    connection.commit()

    with pytest.raises(AnswerInvestigationDenied, match="continuity"):
        authority.read(tenant_id="tenant-a", confirmation_id=confirmation.confirmation_id)


def test_confirmed_revision_requires_the_exact_durable_validation() -> None:
    _, _, authority, result = _fixture()
    authority.confirm(_command(result))

    assert (
        authority.confirmed_revision(
            tenant_id="tenant-a",
            request_id=result.intent.request_id,
            requester_id="requester-a",
            validation=result.validation,
        )
        == 2
    )
    assert (
        authority.confirmed_revision(
            tenant_id="tenant-b",
            request_id=result.intent.request_id,
            requester_id="requester-a",
            validation=result.validation,
        )
        is None
    )


def _admission_service(
    requests: SQLiteRequestRepository,
    investigations: SQLiteAnswerInvestigationAuthority,
) -> AnswerPolicyAdmissionService:
    authorities = _DefinitionAuthorities()
    return AnswerPolicyAdmissionService(
        SQLiteAnswerAdmissionRepository(requests),
        plans=authorities,
        breaches=authorities,
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-a",
        investigations=investigations,
    )


def _admit_definition(
    service: AnswerPolicyAdmissionService,
    result: AnswerQuestionValidationResult,
    *,
    policy: AnswerScopePolicy | None = None,
    restatement_acceptance_ref: str | None = None,
) -> PolicyAdmissionResult:
    resolved_policy = policy or _policy()
    return service.admit(
        intent=result.intent,
        validation=result.validation,
        policy=resolved_policy,
        plan=None,
        restatement_acceptance_ref=restatement_acceptance_ref,
        current_entitlement_snapshot_digest=result.validation.entitlement_snapshot_digest,
        latest_policy_revision=resolved_policy.revision,
        actor_id="system:answer-policy",
    )


def test_durable_confirmation_authorizes_definition_admission_and_historical_replay() -> None:
    _, requests, investigations, result = _fixture()
    investigations.confirm(_command(result))
    service = _admission_service(requests, investigations)

    first = _admit_definition(service, result)
    replay = _admit_definition(service, result)

    assert first.receipt is not None
    assert first.receipt.request_revision == 2
    assert first.receipt.restatement_acceptance_ref is None
    assert replay.receipt == first.receipt
    assert replay.request.state is RequestState.EXECUTING
    assert replay.request.revision == 3


def test_investigation_confirmation_never_satisfies_required_restatement() -> None:
    policy = _policy(restatement_confirmation="always")
    _, requests, investigations, result = _fixture(policy=policy)
    investigations.confirm(_command(result))
    service = _admission_service(requests, investigations)

    admission = _admit_definition(
        service,
        result,
        policy=policy,
    )

    assert admission.receipt is None
    assert "restatement_not_accepted" in admission.evaluation.reason_codes


def test_nonempty_restatement_reference_disables_investigation_continuity() -> None:
    _, requests, investigations, result = _fixture()
    investigations.confirm(_command(result))
    service = _admission_service(requests, investigations)

    admission = _admit_definition(
        service,
        result,
        restatement_acceptance_ref="restatement-invented",
    )

    assert admission.receipt is None
    assert "intent_binding_mismatch" in admission.evaluation.reason_codes


def test_corrupt_investigation_transition_denies_admission_and_replay() -> None:
    connection, requests, investigations, result = _fixture()
    investigations.confirm(_command(result))
    service = _admission_service(requests, investigations)
    connection.execute(
        "DELETE FROM transition_events WHERE request_id = ? AND request_revision = 2",
        (result.intent.request_id,),
    )
    connection.commit()

    with pytest.raises(AnswerInvestigationDenied, match="continuity"):
        _admit_definition(service, result)
