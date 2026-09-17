from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pillarmesh_contract_model import digest
from pillarmesh_execution_graph import (
    InvalidProductInputCardinalityEvidence,
    ProductInputCardinalityEvidence,
    ProductInputCardinalityEvidenceVerifier,
    ProductPhysicalPlan,
    SignedProductInputCardinalityEvidence,
)
from pillarmesh_iir import ProductIntentIR
from pillarmesh_provider_sdk import (
    InvalidProductSqlProviderObservation,
    ProductSqlProviderObservation,
    ProductSqlProviderObservationVerifier,
    SignedProductSqlProviderObservation,
)
from pydantic import ValidationError

from .models import NoValidPlan, PreconditionResult
from .numeric_bounds import (
    MAX_PRODUCT_INPUT_ROWS,
    prove_decimal_sum_bound,
)
from .product_physical_plan import (
    ProductPhysicalPlanAuthority,
    compose_product_physical_plan_candidate,
)
from .product_semantics import ProductSemanticError, validate_product_semantics
from .restricted_sql import evaluate_project_sum_shape

PRODUCT_SQL_RULE_ID = "PRODUCT-SQL-V2-UNAPPROVED"
PRODUCT_SQL_CANDIDATE_RULE_ID = "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"

_ENGINE_VERSIONS = {
    "postgresql": "18.6",
    "clickhouse": "25.8.32.4",
}
_ENGINE_IMAGE_DIGESTS = {
    "postgresql": "33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935",
    "clickhouse": "7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0",
}
_MAX_OBSERVATION_AGE = timedelta(minutes=10)
# Precondition 15 is worded per engine for the same reason as 17: PostgreSQL's runtime magnitude
# enforcement is not evidence that any other engine enforces it. ClickHouse wraps on overflow.
_RUNTIME_MAGNITUDE_REASONS = {
    "postgresql": (
        "enforce checked Decimal(57,9) result magnitude at runtime on the pinned PostgreSQL engine"
    ),
    "clickhouse": (
        "bind ClickHouse runtime Decimal(57,9) magnitude enforcement to its own activation"
    ),
}
# Precondition 17 is activated per engine (ADR-0004 amendment 2026-09-15). PostgreSQL
# activation rests on PostgreSQL evidence alone and withholds any cross-engine claim, and it
# never admits ClickHouse, which needs its own evidence and its own review.
_LIVE_SUM_REVIEW_REASONS = {
    "postgresql": (
        "review live checked SUM on the pinned PostgreSQL engine; "
        "cross-engine equivalence is not claimed"
    ),
    "clickhouse": (
        "activate this rule for ClickHouse with its own live checked SUM evidence and review"
    ),
}


