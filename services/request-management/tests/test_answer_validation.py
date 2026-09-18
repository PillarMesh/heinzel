from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AnswerIntentCandidate,
    AnswerIntentValidation,
    AnswerQuestionIntent,
    AnswerScopePolicy,
    AnswerValidationContext,
    BoundSemanticReference,
    FilterCandidate,
    FilterDomain,
    OrderCandidate,
    TimeWindowCandidate,
    validate_answer_intent,
)
from heinzel_request_management.answer_models import AnswerProductGenerationReference

NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _reference(identifier: str, version: int = 1) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=version, digest=DIGEST)


def _generation(
    product_ref: ArtifactReference | None = None, *, generation: int = 8
) -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(
        product_ref=product_ref or _reference("orders-product", 4),
        generation=generation,
    )


def _policy(**changes: object) -> AnswerScopePolicy:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "tenant_id": "tenant-1",
        "revision": 2,
        "prior_policy_digest": "b" * 64,
        "principal_scope": ("principal-1",),
        "purposes": ("operations",),
        "semantic_version_ref": _reference("semantic", 3),
        "data_product_version_refs": (_reference("orders-product", 4),),
        "metric_version_refs": (_reference("net-revenue", 2),),
        "dimension_refs": (_reference("region", 1),),
        "filter_domains": (FilterDomain(dimension_ref="region", values=("east", "west")),),
        "max_time_window": 31 * 24 * 60 * 60,
        "max_staleness": 3600,
        "quality_disposition": "block",
        "disclosure_classifications": (),
        "disclosure_entity": "customer",
        "minimum_group_size": 1,
        "restatement_confirmation": "model_interpreted",
        "row_ceiling": 100,
        "byte_ceiling": 1_000_000,
        "scan_ceiling": 10_000,
        "period_scan_budget": 100_000,
        "statement_timeout": 30,
        "result_retention": 86_400,
        "agent_access": "allowed",
        "model_disclosure": "metadata",
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=30),
        "approval_ids": ("approval-architect", "approval-owner"),
        "created_at": NOW - timedelta(days=1),
    }
    values.update(changes)
    return AnswerScopePolicy.model_validate(values)


def _candidate(**changes: object) -> AnswerIntentCandidate:
    values: dict[str, object] = {
        "intent_kind": "metric_value",
        "metric_refs": ("revenue",),
        "dimension_refs": ("region",),
        "filters": (FilterCandidate(dimension_ref="region", operator="equals", values=("east",)),),
        "time_window": TimeWindowCandidate(
            start=NOW - timedelta(days=7),
            end=NOW,
        ),
        "ordering": (OrderCandidate(reference="revenue", direction="descending"),),
        "row_limit": 25,
    }
    values.update(changes)
    return AnswerIntentCandidate.model_validate(values)


def _context(**changes: object) -> AnswerValidationContext:
    values: dict[str, object] = {
        "semantic_version_digest": DIGEST,
        "entitlement_snapshot_digest": "c" * 64,
        "bindings": (
            BoundSemanticReference(
                canonical_ref="net-revenue",
                kind="metric",
                aliases=("revenue",),
                version_ref=_reference("net-revenue", 2),
                product_version_ref=_reference("orders-product", 4),
            ),
            BoundSemanticReference(
                canonical_ref="region",
                kind="dimension",
                aliases=("sales region",),
                version_ref=_reference("region", 1),
                product_version_ref=_reference("orders-product", 4),
            ),
        ),
        "entitled_refs": ("net-revenue", "region"),
        "answer_enabled_product_refs": (_reference("orders-product", 4),),
        "product_generation_refs": (_generation(),),
        "product_staleness": 60,
        "quality_blocked": False,
        "authority_conflict": False,
        "requester_principal_ref": "principal-1",
        "purpose": "operations",
        "acting_as_agent": False,
        "latest_policy_revision": 2,
    }
    values.update(changes)
    return AnswerValidationContext.model_validate(values)


def _validate(
    candidate: AnswerIntentCandidate | None = None,
    *,
    policy: AnswerScopePolicy | None = None,
    context: AnswerValidationContext | None = None,
) -> AnswerIntentValidation:
    resolved_candidate = candidate or _candidate()
    intent = AnswerQuestionIntent(
        intent_id="intent-1",
        tenant_id="tenant-1",
        request_id="request-1",
        request_revision=3,
        question_digest="d" * 64,
        interpreter="model",
        interpreter_ref="scripted-v1",
        created_at=NOW,
        **resolved_candidate.model_dump(),
    )
    return validate_answer_intent(
        validation_id="validation-1",
        intent=intent,
        policy=policy or _policy(),
        context=context or _context(),
        created_at=NOW,
    )


def test_validation_context_accepts_an_explicit_product_generation_reference() -> None:
    context = AnswerValidationContext.model_validate(
        {
            **_context().model_dump(),
            "product_generation_refs": (_generation(),),
        }
    )

    assert context.product_generation_refs == (_generation(),)


