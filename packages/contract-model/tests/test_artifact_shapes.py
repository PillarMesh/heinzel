"""Pin the exact, ordered field list of this component's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field changes that contract, so it must be a deliberate edit that updates this table as well.
"""

from __future__ import annotations

import pytest
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ManagedIntegrationContract,
)
from pydantic import BaseModel


@pytest.mark.parametrize(
    ("artifact", "fields"),
    (
        pytest.param(
            ManagedIntegrationContract,
            (
                "schema_version",
                "contract_id",
                "tenant_id",
                "version",
                "formation_status",
                "semantic_version_ref",
                "source_observation_refs",
                "mappings",
                "integrity_constraints",
                "destination_product",
                "freshness",
                "quality",
                "trigger_policy",
                "access_policy",
                "evidence_policy",
                "failure_policy",
                "approval_ids",
            ),
            id="ManagedIntegrationContract",
        ),
        pytest.param(
            ApprovedSemanticVersion,
            (
                "schema_version",
                "semantic_version_id",
                "tenant_id",
                "version",
                "process_package_ref",
                "candidate_set_digest",
                "review_bundle_digest",
                "entities",
                "events",
                "states",
                "relationships",
                "identity_rules",
                "constraints",
                "metrics",
                "classifications",
                "authority_bindings",
                "approval_ids",
                "created_at",
            ),
            id="ApprovedSemanticVersion",
        ),
    ),
)
def test_durable_artifact_fields_are_exact_and_ordered(
    artifact: type[BaseModel], fields: tuple[str, ...]
) -> None:
    assert tuple(artifact.model_fields) == fields
