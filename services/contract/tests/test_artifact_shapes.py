"""Pin the exact, ordered field list of this component's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field changes that contract, so it must be a deliberate edit that updates this table as well.
"""

from __future__ import annotations

from heinzel_contract_service import SourceFreshnessObservation


def test_source_freshness_observation_fields_are_exact_and_ordered() -> None:
    assert tuple(SourceFreshnessObservation.model_fields) == (
        "schema_version",
        "observation_id",
        "tenant_id",
        "version",
        "source_ref",
        "input_generation_digest",
        "data_observation_ref",
        "watermark_at",
        "observed_at",
    )
