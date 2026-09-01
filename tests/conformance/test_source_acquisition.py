from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Literal

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    AcquisitionCeilingExceeded,
    SourceConformanceError,
    verify_checkpoint_lifecycle,
)


def test_shared_harness_runs_three_object_source_without_resolving_destination() -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    provider = FakeSourceProvider(fixture)
    destination_guard = DestinationProviderResolutionGuard()

    result = run_shared_source_conformance(
        provider,
        fixture,
        destination_provider_resolver=destination_guard,
    )

    assert fixture.scenario.intent.object_refs == (
        "account-segments",
        "orders",
        "subscriptions",
    )
    schemas = {
        schema.logical_object_ref: tuple(field.name for field in schema.fields)
        for schema in fixture.scenario.schemas
    }
    assert schemas == {
        "account-segments": ("account_id", "segment", "updated_at"),
        "orders": ("order_id", "amount", "updated_at"),
        "subscriptions": ("subscription_id", "lifecycle_status", "updated_at"),
    }
    assert tuple(record.logical_object_ref for record in result.provider_result.records) == (
        "account-segments",
        "orders",
        "subscriptions",
    )
    assert destination_guard.resolved_provider_refs == ()


def test_strict_consumer_links_the_exact_prepared_batch_to_one_checkpoint_revision() -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    result = run_shared_source_conformance(
        FakeSourceProvider(fixture),
        fixture,
        destination_provider_resolver=DestinationProviderResolutionGuard(),
    )

    verify_checkpoint_lifecycle(
        result.prepared_receipt,
        result.acknowledgement,
        result.checkpoint_receipt,
    )
    assert result.acknowledgement.consumer_ref == "strict-source-test-consumer"
    assert result.checkpoint_receipt.previous_revision == 0
    assert result.checkpoint_receipt.committed_revision == 1
    assert result.checkpoint_receipt.cursor_digest == result.provider_result.candidate_cursor_digest


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("tenant_id", "tenant-b"),
        ("intent_key", "a" * 64),
        ("batch_id", "b" * 64),
        ("batch_manifest_digest", "f" * 64),
        ("prior_checkpoint_revision", 1),
        ("candidate_checkpoint_digest", "d" * 64),
    ),
)
def test_strict_consumer_rejects_any_forged_prepared_linkage(
    field_name: str,
    forged_value: str | int,
) -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        StrictAcknowledgementConsumer,
        StrictAcknowledgementError,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    result = run_shared_source_conformance(
        FakeSourceProvider(fixture),
        fixture,
        destination_provider_resolver=DestinationProviderResolutionGuard(),
    )
    forged_prepared = result.prepared_receipt.model_copy(update={field_name: forged_value})

    with pytest.raises(StrictAcknowledgementError, match="acknowledgement_authority_mismatch"):
        StrictAcknowledgementConsumer().acknowledge(
            forged_prepared,
            result.batch_manifest,
        )


def test_strict_consumer_revalidates_a_forged_manifest_before_acknowledging() -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        StrictAcknowledgementConsumer,
        StrictAcknowledgementError,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    result = run_shared_source_conformance(
        FakeSourceProvider(fixture),
        fixture,
        destination_provider_resolver=DestinationProviderResolutionGuard(),
    )
    forged_manifest = result.batch_manifest.model_copy(update={"total_record_count": 99})
    forged_prepared = result.prepared_receipt.model_copy(
        update={"batch_manifest_digest": digest(forged_manifest)}
    )

    with pytest.raises(StrictAcknowledgementError, match="malformed_acknowledgement_input"):
        StrictAcknowledgementConsumer().acknowledge(forged_prepared, forged_manifest)


def test_public_evidence_excludes_source_rows_provider_ids_cursors_and_private_digests() -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    result = run_shared_source_conformance(
        FakeSourceProvider(fixture),
        fixture,
        destination_provider_resolver=DestinationProviderResolutionGuard(),
    )

    public_payload = canonical_bytes(result.public_evidence_receipts)
    private_needles = (
        fixture.candidate_cursor_payload,
        b"private-provider-object-id-collision",
        b"private-account-id-7",
        b"private-order-id-7",
        b"private-subscription-id-7",
        result.batch_manifest.batch_id.encode(),
        result.prepared_receipt.batch_manifest_digest.encode(),
        result.provider_result.candidate_cursor_digest.encode(),
    )
    assert tuple(receipt.outcome for receipt in result.public_evidence_receipts) == (
        "prepared",
        "acknowledged",
    )
    assert all(needle not in public_payload for needle in private_needles)


@pytest.mark.parametrize(
    ("limit_kind", "expected_ceiling"),
    (("records", 2), ("encoded_bytes", 1)),
)
def test_shared_ceiling_refusal_aborts_before_completion_without_a_destination(
    limit_kind: Literal["records", "encoded_bytes"],
    expected_ceiling: int,
) -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        build_plan4a_application_source_fixture,
        exercise_source_ceiling_refusal,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="postgresql")
    provider = FakeSourceProvider(fixture)
    destination_guard = DestinationProviderResolutionGuard()

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        exercise_source_ceiling_refusal(
            provider,
            fixture,
            limit_kind=limit_kind,
            destination_provider_resolver=destination_guard,
        )

    assert captured.value.limit_kind == limit_kind
    assert captured.value.ceiling == expected_ceiling
    assert len(provider.opened_sessions) == 1
    assert provider.opened_sessions[0].abort_count == 1
    assert provider.opened_sessions[0].completion_count == 0
    assert destination_guard.resolved_provider_refs == ()


def test_shared_replay_rejects_changed_canonical_source_content() -> None:
    from tests.conformance.source_acquisition import (
        DestinationProviderResolutionGuard,
        FakeSourceProvider,
        build_plan4a_application_source_fixture,
        run_shared_source_conformance,
    )

    fixture = build_plan4a_application_source_fixture(provider_kind="stripe")
    changed_order = fixture.records[1].model_copy(
        update={
            "fields": tuple(
                field.model_copy(update={"value": Decimal("126.00")})
                if field.name == "amount"
                else field
                for field in fixture.records[1].fields
            )
        }
    )
    replay_fixture = replace(
        fixture,
        records=(fixture.records[0], changed_order, fixture.records[2]),
    )
    provider = FakeSourceProvider(fixture, replay_fixture=replay_fixture)

    with pytest.raises(SourceConformanceError, match="non_deterministic_replay"):
        run_shared_source_conformance(
            provider,
            fixture,
            destination_provider_resolver=DestinationProviderResolutionGuard(),
        )

    assert len(provider.opened_sessions) == 2
