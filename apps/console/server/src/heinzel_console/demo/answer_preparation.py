"""Compile and admit the governed query for one request, so its answer can be executed.

`GovernedAnswerRuntime.execute_answer` runs an admitted plan. It does not make one: it loads the
admissions for a request and refuses when none carries a plan. Nothing in the product composed
the step that produces one -- the acceptance fixture does it inline, in the test -- so this is
that step, for the demonstration.

What it does, per request: resolve the requester's entitlement, read what the materialization
established about the product, validate the question against the approved scope policy, read
which column answers which approved term out of the durable query binding, compile the
statement and have the warehouse estimate its scan, then put the plan to policy admission.

Several values the acceptance fixture asserts by hand are derived here instead -- the entitled
references from the resolved snapshot, the staleness from the freshness observation, and the
quality disposition from the materialization receipt -- because a demonstration that asserted
them would be demonstrating its own assertions.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from heinzel_access_control import CurrentEntitlementResolver
from heinzel_compiler import (
    GovernedQueryInput,
    GovernedQueryPlan,
    ProductGenerationReference,
    QueryCeilings,
    QueryEstimator,
    QueryOrder,
    QueryReference,
    QueryScan,
    compile_governed_query,
)
from heinzel_compiler.query_signing import QueryPlanSigner
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AnswerPolicyAdmissionService,
    AnswerProductGenerationReference,
    AnswerQuestion,
    AnswerQuestionService,
    AnswerScopePolicy,
    AnswerValidationContext,
    BoundSemanticReference,
    PolicyAdmissionReceipt,
    StakeholderQuestion,
)

from ..answers import (
    DurableProductQueryBindingReader,
    MaterializationReceiptReader,
    SourceFreshnessReader,
)
from .answers import DemoAnswerInterpreter
from .publication import DEMO_TENANT_ID

__all__ = [
    "DemoAnswerPreparation",
    "DemoGovernedAnswerUnavailable",
    "GovernedQueryPlanRepository",
    "entitled_references",
    "product_staleness_seconds",
]


class GovernedQueryPlanRepository(Protocol):
    """Where a compiled plan is kept until the admission decides about it.

    A protocol rather than the SQLite repository itself, because this needs only that the plan it
    compiled is the plan admission is given back.
    """

    def save(self, plan: GovernedQueryPlan) -> GovernedQueryPlan: ...


# The row component of the scan ceiling, which `AnswerScopePolicy` has no field for: it bounds
# scan in bytes alone (`scan_ceiling`), while `QueryCeilings.scan` carries rows and bytes and the
# compiler routes a query to per-question review when the estimate exceeds either. So this number
# has no policy behind it -- it is the demonstration's, and that is a gap in the policy artifact
# rather than a choice this module can make well. It is set far above the three rows the
# demonstration's own product holds, so it is the bytes ceiling that binds.
_SCAN_ROW_CEILING = 1_000_000
_PERIOD_SCAN_ROW_CEILING = 10_000_000


class DemoGovernedAnswerUnavailable(RuntimeError):
    """The demonstration could not reach an admitted plan, and says which step refused."""


def entitled_references(
    bindings: tuple[BoundSemanticReference, ...], *, semantic_refs: tuple[ArtifactReference, ...]
) -> tuple[str, ...]:
    """The approved terms an entitlement actually covers, read from what it asserts.

    Derived rather than listed, so a narrowed entitlement narrows what the question may name. A
    caller that listed every bound term would make the entitlement's own scope decorative: the
    validation would admit a reference the authority never granted.

    The match is on the whole reference, digest included, so an entitlement naming an earlier
    revision of a term covers nothing: a term's meaning is part of its identity.
    """
    entitled = frozenset(semantic_refs)
    return tuple(binding.canonical_ref for binding in bindings if binding.version_ref in entitled)


def product_staleness_seconds(watermarks: tuple[datetime, ...], *, now: datetime) -> int:
    """How old the least current input a product was built from is, in seconds.

    The oldest rather than the newest, because a product is as stale as the least current thing
    behind it. Never negative: a watermark ahead of the clock is a source that was written in
    this instant's future, which the freshness observation already refuses, and reporting it as
    negative staleness here would be read as a very fresh product.
    """
    if not watermarks:
        raise DemoGovernedAnswerUnavailable("a product generation names no input to measure")
    return max(int((now - min(watermarks)).total_seconds()), 0)


@dataclass(frozen=True, slots=True)
class DemoAnswerPreparation:
    """Everything the governed answer needs that does not change from request to request.

    Assembled once when the console starts, because every part of it is standing authority: the
    scope policy, the product generation, the approved terms, the signing key and the warehouse
    the estimate comes from. Only the request changes, and `prepare` takes that.
    """

    entitlements: CurrentEntitlementResolver
    questions: AnswerQuestionService
    plans: GovernedQueryPlanRepository
    admissions: AnswerPolicyAdmissionService
    materializations: MaterializationReceiptReader
    freshness: SourceFreshnessReader
    query_bindings: DurableProductQueryBindingReader
    policy: AnswerScopePolicy
    bindings: tuple[BoundSemanticReference, ...]
    product_ref: ArtifactReference
    generation: int
    purpose: str
    principal_ref: str
    signer: QueryPlanSigner
    estimator: QueryEstimator
    clock: Callable[[], datetime]

    def prepare(
        self,
        *,
        request_id: str,
        request_revision: int,
        question: StakeholderQuestion,
        actor_id: str,
    ) -> PolicyAdmissionReceipt:
        """Reach an admitted plan for this request, or refuse saying which step would not.

        The whole question rather than its digest, because the interpreter resolves the governed
        terms the question selected and the validation binds the digest of the content those terms
        were selected in. A caller that passed the two separately could hand over a digest of one
        question and the selection of another, and the intent would name terms the request never
        carried.

        The order is not free. The entitlement has to be resolved before the question is
        validated, because the validation records the snapshot it was resolved against; the
        product's state has to be read before that too, because staleness and quality are part of
        what the policy admits; and the plan has to exist before admission, because admission is
        a decision about a statement.
        """
        now = self._now()
        snapshot = self.entitlements.resolve_current(
            tenant_id=DEMO_TENANT_ID,
            principal_ref=self.principal_ref,
            purpose_digest=digest(self.purpose),
        )
        if snapshot is None:
            raise DemoGovernedAnswerUnavailable(
                "no current entitlement resolved for the demonstration's requester"
            )
        receipt = self.materializations.read_receipt(
            tenant_id=DEMO_TENANT_ID,
            product_id=self.product_ref.artifact_id,
            product_revision=self.product_ref.version,
            product_generation=self.generation,
        )
        if receipt is None:
            raise DemoGovernedAnswerUnavailable(
                "the demonstration's product generation has no materialization receipt"
            )
        validated = self.questions.interpret_and_validate(
            question=AnswerQuestion(
                tenant_id=DEMO_TENANT_ID,
                request_id=request_id,
                request_revision=request_revision,
                question_digest=digest(question),
                interpreter="form",
                interpreter_ref=DemoAnswerInterpreter.interpreter_ref,
                selection=question.selection,
            ),
            policy=self.policy,
            context=AnswerValidationContext(
                semantic_version_digest=self.policy.semantic_version_ref.digest,
                entitlement_snapshot_digest=snapshot.snapshot_digest,
                bindings=self.bindings,
                entitled_refs=entitled_references(
                    self.bindings, semantic_refs=snapshot.semantic_refs
                ),
                answer_enabled_product_refs=self.policy.data_product_version_refs,
                product_generation_refs=(
                    AnswerProductGenerationReference(
                        product_ref=self.product_ref, generation=self.generation
                    ),
                ),
                product_staleness=product_staleness_seconds(
                    self._input_watermarks(receipt.input_generation_digests), now=now
                ),
                # `limited` is the disposition that blocks: `not_asserted` never reaches here
                # because the materialization refuses to commit a generation without quality.
                quality_blocked=receipt.quality_disposition == "limited",
                authority_conflict=False,
                requester_principal_ref=self.principal_ref,
                purpose=self.purpose,
                acting_as_agent=False,
                latest_policy_revision=self.policy.revision,
            ),
        )
        # The terms the validated intent names, not every term the product carries: the statement
        # has to be the statement the admitted intent describes. Reading the whole binding would
        # compile a query over terms the requester did not select and the validation did not admit.
        projection = self.query_bindings.read(
            tenant_id=DEMO_TENANT_ID,
            product_ref=self.product_ref,
            generation=self.generation,
            metric_refs=self._version_refs(validated.intent.metric_refs),
            dimension_refs=self._version_refs(validated.intent.dimension_refs),
        )
        compiled = compile_governed_query(
            GovernedQueryInput(
                tenant_id=DEMO_TENANT_ID,
                validation_digest=digest(validated.validation),
                intent_kind="metric_value",
                engine_kind=projection.engine_kind,
                consumption_object=projection.consumption_object,
                product_generation_refs=(
                    ProductGenerationReference(
                        product_ref=QueryReference.model_validate(
                            self.product_ref.model_dump(mode="python"), strict=True
                        ),
                        generation=self.generation,
                    ),
                ),
                metrics=projection.metrics,
                dimensions=projection.dimensions,
                filters=(),
                time_window=None,
                ordering=tuple(
                    QueryOrder(output_name=dimension.output_name, direction="ascending")
                    for dimension in projection.dimensions
                ),
                row_limit=validated.intent.row_limit,
                disclosure_entity_column=projection.disclosure_entity_column,
                minimum_group_size=self.policy.minimum_group_size,
                # Left for the estimator to fill. A caller-supplied estimate would make the scan
                # ceiling a check against a number the caller chose.
                estimated_scan=None,
                # The compiler's own period-budget hint only. The durable period accounting is
                # the admission service's, which reads what this policy has already consumed from
                # its own usage authority rather than being told.
                period_scan_consumed=QueryScan(rows=0, bytes=0),
                ceilings=QueryCeilings(
                    row_limit=self.policy.row_ceiling,
                    scan=QueryScan(rows=_SCAN_ROW_CEILING, bytes=self.policy.scan_ceiling),
                    period_scan=QueryScan(
                        rows=_PERIOD_SCAN_ROW_CEILING, bytes=self.policy.period_scan_budget
                    ),
                ),
            ),
            signer=self.signer,
            estimator=self.estimator,
        )
        if not isinstance(compiled, GovernedQueryPlan):
            # `NoValidPlan` carries its unsatisfied precondition, which is the whole value of the
            # refusal; a message that dropped it would leave a reader with nothing to act on.
            raise DemoGovernedAnswerUnavailable(
                f"the demonstration's governed query did not compile: {compiled}"
            )
        admission = self.admissions.admit(
            intent=validated.intent,
            validation=validated.validation,
            policy=self.policy,
            plan=self.plans.save(compiled),
            restatement_acceptance_ref=None,
            current_entitlement_snapshot_digest=snapshot.snapshot_digest,
            latest_policy_revision=self.policy.revision,
            # The actor who admitted it, not the service that recorded it: the admission is a
            # decision somebody made, and the receipt names them.
            actor_id=actor_id,
        )
        if admission.receipt is None:
            raise DemoGovernedAnswerUnavailable(
                "the demonstration's governed query was not admitted: "
                f"{admission.evaluation.reason_codes}"
            )
        return admission.receipt

    def _version_refs(self, canonical_refs: tuple[str, ...]) -> tuple[ArtifactReference, ...]:
        """The approved version each canonical reference resolves to, in the order named.

        The validation has already refused an intent naming a term the bindings do not carry, so
        an unresolved reference here is a composition mistake rather than a requester's: it is
        raised as the demonstration's own unavailability rather than compiled around.
        """
        by_canonical_ref = {binding.canonical_ref: binding for binding in self.bindings}
        resolved: list[ArtifactReference] = []
        for canonical_ref in canonical_refs:
            binding = by_canonical_ref.get(canonical_ref)
            if binding is None:
                raise DemoGovernedAnswerUnavailable(
                    f"the admitted intent names {canonical_ref!r}, which this product's approved "
                    "terms do not carry"
                )
            resolved.append(binding.version_ref)
        return tuple(resolved)

    def _input_watermarks(self, input_generation_digests: tuple[str, ...]) -> tuple[datetime, ...]:
        """The measured watermark of every input the product reads."""
        watermarks: list[datetime] = []
        for generation_digest in input_generation_digests:
            observation = self.freshness.read_for_generation(
                tenant_id=DEMO_TENANT_ID, input_generation_digest=generation_digest
            )
            if observation is None:
                raise DemoGovernedAnswerUnavailable(
                    "the demonstration's product has an input with no freshness observation"
                )
            watermarks.append(observation.watermark_at)
        return tuple(watermarks)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("the demonstration clock must return timezone-aware UTC")
        return value.astimezone(UTC)
