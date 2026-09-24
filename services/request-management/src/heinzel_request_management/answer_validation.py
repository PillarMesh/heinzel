from __future__ import annotations

from datetime import datetime
from typing import Literal

from heinzel_contract_model import ArtifactModel, ArtifactReference, digest
from pydantic import Field

from .answer_models import (
    AnswerIntentValidation,
    AnswerProductGenerationReference,
    AnswerQuestionIntent,
    AnswerValidationOutcome,
    AnswerValidationReason,
    BoundFilter,
    FilterCandidate,
)
from .answer_policy import AnswerScopePolicy


class BoundSemanticReference(ArtifactModel):
    canonical_ref: str = Field(min_length=1)
    kind: Literal["metric", "dimension"]
    aliases: tuple[str, ...]
    version_ref: ArtifactReference
    product_version_ref: ArtifactReference


class AnswerValidationContext(ArtifactModel):
    semantic_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entitlement_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: tuple[BoundSemanticReference, ...]
    entitled_refs: tuple[str, ...]
    answer_enabled_product_refs: tuple[ArtifactReference, ...]
    product_generation_refs: tuple[AnswerProductGenerationReference, ...]
    product_staleness: int = Field(ge=0)
    quality_blocked: bool
    authority_conflict: bool
    requester_principal_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    acting_as_agent: bool
    latest_policy_revision: int = Field(gt=0)


def _matches(
    reference: str,
    kind: Literal["metric", "dimension"],
    bindings: tuple[BoundSemanticReference, ...],
) -> tuple[BoundSemanticReference, ...]:
    exact = tuple(
        binding
        for binding in bindings
        if binding.kind == kind and binding.canonical_ref == reference
    )
    if exact:
        return exact
    return tuple(
        binding for binding in bindings if binding.kind == kind and reference in binding.aliases
    )


def _append_once(reasons: list[AnswerValidationReason], reason: AnswerValidationReason) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _prepend_once(reasons: list[AnswerValidationReason], reason: AnswerValidationReason) -> None:
    if reason not in reasons:
        reasons.insert(0, reason)