def compile_product_iir(
    product_iir: ProductIntentIR,
    *,
    engine: Literal["postgresql", "clickhouse"],
    engine_version: str | None = None,
    capabilities: tuple[str, ...] = (),
    provider_observation: ProductSqlProviderObservation | None = None,
    signed_provider_observation: SignedProductSqlProviderObservation | None = None,
    provider_observation_verifier: ProductSqlProviderObservationVerifier | None = None,
    expected_provider_observation_digest: str | None = None,
    expected_tenant_id: str | None = None,
    expected_warehouse_binding_id: str | None = None,
    expected_warehouse_binding_revision: int | None = None,
    expected_relation_ref: str | None = None,
    expected_relation_namespace: str | None = None,
    expected_engine_image_digest: str | None = None,
    expected_engine_build_digest: str | None = None,
    evaluated_at: datetime | None = None,
    physical_plan_authority: ProductPhysicalPlanAuthority | None = None,
    cardinality_evidence: ProductInputCardinalityEvidence | None = None,
    signed_cardinality_evidence: SignedProductInputCardinalityEvidence | None = None,
    cardinality_evidence_verifier: ProductInputCardinalityEvidenceVerifier | None = None,
) -> NoValidPlan:
    uses_legacy_unapproved_contract = (
        provider_observation is None
        and signed_provider_observation is None
        and engine_version is None
        and not capabilities
    )
    try:
        validate_product_semantics(product_iir)
    except ProductSemanticError as error:
        return NoValidPlan(
            rule_id=(
                PRODUCT_SQL_RULE_ID
                if uses_legacy_unapproved_contract
                else PRODUCT_SQL_CANDIDATE_RULE_ID
            ),
            preconditions=(
                PreconditionResult(number=1, status="unsatisfied", reason=error.reason),
            ),
            smallest_changes=("declare the referenced relation and column with its scalar type",),
        )
    if uses_legacy_unapproved_contract:
        return NoValidPlan(
            rule_id=PRODUCT_SQL_RULE_ID,
            preconditions=(
                PreconditionResult(
                    number=1,
                    status="unsatisfied",
                    reason="restricted product SQL has no approved legality rule",
                ),
            ),
            smallest_changes=(
                "approve a legality rule with proof, fixtures, mutation tests, "
                "and independent review",
            ),
        )

    raw_observation_was_supplied = provider_observation is not None
    signed_observation_was_verified = False
    if signed_provider_observation is not None:
        provider_observation, signed_observation_was_verified = _verify_signed_observation(
            signed_provider_observation,
            verifier=provider_observation_verifier,
            evaluated_at=evaluated_at,
        )
        if raw_observation_was_supplied:
            signed_observation_was_verified = False
    provider_observation, observation_validation_reason = _revalidate_observation(
        provider_observation
    )
    signed_observation_was_verified = (
        signed_observation_was_verified and observation_validation_reason is None
    )
    physical_candidate = _compose_physical_candidate(
        product_iir,
        engine=engine,
        authority=physical_plan_authority,
        provider_observation=provider_observation,
        expected_provider_observation_digest=expected_provider_observation_digest,
        expected_tenant_id=expected_tenant_id,
        expected_warehouse_binding_id=expected_warehouse_binding_id,
        expected_warehouse_binding_revision=expected_warehouse_binding_revision,
        expected_relation_ref=expected_relation_ref,
        expected_relation_namespace=expected_relation_namespace,
    )
    verified_cardinality_evidence = (
        _verify_signed_cardinality_evidence(
            signed_cardinality_evidence,
            verifier=cardinality_evidence_verifier,
            evaluated_at=evaluated_at,
        )
        if physical_candidate is not None
        else None
    )
    if cardinality_evidence is not None:
        verified_cardinality_evidence = None
    shape_results = tuple(
        _precondition(number, check.satisfied, check.reason)
        for number, check in enumerate(evaluate_project_sum_shape(product_iir), start=1)
    )
    results = (
        *shape_results,
        _precondition(
            7,
            observation_validation_reason is None
            and _observation_is_bound(
                provider_observation,
                expected_digest=expected_provider_observation_digest,
                expected_tenant_id=expected_tenant_id,
                expected_binding_id=expected_warehouse_binding_id,
                expected_binding_revision=expected_warehouse_binding_revision,
                expected_relation_ref=expected_relation_ref,
                expected_relation_namespace=expected_relation_namespace,
            ),
            observation_validation_reason
            or (
                "bind the provider observation to the expected tenant, warehouse, relation, "
                "and digest"
            ),
        ),
        _precondition(
            8,
            _engine_identity_is_bound(
                provider_observation,
                engine=engine,
                expected_image_digest=expected_engine_image_digest,
                expected_build_digest=expected_engine_build_digest,
            ),
            "bind the pinned engine version, image, and build to the provider observation",
        ),
        _precondition(
            9,
            _observation_is_fresh(provider_observation, evaluated_at=evaluated_at),
            "use a provider observation no older than ten minutes at evaluation time",
        ),
        _precondition(
            10,
            _landing_relation_matches(
                product_iir, physical_plan_authority, provider_observation, engine=engine
            ),
            "observe the non-null text generation and jsonb payload columns of the landing "
            "relation the statement reads",
        ),
        _precondition(
            11,
            _sum_semantics_match(provider_observation, engine=engine),
            "observe the pinned engine-specific SUM input, accumulator, result, overflow, null, "
            "and empty-group semantics",
        ),
        _precondition(
            12,
            _decimal_sum_bound_is_proven(),
            "prove NUMERIC(38,9) sums fit exact Decimal(57,9) arithmetic at the signed ledger "
            "ceiling",
        ),
        _precondition(
            13,
            physical_candidate is not None,
            "bind the physical source to exactly one authoritative generation and receipt digest",
        ),
        _precondition(
            14,
            _cardinality_evidence_is_bound(
                verified_cardinality_evidence,
                physical_plan=physical_candidate,
            ),
            "bind the contributing row ceiling to owning-service cardinality evidence",
        ),
        PreconditionResult(
            number=15,
            status="unsatisfied",
            reason=_RUNTIME_MAGNITUDE_REASONS[engine],
        ),
        PreconditionResult(
            number=16,
            status=(
                "satisfied"
                if signed_observation_was_verified
                and _observation_is_bound(
                    provider_observation,
                    expected_digest=expected_provider_observation_digest,
                    expected_tenant_id=expected_tenant_id,
                    expected_binding_id=expected_warehouse_binding_id,
                    expected_binding_revision=expected_warehouse_binding_revision,
                    expected_relation_ref=expected_relation_ref,
                    expected_relation_namespace=expected_relation_namespace,
                )
                and _engine_identity_is_bound(
                    provider_observation,
                    engine=engine,
                    expected_image_digest=expected_engine_image_digest,
                    expected_build_digest=expected_engine_build_digest,
                )
                and _observation_is_fresh(provider_observation, evaluated_at=evaluated_at)
                else "unsatisfied"
            ),
            reason="authenticate the observation with provider-owned provenance authority",
        ),
        PreconditionResult(
            number=17,
            status="unsatisfied",
            reason=_LIVE_SUM_REVIEW_REASONS[engine],
        ),
        PreconditionResult(
            number=18,
            status="unsatisfied",
            reason="independent legality review has not approved this rule",
        ),
    )
    smallest_changes = tuple(result.reason for result in results if result.status != "satisfied")
    return NoValidPlan(
        rule_id=PRODUCT_SQL_CANDIDATE_RULE_ID,
        preconditions=results,
        smallest_changes=smallest_changes,
    )