def test_valid_intent_is_admitted_with_exact_bindings_and_deterministic_restatement() -> None:
    result = _validate()

    assert result.schema_version == "2"
    assert result.outcome == "admitted"
    assert result.reason_codes == ()
    assert result.product_generation_refs == (_generation(),)
    assert result.intent_digest == digest(
        AnswerQuestionIntent(
            intent_id="intent-1",
            tenant_id="tenant-1",
            request_id="request-1",
            request_revision=3,
            question_digest="d" * 64,
            interpreter="model",
            interpreter_ref="scripted-v1",
            created_at=NOW,
            **_candidate().model_dump(),
        )
    )
    assert result.bound_metric_versions == (_reference("net-revenue", 2),)
    assert result.bound_dimensions == (_reference("region", 1),)
    assert result.bound_filters[0].values == ("east",)
    assert result.restatement == (
        "Metric net-revenue by region where region equals [east] from "
        "2026-09-04T18:00:00+00:00 to 2026-09-11T18:00:00+00:00; limit 25."
    )


@pytest.mark.parametrize(
    "product_generation_refs",
    (
        (),
        (_generation(_reference("other-product", 1)),),
        (_generation(), _generation(generation=9)),
    ),
)
def test_generation_authority_must_exactly_cover_each_selected_product_once(
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> None:
    result = _validate(context=_context(product_generation_refs=product_generation_refs))

    assert result.reason_codes == ("authority_conflict",)
    assert result.outcome == "no_valid_plan"
    assert result.product_generation_refs == ()


def test_unselected_product_generation_is_filtered_without_rejecting_the_question() -> None:
    result = _validate(
        context=_context(
            product_generation_refs=(
                _generation(_reference("other-product", 1)),
                _generation(),
            )
        )
    )

    assert result.reason_codes == ()
    assert result.outcome == "admitted"
    assert result.product_generation_refs == (_generation(),)


@pytest.mark.parametrize(
    ("policy", "context"),
    (
        (_policy(tenant_id="tenant-2"), _context()),
        (
            _policy(
                semantic_version_ref=_reference("semantic", 3).model_copy(
                    update={"digest": "b" * 64}
                )
            ),
            _context(),
        ),
        (_policy(), _context(latest_policy_revision=1)),
        (_policy(valid_until=NOW), _context()),
    ),
)
def test_each_authority_mismatch_independently_has_highest_precedence(
    policy: AnswerScopePolicy,
    context: AnswerValidationContext,
) -> None:
    result = _validate(policy=policy, context=context)

    assert result.reason_codes == ("authority_conflict",)
    assert result.outcome == "no_valid_plan"


def test_ordering_can_bind_an_approved_dimension() -> None:
    result = _validate(
        _candidate(ordering=(OrderCandidate(reference="region", direction="ascending"),))
    )

    assert result.outcome == "admitted"


@pytest.mark.parametrize(
    ("candidate", "policy", "context", "reason", "outcome"),
    (
        (
            _candidate(metric_refs=("sales",)),
            _policy(),
            _context(
                bindings=(
                    *_context().bindings,
                    BoundSemanticReference(
                        canonical_ref="gross-sales",
                        kind="metric",
                        aliases=("sales",),
                        version_ref=_reference("gross-sales"),
                        product_version_ref=_reference("orders-product", 4),
                    ),
                    BoundSemanticReference(
                        canonical_ref="net-sales",
                        kind="metric",
                        aliases=("sales",),
                        version_ref=_reference("net-sales"),
                        product_version_ref=_reference("orders-product", 4),
                    ),
                )
            ),
            "ambiguous_reference",
            "clarification_required",
        ),
        (
            _candidate(metric_refs=("invented",)),
            _policy(),
            _context(),
            "unknown_candidate_reference",
            "clarification_required",
        ),
        (
            _candidate(
                filters=(
                    FilterCandidate(dimension_ref="region", operator="equals", values=("north",)),
                )
            ),
            _policy(),
            _context(),
            "filter_value_outside_domain",
            "clarification_required",
        ),
        (_candidate(), _policy(), _context(entitled_refs=("region",)), "not_entitled", "denied"),
        (
            _candidate(),
            _policy(),
            _context(answer_enabled_product_refs=()),
            "product_not_answer_enabled",
            "dependency_required",
        ),
        (
            _candidate(),
            _policy(metric_version_refs=()),
            _context(),
            "outside_policy_scope",
            "review_required",
        ),
        (
            _candidate(time_window=TimeWindowCandidate(start=NOW - timedelta(days=32), end=NOW)),
            _policy(),
            _context(),
            "time_window_exceeded",
            "review_required",
        ),
        (
            _candidate(),
            _policy(),
            _context(product_staleness=3601),
            "stale_product",
            "review_required",
        ),
        (
            _candidate(),
            _policy(),
            _context(quality_blocked=True),
            "quality_blocked",
            "review_required",
        ),
        (
            _candidate(),
            _policy(),
            _context(authority_conflict=True),
            "authority_conflict",
            "no_valid_plan",
        ),
    ),
)
def test_validation_classifies_each_ordered_policy_failure(
    candidate: AnswerIntentCandidate,
    policy: AnswerScopePolicy,
    context: AnswerValidationContext,
    reason: str,
    outcome: str,
) -> None:
    result = _validate(candidate, policy=policy, context=context)

    assert result.reason_codes == (reason,)
    assert result.outcome == outcome


def test_binding_failure_discards_all_partially_bound_candidate_values() -> None:
    candidate = _candidate(metric_refs=("revenue", "unknown"))

    result = _validate(candidate)

    assert result.reason_codes == ("unknown_candidate_reference",)
    assert result.bound_metric_versions == ()
    assert result.bound_dimensions == ()
    assert result.bound_filters == ()
    assert result.product_generation_refs == ()


@pytest.mark.parametrize(
    "candidate",
    (
        _candidate(metric_refs=("sales",)),
        _candidate(
            filters=(FilterCandidate(dimension_ref="region", operator="equals", values=("north",)),)
        ),
    ),
)
def test_every_binding_failure_discards_resolved_values(candidate: AnswerIntentCandidate) -> None:
    context = _context()
    if candidate.metric_refs == ("sales",):
        context = _context(
            bindings=(
                *_context().bindings,
                BoundSemanticReference(
                    canonical_ref="gross-sales",
                    kind="metric",
                    aliases=("sales",),
                    version_ref=_reference("gross-sales"),
                    product_version_ref=_reference("orders-product", 4),
                ),
                BoundSemanticReference(
                    canonical_ref="net-sales",
                    kind="metric",
                    aliases=("sales",),
                    version_ref=_reference("net-sales"),
                    product_version_ref=_reference("orders-product", 4),
                ),
            ),
        )

    result = _validate(candidate, context=context)

    assert result.bound_metric_versions == ()
    assert result.bound_dimensions == ()
    assert result.bound_filters == ()
    assert result.restatement == "Candidate requires clarification."


def test_reason_codes_record_all_failures_while_first_check_controls_outcome() -> None:
    candidate = _candidate(
        metric_refs=("revenue", "unknown"),
        filters=(FilterCandidate(dimension_ref="region", operator="equals", values=("north",)),),
        time_window=TimeWindowCandidate(start=NOW - timedelta(days=32), end=NOW),
    )

    result = _validate(
        candidate,
        policy=_policy(metric_version_refs=()),
        context=_context(
            authority_conflict=True,
            entitled_refs=(),
            answer_enabled_product_refs=(),
            product_staleness=3601,
            quality_blocked=True,
        ),
    )

    assert result.outcome == "no_valid_plan"
    assert result.reason_codes == (
        "authority_conflict",
        "unknown_candidate_reference",
        "filter_value_outside_domain",
        "not_entitled",
        "product_not_answer_enabled",
        "outside_policy_scope",
        "time_window_exceeded",
        "stale_product",
        "quality_blocked",
    )


def test_agent_and_model_limits_are_part_of_deterministic_policy_scope() -> None:
    result = _validate(
        policy=_policy(agent_access="denied", model_disclosure="none"),
        context=_context(acting_as_agent=True),
    )

    assert result.reason_codes == ("not_entitled", "outside_policy_scope")
    assert result.outcome == "denied"


@pytest.mark.parametrize(
    "context",
    (
        _context(requester_principal_ref="principal-2"),
        _context(purpose="finance"),
    ),
)
def test_principal_and_purpose_scope_are_independently_enforced(
    context: AnswerValidationContext,
) -> None:
    assert _validate(context=context).reason_codes == ("not_entitled",)


def test_agent_denial_does_not_deny_a_non_agent_principal() -> None:
    result = _validate(policy=_policy(agent_access="denied"))

    assert result.outcome == "admitted"


@pytest.mark.parametrize(
    ("candidate", "policy"),
    (
        (_candidate(), _policy(data_product_version_refs=(_reference("other-product"),))),
        (_candidate(), _policy(dimension_refs=())),
        (_candidate(dimension_refs=()), _policy(dimension_refs=())),
    ),
)
def test_each_bound_reference_must_be_inside_policy_scope(
    candidate: AnswerIntentCandidate,
    policy: AnswerScopePolicy,
) -> None:
    result = _validate(candidate, policy=policy)

    assert result.reason_codes == ("outside_policy_scope",)


def test_policy_limits_are_inclusive_at_the_exact_boundary() -> None:
    result = _validate(
        _candidate(
            time_window=TimeWindowCandidate(start=NOW - timedelta(days=31), end=NOW),
            row_limit=100,
        ),
        context=_context(product_staleness=3600),
    )

    assert result.outcome == "admitted"


def test_policy_exposes_exact_restatement_confirmation_rule() -> None:
    policy = _policy(restatement_confirmation="model_interpreted")

    assert policy.requires_restatement_confirmation(interpreter="model") is True
    assert policy.requires_restatement_confirmation(interpreter="form") is False
    assert _policy(restatement_confirmation="always").requires_restatement_confirmation(
        interpreter="form"
    )
    assert not _policy(restatement_confirmation="never").requires_restatement_confirmation(
        interpreter="model"
    )