def validate_answer_intent(
    *,
    validation_id: str,
    intent: AnswerQuestionIntent,
    policy: AnswerScopePolicy,
    context: AnswerValidationContext,
    created_at: datetime,
) -> AnswerIntentValidation:
    reasons: list[AnswerValidationReason] = []
    if (
        context.authority_conflict
        or policy.tenant_id != intent.tenant_id
        or policy.semantic_version_ref.digest != context.semantic_version_digest
        or policy.revision != context.latest_policy_revision
        or not policy.valid_from <= created_at < policy.valid_until
    ):
        _append_once(reasons, "authority_conflict")

    metric_bindings: list[BoundSemanticReference] = []
    dimension_bindings: list[BoundSemanticReference] = []
    filter_bindings: list[tuple[BoundSemanticReference, FilterCandidate]] = []
    all_candidate_refs: tuple[tuple[str, Literal["metric", "dimension"] | None], ...] = (
        *((reference, "metric") for reference in intent.metric_refs),
        *((reference, "dimension") for reference in intent.dimension_refs),
        *((item.dimension_ref, "dimension") for item in intent.filters),
        *((item.reference, None) for item in intent.ordering),
    )
    resolved_for_entitlement: list[BoundSemanticReference] = []
    has_unknown_reference = False
    has_ambiguous_reference = False
    for reference, expected_kind in all_candidate_refs:
        if expected_kind is None:
            matches = _matches(reference, "metric", context.bindings) + _matches(
                reference, "dimension", context.bindings
            )
        else:
            matches = _matches(reference, expected_kind, context.bindings)
        if not matches:
            has_unknown_reference = True
        elif len(matches) > 1:
            has_ambiguous_reference = True
    if has_ambiguous_reference:
        _append_once(reasons, "ambiguous_reference")
    if has_unknown_reference:
        _append_once(reasons, "unknown_candidate_reference")

    for reference in intent.metric_refs:
        matches = _matches(reference, "metric", context.bindings)
        if len(matches) == 1:
            metric_bindings.append(matches[0])
            resolved_for_entitlement.append(matches[0])
    for reference in intent.dimension_refs:
        matches = _matches(reference, "dimension", context.bindings)
        if len(matches) == 1:
            dimension_bindings.append(matches[0])
            resolved_for_entitlement.append(matches[0])
    filter_domains = {
        domain.dimension_ref: frozenset(domain.values) for domain in policy.filter_domains
    }
    for item in intent.filters:
        matches = _matches(item.dimension_ref, "dimension", context.bindings)
        if len(matches) == 1:
            binding = matches[0]
            filter_bindings.append((binding, item))
            resolved_for_entitlement.append(binding)
            domain = filter_domains.get(binding.canonical_ref)
            if domain is None or any(value not in domain for value in item.values):
                _append_once(reasons, "filter_value_outside_domain")

    binding_failed = any(
        reason in reasons
        for reason in (
            "ambiguous_reference",
            "unknown_candidate_reference",
            "filter_value_outside_domain",
        )
    )
    selected_product_refs: list[ArtifactReference] = []
    for binding in resolved_for_entitlement:
        if binding.product_version_ref not in selected_product_refs:
            selected_product_refs.append(binding.product_version_ref)
    selected_generations: list[AnswerProductGenerationReference] = []
    if not binding_failed:
        for product_ref in selected_product_refs:
            generation_matches = tuple(
                reference
                for reference in context.product_generation_refs
                if reference.product_ref == product_ref
            )
            if len(generation_matches) == 1:
                selected_generations.append(generation_matches[0])
            else:
                _prepend_once(reasons, "authority_conflict")

    entitled = frozenset(context.entitled_refs)
    if (
        any(binding.canonical_ref not in entitled for binding in resolved_for_entitlement)
        or context.requester_principal_ref not in policy.principal_scope
        or context.purpose not in policy.purposes
        or (context.acting_as_agent and policy.agent_access == "denied")
    ):
        _append_once(reasons, "not_entitled")

    enabled_products = frozenset(context.answer_enabled_product_refs)
    if any(
        binding.product_version_ref not in enabled_products for binding in resolved_for_entitlement
    ):
        _append_once(reasons, "product_not_answer_enabled")

    allowed_metrics = frozenset(policy.metric_version_refs)
    allowed_dimensions = frozenset(policy.dimension_refs)
    allowed_products = frozenset(policy.data_product_version_refs)
    if (
        any(binding.version_ref not in allowed_metrics for binding in metric_bindings)
        or any(binding.version_ref not in allowed_dimensions for binding in dimension_bindings)
        or any(binding.version_ref not in allowed_dimensions for binding, _ in filter_bindings)
        or any(
            binding.product_version_ref not in allowed_products
            for binding in resolved_for_entitlement
        )
        or (intent.row_limit > policy.row_ceiling)
        or (policy.model_disclosure == "none" and intent.interpreter == "model")
    ):
        _append_once(reasons, "outside_policy_scope")
    if intent.time_window is not None:
        window_seconds = int((intent.time_window.end - intent.time_window.start).total_seconds())
        if window_seconds > policy.max_time_window:
            _append_once(reasons, "time_window_exceeded")
    if context.product_staleness > policy.max_staleness:
        _append_once(reasons, "stale_product")
    if context.quality_blocked and policy.quality_disposition == "block":
        _append_once(reasons, "quality_blocked")

    outcomes: tuple[tuple[frozenset[AnswerValidationReason], AnswerValidationOutcome], ...] = (
        (frozenset(("authority_conflict",)), "no_valid_plan"),
        (
            frozenset(
                (
                    "ambiguous_reference",
                    "unknown_candidate_reference",
                    "filter_value_outside_domain",
                )
            ),
            "clarification_required",
        ),
        (frozenset(("not_entitled",)), "denied"),
        (frozenset(("product_not_answer_enabled",)), "dependency_required"),
        (
            frozenset(
                (
                    "outside_policy_scope",
                    "time_window_exceeded",
                    "stale_product",
                    "quality_blocked",
                )
            ),
            "review_required",
        ),
    )
    outcome: AnswerValidationOutcome = "admitted"
    for reason_group, group_outcome in outcomes:
        if reason_group.intersection(reasons):
            outcome = group_outcome
            break

    bound_metrics = () if binding_failed else tuple(item.version_ref for item in metric_bindings)
    bound_dimensions = (
        () if binding_failed else tuple(item.version_ref for item in dimension_bindings)
    )
    bound_filters = (
        ()
        if binding_failed
        else tuple(
            BoundFilter(
                dimension_ref=binding.version_ref,
                operator=item.operator,
                values=item.values,
            )
            for binding, item in filter_bindings
        )
    )
    restatement = (
        "Candidate requires clarification."
        if binding_failed
        else _render_restatement(intent, metric_bindings, dimension_bindings, filter_bindings)
    )
    product_generation_refs = (
        () if binding_failed or "authority_conflict" in reasons else tuple(selected_generations)
    )
    return AnswerIntentValidation(
        validation_id=validation_id,
        tenant_id=intent.tenant_id,
        request_id=intent.request_id,
        request_revision=intent.request_revision,
        intent_digest=digest(intent),
        semantic_version_digest=context.semantic_version_digest,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        policy_digest=policy.canonical_digest(),
        entitlement_snapshot_digest=context.entitlement_snapshot_digest,
        bound_metric_versions=bound_metrics,
        bound_dimensions=bound_dimensions,
        bound_filters=bound_filters,
        restatement=restatement,
        product_generation_refs=product_generation_refs,
        outcome=outcome,
        reason_codes=tuple(reasons),
        created_at=created_at,
    )


def _render_restatement(
    intent: AnswerQuestionIntent,
    metric_bindings: list[BoundSemanticReference],
    dimension_bindings: list[BoundSemanticReference],
    filter_bindings: list[tuple[BoundSemanticReference, FilterCandidate]],
) -> str:
    kind = "Definition of" if intent.intent_kind == "definition" else "Metric"
    metrics = ", ".join(binding.canonical_ref for binding in metric_bindings) or "unresolved"
    dimensions = ", ".join(binding.canonical_ref for binding in dimension_bindings)
    text = f"{kind} {metrics}"
    if dimensions:
        text += f" by {dimensions}"
    for binding, item in filter_bindings:
        values = ", ".join(item.values)
        text += f" where {binding.canonical_ref} {item.operator} [{values}]"
    if intent.time_window is not None:
        text += (
            f" from {intent.time_window.start.isoformat()} to {intent.time_window.end.isoformat()}"
        )
    return f"{text}; limit {intent.row_limit}."
