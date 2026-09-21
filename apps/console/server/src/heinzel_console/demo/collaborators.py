"""The collaborators the demonstration console gives its fulfillment service.

This is a demonstration, not a deployment. Every class here stands in for something a real
deployment resolves from a governed authority: an identity provider and its role bindings,
a policy decision point, and an answer runtime. They are deliberately small, deterministic
and local, and none of them is a security boundary.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    FreshnessDisposition,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    InboxRequest,
    ResolutionFailure,
    StakeholderAnswerDraft,
)
from heinzel_semantic_registry import (
    CatalogPublicationRepository,
    FulfillmentAuthorityObservation,
    SemanticFulfillmentSnapshotAdapter,
)

from .publication import DEMO_TENANT_ID, DemoPublication

__all__ = [
    "DEMO_ARCHITECT_ID",
    "DEMO_ARCHITECT_PRINCIPAL_REF",
    "DEMO_REQUESTER_ID",
    "DEMO_REQUESTER_PRINCIPAL_REF",
    "DemoAnswerCandidateProvider",
    "DemoAuthorityResolver",
    "DemoFreshnessEvaluator",
    "DemoRoleResolver",
    "build_demo_policy_compiler",
    "build_demo_snapshot_resolver",
    "demo_clock",
]

DEMO_ARCHITECT_ID = "architect-demo"
DEMO_REQUESTER_ID = "requester-demo"
DEMO_ARCHITECT_PRINCIPAL_REF = "role:data_engineering_architect"
DEMO_REQUESTER_PRINCIPAL_REF = f"principal:{DEMO_REQUESTER_ID}"


def demo_clock() -> datetime:
    """The demonstration's clock: the wall clock, in UTC.

    A real deployment injects the same shape, so that domain behaviour stays deterministic
    and testable; the demonstration is the one caller that wants real time to pass.
    """
    return datetime.now(UTC)


class DemoRoleResolver:
    """The demonstration's role bindings, held as a fixed set of pairs.

    This is demonstration-grade. A real deployment resolves the actor's principals from an
    identity provider and the tenant's role assignments, and re-resolves them per decision.

    The pair, not either half, carries the authority. A resolver that answered `True` for
    everything would still make the inbox render, because the read paths only ask whether an
    actor may see a request — but it would also let any actor approve their own proposal,
    which is exactly the gate the demonstration exists to show.
    """

    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == DEMO_TENANT_ID and (actor_id, authority_ref) in {
            (DEMO_REQUESTER_ID, DEMO_REQUESTER_PRINCIPAL_REF),
            (DEMO_ARCHITECT_ID, DEMO_ARCHITECT_PRINCIPAL_REF),
        }


class DemoAuthorityResolver:
    """The demonstration's fulfillment authority, read back from its own publication.

    This is demonstration-grade. A real deployment resolves the observation from a policy
    decision point over live entitlements, approvals and classification rules, and the
    validity window is the one that authority itself asserts.

    The window here is the demonstration publication's own, which is deliberately long:
    the fulfillment adapter refuses on an expired authority, so a window measured in hours
    would leave a demonstration that has been left running refusing every request with no
    visible cause. `maximum_expiry` covers the same horizon, because it flows into the
    expiry of the delivered access grant and a nearer value would truncate it silently.
    """

    def __init__(
        self,
        *,
        publications: CatalogPublicationRepository,
        publication: DemoPublication,
        clock: Callable[[], datetime],
    ) -> None:
        self._publications = publications
        self._publication = publication
        self._clock = clock

    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> FulfillmentAuthorityObservation | ResolutionFailure:
        publication_id = self._publication.receipt.publication_id
        intent, _receipt, _references = self._publications.load_publication(
            tenant_id=tenant_id, publication_id=publication_id
        )
        semantic_version, integration_contract = self._publications.load_inputs(
            tenant_id=tenant_id, operation_id=intent.operation_id
        )
        product = integration_contract.destination_product
        policy_ref = ArtifactReference(
            artifact_id=f"policy-{integration_contract.contract_id}",
            version=integration_contract.version,
            digest=digest(integration_contract.access_policy),
        )
        classifications_by_id = {
            classification.object_id: classification
            for classification in semantic_version.classifications
        }
        classification_refs = tuple(
            ArtifactReference(
                artifact_id=classification_id,
                version=semantic_version.version,
                digest=digest(classifications_by_id[classification_id]),
            )
            for classification_id in integration_contract.access_policy.classification_refs
            if classification_id in classifications_by_id
        )
        return FulfillmentAuthorityObservation(
            tenant_id=tenant_id,
            catalog_publication_id=publication_id,
            requester_id=request.requester_id,
            requester_principal_ref=f"principal:{request.requester_id}",
            purpose_digest=digest(request.payload.purpose),
            authorization_policy_ref=policy_ref,
            approved_policy_refs=(policy_ref,),
            entitlement_observation_refs=tuple(
                ArtifactReference(
                    artifact_id=approval_id,
                    version=integration_contract.version,
                    digest=digest(approval_id),
                )
                for approval_id in integration_contract.approval_ids
            ),
            classification_rule_refs=classification_refs,
            permitted_data_product_refs=(
                ArtifactReference(
                    artifact_id=product.product_name,
                    version=integration_contract.version,
                    digest=digest(product),
                ),
            ),
            permitted_access_modes=("dashboard", "query"),
            maximum_expiry=self._publication.valid_until,
            policy_authority_classifications=(
                integration_contract.access_policy.classification_refs
            ),
            freshness_observation_ref=None,
            quality_observation_refs=(),
            data_observation_refs=(),
            observed_at=self._clock(),
            valid_until=self._publication.valid_until,
        )


def build_demo_snapshot_resolver(
    *,
    publications: CatalogPublicationRepository,
    publication: DemoPublication,
    clock: Callable[[], datetime],
) -> SemanticFulfillmentSnapshotAdapter:
    """The real snapshot adapter over the demonstration's publication and authority.

    The adapter itself is production code: only the authority it consults is
    demonstration-grade. It returns a refusal as a value rather than raising, so callers
    must inspect the result.
    """
    return SemanticFulfillmentSnapshotAdapter(
        publication_repository=publications,
        authority_resolver=DemoAuthorityResolver(
            publications=publications, publication=publication, clock=clock
        ),
        clock=clock,
    )


class DemoAnswerCandidateProvider:
    """The demonstration's answer runtime: the published definition, restated.

    This is demonstration-grade. A real deployment compiles and executes a governed plan
    against the warehouse and returns the answer with its evidence. Here the candidate is
    read straight from the approved semantic version, so the demonstration never invents a
    number and never reaches outside its own publication.
    """

    def __init__(self, *, publication: DemoPublication) -> None:
        self._metrics_by_id = {
            metric.object_id: metric for metric in publication.semantic_version.metrics
        }

    def propose(
        self, *, request: InboxRequest, grounding: FulfillmentGroundingSnapshot
    ) -> StakeholderAnswerDraft:
        del request
        metric_refs = tuple(
            reference
            for reference in grounding.metric_refs
            if reference.artifact_id in self._metrics_by_id
        )
        if not metric_refs:
            raise ValueError("the request does not ground in one published demonstration metric")
        metric = self._metrics_by_id[metric_refs[0].artifact_id]
        definition = metric.definition
        return StakeholderAnswerDraft(
            answer_text=f"{metric.name} is {definition[:1].lower()}{definition[1:]}",
            governed_dataset_refs=grounding.governed_dataset_refs,
            metric_refs=metric_refs[:1],
            as_of=grounding.as_of,
            freshness_disposition=_derive_freshness(grounding),
            material_quality_limitations=(),
            lineage_refs=grounding.lineage_refs,
            # The demonstration answers with a definition, which discloses no classified
            # value, so it claims no disclosure classification and needs no extra approver.
            disclosure_classifications=(),
        )


def _derive_freshness(grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition:
    """`current` only when the grounding carries a freshness observation to say so."""
    return "current" if grounding.freshness_observation_ref is not None else "not_applicable"


class DemoFreshnessEvaluator:
    """The demonstration's freshness rule, read from the grounding alone.

    This is demonstration-grade. A real deployment derives freshness from the product's
    observed materialization against the contract's requirement, and can report `stale`.
    """

    def derive(self, grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition:
        return _derive_freshness(grounding)


def build_demo_policy_compiler() -> FulfillmentPolicyCompiler:
    """The real policy compiler, with the demonstration's freshness evaluator."""
    return FulfillmentPolicyCompiler(freshness_evaluator=DemoFreshnessEvaluator())
