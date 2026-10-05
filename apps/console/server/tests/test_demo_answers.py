"""What the demonstration must have in place before its question can be answered.

These need no warehouse and no runtime. What they ask is whether the pieces the governed answer
compares against each other agree: the interpreter's refs with the bindings', the scope policy's
purpose with the seeded request's, and the entitlement's terms with the product's.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pytest
from heinzel_console.demo.answers import (
    DEMO_ANSWER_POLICY_ID,
    DemoAnswerInterpreter,
    activate_demo_answer_scope_policy,
    demo_answer_bindings,
    demo_entitlement_body,
    demo_product_reference,
)
from heinzel_console.demo.catalog import (
    demo_dimension_reference,
    demo_metric_reference,
    demo_semantic_version_reference,
)
from heinzel_console.demo.materialization import DEMO_GROUP_COLUMN, DEMO_RETENTION_SECONDS
from heinzel_console.demo.publication import (
    DEMO_PURPOSE,
    DEMO_QUESTION,
    DEMO_TENANT_ID,
    DemoPublication,
    build_demo_publication,
)
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import ApprovedSemanticVersion, ArtifactReference, digest
from heinzel_request_management import (
    AnswerQuestion,
    SQLiteAnswerScopePolicyRepository,
    StakeholderQuestion,
)

_NOW = datetime(2026, 9, 13, tzinfo=UTC)
_VALID_FOR = timedelta(days=365)
_PRINCIPAL = "principal:requester-demo"


@pytest.fixture(name="published")
def _published(tmp_path: Path) -> Iterator[DemoPublication]:
    stores = DemoStores(tmp_path / "state")
    try:
        yield build_demo_publication(stores, clock=lambda: _NOW)
    finally:
        stores.close()


@pytest.fixture(name="policies")
def _policies(tmp_path: Path) -> Iterator[SQLiteAnswerScopePolicyRepository]:
    connection = sqlite3.connect(tmp_path / "policies.sqlite3")
    try:
        yield SQLiteAnswerScopePolicyRepository(connection)
    finally:
        connection.close()


def _product_ref(published: DemoPublication) -> ArtifactReference:
    return demo_product_reference(
        published.contract.version, digest(published.contract.destination_product)
    )


def _question() -> AnswerQuestion:
    return AnswerQuestion(
        tenant_id=DEMO_TENANT_ID,
        request_id="request-demo-1",
        request_revision=2,
        question_digest=digest(StakeholderQuestion(question=DEMO_QUESTION, purpose=DEMO_PURPOSE)),
        interpreter="form",
        interpreter_ref=DemoAnswerInterpreter.interpreter_ref,
    )


def test_the_interpreter_names_terms_the_bindings_resolve(published: DemoPublication) -> None:
    """Validation matches the intent's refs against the bindings', so the two must agree.

    A mismatch is not a type error anywhere: the intent compiles, validation refuses it, and the
    refusal names an unresolved reference rather than the disagreement that caused it.
    """
    candidate = DemoAnswerInterpreter().interpret(_question())
    bindings = demo_answer_bindings(published.semantic_version, product_ref=_product_ref(published))

    assert {binding.canonical_ref for binding in bindings if binding.kind == "metric"} == set(
        candidate.metric_refs
    )
    assert {binding.canonical_ref for binding in bindings if binding.kind == "dimension"} == set(
        candidate.dimension_refs
    )


def test_the_bindings_carry_the_publications_own_term_digests(published: DemoPublication) -> None:
    """A reworded term is a different reference, so the binding stops matching rather than
    describing the old meaning under the new name."""
    bindings = demo_answer_bindings(published.semantic_version, product_ref=_product_ref(published))
    by_kind = {binding.kind: binding for binding in bindings}

    assert by_kind["metric"].version_ref == demo_metric_reference(published.semantic_version)
    assert by_kind["dimension"].version_ref == demo_dimension_reference(published.semantic_version)


def test_the_interpreter_refuses_another_tenants_question() -> None:
    """An intent built for one tenant would be validated against this one's policy."""
    foreign = _question().model_copy(update={"tenant_id": "tenant-somebody-else"})

    with pytest.raises(ValueError, match="another tenant"):
        DemoAnswerInterpreter().interpret(foreign)


