from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AnswerIntentCandidate,
    AnswerQuestion,
    AnswerQuestionService,
    AnswerScopePolicy,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    AnswerScopePolicyLifecycle,
    AnswerValidationContext,
    BoundSemanticReference,
    FilterDomain,
    MissingAnswerPolicyApproval,
    ProductOwnerAuthority,
    SQLiteAnswerValidationRepository,
)
from pillarmesh_request_management.answer_models import AnswerProductGenerationReference
from pillarmesh_request_management.answer_service import AnswerInterpretationDenied

NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _reference(identifier: str, version: int = 1) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=version, digest=DIGEST)


def _draft(**changes: object) -> AnswerScopePolicyDraft:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "tenant_id": "tenant-1",
        "revision": 1,
        "prior_policy_digest": None,
        "principal_scope": ("principal-1",),
        "purposes": ("operations",),
        "semantic_version_ref": _reference("semantic"),
        "data_product_version_refs": (_reference("orders-product"),),
        "metric_version_refs": (_reference("net-revenue"),),
        "dimension_refs": (),
        "filter_domains": (),
        "max_time_window": 3600,
        "max_staleness": 300,
        "quality_disposition": "block",
        "disclosure_classifications": (),
        "disclosure_entity": "customer",
        "minimum_group_size": 1,
        "restatement_confirmation": "model_interpreted",
        "row_ceiling": 100,
        "byte_ceiling": 100_000,
        "scan_ceiling": 1000,
        "period_scan_budget": 10_000,
        "statement_timeout": 30,
        "result_retention": 3600,
        "agent_access": "allowed",
        "model_disclosure": "metadata",
        "valid_from": NOW,
        "valid_until": NOW + timedelta(days=30),
        "created_at": NOW,
    }
    values.update(changes)
    return AnswerScopePolicyDraft.model_validate(values)


def _approval(
    identifier: str,
    authority_ref: str,
    draft: AnswerScopePolicyDraft,
) -> AnswerScopePolicyApproval:
    return AnswerScopePolicyApproval(
        approval_id=identifier,
        tenant_id="tenant-1",
        policy_id="policy-1",
        policy_revision=draft.revision,
        policy_digest=digest(draft),
        authority_ref=authority_ref,
        actor_id=f"actor-{identifier}",
        decision="approve",
        created_at=NOW,
    )


class PolicyRepository:
    def __init__(self) -> None:
        self.policies: list[AnswerScopePolicy] = []

    def list_revisions(self, tenant_id: str, policy_id: str) -> tuple[AnswerScopePolicy, ...]:
        return tuple(
            policy
            for policy in self.policies
            if policy.tenant_id == tenant_id and policy.policy_id == policy_id
        )

    def save(
        self,
        policy: AnswerScopePolicy,
        *,
        approvals: tuple[AnswerScopePolicyApproval, ...],
    ) -> None:
        assert tuple(approval.approval_id for approval in approvals) == policy.approval_ids
        self.policies.append(policy)


