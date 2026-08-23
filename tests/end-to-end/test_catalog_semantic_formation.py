from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pillarmesh_catalog_control import CatalogBindingState
from pillarmesh_request_management import RequestState

from tests.acceptance.plan2_orchestration import (
    ExactCleanupTarget,
    OfflinePlan2Harness,
    OwnerDecision,
    legacy_refund_attribute_catalog,
    refund_entity_package,
    revenue_to_cash_package,
    revised_revenue_to_cash_package,
    strict_validating_catalog,
)


def test_revenue_to_cash_forms_approved_contract_and_verified_publication(
    tmp_path: Path,
) -> None:
    harness = OfflinePlan2Harness(
        database_path=tmp_path / "plan2.db",
        catalog=strict_validating_catalog(),
    )

    result = harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
    )

    assert result.catalog_binding is not None
    assert result.catalog_binding.lifecycle_state is CatalogBindingState.READY
    assert result.semantic_version is not None
    assert result.semantic_version.process_package_ref.version == 1
    assert result.semantic_version.approval_ids
    assert result.contract is not None
    assert result.contract.formation_status == "ready_to_activate"
    assert result.contract.source_observation_refs
    assert result.publication_receipt is not None
    assert result.publication_receipt.round_trip_verified is True
    assert result.request.state is RequestState.DELIVERED


def test_refund_cross_kind_conflict_ends_in_no_valid_plan_without_effects(
    tmp_path: Path,
) -> None:
    catalog = legacy_refund_attribute_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)

    result = harness.run(
        tenant_id="tenant-a",
        process_package=refund_entity_package(),
        owner_decisions=(),
    )

    assert result.authority_resolution.reason_code == "cross_kind_conflict"
    assert result.request.state is RequestState.NO_VALID_PLAN
    assert result.execution_occurred is False
    assert result.publication_receipt is None
    assert result.contract is None
    assert result.catalog_binding is None
    assert catalog.provider_effect_count == 0
    assert catalog.publication_effect_count == 0


def test_tenant_cannot_read_any_other_tenant_acceptance_artifact_with_identifiers(
    tmp_path: Path,
) -> None:
    catalog = strict_validating_catalog()
    harness = OfflinePlan2Harness(
        database_path=tmp_path / "plan2.db",
        catalog=catalog,
    )
    tenant_a = harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
    )
    tenant_b = harness.run(
        tenant_id="tenant-b",
        process_package=revenue_to_cash_package(),
    )

    assert tenant_a.candidate_set is not None
    assert tenant_b.candidate_set is not None
    assert tenant_a.candidate_set.original_digest == tenant_b.candidate_set.original_digest
    assert tenant_a.identifiers.process_package_id != tenant_b.identifiers.process_package_id

    identifiers = tenant_a.identifiers
    artifact_reads = (
        lambda: harness.get_process_package("tenant-b", identifiers.process_package_id),
        lambda: harness.get_candidate_set("tenant-b", identifiers.candidate_set_id),
        lambda: harness.get_authority_observation("tenant-b", identifiers.authority_observation_id),
        lambda: harness.get_review_bundle("tenant-b", identifiers.review_bundle_id),
        lambda: harness.get_semantic_version("tenant-b", identifiers.semantic_version_id),
        lambda: harness.get_contract("tenant-b", identifiers.contract_id),
        lambda: harness.get_catalog_binding("tenant-b", identifiers.catalog_binding_id),
        lambda: harness.get_publication_receipt("tenant-b", identifiers.publication_receipt_id),
        lambda: harness.get_request("tenant-b", identifiers.request_id),
    )

    for read_artifact in artifact_reads:
        with pytest.raises(LookupError):
            read_artifact()

    with pytest.raises(ValueError, match="exact recorded cleanup target required"):
        harness.cleanup(
            tenant_id="tenant-b",
            targets=(
                ExactCleanupTarget(
                    resource_kind="catalog_binding",
                    resource_id=identifiers.catalog_binding_id,
                ),
            ),
        )

    assert catalog.cleanup_effect_count == 0