def test_the_entitlement_names_the_product_and_its_approved_terms(
    published: DemoPublication,
) -> None:
    """An entitlement over anything else resolves nothing, so it names exactly these."""
    body = demo_entitlement_body(
        principal_ref=_PRINCIPAL,
        purpose=DEMO_PURPOSE,
        semantic_version=published.semantic_version,
        product_ref=_product_ref(published),
        now=_NOW,
        valid_for=_VALID_FOR,
    )

    assert body.product_version_refs == (_product_ref(published),)
    assert set(body.semantic_refs) == {
        demo_semantic_version_reference(published.semantic_version),
        demo_metric_reference(published.semantic_version),
        demo_dimension_reference(published.semantic_version),
    }
    assert body.purpose_digest == digest(DEMO_PURPOSE)
    assert body.decision == "active"


def test_the_scope_policy_covers_the_purpose_the_seed_submits(
    published: DemoPublication, policies: SQLiteAnswerScopePolicyRepository
) -> None:
    """`interpret_and_validate` refuses a request whose purpose the policy does not list."""
    policy = activate_demo_answer_scope_policy(
        policies,
        semantic_version=published.semantic_version,
        product_ref=_product_ref(published),
        principal_ref=_PRINCIPAL,
        purpose=DEMO_PURPOSE,
        clock=lambda: _NOW,
        valid_for=_VALID_FOR,
    )

    assert DEMO_PURPOSE in policy.purposes
    assert _PRINCIPAL in policy.principal_scope
    assert policy.disclosure_entity == DEMO_GROUP_COLUMN


def test_the_scope_policy_admits_a_product_for_as_long_as_it_is_retained(
    published: DemoPublication, policies: SQLiteAnswerScopePolicyRepository
) -> None:
    """The demonstration's source is never updated, so staleness is the demonstration's age.

    A bound shorter than the generation's own retention would refuse a product the runtime still
    considers answerable, which reads as a broken demonstration rather than a policy decision.
    """
    policy = activate_demo_answer_scope_policy(
        policies,
        semantic_version=published.semantic_version,
        product_ref=_product_ref(published),
        principal_ref=_PRINCIPAL,
        purpose=DEMO_PURPOSE,
        clock=lambda: _NOW,
        valid_for=_VALID_FOR,
    )

    assert policy.max_staleness >= DEMO_RETENTION_SECONDS


def test_the_scope_policy_carries_the_approvals_the_lifecycle_requires(
    published: DemoPublication, policies: SQLiteAnswerScopePolicyRepository
) -> None:
    """`activate` derives the required authorities itself and refuses the ones it did not get."""
    policy = activate_demo_answer_scope_policy(
        policies,
        semantic_version=published.semantic_version,
        product_ref=_product_ref(published),
        principal_ref=_PRINCIPAL,
        purpose=DEMO_PURPOSE,
        clock=lambda: _NOW,
        valid_for=_VALID_FOR,
    )

    assert len(policy.approval_ids) == 2
    # The approver the contract's own access policy names.
    assert published.contract.access_policy.required_approver_refs == (
        f"owner:{published.contract.destination_product.product_name}",
    )


class _PolicyArguments(TypedDict):
    """The standing authority both activations below are given, spread into each call.

    A `TypedDict` rather than a plain literal, whose values would be inferred as `object` and
    could be spread into any signature at all. Only the clock differs between the two calls,
    which is the whole point of the case.
    """

    semantic_version: ApprovedSemanticVersion
    product_ref: ArtifactReference
    principal_ref: str
    purpose: str
    valid_for: timedelta


def test_a_second_start_returns_the_policy_the_first_activated(
    published: DemoPublication, policies: SQLiteAnswerScopePolicyRepository
) -> None:
    """The draft carries its own timestamps, so a second activation would not supersede."""
    arguments: _PolicyArguments = {
        "semantic_version": published.semantic_version,
        "product_ref": _product_ref(published),
        "principal_ref": _PRINCIPAL,
        "purpose": DEMO_PURPOSE,
        "valid_for": _VALID_FOR,
    }
    first = activate_demo_answer_scope_policy(policies, clock=lambda: _NOW, **arguments)
    later = activate_demo_answer_scope_policy(
        policies, clock=lambda: _NOW + timedelta(hours=3), **arguments
    )

    assert later == first
    assert policies.list_revisions(DEMO_TENANT_ID, DEMO_ANSWER_POLICY_ID) == (first,)
