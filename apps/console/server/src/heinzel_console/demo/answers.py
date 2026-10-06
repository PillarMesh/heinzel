"""What the demonstration must have in place before its question can be answered.

The governed answer does not run on the product alone. It resolves the requester's entitlement
from a connected policy authority, checks the question against an approved answer scope policy,
and refuses when either is absent. This module is the demonstration's own: the entitlement it
publishes into its local authority, the scope policy it activates, and the interpreter that
resolves the governed terms a question selected into an intent naming approved terms.

This is demonstration data, not a deployment's. The entitlement is published by the
demonstration rather than resolved from an enterprise directory, and the policy's approvals
record authorities the demonstration supplies rather than people who approved anything. Every
check they feed is real; what stands in is who said so.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from heinzel_access_control import SignedEntitlementBody
from heinzel_contract_model import ApprovedSemanticVersion, ArtifactReference, digest
from heinzel_request_management import (
    AnswerIntentCandidate,
    AnswerQuestion,
    AnswerScopePolicy,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    AnswerScopePolicyLifecycle,
    AnswerScopePolicyRepository,
    BoundSemanticReference,
    ProductOwnerAuthority,
    QuestionTermSelection,
)

from .catalog import (
    DEMO_DIMENSION_TERM_ID,
    DEMO_METRIC_TERM_ID,
    demo_dimension_reference,
    demo_metric_reference,
    demo_semantic_version_reference,
)
from .materialization import DEMO_GROUP_COLUMN
from .publication import DEMO_PRODUCT_NAME, DEMO_TENANT_ID

__all__ = [
    "DEMO_ANSWER_POLICY_ID",
    "DEMO_ENTITLEMENT_PERMISSIONS",
    "DemoAnswerInterpreter",
    "DemoAnswerSelectionRefused",
    "activate_demo_answer_scope_policy",
    "demo_answer_bindings",
    "demo_entitlement_body",
    "demo_product_reference",
    "demo_question_selection",
]

DEMO_ANSWER_POLICY_ID = "answer-policy-demo"

# What the requester may do with an answer. `view` and `query` are what the demonstration
# shows; `download` is included because the console offers it and a permission the console
# offers but the entitlement withholds is a button that always refuses.
DEMO_ENTITLEMENT_PERMISSIONS: tuple[Literal["dashboard", "download", "query", "view"], ...] = (
    "download",
    "query",
    "view",
)

# The approver the contract's access policy requires, and the architect role every answer scope
# policy requires. `AnswerScopePolicyLifecycle.activate` derives the required set itself and
# refuses with the ones it did not get, so these cannot be quietly short of what it wants.
_ARCHITECT_AUTHORITY = "role:data_engineering_architect"
_OWNER_AUTHORITY = f"owner:{DEMO_PRODUCT_NAME}"

# How stale the product may be. The demonstration's source is seeded once and never updated, so
# the product's staleness is the demonstration's own age: this bounds how long a container may
# run before its answer is refused, which is the same thing the generation's retention and the
# publication's authority validity bound, and is set to match them.
_MAX_STALENESS_SECONDS = 365 * 86400
# The widest question the policy admits. The product has one row per day over three days, so
# this is far wider than the demonstration needs; it is a ceiling, not a target.
_MAX_TIME_WINDOW_SECONDS = 365 * 86400

# Ceilings. The product is three rows, so every one of these is far above what the
# demonstration's own question costs -- which is the point: a ceiling that the expected query
# sits under is a ceiling that can still catch a query that does not.
_ROW_CEILING = 100
_BYTE_CEILING = 1_000_000
_SCAN_CEILING = 100_000
_PERIOD_SCAN_BUDGET = 1_000_000
# Above the budget, so no `role:budget_authority` approval is required. A demonstration that
# needed one would be claiming a budget authority it does not have.
_PERIOD_SCAN_BUDGET_THRESHOLD = 2_000_000

_STATEMENT_TIMEOUT_SECONDS = 15
_RESULT_RETENTION_SECONDS = 3600
# The form of reading this interpreter performs, recorded in every intent it produces. It names
# the term selection rather than a form, because that is what it now resolves: an intent recorded
# under the old name was produced by a reading that did not look at the question at all, and the
# two must stay distinguishable in the evidence.
_INTERPRETER_REF = "answer-term-selection-v1"
_ROW_LIMIT = 10


def demo_product_reference(
    contract_version: int, destination_product_digest: str
) -> ArtifactReference:
    """The product reference every answer authority compares against."""
    return ArtifactReference(
        artifact_id=DEMO_PRODUCT_NAME,
        version=contract_version,
        digest=destination_product_digest,
    )


def demo_answer_bindings(
    semantic_version: ApprovedSemanticVersion, *, product_ref: ArtifactReference
) -> tuple[BoundSemanticReference, ...]:
    """The approved terms the question may name, bound to the product that carries them.

    The canonical references are the approved terms' own identifiers rather than the product's
    column names: an intent names meanings, and the query binding is what turns a meaning into a
    column. A question resolved straight to a column would be asking the warehouse rather than
    the semantic layer.
    """
    return (
        BoundSemanticReference(
            canonical_ref=DEMO_METRIC_TERM_ID,
            kind="metric",
            aliases=(),
            version_ref=demo_metric_reference(semantic_version),
            product_version_ref=product_ref,
        ),
        BoundSemanticReference(
            canonical_ref=DEMO_DIMENSION_TERM_ID,
            kind="dimension",
            aliases=(),
            version_ref=demo_dimension_reference(semantic_version),
            product_version_ref=product_ref,
        ),
    )


def demo_question_selection() -> QuestionTermSelection:
    """The governed terms the demonstration's own question is composed from.

    The same two identifiers `demo_answer_bindings` binds, and the same two the catalog's query
    binding declares: `demo_metric_reference` and `demo_dimension_reference` resolve them out of
    the approved semantic version and raise when it publishes no such term, so a selection naming
    something the publication dropped fails where the publication is read rather than here.

    A function rather than a constant, because `QuestionTermSelection` is frozen but a module
    constant shared between the seed and the tests would still read as one object two callers
    pass around; building it per call keeps each caller's selection its own.
    """
    return QuestionTermSelection(
        metric_ref=DEMO_METRIC_TERM_ID, dimension_refs=(DEMO_DIMENSION_TERM_ID,)
    )


def demo_entitlement_body(
    *,
    principal_ref: str,
    purpose: str,
    semantic_version: ApprovedSemanticVersion,
    product_ref: ArtifactReference,
    now: datetime,
    valid_for: timedelta,
) -> SignedEntitlementBody:
    """What the demonstration's local policy authority will assert about its requester.

    The body names the exact product and the exact approved terms, so an answer over anything
    else resolves no entitlement. `source_payload_digest` is computed from the claims rather than
    supplied, because the body refuses a digest that does not match what it carries.
    """
    claims = {
        "schema_version": "1",
        "tenant_id": DEMO_TENANT_ID,
        "principal_ref": principal_ref,
        "purpose_digest": digest(purpose),
        "decision": "active",
        "product_version_refs": (product_ref,),
        "semantic_refs": (
            demo_semantic_version_reference(semantic_version),
            demo_metric_reference(semantic_version),
            demo_dimension_reference(semantic_version),
        ),
        "filter_domains": (),
        "permissions": DEMO_ENTITLEMENT_PERMISSIONS,
        "effective_at": now,
        "valid_until": now + valid_for,
        "source_revision": 1,
    }
    return SignedEntitlementBody.model_validate(
        claims
        | {"source_payload_digest": SignedEntitlementBody.compute_source_payload_digest(claims)}
    )


def activate_demo_answer_scope_policy(
    policies: AnswerScopePolicyRepository,
    *,
    semantic_version: ApprovedSemanticVersion,
    product_ref: ArtifactReference,
    principal_ref: str,
    purpose: str,
    clock: Callable[[], datetime],
    valid_for: timedelta,
) -> AnswerScopePolicy:
    """Activate the demonstration's answer scope policy, once, into `policies`.

    A policy the state directory already holds is returned rather than activated again, because
    the draft carries its own timestamps: a second start would build a different revision 1 and
    `activate` would refuse it as not superseding the stored one. This mirrors how the
    demonstration's catalog publication handles a second start.

    The disclosure classifications are deliberately empty, and the demonstration's contract does
    carry one (`commercial`). Carrying it here would require a `role:policy_authority` approval,
    which the demonstration has no actor for: it has an architect and a requester. That is a
    stated gap rather than an approval invented to fill it, and it means the demonstration shows
    no disclosure control over a classified product.
    """
    now = clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("the demonstration clock must return timezone-aware UTC")
    existing = policies.list_revisions(DEMO_TENANT_ID, DEMO_ANSWER_POLICY_ID)
    if existing:
        return max(existing, key=lambda policy: policy.revision)

    draft = AnswerScopePolicyDraft(
        policy_id=DEMO_ANSWER_POLICY_ID,
        tenant_id=DEMO_TENANT_ID,
        revision=1,
        principal_scope=(principal_ref,),
        purposes=(purpose,),
        semantic_version_ref=demo_semantic_version_reference(semantic_version),
        data_product_version_refs=(product_ref,),
        metric_version_refs=(demo_metric_reference(semantic_version),),
        dimension_refs=(demo_dimension_reference(semantic_version),),
        filter_domains=(),
        max_time_window=_MAX_TIME_WINDOW_SECONDS,
        max_staleness=_MAX_STALENESS_SECONDS,
        quality_disposition="block",
        disclosure_classifications=(),
        disclosure_entity=DEMO_GROUP_COLUMN,
        minimum_group_size=1,
        row_ceiling=_ROW_CEILING,
        byte_ceiling=_BYTE_CEILING,
        scan_ceiling=_SCAN_CEILING,
        period_scan_budget=_PERIOD_SCAN_BUDGET,
        statement_timeout=_STATEMENT_TIMEOUT_SECONDS,
        result_retention=_RESULT_RETENTION_SECONDS,
        agent_access="denied",
        model_disclosure="metadata",
        valid_from=now,
        valid_until=now + valid_for,
        created_at=now,
    )
    approvals = tuple(
        AnswerScopePolicyApproval(
            approval_id=f"answer-policy-approval-demo-{position}",
            tenant_id=DEMO_TENANT_ID,
            policy_id=draft.policy_id,
            policy_revision=draft.revision,
            policy_digest=digest(draft),
            authority_ref=authority_ref,
            actor_id=f"demo-policy-actor-{position}",
            decision="approve",
            created_at=now,
        )
        for position, authority_ref in enumerate((_ARCHITECT_AUTHORITY, _OWNER_AUTHORITY), start=1)
    )
    return AnswerScopePolicyLifecycle(policies, clock=clock).activate(
        draft=draft,
        approvals=approvals,
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=product_ref, authority_ref=_OWNER_AUTHORITY
            ),
        ),
        period_scan_budget_threshold=_PERIOD_SCAN_BUDGET_THRESHOLD,
    )


class DemoAnswerSelectionRefused(ValueError):
    """A question selected terms this publication does not carry, or carries differently.

    Raised rather than resolved around: substituting the terms the product happens to publish
    would answer a question nobody asked, which is the whole failure this interpreter exists to
    stop. The console classifies it as an integrity refusal, because it is a reading the console
    cannot attribute to a revision the requester could reload.
    """


@dataclass(frozen=True, slots=True)
class DemoAnswerInterpreter:
    """Resolve the terms a question selected, against the terms the publication carries.

    It still reads none of the question's words -- a question's words are a label, and resolving
    prose against a governed vocabulary is a capability this demonstration does not deliver. What
    it reads instead is the selection the requester composed from the published terms, so the
    intent names what they chose rather than what this module happens to know about.

    `published` is the same `demo_answer_bindings` tuple the validation is given, so a term that
    is not in the publication is not in here either. The builder in the console offers exactly
    these terms and nothing else, which makes a refusal below unreachable from a browser; it is
    checked anyway, because an interpreter that trusted its caller to have offered the right
    options would be the authority for a reading it never verified.

    The tenant is checked rather than assumed, because an intent built for another tenant would
    be validated against this one's policy.
    """

    published: tuple[BoundSemanticReference, ...]

    interpreter_ref: ClassVar[str] = _INTERPRETER_REF

    def interpret(self, question: AnswerQuestion) -> AnswerIntentCandidate:
        if question.tenant_id != DEMO_TENANT_ID:
            raise ValueError("the demonstration's interpreter received another tenant's question")
        selection = question.selection
        if selection is None:
            raise DemoAnswerSelectionRefused(
                "the question names no governed terms, and this interpreter does not read its "
                "words: compose the question from the published terms and ask again"
            )
        metrics = self._refs_of_kind("metric")
        dimensions = self._refs_of_kind("dimension")
        carried = metrics | dimensions
        unpublished = tuple(
            reference
            for reference in (selection.metric_ref, *selection.dimension_refs)
            if reference not in carried
        )
        if unpublished:
            raise DemoAnswerSelectionRefused(
                f"the publication carries no approved term named {', '.join(sorted(unpublished))}"
            )
        if selection.metric_ref not in metrics:
            raise DemoAnswerSelectionRefused(
                f"{selection.metric_ref!r} is an approved dimension, not a metric, so it cannot "
                "be what the question measures"
            )
        not_dimensions = tuple(
            reference for reference in selection.dimension_refs if reference not in dimensions
        )
        if not_dimensions:
            raise DemoAnswerSelectionRefused(
                f"{', '.join(sorted(not_dimensions))} names an approved metric, not a dimension, "
                "so it cannot be what the question is broken down over"
            )
        return AnswerIntentCandidate(
            intent_kind="metric_value",
            metric_refs=(selection.metric_ref,),
            dimension_refs=selection.dimension_refs,
            filters=(),
            time_window=None,
            ordering=(),
            row_limit=_ROW_LIMIT,
        )

    def _refs_of_kind(self, kind: Literal["dimension", "metric"]) -> frozenset[str]:
        return frozenset(
            binding.canonical_ref for binding in self.published if binding.kind == kind
        )