def test_replay_converges_without_duplicate_semantic_or_provider_effects(tmp_path: Path) -> None:
    catalog = strict_validating_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)

    first = harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
        correlation_id="correlation-replay",
    )
    replay = harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
        correlation_id="correlation-replay",
    )

    assert replay == first
    assert harness.semantic_version_count("tenant-a") == 1
    assert harness.review_submission_count("tenant-a") == 1
    assert catalog.publication_effect_count == 1


def test_replay_rejects_changed_manifest_source_bytes(tmp_path: Path) -> None:
    harness = OfflinePlan2Harness(
        database_path=tmp_path / "plan2.db", catalog=strict_validating_catalog()
    )
    package = revenue_to_cash_package()
    harness.run(
        tenant_id="tenant-a",
        process_package=package,
        correlation_id="manifest-replay",
    )

    with pytest.raises(ValueError, match="correlation replay input differs"):
        harness.run(
            tenant_id="tenant-a",
            process_package=replace(package, manifest=package.manifest + b" "),
            correlation_id="manifest-replay",
        )


def test_replay_rehydrates_from_durable_correlation_in_a_fresh_harness(tmp_path: Path) -> None:
    catalog = strict_validating_catalog()
    database_path = tmp_path / "plan2.db"
    first_harness = OfflinePlan2Harness(database_path=database_path, catalog=catalog)
    first = first_harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
        correlation_id="durable-correlation",
    )
    replay_harness = OfflinePlan2Harness(database_path=database_path, catalog=catalog)

    replay = replay_harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
        correlation_id="durable-correlation",
    )

    assert replay == first
    assert replay_harness.semantic_version_count("tenant-a") == 1
    assert replay_harness.review_submission_count("tenant-a") == 1
    assert catalog.publication_effect_count == 1


def test_no_valid_plan_replay_preserves_every_conflicting_observation(tmp_path: Path) -> None:
    catalog = legacy_refund_attribute_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)
    first = harness.run(
        tenant_id="tenant-a",
        process_package=refund_entity_package(),
        correlation_id="refund-conflict",
    )

    replay = harness.run(
        tenant_id="tenant-a",
        process_package=refund_entity_package(),
        correlation_id="refund-conflict",
    )

    assert replay == first
    assert len(replay.authority_observations) == 2
    assert replay.authority_resolution.reason_code == "cross_kind_conflict"
    assert catalog.publication_effect_count == 0


def test_persisted_approval_ids_from_an_older_review_are_denied_before_publication(
    tmp_path: Path,
) -> None:
    catalog = strict_validating_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)
    original = harness.run(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
    )
    revised = harness.run(
        tenant_id="tenant-a",
        process_package=revised_revenue_to_cash_package(),
    )
    assert original.semantic_version is not None
    assert revised.semantic_version is not None
    publication_count = catalog.publication_effect_count

    with pytest.raises(RuntimeError, match="contract formation failed"):
        harness.form_contract_with_approval_ids(
            tenant_id="tenant-a",
            semantic_version=revised.semantic_version,
            approval_ids=original.semantic_version.approval_ids,
        )

    assert catalog.publication_effect_count == publication_count


def test_stale_synthetic_owner_decision_is_denied_before_formation(tmp_path: Path) -> None:
    catalog = strict_validating_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)

    with pytest.raises(ValueError, match="stale approval"):
        harness.run(
            tenant_id="tenant-a",
            process_package=revenue_to_cash_package(),
            owner_decisions=(
                OwnerDecision(
                    review_bundle_digest="0" * 64,
                    candidate_set_digest="1" * 64,
                    approved=True,
                ),
            ),
        )

    assert catalog.publication_effect_count == 0


@pytest.mark.parametrize(
    "target",
    [
        ExactCleanupTarget(resource_kind="catalog_binding", resource_id="unrecorded"),
        ExactCleanupTarget(resource_kind="tenant_namespace", resource_id="*"),
    ],
)
def test_cleanup_denies_unrecorded_or_broad_targets(
    tmp_path: Path,
    target: ExactCleanupTarget,
) -> None:
    catalog = strict_validating_catalog()
    harness = OfflinePlan2Harness(database_path=tmp_path / "plan2.db", catalog=catalog)

    with pytest.raises(ValueError, match="exact recorded cleanup target required"):
        harness.cleanup(tenant_id="tenant-a", targets=(target,))

    assert catalog.cleanup_effect_count == 0
