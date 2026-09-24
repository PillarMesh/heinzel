"""Pin the exact shapes and review vocabularies of the semantic registry's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field or vocabulary member changes that contract, so it must be a deliberate edit that updates
these tables as well.
"""

from __future__ import annotations

from typing import get_args

import pytest
from heinzel_semantic_registry import (
    ApprovedProductVersionMetadata,
    AuthorityObservation,
    OntologyReviewBundle,
    OntologyReviewItem,
    ReviewItemDecision,
)
from pydantic import BaseModel


@pytest.mark.parametrize(
    ("artifact", "fields"),
    (
        pytest.param(
            ApprovedProductVersionMetadata,
            (
                "schema_version",
                "tenant_id",
                "product_ref",
                "generation",
                "contract_ref",
                "semantic_version_ref",
                "materialization_receipt_ref",
                "lineage_digest",
                "approved_narrative_terms",
                "recorded_at",
            ),
            id="ApprovedProductVersionMetadata",
        ),
        pytest.param(
            AuthorityObservation,
            (
                "schema_version",
                "observation_id",
                "tenant_id",
                "information_kind",
                "source_kind",
                "subject_ref",
                "assertion",
                "authority_ref",
                "observed_digest",
                "observed_at",
                "valid_until",
            ),
            id="AuthorityObservation",
        ),
        pytest.param(
            OntologyReviewBundle,
            (
                "schema_version",
                "bundle_id",
                "tenant_id",
                "revision",
                "candidate_set_digest",
                "authority_observation_digests",
                "items",
                "required_authority_refs",
                "status",
                "created_at",
                "updated_at",
            ),
            id="OntologyReviewBundle",
        ),
    ),
)
def test_durable_artifact_fields_are_exact_and_ordered(
    artifact: type[BaseModel], fields: tuple[str, ...]
) -> None:
    assert tuple(artifact.model_fields) == fields


def test_review_item_decisions_are_exact_and_ordered() -> None:
    assert tuple(member.value for member in ReviewItemDecision) == (
        "accept",
        "reject",
        "revise",
        "merge",
        "unresolved",
    )


def test_review_item_statuses_are_exact_and_ordered() -> None:
    assert get_args(OntologyReviewItem.model_fields["status"].annotation) == (
        "pending",
        "accepted",
        "rejected",
        "revised",
        "merged",
        "unresolved",
    )