def _compose_physical_candidate(
    product_iir: ProductIntentIR,
    *,
    engine: Literal["postgresql", "clickhouse"],
    authority: ProductPhysicalPlanAuthority | None,
    provider_observation: ProductSqlProviderObservation | None,
    expected_provider_observation_digest: str | None,
    expected_tenant_id: str | None,
    expected_warehouse_binding_id: str | None,
    expected_warehouse_binding_revision: int | None,
    expected_relation_ref: str | None,
    expected_relation_namespace: str | None,
) -> ProductPhysicalPlan | None:
    try:
        if (
            authority is None
            or provider_observation is None
            or expected_provider_observation_digest is None
            or not _observation_is_bound(
                provider_observation,
                expected_digest=expected_provider_observation_digest,
                expected_tenant_id=expected_tenant_id,
                expected_binding_id=expected_warehouse_binding_id,
                expected_binding_revision=expected_warehouse_binding_revision,
                expected_relation_ref=expected_relation_ref,
                expected_relation_namespace=expected_relation_namespace,
            )
            or provider_observation.engine != engine
            or provider_observation.relation_namespace != authority.source.namespace
            or provider_observation.relation_name != authority.source.relation_name
            or authority.tenant_id != expected_tenant_id
            or authority.warehouse_binding_id != expected_warehouse_binding_id
            or authority.warehouse_binding_revision != expected_warehouse_binding_revision
        ):
            return None
        return compose_product_physical_plan_candidate(
            product_iir,
            authority=authority,
            engine=engine,
            provider_observation_digest=expected_provider_observation_digest,
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        return None


def _verify_signed_cardinality_evidence(
    signed_evidence: SignedProductInputCardinalityEvidence | None,
    *,
    verifier: ProductInputCardinalityEvidenceVerifier | None,
    evaluated_at: datetime | None,
) -> ProductInputCardinalityEvidence | None:
    if (
        signed_evidence is None
        or not isinstance(verifier, ProductInputCardinalityEvidenceVerifier)
        or evaluated_at is None
    ):
        return None
    try:
        return verifier.verify(signed_evidence, evaluated_at=evaluated_at)
    except (InvalidProductInputCardinalityEvidence, AttributeError, TypeError, ValueError):
        return None


def _cardinality_evidence_is_bound(
    evidence: ProductInputCardinalityEvidence | None,
    *,
    physical_plan: ProductPhysicalPlan | None,
) -> bool:
    if evidence is None or physical_plan is None:
        return False
    try:
        proof = prove_decimal_sum_bound(evidence.total_contributing_row_ceiling)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        evidence.tenant_id == physical_plan.tenant_id
        and evidence.contract_ref == physical_plan.contract_ref
        and evidence.contract_revision == physical_plan.contract_revision
        and evidence.contract_digest == physical_plan.contract_digest
        and evidence.product_plan_digest == digest(physical_plan)
        and evidence.relation_ref == physical_plan.source.relation_name
        and evidence.generation_ids == (physical_plan.source.generation_id,)
        and len(evidence.receipts) == 1
        and evidence.receipts[0].generation_id == physical_plan.source.generation_id
        and evidence.receipts[0].receipt_digest == physical_plan.source.landing_receipt_digest
        and evidence.receipts[0].record_count == evidence.total_contributing_row_ceiling
        and evidence.decimal_input_precision == proof.input_precision
        and evidence.decimal_input_scale == proof.input_scale
        and evidence.maximum_scaled_sum == proof.maximum_scaled_sum
    )


