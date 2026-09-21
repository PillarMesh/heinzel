"""The collaborators the demonstration console gives its fulfillment service.

This is a demonstration, not a deployment. Every class here stands in for something a real
deployment resolves from a governed authority: an identity provider and its role bindings,
a policy decision point, and an answer runtime. They are deliberately small, deterministic
and local, and none of them is a security boundary.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    SemanticObject,
    digest,
)
from heinzel_request_management import (
    FreshnessDisposition,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    InboxRequest,
    ResolutionFailure,
    StakeholderAnswerDraft,
)
from heinzel_request_management.models import DataAccessRequest, StakeholderQuestion
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


def _words(text: str) -> tuple[str, ...]:
    # Unicode word characters, with underscore as a separator: "Umsätze" is one word, and
    # "customer_audit" reads the same as "Customer audit".
    return tuple(word for word in re.split(r"[\W_]+", text.casefold()) if word)


def _published_semantic_objects(
    semantic_version: ApprovedSemanticVersion,
) -> tuple[SemanticObject, ...]:
    """Every term of an approved semantic version, in the order the publication lists them.

    This mirrors what `publication_intent` puts in `CatalogPublicationIntent.semantic_objects`,
    so that matching against a loaded publication and matching against the version in hand
    agree on the same set of terms.
    """
    return (
        *semantic_version.entities,
        *semantic_version.events,
        *semantic_version.states,
        *semantic_version.relationships,
        *semantic_version.metrics,
        *semantic_version.classifications,
    )


def _semantic_matches(
    *, semantic_objects: tuple[SemanticObject, ...], request: InboxRequest
) -> tuple[SemanticObject, ...]:
    """Every published term the question names as whole words.

    Matching is on word sequences, never substrings, so "invoiced" does not name "Invoice". A
    term whose words sit wholly inside a longer named term is discarded, so asking about
    "Daily order count" names that term rather than also naming "Order".

    Matching is exact on whole words and does nothing about stemming, plurals or inflection,
    so "orders", "ordering" and "counts" name nothing at all. A question must use a published
    term's words as published.

    This is demonstration-grade: a real deployment resolves the question against the governed
    semantic layer rather than by matching words. It exists because without it the
    demonstration grounds every question in its one publication and answers all of them.
    """
    if not isinstance(request.payload, StakeholderQuestion):
        return ()
    question = _words(request.payload.question)
    spans: list[tuple[int, int, SemanticObject]] = []
    for semantic_object in semantic_objects:
        for phrase in {_words(semantic_object.name), _words(semantic_object.object_id)}:
            if not phrase:
                continue
            spans.extend(
                (start, start + len(phrase), semantic_object)
                for start in range(len(question) - len(phrase) + 1)
                if question[start : start + len(phrase)] == phrase
            )
    named = {
        id(semantic_object): semantic_object
        for start, end, semantic_object in spans
        if not any(
            other_start <= start and end <= other_end and (other_end - other_start) > (end - start)
            for other_start, other_end, _ in spans
        )
    }
    return tuple(named.values())


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

    The window here is the demonstration publication's own, which is deliberately long. Once
    `valid_until` has passed, this resolver can no longer build an observation at all: the
    model validator rejects a non-positive window, and the adapter turns that into
    `authority_resolution_failed` — not `entitlement_expired`, so nothing in the refusal
    says the window is what lapsed. A window measured in hours would therefore leave a
    demonstration that has been left running refusing every request with no visible cause.
    Restarting the demonstration re-derives the window from the current clock and clears it.

    `maximum_expiry` covers the same horizon, because it flows into the expiry of the
    delivered access grant and a nearer value would truncate it silently.

    It refuses, as a value rather than an exception, anything its one publication cannot
    ground: the grounding is built from the whole publication, so without these branches
    every question would be answered with the published definition regardless of what it
    asked.
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
        if isinstance(request.payload, DataAccessRequest):
            if request.payload.data_product_id != product.product_name:
                return ResolutionFailure(
                    reason_codes=("published_data_product_not_found",),
                    constraint_refs=(),
                    smallest_changes=("Choose the current governed data product.",),
                    requester_safe_explanation=(
                        "The selected data product is not in the current governed catalog."
                    ),
                )
        else:
            matches = _semantic_matches(semantic_objects=intent.semantic_objects, request=request)
            if len(matches) > 1:
                return ResolutionFailure(
                    reason_codes=("published_semantic_term_ambiguous",),
                    constraint_refs=(),
                    smallest_changes=(
                        "Ask about exactly one term in the workspace's current approved semantic "
                        "publication.",
                    ),
                    requester_safe_explanation=(
                        "This question names more than one term in the current governed catalog. "
                        "Ask about one term at a time."
                    ),
                )
            if not matches:
                return ResolutionFailure(
                    reason_codes=("published_semantic_term_not_found",),
                    constraint_refs=(),
                    smallest_changes=(
                        "Ask about one term in the workspace's current approved semantic "
                        "publication.",
                    ),
                    requester_safe_explanation=(
                        "The current governed catalog does not contain one unambiguous term for "
                        "this question."
                    ),
                )
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

    The answer is about the term the question named, resolved by the same matching the
    authority resolver used. Answering from the publication's one metric instead would tell
    a requester who asked about the entity or the classification about the metric, and cite
    a metric reference the answer never used.
    """

    def __init__(self, *, publication: DemoPublication) -> None:
        self._semantic_objects = _published_semantic_objects(publication.semantic_version)

    def propose(
        self, *, request: InboxRequest, grounding: FulfillmentGroundingSnapshot
    ) -> StakeholderAnswerDraft:
        matches = _semantic_matches(semantic_objects=self._semantic_objects, request=request)
        if len(matches) != 1:
            # An invariant guard, not a requester-facing refusal: `DemoAuthorityResolver`
            # already refuses a request that does not name exactly one published term, so
            # reaching here means this provider and the snapshot resolver disagree about
            # what was published. The console reports the failure as a stale revision, whose
            # advice to reload cannot help, so the message names the composition bug.
            raise ValueError(
                "demonstration answer provider matched "
                f"{len(matches)} published terms where its authority resolver admitted one; "
                "the resolver and the answer provider were built from different publications"
            )
        semantic_object = matches[0]
        definition = semantic_object.definition
        # Only a metric match cites a metric reference: an entity or a classification
        # correctly yields none, because the answer restates a definition, not a measure.
        metric_refs = tuple(
            reference
            for reference in grounding.metric_refs
            if reference.artifact_id == semantic_object.object_id
        )
        return StakeholderAnswerDraft(
            answer_text=(f"{semantic_object.name} is {definition[:1].lower()}{definition[1:]}"),
            governed_dataset_refs=grounding.governed_dataset_refs,
            metric_refs=metric_refs,
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
