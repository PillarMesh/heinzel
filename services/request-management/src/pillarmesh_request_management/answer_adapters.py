from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from .answer_errors import GovernedAnswerNotVisible, GovernedAnswerVerificationError
from .answer_models import (
    AnswerExecutionEvidence,
    AnswerPlanCeilingsEvidence,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    AnswerResultEvidence,
    AnswerScanCeilingEvidence,
)

if TYPE_CHECKING:
    from pillarmesh_compiler import GovernedQueryPlan
    from pillarmesh_runtime import AnswerExecutionReceipt, AnswerResultSnapshot


def adapt_runtime_execution_receipt(
    receipt: AnswerExecutionReceipt,
    *,
    tenant_id: str,
    request_id: str,
    plan_digest: str,
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> AnswerExecutionEvidence:
    try:
        evidence = AnswerExecutionEvidence.model_validate(
            receipt.model_dump(mode="python"), strict=True
        )
    except ValidationError as error:
        raise GovernedAnswerVerificationError(
            "answer evidence does not match its strict component contract"
        ) from error
    _verify_runtime_binding(
        evidence.tenant_id,
        evidence.request_id,
        evidence.plan_digest,
        evidence.product_generation_refs,
        tenant_id=tenant_id,
        request_id=request_id,
        plan_digest=plan_digest,
        product_generation_refs=product_generation_refs,
    )
    return evidence


def adapt_runtime_result_snapshot(
    snapshot: AnswerResultSnapshot,
    *,
    tenant_id: str,
    request_id: str,
    plan_digest: str,
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> AnswerResultEvidence:
    try:
        evidence = AnswerResultEvidence.model_validate(
            snapshot.model_dump(mode="python"), strict=True
        )
    except ValidationError as error:
        raise GovernedAnswerVerificationError(
            "answer evidence does not match its strict component contract"
        ) from error
    _verify_runtime_binding(
        evidence.tenant_id,
        evidence.request_id,
        evidence.plan_digest,
        evidence.product_generation_refs,
        tenant_id=tenant_id,
        request_id=request_id,
        plan_digest=plan_digest,
        product_generation_refs=product_generation_refs,
    )
    return evidence


def adapt_compiler_query_plan(
    plan: GovernedQueryPlan,
    *,
    tenant_id: str,
    plan_digest: str,
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> AnswerPlanEvidence:
    try:
        generations = tuple(
            AnswerProductGenerationReference.model_validate(
                reference.model_dump(mode="python"), strict=True
            )
            for reference in plan.product_generation_refs
        )
        evidence = AnswerPlanEvidence(
            tenant_id=plan.tenant_id,
            validation_digest=plan.validation_digest,
            plan_digest=plan.plan_digest,
            product_generation_refs=generations,
            minimum_group_size=plan.minimum_group_size,
            statement=plan.statement,
            ceilings=AnswerPlanCeilingsEvidence(
                row_limit=plan.ceilings.row_limit,
                scan=AnswerScanCeilingEvidence(
                    rows=plan.ceilings.scan.rows, bytes=plan.ceilings.scan.bytes
                ),
                period_scan=AnswerScanCeilingEvidence(
                    rows=plan.ceilings.period_scan.rows,
                    bytes=plan.ceilings.period_scan.bytes,
                ),
            ),
        )
    except ValidationError as error:
        raise GovernedAnswerVerificationError(
            "answer evidence does not match its strict component contract"
        ) from error
    if evidence.tenant_id != tenant_id:
        raise GovernedAnswerNotVisible("answer delivery authority is not visible")
    _verify_digest_and_generations(
        evidence.plan_digest,
        evidence.product_generation_refs,
        plan_digest=plan_digest,
        product_generation_refs=product_generation_refs,
    )
    return evidence


def _verify_runtime_binding(
    actual_tenant_id: str,
    actual_request_id: str,
    actual_plan_digest: str,
    actual_generations: tuple[AnswerProductGenerationReference, ...],
    *,
    tenant_id: str,
    request_id: str,
    plan_digest: str,
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> None:
    if actual_tenant_id != tenant_id or actual_request_id != request_id:
        raise GovernedAnswerNotVisible("answer delivery authority is not visible")
    _verify_digest_and_generations(
        actual_plan_digest,
        actual_generations,
        plan_digest=plan_digest,
        product_generation_refs=product_generation_refs,
    )


def _verify_digest_and_generations(
    actual_plan_digest: str,
    actual_generations: tuple[AnswerProductGenerationReference, ...],
    *,
    plan_digest: str,
    product_generation_refs: tuple[AnswerProductGenerationReference, ...],
) -> None:
    if actual_plan_digest != plan_digest:
        raise GovernedAnswerVerificationError(
            "answer evidence plan digest does not match authority"
        )
    if actual_generations != product_generation_refs:
        raise GovernedAnswerVerificationError(
            "answer evidence product generation does not match authority"
        )