def _decimal_sum_bound_is_proven() -> bool:
    prove_decimal_sum_bound(MAX_PRODUCT_INPUT_ROWS)
    return True


def _verify_signed_observation(
    signed_observation: SignedProductSqlProviderObservation,
    *,
    verifier: ProductSqlProviderObservationVerifier | None,
    evaluated_at: datetime | None,
) -> tuple[ProductSqlProviderObservation | None, bool]:
    if not isinstance(verifier, ProductSqlProviderObservationVerifier) or evaluated_at is None:
        return None, False
    try:
        observation = verifier.verify(signed_observation, evaluated_at=evaluated_at)
    except (InvalidProductSqlProviderObservation, AttributeError, TypeError, ValueError):
        return None, False
    return observation, True


def _precondition(number: int, satisfied: bool, reason: str) -> PreconditionResult:
    return PreconditionResult(
        number=number,
        status="satisfied" if satisfied else "unsatisfied",
        reason=reason,
    )


def _revalidate_observation(
    observation: ProductSqlProviderObservation | None,
) -> tuple[ProductSqlProviderObservation | None, str | None]:
    if observation is None:
        return None, None
    try:
        payload = observation.model_dump(mode="python")
    except (TypeError, ValueError):
        return None, "provider observation could not be serialized for strict validation"
    try:
        validated = ProductSqlProviderObservation.model_validate(payload, strict=True)
    except ValidationError as error:
        detail = error.errors(include_input=False)[0]
        location = ".".join(str(part) for part in detail["loc"])
        return None, f"provider observation is invalid at {location}: {detail['msg']}"
    return validated, None


def _observation_is_bound(
    observation: ProductSqlProviderObservation | None,
    *,
    expected_digest: str | None,
    expected_tenant_id: str | None,
    expected_binding_id: str | None,
    expected_binding_revision: int | None,
    expected_relation_ref: str | None,
    expected_relation_namespace: str | None,
) -> bool:
    return (
        observation is not None
        and expected_digest is not None
        and digest(observation) == expected_digest
        and observation.tenant_id == expected_tenant_id
        and observation.warehouse_binding_id == expected_binding_id
        and observation.warehouse_binding_revision == expected_binding_revision
        and observation.relation_ref == expected_relation_ref
        and observation.relation_namespace == expected_relation_namespace
    )


