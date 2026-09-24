"""Pin the exact, ordered field list of this component's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field changes that contract, so it must be a deliberate edit that updates this table as well.
"""

from __future__ import annotations

import pytest
from heinzel_access_control import (
    ConnectedAuthorityProvenance,
    CurrentEntitlementSnapshot,
    EnterpriseEntitlementAssertion,
    EnterpriseEntitlementObservation,
    EntitlementFilterDomain,
    EntitlementLookupRequest,
    SignedEntitlementBody,
    SignedEntitlementEnvelope,
)
from pydantic import BaseModel


@pytest.mark.parametrize(
    ("artifact", "fields"),
    (
        pytest.param(
            ConnectedAuthorityProvenance,
            (
                "connected_authority_ref",
                "connection_binding_ref",
                "source_revision",
                "source_payload_digest",
                "authentication_method",
                "authentication_key_ref",
                "authentication_evidence_digest",
                "adapter_ref",
            ),
            id="ConnectedAuthorityProvenance",
        ),
        pytest.param(
            EntitlementFilterDomain,
            (
                "dimension_ref",
                "values",
            ),
            id="EntitlementFilterDomain",
        ),
        pytest.param(
            EnterpriseEntitlementAssertion,
            (
                "schema_version",
                "tenant_id",
                "principal_ref",
                "purpose_digest",
                "decision",
                "product_version_refs",
                "semantic_refs",
                "filter_domains",
                "permissions",
                "effective_at",
                "valid_until",
                "provenance",
            ),
            id="EnterpriseEntitlementAssertion",
        ),
        pytest.param(
            EnterpriseEntitlementObservation,
            (
                "schema_version",
                "tenant_id",
                "principal_ref",
                "purpose_digest",
                "decision",
                "product_version_refs",
                "semantic_refs",
                "filter_domains",
                "permissions",
                "effective_at",
                "valid_until",
                "provenance",
                "observation_id",
                "recorded_at",
            ),
            id="EnterpriseEntitlementObservation",
        ),
        pytest.param(
            CurrentEntitlementSnapshot,
            (
                "schema_version",
                "snapshot_id",
                "snapshot_digest",
                "tenant_id",
                "principal_ref",
                "purpose_digest",
                "connected_authority_ref",
                "source_revision",
                "source_payload_digest",
                "observation_id",
                "product_version_refs",
                "semantic_refs",
                "filter_domains",
                "permissions",
                "effective_at",
                "valid_until",
                "resolved_at",
            ),
            id="CurrentEntitlementSnapshot",
        ),
        pytest.param(
            EntitlementLookupRequest,
            (
                "schema_version",
                "tenant_id",
                "principal_ref",
                "purpose_digest",
            ),
            id="EntitlementLookupRequest",
        ),
        pytest.param(
            SignedEntitlementBody,
            (
                "schema_version",
                "tenant_id",
                "principal_ref",
                "purpose_digest",
                "decision",
                "product_version_refs",
                "semantic_refs",
                "filter_domains",
                "permissions",
                "effective_at",
                "valid_until",
                "source_revision",
                "source_payload_digest",
            ),
            id="SignedEntitlementBody",
        ),
        pytest.param(
            SignedEntitlementEnvelope,
            (
                "schema_version",
                "key_ref",
                "body",
                "signature",
            ),
            id="SignedEntitlementEnvelope",
        ),
    ),
)
def test_durable_artifact_fields_are_exact_and_ordered(
    artifact: type[BaseModel], fields: tuple[str, ...]
) -> None:
    assert tuple(artifact.model_fields) == fields
