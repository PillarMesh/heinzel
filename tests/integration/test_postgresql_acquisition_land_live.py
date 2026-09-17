"""A composed acquisition batch lands in PostgreSQL and only then advances its checkpoint.

The acceptance ledger's LAND row recorded that LAND had been proved on its own, against
hand-built segments, while the composed acquisition checkpoint journey remained open. This journey
closes that seam on the digest-pinned PostgreSQL 18.6 image: fresh source rows are prepared by
`compose_acquisition_application` under a contract activated through the intent-bound service, and
`AcquisitionLandingCoordinator` reads the verified batch artifacts, lands each segment through
`DestinationLandingRuntime` into the raw table, and acknowledges the checkpoint with a consumer
receipt bound to those LAND receipts.

It proves that the checkpoint does not move while LAND is refused, that one committed LAND is the
only thing that moves it, that replaying the landed batch writes no rows or receipts and returns
the same result, and what the raw table actually holds.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from pillarmesh_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from pillarmesh_console.governed_adapters import GovernedApprovedProductIntentSources
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_contract_service import (
    AcquisitionActivationApproval,
    ActivatedAcquisitionContractRecord,
    ProductIntentBoundActivationService,
    SQLiteAcquisitionContractLifecycleRepository,
    ValidatedSourceBinding,
)
from pillarmesh_evidence import SQLiteAcquisitionEvidenceWriter, SQLiteStore
from pillarmesh_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLSourceObjectDeclaration,
)
from pillarmesh_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionSourceObservation,
    ProviderError,
    SourceObservationRequest,
)
from pillarmesh_request_management import (
    ApprovedProductIntent,
    DeliveryIntent,
    DimensionIntent,
    FreshnessObjective,
    Grain,
    MeasureIntent,
    ProductIntent,
    ProductIntentApprovalService,
    ProductIntentAuthorityRefs,
    ProductIntentConstraints,
    RequestManagementService,
    SQLiteRequestRepository,
)
from pillarmesh_runtime import (
    AcquisitionApplication,
    AcquisitionDeclaredActivation,
    AcquisitionDestinationRouting,
    AcquisitionLandingApplication,
    AcquisitionObjectRoute,
    AcquisitionPreparationResult,
    AcquisitionRunner,
    DestinationLandingRuntime,
    GenerationLedger,
    acquisition_run_now_reference,
    activated_contract_resolver,
    compose_acquisition_application,
    compose_acquisition_landing,
    compose_activated_acquisition_contract,
    landing_contract_resolver,
    opaque_reference_factory,
    source_binding_resolver,
)
from pillarmesh_state import (
    AcquisitionStateNotFoundError,
    LocalAcquisitionArtifactStore,
    SQLiteAcquisitionStateRepository,
)
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from pydantic import SecretStr

from tests.acceptance.run_plan4a import _CursorCipher, _MutableClock
from tests.integration.test_postgresql_checked_sum_evidence import _pinned_postgresql
from tests.integration.test_postgresql_product_materialization_live import (
    _provision_cluster,
    _role_dsn,
)

_RUN_LIVE = os.environ.get("PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_TENANT = "tenant-live-a"
_BINDING_REF = "source-live-a"
_DESTINATION_BINDING_REF = "destination-live-a"
_CONTRACT_REF = "acquisition-contract-sales"
_TRIGGER_WINDOW = "2026-09-17T00:00:00Z/P1D"
_CAPABILITY_DIGEST = "c" * 64
_NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
_FIELDS = (
    AcquisitionField(name="sale_id", value_type="integer", nullable=False),
    AcquisitionField(name="region", value_type="string", nullable=False),
    AcquisitionField(name="customer_id", value_type="integer", nullable=False),
    AcquisitionField(name="revenue", value_type="decimal", nullable=False),
    AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
)


class _GrantingAuthority:
    """Constraints for the approval; approval authority is covered in request-management tests."""

    def resolve_constraints(self, **_: object) -> ProductIntentConstraints:
        return ProductIntentConstraints(
            approved_source_refs=(_BINDING_REF,),
            approved_metric_refs=("total-revenue",),
            approved_dimension_refs=("region",),
            minimum_source_interval_seconds=86_400,
        )


@dataclass(frozen=True)
class _ComposedAcquisition:
    application: AcquisitionApplication
    acknowledger: AcquisitionRunner
    record: ActivatedAcquisitionContractRecord
    lifecycles: SQLiteAcquisitionContractLifecycleRepository
    state: SQLiteAcquisitionStateRepository
    artifacts: LocalAcquisitionArtifactStore
    evidence: SQLiteStore


def _source_provider(dsn: str, clock: _MutableClock) -> PostgreSQLAcquisitionProvider:
    return PostgreSQLAcquisitionProvider(
        PostgreSQLAcquisitionSettings(
            dsn=SecretStr(dsn),
            connection_handle="native-live-source",
            objects=(
                PostgreSQLSourceObjectDeclaration(
                    logical_object_ref="sales",
                    schema_name="source_data",
                    table_name="sales",
                    field_names=tuple(field.name for field in _FIELDS),
                    key_name="sale_id",
                    source_updated_at_field="updated_at",
                ),
            ),
            unrelated_schema_name="private_admin",
            max_write_transaction_duration=timedelta(minutes=5),
        ),
        clock=clock,
        private_boundary_reference_factory=lambda tenant, reference: (
            f"private://{tenant}/{reference}"
        ),
        private_boundary_writer=lambda _tenant, _reference, _payload: None,
    )


def _approve(
    repository: SQLiteRequestRepository, clock: _MutableClock
) -> tuple[ProductIntentApprovalService, ApprovedProductIntent]:
    approvals = ProductIntentApprovalService(
        repository, clock=clock, authority=_GrantingAuthority()
    )
    request = RequestManagementService(repository, clock=clock).submit_question(
        tenant_id=_TENANT,
        requester_id="requester-live",
        purpose="Regional revenue",
        question="What is revenue by region?",
    )
    approved = approvals.approve(
        tenant_id=_TENANT,
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-live",
        intent=ProductIntent(
            request_id=request.request_id,
            title="Revenue by region",
            business_outcome="Regional revenue leaders see governed revenue.",
            source_refs=(_BINDING_REF,),
            grain=Grain(keys=("region",)),
            measures=(MeasureIntent(metric_ref="total-revenue", aggregation="sum"),),
            dimensions=(DimensionIntent(dimension_ref="region"),),
            filters=(),
            freshness=FreshnessObjective(maximum_age_seconds=86_400),
            delivery=DeliveryIntent(outputs=("dataset",)),
        ),
        authority_refs=ProductIntentAuthorityRefs(
            semantic_version=ArtifactReference(artifact_id="semantic", version=1, digest="1" * 64),
            source_observations=(
                ArtifactReference(artifact_id="observation", version=1, digest="2" * 64),
            ),
        ),
    )
    assert isinstance(approved, ApprovedProductIntent)
    return approvals, approved


@contextmanager
def _composed_acquisition(
    tmp_path: Path, source_dsn: str, clock: _MutableClock
) -> Iterator[_ComposedAcquisition]:
    """Activate a governed contract whose acknowledging consumer is the destination binding."""
    provider = _source_provider(source_dsn, clock)
    observation = provider.observe_source(
        SourceObservationRequest(
            tenant_id=_TENANT, source_binding_ref=_BINDING_REF, object_refs=("sales",)
        )
    )
    observation_ref = "source-observation-live-sales"
    binding = SourceConnectionBinding(
        binding_id=_BINDING_REF,
        tenant_id=_TENANT,
        provider_kind="postgresql",
        connection_handle="native-live-source",
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.READY,
        approved_object_refs=("sales",),
        capability_profile_digest=_CAPABILITY_DIGEST,
        source_observation_ref=observation_ref,
        credential_revision=1,
        revision=1,
        created_at=_NOW,
        updated_at=_NOW,
    )
    request_repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    lifecycles = SQLiteAcquisitionContractLifecycleRepository(
        str(tmp_path / "acquisition-lifecycle.sqlite3")
    )
    references = opaque_reference_factory()
    state = SQLiteAcquisitionStateRepository(
        str(tmp_path / "acquisition-state.sqlite"),
        cipher=_CursorCipher(),
        reference_factory=references,
    )
    evidence = SQLiteStore.open(tmp_path / "acquisition-evidence.sqlite")
    artifacts = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    try:
        approvals, approval = _approve(request_repository, clock)
        lifecycle = lifecycles.activate(
            tenant_id=_TENANT, contract_digest="d" * 64, activated_at=_NOW
        )
        contract = compose_activated_acquisition_contract(
            lifecycle=lifecycle,
            binding=binding,
            observation=observation,
            declared=AcquisitionDeclaredActivation(
                contract_ref=_CONTRACT_REF,
                process_package_ref=ArtifactReference(
                    artifact_id="process-revenue", version=1, digest="1" * 64
                ),
                product_intent_ref=approval.artifact_reference,
                destination_product_ref="product-revenue",
                acknowledgement_consumer_ref=_DESTINATION_BINDING_REF,
                object_schemas=(
                    AcquisitionObjectSchema(
                        logical_object_ref="sales",
                        schema_digest=digest(_FIELDS),
                        fields=_FIELDS,
                        record_key_fields=("sale_id",),
                        source_updated_at_field="updated_at",
                    ),
                ),
                record_ceiling=100,
                encoded_byte_ceiling=1_000_000,
                activated_by="architect-live",
                activated_at=_NOW,
            ),
        )
        record = ProductIntentBoundActivationService(
            lifecycles, product_intents=GovernedApprovedProductIntentSources(approvals)
        ).activate(
            idempotency_key="activate-sales-live",
            contract=contract,
            approval=AcquisitionActivationApproval(
                tenant_id=_TENANT,
                process_package_ref=contract.process_package_ref,
                product_intent_ref=contract.product_intent_ref,
                destination_product_ref=contract.destination_product_ref,
                approved_by=contract.activated_by,
                approved_at=_NOW,
            ),
            source_validation=ValidatedSourceBinding(
                tenant_id=_TENANT,
                source_binding_ref=binding.binding_id,
                source_binding_revision=binding.revision,
                credential_revision=binding.credential_revision,
                capability_profile_digest=_CAPABILITY_DIGEST,
                source_observation_ref=contract.source_observation_ref,
                source_observation_digest=contract.source_observation_digest,
                validated_at=_NOW,
            ),
        )
        state.activate_contract_authority(_TENANT, contract.contract_digest)

        class _Bindings:
            def load(self, tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
                if (tenant_id, binding_ref) != (_TENANT, _BINDING_REF):
                    raise KeyError("binding not found")
                return binding

        def resolve_observation(tenant_id: str, ref: str) -> AcquisitionSourceObservation:
            if (tenant_id, ref) != (_TENANT, observation_ref):
                raise KeyError("observation not found")
            return observation

        bindings = _Bindings()
        evidence_writer = SQLiteAcquisitionEvidenceWriter(evidence)
        application = compose_acquisition_application(
            contract_repository=lifecycles,
            binding_repository=bindings,
            observation_resolver=resolve_observation,
            provider_resolver=lambda _binding: provider,
            state_store=state,
            artifact_store=artifacts,
            evidence_writer=evidence_writer,
            reference_factory=references,
            clock=clock,
        )
        # The composed application exposes preparation only; the acknowledging runner is the
        # role the landing coordinator consumes, composed over the same durable stores.
        acknowledger = AcquisitionRunner(
            binding_resolver=source_binding_resolver(bindings),
            contract_resolver=lambda tenant_id, contract_ref: (
                activated_contract_resolver(lifecycles)(tenant_id, contract_ref).contract
            ),
            observation_resolver=resolve_observation,
            provider_resolver=lambda _binding: provider,
            state_store=state,
            artifact_store=artifacts,
            evidence_writer=evidence_writer,
            reference_factory=references,
            clock=clock,
        )
        yield _ComposedAcquisition(
            application=application,
            acknowledger=acknowledger,
            record=record,
            lifecycles=lifecycles,
            state=state,
            artifacts=artifacts,
            evidence=evidence,
        )
    finally:
        for resource in (request_repository, lifecycles, state, evidence):
            resource.close()


def _intent(
    composed: _ComposedAcquisition, preparation: AcquisitionPreparationResult
) -> AcquisitionIntent:
    assert preparation.prepared_receipt is not None
    contract = composed.record.contract
    return AcquisitionIntent(
        intent_key=preparation.prepared_receipt.intent_key,
        tenant_id=_TENANT,
        run_intent_ref=acquisition_run_now_reference(
            record=composed.record, trigger_window=_TRIGGER_WINDOW
        ),
        contract_ref=_CONTRACT_REF,
        contract_digest=contract.contract_digest,
        source_binding_ref=_BINDING_REF,
        source_observation_digest=contract.source_observation_digest,
        acquisition_mode="snapshot",
        object_refs=("sales",),
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=contract.record_ceiling,
        encoded_byte_ceiling=contract.encoded_byte_ceiling,
        admitted_at=_NOW,
    )


class _DestinationBindings:
    def __init__(self) -> None:
        self.state = WarehouseBindingState.READY

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if (tenant_id, binding_id) != (_TENANT, _DESTINATION_BINDING_REF):
            return None
        return WarehouseBinding(
            binding_id=_DESTINATION_BINDING_REF,
            tenant_id=_TENANT,
            engine_kind=EngineKind.POSTGRESQL,
            region="local",
            capability_profile_digest="e" * 64,
            lifecycle_state=self.state,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )


def _landing(
    composed: _ComposedAcquisition,
    landing_dsn: str,
    destination_bindings: _DestinationBindings,
    ledger: GenerationLedger,
    clock: _MutableClock,
) -> AcquisitionLandingApplication:
    """The product's own LAND composition: contract authority plus the deployment's routing."""

    def destination_provider(_binding: WarehouseBinding) -> PostgreSQLDestinationProvider:
        return PostgreSQLDestinationProvider(
            store=PostgreSQLLandStore(
                PostgreSQLLandStoreSettings(
                    dsn=SecretStr(landing_dsn),
                    raw_schema_name="raw",
                    ledger_schema_name="land_control",
                    ledger_table_name="land_receipts",
                )
            )
        )

    return compose_acquisition_landing(
        contract_resolver=landing_contract_resolver(composed.lifecycles),
        routing=AcquisitionDestinationRouting(
            tenant_id=_TENANT,
            contract_ref=_CONTRACT_REF,
            destination_binding_ref=_DESTINATION_BINDING_REF,
            routes=(AcquisitionObjectRoute(logical_object_ref="sales", table_ref="raw_sales"),),
        ),
        landing=DestinationLandingRuntime(
            binding_authority=destination_bindings,
            provider_factories={EngineKind.POSTGRESQL: destination_provider},
            ledger=ledger,
            clock=clock,
        ),
        artifact_store=composed.artifacts,
        acknowledger=composed.acknowledger,
    )


def _raw_rows(bootstrap_dsn: str) -> list[tuple[str, int, dict[str, object]]]:
    with psycopg.connect(bootstrap_dsn) as connection:
        return [
            (str(generation_id), int(ordinal), payload)
            for generation_id, ordinal, payload in connection.execute(
                "SELECT generation_id, row_ordinal, payload FROM raw.raw_sales "
                "ORDER BY generation_id, row_ordinal"
            ).fetchall()
        ]


def _land_receipt_count(bootstrap_dsn: str) -> int:
    with psycopg.connect(bootstrap_dsn) as connection:
        row = connection.execute("SELECT count(*) FROM land_control.land_receipts").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_composed_acquisition_batch_lands_once_before_its_checkpoint_advances(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(_NOW)
    with _pinned_postgresql() as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        landing_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=landing_password,
            materialization_password=secrets.token_urlsafe(24),
        )
        with _composed_acquisition(
            tmp_path,
            _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password),
            clock,
        ) as composed:
            contract = composed.record.contract
            preparation = composed.application.run_now(
                tenant_id=_TENANT,
                contract_ref=_CONTRACT_REF,
                trigger_window=_TRIGGER_WINDOW,
                acquisition_mode="snapshot",
            )
            intent = _intent(composed, preparation)
            destination_bindings = _DestinationBindings()
            ledger = GenerationLedger.in_memory()
            landing_dsn = _role_dsn(bootstrap_dsn, "landing_runtime", landing_password)

            # A destination binding that is not ready refuses LAND; nothing moves.
            destination_bindings.state = WarehouseBindingState.SUSPENDED
            with pytest.raises(ProviderError) as refused:
                asyncio.run(
                    _landing(composed, landing_dsn, destination_bindings, ledger, clock).land(
                        trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation
                    )
                )
            rows_after_refusal = _raw_rows(bootstrap_dsn)
            with pytest.raises(AcquisitionStateNotFoundError):
                composed.state.load_checkpoint(_TENANT, contract.contract_digest, _BINDING_REF)

            destination_bindings.state = WarehouseBindingState.READY
            land_application = _landing(composed, landing_dsn, destination_bindings, ledger, clock)
            landed = asyncio.run(
                land_application.land(
                    trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation
                )
            )
            rows_after_land = _raw_rows(bootstrap_dsn)
            receipts_after_land = _land_receipt_count(bootstrap_dsn)
            checkpoint = composed.state.load_checkpoint(
                _TENANT, contract.contract_digest, _BINDING_REF
            )

            replayed = asyncio.run(
                land_application.land(
                    trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation
                )
            )
            rows_after_replay = _raw_rows(bootstrap_dsn)
            with psycopg.connect(bootstrap_dsn) as connection:
                top_level_region_types = connection.execute(
                    "SELECT DISTINCT pg_catalog.jsonb_typeof(payload OPERATOR(pg_catalog.->) "
                    "'region') FROM raw.raw_sales"
                ).fetchall()
            receipts_after_replay = _land_receipt_count(bootstrap_dsn)
            evidence = composed.evidence.list_acquisition_receipts(_TENANT)

    assert refused.value.classification == "authorization_denied"
    assert rows_after_refusal == []

    assert preparation.batch_manifest is not None
    (landing,) = landed.landings
    assert landing.receipt.record_count == 3
    assert landing.receipt.destination_binding_ref == _DESTINATION_BINDING_REF
    assert landed.checkpoint_receipt.batch_id == preparation.batch_manifest.batch_id
    assert (
        landed.checkpoint_receipt.previous_revision,
        landed.checkpoint_receipt.committed_revision,
    ) == (0, 1)
    assert checkpoint.revision == 1
    assert checkpoint.last_batch_id == preparation.batch_manifest.batch_id

    assert receipts_after_land == 1
    assert {generation for generation, _, _ in rows_after_land} == {landing.generation_key}
    assert [ordinal for _, ordinal, _ in rows_after_land] == [0, 1, 2]

    assert replayed == landed
    assert rows_after_replay == rows_after_land
    assert receipts_after_replay == receipts_after_land
    assert [receipt.outcome for receipt in evidence].count("acknowledged") == 2

    # LAND stores the canonical acquisition record: values sit in a `fields` array, not under
    # top-level keys. The compiler's generation-scoped decode reads `payload -> '<field>'`, so it
    # finds no JSON string in any composed row and would refuse the generation. The source-to-answer
    # journey lands flattened field objects by hand, which hides this seam.
    first_payload = rows_after_land[0][2]
    assert set(first_payload) == {
        "fields",
        "logical_object_ref",
        "operation",
        "record_key",
        "schema_version",
        "source_created_at",
        "source_updated_at",
    }
    assert first_payload["fields"][1] == {"name": "region", "value": "west"}  # type: ignore[index]
    assert top_level_region_types == [(None,)]