def _engine_identity_is_bound(
    observation: ProductSqlProviderObservation | None,
    *,
    engine: Literal["postgresql", "clickhouse"],
    expected_image_digest: str | None,
    expected_build_digest: str | None,
) -> bool:
    return (
        observation is not None
        and observation.engine == engine
        and observation.engine_version == _ENGINE_VERSIONS[engine]
        and observation.engine_image_digest == _ENGINE_IMAGE_DIGESTS[engine]
        and observation.engine_image_digest == expected_image_digest
        and observation.engine_build_digest == expected_build_digest
    )


def _observation_is_fresh(
    observation: ProductSqlProviderObservation | None,
    *,
    evaluated_at: datetime | None,
) -> bool:
    if observation is None or evaluated_at is None:
        return False
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() != timedelta(0):
        return False
    age = evaluated_at - observation.observed_at
    return timedelta(0) <= age <= _MAX_OBSERVATION_AGE


# The statement reads exactly two landing columns. Their physical types are fixed per engine;
# every other column of the landing relation is irrelevant to the statement and not constrained.
# Only PostgreSQL has a landing observation contract. ClickHouse has none until its own
# activation, so its precondition 10 cannot be satisfied.
_LANDING_COLUMN_TYPES: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "postgresql": (("string", "TEXT"), ("json", "JSONB")),
}
# Generation equality must be bytewise. These collations are always deterministic.
_DETERMINISTIC_GENERATION_COLLATIONS = frozenset({"C", "POSIX", "default"})


def _landing_relation_matches(
    product_iir: ProductIntentIR,
    authority: ProductPhysicalPlanAuthority | None,
    observation: ProductSqlProviderObservation | None,
    *,
    engine: Literal["postgresql", "clickhouse"],
) -> bool:
    """Bind the observation to the relation the emitted statement actually reads.

    The statement reads `authority.source` -- the generation-scoped landing relation -- and
    decodes typed fields out of its JSON payload. The declared IIR source is a logical alias with
    no physical existence, so an observation of it proves nothing about the statement. Field
    typing, non-null keys and binary collation are enforced by the statement's own decode guard;
    this precondition proves the two columns that guard reads exist with the expected types.
    """
    expected = _LANDING_COLUMN_TYPES.get(engine)
    if authority is None or observation is None or expected is None:
        return False
    source = authority.source
    if (
        product_iir.source.relation_namespace != source.namespace
        or observation.relation_namespace != source.namespace
        or observation.relation_name != source.relation_name
    ):
        return False
    columns = {column.name: column for column in observation.columns}
    generation = columns.get(source.generation_column)
    payload = columns.get(source.payload_column)
    if generation is None or payload is None or generation.name == payload.name:
        return False
    (generation_kind, generation_type), (payload_kind, payload_type) = expected
    return (
        generation.logical_type == generation_kind
        and generation.physical_type == generation_type
        and not generation.nullable
        and generation.collation in _DETERMINISTIC_GENERATION_COLLATIONS
        and generation.encoding == "UTF8"
        and payload.logical_type == payload_kind
        and payload.physical_type == payload_type
        and not payload.nullable
    )


def _sum_semantics_match(
    observation: ProductSqlProviderObservation | None,
    *,
    engine: Literal["postgresql", "clickhouse"],
) -> bool:
    if observation is None:
        return False
    semantics = observation.sum_semantics
    expected = {
        "postgresql": ("NUMERIC(38,9)", "INTERNAL", "NUMERIC", "promote"),
        "clickhouse": ("Decimal(38, 9)", "Decimal(38, 9)", "Decimal(38, 9)", "wrap"),
    }[engine]
    return (
        semantics.input_physical_type,
        semantics.accumulator_physical_type,
        semantics.result_physical_type,
        semantics.overflow_behavior,
    ) == expected and (
        semantics.null_input_behavior == "exclude" and semantics.empty_group_behavior == "no_row"
    )
