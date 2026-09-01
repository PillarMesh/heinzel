from __future__ import annotations

import pytest
from pillarmesh_state import AcquisitionArtifactNotFoundError

from tests.acceptance.run_plan4a import Plan4AJourney, execute_plan4a_journey


@pytest.fixture(scope="module")
def journey(tmp_path_factory: pytest.TempPathFactory) -> Plan4AJourney:
    return execute_plan4a_journey(tmp_path_factory.mktemp("plan4a-postgresql"))


def test_initial_snapshot_contains_orders_subscription_lifecycle_and_account_segments(
    journey: Plan4AJourney,
) -> None:
    manifest = journey.tenant_a_initial.batch_manifest
    assert manifest is not None
    assert tuple(item.logical_object_ref for item in manifest.segment_manifests) == (
        "account_segments",
        "orders",
        "subscriptions",
    )
    assert all(item.record_count > 0 for item in manifest.segment_manifests)

    records = journey.consumer.records_for(manifest.batch_id)
    subscription_fields = {
        field.name: field.value
        for record in records
        if record.logical_object_ref == "subscriptions"
        for field in record.fields
    }
    segment_fields = {
        field.name: field.value
        for record in records
        if record.logical_object_ref == "account_segments"
        for field in record.fields
    }

    assert subscription_fields["lifecycle_status"] in {"active", "trialing"}
    assert segment_fields["segment_name"] == "growth"


def test_colliding_private_provider_ids_remain_tenant_isolated(
    journey: Plan4AJourney,
) -> None:
    tenant_a_manifest = journey.tenant_a_initial.batch_manifest
    tenant_b_manifest = journey.tenant_b_initial.batch_manifest
    tenant_a_receipt = journey.tenant_a_initial.prepared_receipt
    assert tenant_a_manifest is not None and tenant_b_manifest is not None
    assert tenant_a_receipt is not None
    assert (
        journey.provider_object_identities["tenant-a"]
        == journey.provider_object_identities["tenant-b"]
    )
    assert tenant_a_manifest.batch_id != tenant_b_manifest.batch_id

    with pytest.raises(AcquisitionArtifactNotFoundError):
        journey.artifact_store.open_verified(
            tenant_id="tenant-b",
            artifact_digest=tenant_a_receipt.batch_manifest_digest,
        )

    assert journey.denial_reason_codes["cross_tenant_acknowledgement"] == (
        "acknowledgement_tenant_mismatch"
    )


def test_replay_and_exact_acknowledgement_advance_once(
    journey: Plan4AJourney,
) -> None:
    assert journey.tenant_a_replay.prepared_receipt == journey.tenant_a_initial.prepared_receipt
    assert journey.tenant_a_replay.batch_manifest == journey.tenant_a_initial.batch_manifest
    assert journey.initial_checkpoint.committed_revision == 1
    assert journey.acknowledgement_replay == journey.initial_checkpoint
    assert journey.provider_resolutions_at_replay == journey.provider_resolutions_before_replay
    assert journey.denial_reason_codes["stale_acknowledgement"] == "prepared_acquisition_not_found"


def test_late_commit_is_absent_from_open_snapshot_and_present_in_following_incremental(
    journey: Plan4AJourney,
) -> None:
    first_records = journey.consumer.records_for(journey.first_incremental_batch_id)
    second_records = journey.consumer.records_for(journey.late_incremental_batch_id)

    assert "late-commit" not in {
        field.value for record in first_records for field in record.fields if field.name == "label"
    }
    assert "late-commit" in {
        field.value for record in second_records for field in record.fields if field.name == "label"
    }
    assert journey.late_checkpoint.committed_revision == 3


def test_drift_write_denial_and_ceiling_fail_without_durable_acquisition_effect(
    journey: Plan4AJourney,
) -> None:
    assert journey.denial_reason_codes["write_capability"] == "authorization_denied"
    assert journey.denial_reason_codes["source_drift"] == "source_observation_authority_mismatch"
    assert journey.denial_reason_codes["ceiling"] == (
        "encoded_bytes_ceiling_exceeded:account_segments"
    )
    assert journey.artifact_count_before_ceiling == journey.artifact_count_after_ceiling
    assert journey.checkpoint_revision_before_ceiling == journey.checkpoint_revision_after_ceiling


def test_public_evidence_is_private_value_free_and_source_only(journey: Plan4AJourney) -> None:
    public_payload = journey.public_payload

    for canary in journey.private_canaries:
        assert canary not in public_payload
    assert journey.destination_provider_resolutions == 0
    assert journey.destination_writes == 0
    assert b'"delivery_claimed":false' in public_payload