def test_policy_lifecycle_enforces_approval_matrix_and_supersedes_prior_revision() -> None:
    repository = PolicyRepository()
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    first_draft = _draft()
    first = lifecycle.activate(
        draft=first_draft,
        approvals=(
            _approval("architect-1", "role:data_engineering_architect", first_draft),
            _approval("owner-1", "owner:orders-product", first_draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )
    second_draft = _draft(
        revision=2,
        prior_policy_digest=first.canonical_digest(),
        model_disclosure="results",
        created_at=NOW + timedelta(minutes=1),
    )

    second = lifecycle.activate(
        draft=second_draft,
        approvals=(
            _approval("architect-2", "role:data_engineering_architect", second_draft),
            _approval("owner-2", "owner:orders-product", second_draft),
            _approval("policy-2", "role:policy_authority", second_draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )
    repository.policies.reverse()
    third_draft = _draft(
        revision=3,
        prior_policy_digest=second.canonical_digest(),
        created_at=NOW + timedelta(minutes=2),
    )
    third = lifecycle.activate(
        draft=third_draft,
        approvals=(
            _approval("architect-3", "role:data_engineering_architect", third_draft),
            _approval("owner-3", "owner:orders-product", third_draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )

    assert lifecycle.resolve_current(tenant_id="tenant-1", policy_id="policy-1") == third
    assert first.revision == 1
    assert second.revision == 2
    assert third.revision == 3


def test_policy_lifecycle_rejects_missing_conditional_and_budget_approvals() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft(
        filter_domains=(),
        minimum_group_size=5,
        period_scan_budget=30_000,
        model_disclosure="results",
    )

    with pytest.raises(MissingAnswerPolicyApproval) as error:
        lifecycle.activate(
            draft=draft,
            approvals=(
                _approval("architect", "role:data_engineering_architect", draft),
                _approval("owner", "owner:orders-product", draft),
            ),
            product_owner_bindings=(
                ProductOwnerAuthority(
                    data_product_version_ref=_reference("orders-product"),
                    authority_ref="owner:orders-product",
                ),
            ),
            period_scan_budget_threshold=20_000,
        )

    assert error.value.missing_authority_refs == (
        "role:policy_authority",
        "role:budget_authority",
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"disclosure_classifications": ("classified",)},
        {"filter_domains": (FilterDomain(dimension_ref="region", values=("east",)),)},
        {"minimum_group_size": 2},
        {"model_disclosure": "results"},
    ),
)
def test_each_conditional_scope_independently_requires_policy_authority(
    changes: dict[str, object],
) -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft(**changes)

    with pytest.raises(MissingAnswerPolicyApproval) as error:
        lifecycle.activate(
            draft=draft,
            approvals=(
                _approval("architect", "role:data_engineering_architect", draft),
                _approval("owner", "owner:orders-product", draft),
            ),
            product_owner_bindings=(
                ProductOwnerAuthority(
                    data_product_version_ref=_reference("orders-product"),
                    authority_ref="owner:orders-product",
                ),
            ),
            period_scan_budget_threshold=20_000,
        )

    assert error.value.missing_authority_refs == ("role:policy_authority",)


def test_budget_equal_to_threshold_does_not_require_budget_authority() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft(period_scan_budget=20_000)

    policy = lifecycle.activate(
        draft=draft,
        approvals=(
            _approval("architect", "role:data_engineering_architect", draft),
            _approval("owner", "owner:orders-product", draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )

    assert policy.approval_ids == ("architect", "owner")


def test_zero_budget_threshold_is_valid_and_requires_budget_authority() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft()

    policy = lifecycle.activate(
        draft=draft,
        approvals=(
            _approval("architect", "role:data_engineering_architect", draft),
            _approval("owner", "owner:orders-product", draft),
            _approval("budget", "role:budget_authority", draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=0,
    )

    assert policy.approval_ids == ("architect", "owner", "budget")


def test_policy_records_only_exact_approved_bindings() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft()
    architect = _approval("architect", "role:data_engineering_architect", draft)
    owner = _approval("owner", "owner:orders-product", draft)
    unrelated = (
        architect.model_copy(update={"approval_id": "rejected", "decision": "reject"}),
        architect.model_copy(update={"approval_id": "tenant", "tenant_id": "tenant-2"}),
        architect.model_copy(update={"approval_id": "policy", "policy_id": "policy-2"}),
        architect.model_copy(update={"approval_id": "revision", "policy_revision": 2}),
        architect.model_copy(update={"approval_id": "digest", "policy_digest": "b" * 64}),
    )

    policy = lifecycle.activate(
        draft=draft,
        approvals=(architect, owner, *unrelated),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )

    assert policy.approval_ids == ("architect", "owner")


def test_policy_lifecycle_rejects_approvals_for_a_materially_different_draft() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    approved_draft = _draft()
    changed_draft = _draft(row_ceiling=101)

    with pytest.raises(MissingAnswerPolicyApproval):
        lifecycle.activate(
            draft=changed_draft,
            approvals=(
                _approval("architect", "role:data_engineering_architect", approved_draft),
                _approval("owner", "owner:orders-product", approved_draft),
            ),
            product_owner_bindings=(
                ProductOwnerAuthority(
                    data_product_version_ref=_reference("orders-product"),
                    authority_ref="owner:orders-product",
                ),
            ),
            period_scan_budget_threshold=20_000,
        )


def test_policy_lifecycle_requires_exact_owner_authority_for_every_product() -> None:
    lifecycle = AnswerScopePolicyLifecycle(PolicyRepository(), clock=lambda: NOW)
    draft = _draft(
        data_product_version_refs=(
            _reference("orders-product"),
            _reference("customer-product"),
        )
    )

    with pytest.raises(ValueError, match="exactly once for every data product"):
        lifecycle.activate(
            draft=draft,
            approvals=(
                _approval("architect", "role:data_engineering_architect", draft),
                _approval("owner", "owner:orders-product", draft),
            ),
            product_owner_bindings=(
                ProductOwnerAuthority(
                    data_product_version_ref=_reference("orders-product"),
                    authority_ref="owner:orders-product",
                ),
            ),
            period_scan_budget_threshold=20_000,
        )


def test_expired_policy_resolves_to_no_current_authority() -> None:
    repository = PolicyRepository()
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    draft = _draft(valid_from=NOW - timedelta(days=2), valid_until=NOW - timedelta(days=1))
    lifecycle.activate(
        draft=draft,
        approvals=(
            _approval("architect", "role:data_engineering_architect", draft),
            _approval("owner", "owner:orders-product", draft),
        ),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )

    assert lifecycle.resolve_current(tenant_id="tenant-1", policy_id="policy-1") is None


class ScriptedInterpreter:
    def __init__(self, candidate: AnswerIntentCandidate) -> None:
        self.candidate = candidate
        self.questions: list[AnswerQuestion] = []

    def interpret(self, question: AnswerQuestion) -> AnswerIntentCandidate:
        self.questions.append(question)
        return self.candidate


class ScriptedCurrentPolicyResolver:
    def __init__(self, policies: tuple[AnswerScopePolicy, ...]) -> None:
        self._policies = policies
        self.calls = 0

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose: str
    ) -> AnswerScopePolicy | None:
        policy = self._policies[min(self.calls, len(self._policies) - 1)]
        self.calls += 1
        return (
            policy
            if tenant_id == policy.tenant_id
            and principal_ref in policy.principal_scope
            and purpose in policy.purposes
            else None
        )


def _policy_revision(policy: AnswerScopePolicy, revision: int) -> AnswerScopePolicy:
    return AnswerScopePolicy.model_validate(
        {
            **policy.model_dump(mode="python"),
            "revision": revision,
            "prior_policy_digest": None if revision == 1 else "d" * 64,
        }
    )


def _answer_service_fixture() -> tuple[
    AnswerQuestionService,
    ScriptedInterpreter,
    AnswerQuestion,
    AnswerScopePolicy,
    AnswerValidationContext,
]:
    interpreter = ScriptedInterpreter(
        AnswerIntentCandidate(
            intent_kind="definition",
            metric_refs=("revenue",),
            dimension_refs=(),
            filters=(),
            time_window=None,
            ordering=(),
            row_limit=1,
        )
    )
    policy = AnswerScopePolicy.model_validate(
        {**_draft().model_dump(), "approval_ids": ("architect", "owner")}
    )
    context = AnswerValidationContext(
        semantic_version_digest=DIGEST,
        entitlement_snapshot_digest="b" * 64,
        bindings=(
            BoundSemanticReference(
                canonical_ref="net-revenue",
                kind="metric",
                aliases=("revenue",),
                version_ref=_reference("net-revenue"),
                product_version_ref=_reference("orders-product"),
            ),
        ),
        entitled_refs=("net-revenue",),
        answer_enabled_product_refs=(_reference("orders-product"),),
        product_generation_refs=(
            AnswerProductGenerationReference(
                product_ref=_reference("orders-product"), generation=1
            ),
        ),
        product_staleness=60,
        quality_blocked=False,
        authority_conflict=False,
        requester_principal_ref="principal-1",
        purpose="operations",
        acting_as_agent=False,
        latest_policy_revision=1,
    )
    question = AnswerQuestion(
        tenant_id="tenant-1",
        request_id="request-1",
        request_revision=4,
        question_digest="c" * 64,
        interpreter="form",
        interpreter_ref="answer-form-v1",
    )
    service = AnswerQuestionService(
        interpreter,
        SQLiteAnswerValidationRepository(sqlite3.connect(":memory:")),
        policy_resolver=ScriptedCurrentPolicyResolver((policy, policy)),
        intent_identifier=lambda: "intent-1",
        validation_identifier=lambda: "validation-1",
        clock=lambda: NOW,
    )

    return service, interpreter, question, policy, context


def test_answer_service_uses_injected_interpreter_and_stops_at_validation() -> None:
    service, interpreter, question, policy, context = _answer_service_fixture()

    result = service.interpret_and_validate(question=question, policy=policy, context=context)

    assert interpreter.questions == [question]
    assert result.intent.intent_id == "intent-1"
    assert result.validation.outcome == "admitted"
    assert result.restatement_confirmation_required is False
    assert not hasattr(result, "answer")
    assert not hasattr(service, "execute")


@pytest.mark.parametrize(
    "denial",
    [
        "model_disclosure",
        "tenant",
        "principal",
        "purpose",
        "agent",
        "expired",
        "superseded",
    ],
)
def test_answer_service_denies_disclosure_before_invoking_the_interpreter(denial: str) -> None:
    service, interpreter, question, policy, context = _answer_service_fixture()
    if denial == "model_disclosure":
        question = question.model_copy(update={"interpreter": "model"})
        policy = policy.model_copy(update={"model_disclosure": "none"})
    elif denial == "tenant":
        question = question.model_copy(update={"tenant_id": "another-tenant"})
    elif denial == "principal":
        context = context.model_copy(update={"requester_principal_ref": "another-principal"})
    elif denial == "purpose":
        context = context.model_copy(update={"purpose": "unapproved-purpose"})
    elif denial == "agent":
        context = context.model_copy(update={"acting_as_agent": True})
        policy = policy.model_copy(update={"agent_access": "denied"})
    elif denial == "expired":
        policy = policy.model_copy(
            update={"valid_from": NOW - timedelta(seconds=1), "valid_until": NOW}
        )
    else:
        context = context.model_copy(update={"latest_policy_revision": 2})

    with pytest.raises(AnswerInterpretationDenied, match="interpretation is not authorized"):
        service.interpret_and_validate(question=question, policy=policy, context=context)

    assert interpreter.questions == []


def test_answer_service_denies_matching_stale_policy_and_caller_revision() -> None:
    service, interpreter, question, policy, context = _answer_service_fixture()
    service = AnswerQuestionService(
        interpreter,
        SQLiteAnswerValidationRepository(sqlite3.connect(":memory:")),
        policy_resolver=ScriptedCurrentPolicyResolver((_policy_revision(policy, 2),)),
        intent_identifier=lambda: "intent-1",
        validation_identifier=lambda: "validation-1",
        clock=lambda: NOW,
    )

    with pytest.raises(AnswerInterpretationDenied, match="interpretation is not authorized"):
        service.interpret_and_validate(question=question, policy=policy, context=context)

    assert interpreter.questions == []


def test_answer_service_denies_caller_policy_that_does_not_match_current_authority() -> None:
    service, interpreter, question, policy, context = _answer_service_fixture()
    service = AnswerQuestionService(
        interpreter,
        SQLiteAnswerValidationRepository(sqlite3.connect(":memory:")),
        policy_resolver=ScriptedCurrentPolicyResolver((_policy_revision(policy, 2),)),
        intent_identifier=lambda: "intent-1",
        validation_identifier=lambda: "validation-1",
        clock=lambda: NOW,
    )
    context = context.model_copy(update={"latest_policy_revision": 2})

    with pytest.raises(AnswerInterpretationDenied, match="interpretation is not authorized"):
        service.interpret_and_validate(question=question, policy=policy, context=context)

    assert interpreter.questions == []


def test_answer_service_denies_policy_superseded_during_interpretation() -> None:
    service, interpreter, question, policy, context = _answer_service_fixture()
    service = AnswerQuestionService(
        interpreter,
        SQLiteAnswerValidationRepository(sqlite3.connect(":memory:")),
        policy_resolver=ScriptedCurrentPolicyResolver((policy, _policy_revision(policy, 2))),
        intent_identifier=lambda: "intent-1",
        validation_identifier=lambda: "validation-1",
        clock=lambda: NOW,
    )

    with pytest.raises(AnswerInterpretationDenied, match="interpretation is not authorized"):
        service.interpret_and_validate(question=question, policy=policy, context=context)

    assert interpreter.questions == [question]
