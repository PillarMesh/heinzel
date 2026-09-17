"""A fresh PostgreSQL transaction through the composed acquisition path, on the pinned engine.

The acceptance ledger's acquisition row recorded that no fresh PostgreSQL transaction had crossed
the composed path: the only end-to-end acquisition journey ran against an in-process fake database.
This journey drives the product's composition, `compose_acquisition_application` with the durable
state repository, artifact store and evidence writer, against real source rows in the digest-pinned
PostgreSQL 18.6 image.

The activated contract it runs is itself governed: it is activated through the intent-bound service
from a product intent approval recorded in request management, and the application resolves it from
contract-service's lifecycle store.

It proves that a fresh run produces a prepared receipt, a batch manifest, segment artifacts and
evidence; that nothing advances the checkpoint until a consumer acknowledges; that replaying the
same run writes no new artifacts and returns the same preparation; that acknowledgement advances the
checkpoint exactly once; and that a binding whose credentials are wrong acquires nothing and
records a failed evidence receipt.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from pillarmesh_console.governed_adapters import GovernedApprovedProductIntentSources
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_contract_service import (
    AcquisitionActivationApproval,
    ProductIntentBoundActivationService,
    SQLiteAcquisitionContractLifecycleRepository,
    ValidatedSourceBinding,
)
from pillarmesh_evidence import SQLiteAcquisitionEvidenceWriter, SQLiteStore
from pillarmesh_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceObjectDeclaration,
)
from pillarmesh_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionSourceObservation,
    SourceObservationRequest,
    acquisition_intent_key,
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
    AcquisitionDeclaredActivation,
    AcquisitionRunner,
    AcquisitionRuntimeError,
    acquisition_run_now_reference,
    activated_contract_resolver,
    compose_acquisition_application,
    compose_activated_acquisition_contract,
    opaque_reference_factory,
    source_binding_resolver,
)
from pillarmesh_state import (
    AcquisitionStateNotFoundError,
    LocalAcquisitionArtifactStore,
    SQLiteAcquisitionStateRepository,
)
from pydantic import SecretStr

from tests.acceptance.run_plan4a import (
    _CONSUMER_REF,
    _CursorCipher,
    _MutableClock,
    _StrictAcknowledgementConsumer,
)
from tests.integration.test_postgresql_checked_sum_evidence import _pinned_postgresql
from tests.integration.test_postgresql_product_materialization_live import (
    _provision_cluster,
    _role_dsn,
)

_RUN_LIVE = os.environ.get("PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_TENANT = "tenant-live-a"
_BINDING_REF = "source-live-a"
_CONTRACT_REF = "acquisition-contract-sales"
_TRIGGER_WINDOW = "2026-09-16T00:00:00Z/P1D"
_CAPABILITY_DIGEST = "c" * 64
_NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)
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


def _approved_intent(
    requests: RequestManagementService, approvals: ProductIntentApprovalService
) -> ApprovedProductIntent:
    request = requests.submit_question(
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
    return approved


def _provider(dsn: str, clock: _MutableClock) -> PostgreSQLAcquisitionProvider:
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


@contextmanager
def _closing_all(*resources: object) -> Iterator[None]:
    try:
        yield
    finally:
        for resource in resources:
            close = getattr(resource, "close", None)
            if close is not None:
                close()


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_fresh_postgresql_rows_cross_the_composed_acquisition_path(tmp_path: Path) -> None:
    clock = _MutableClock(_NOW)
    with _pinned_postgresql() as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=secrets.token_urlsafe(24),
            materialization_password=secrets.token_urlsafe(24),
        )
        acquisition_dsn = _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password)
        provider = _provider(acquisition_dsn, clock)
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
        evidence_store = SQLiteStore.open(tmp_path / "acquisition-evidence.sqlite")
        artifact_root = tmp_path / "artifacts"
        artifacts = LocalAcquisitionArtifactStore(artifact_root)
        with _closing_all(request_repository, lifecycles, state, evidence_store):
            # Govern the contract: approve the intent, then activate through the intent-bound gate.
            approvals = ProductIntentApprovalService(
                request_repository, clock=clock, authority=_GrantingAuthority()
            )
            approval = _approved_intent(
                RequestManagementService(request_repository, clock=clock), approvals
            )
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
                    acknowledgement_consumer_ref=_CONSUMER_REF,
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

            provider_resolutions: list[str] = []
            active_provider = {"value": provider}

            def resolve_provider(
                resolved: SourceConnectionBinding,
            ) -> PostgreSQLAcquisitionProvider:
                provider_resolutions.append(resolved.binding_id)
                return active_provider["value"]

            class _Bindings:
                def load(self, tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
                    if tenant_id != _TENANT or binding_ref != _BINDING_REF:
                        raise KeyError("binding not found")
                    return binding

            def resolve_observation(tenant_id: str, ref: str) -> AcquisitionSourceObservation:
                if (tenant_id, ref) != (_TENANT, observation_ref):
                    raise KeyError("observation not found")
                return observation

            bindings = _Bindings()
            evidence_writer = SQLiteAcquisitionEvidenceWriter(evidence_store)
            application = compose_acquisition_application(
                contract_repository=lifecycles,
                binding_repository=bindings,
                observation_resolver=resolve_observation,
                provider_resolver=resolve_provider,
                state_store=state,
                artifact_store=artifacts,
                evidence_writer=evidence_writer,
                reference_factory=references,
                clock=clock,
            )

            first = application.run_now(
                tenant_id=_TENANT,
                contract_ref=_CONTRACT_REF,
                trigger_window=_TRIGGER_WINDOW,
                acquisition_mode="snapshot",
            )
            artifacts_after_first = sorted(p for p in artifact_root.rglob("*") if p.is_file())
            resolutions_after_first = len(provider_resolutions)
            with pytest.raises(AcquisitionStateNotFoundError):
                state.load_checkpoint(_TENANT, contract.contract_digest, _BINDING_REF)

            replay = application.run_now(
                tenant_id=_TENANT,
                contract_ref=_CONTRACT_REF,
                trigger_window=_TRIGGER_WINDOW,
                acquisition_mode="snapshot",
            )
            artifacts_after_replay = sorted(p for p in artifact_root.rglob("*") if p.is_file())
            resolutions_at_replay = len(provider_resolutions)

            assert first.prepared_receipt is not None and first.batch_manifest is not None
            intent = AcquisitionIntent(
                intent_key=first.prepared_receipt.intent_key,
                tenant_id=_TENANT,
                run_intent_ref=acquisition_run_now_reference(
                    record=record, trigger_window=_TRIGGER_WINDOW
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
            assert intent.intent_key == acquisition_intent_key(
                tenant_id=_TENANT,
                run_intent_ref=intent.run_intent_ref,
                contract_digest=contract.contract_digest,
                source_binding_ref=_BINDING_REF,
                acquisition_mode="snapshot",
                object_refs=("sales",),
                prior_checkpoint_revision=0,
            )
            consumer = _StrictAcknowledgementConsumer(
                artifacts, clock=clock, reference_factory=references
            )
            acknowledgement = consumer.acknowledge(intent, first)
            # The composed application exposes preparation only; acknowledgement is the runner role
            # the landing coordinator consumes, composed here over the same durable stores.
            acknowledger = AcquisitionRunner(
                binding_resolver=source_binding_resolver(bindings),
                contract_resolver=lambda tenant_id, contract_ref: (
                    activated_contract_resolver(lifecycles)(tenant_id, contract_ref).contract
                ),
                observation_resolver=resolve_observation,
                provider_resolver=resolve_provider,
                state_store=state,
                artifact_store=artifacts,
                evidence_writer=evidence_writer,
                reference_factory=references,
                clock=clock,
            )
            checkpoint = acknowledger.acknowledge(intent, acknowledgement)
            acknowledgement_replay = acknowledger.acknowledge(intent, acknowledgement)
            acquired = consumer.records_for(first.prepared_receipt.batch_id)

            # Denied credentials: the same binding resolved to a provider with the wrong password.
            active_provider["value"] = _provider(
                _role_dsn(bootstrap_dsn, "acquisition_runtime", "not-the-password"), clock
            )
            artifacts_before_denied = sorted(p for p in artifact_root.rglob("*") if p.is_file())
            with pytest.raises(AcquisitionRuntimeError) as denied:
                application.run_now(
                    tenant_id=_TENANT,
                    contract_ref=_CONTRACT_REF,
                    trigger_window="2026-09-17T00:00:00Z/P1D",
                    acquisition_mode="incremental",
                )
            artifacts_after_denied = sorted(p for p in artifact_root.rglob("*") if p.is_file())
            receipts = evidence_store.list_acquisition_receipts(_TENANT)
            checkpoint_after_denied = state.load_checkpoint(
                _TENANT, contract.contract_digest, _BINDING_REF
            )

        assert record.contract.product_intent_ref == approval.artifact_reference
        assert first.evidence.outcome == "prepared"
        assert first.batch_manifest.total_record_count == 3
        assert len(acquired) == 3
        assert artifacts_after_first
        # Replay returns the stored preparation without reaching the source; each attempt is still
        # recorded as its own evidence receipt pointing at the same prepared receipt.
        assert replay.prepared_receipt == first.prepared_receipt
        assert replay.batch_manifest == first.batch_manifest
        assert replay.evidence.prepared_receipt_ref == first.evidence.prepared_receipt_ref
        assert replay.evidence.evidence_id != first.evidence.evidence_id
        assert artifacts_after_replay == artifacts_after_first
        assert resolutions_at_replay == resolutions_after_first
        assert (checkpoint.previous_revision, checkpoint.committed_revision) == (0, 1)
        assert acknowledgement_replay == checkpoint
        assert "password" not in str(denied.value).lower()
        assert acquisition_password not in str(denied.value)
        assert artifacts_after_denied == artifacts_before_denied
        assert checkpoint_after_denied.revision == 1
        # The denied attempt is recorded, not silent: a failed receipt with a public reason only.
        (denied_receipt,) = [r for r in receipts if r.acquisition_mode == "incremental"]
        # Every attempt, replays included, leaves its own receipt.
        assert sorted((r.acquisition_mode, r.outcome) for r in receipts) == [
            ("incremental", "failed"),
            ("snapshot", "acknowledged"),
            ("snapshot", "acknowledged"),
            ("snapshot", "prepared"),
            ("snapshot", "prepared"),
        ]
        assert denied_receipt.outcome == "failed"
        assert denied_receipt.acquisition_mode == "incremental"
        assert denied_receipt.prepared_receipt_ref is None
        assert acquisition_password not in denied_receipt.model_dump_json()


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1")
@pytest.mark.xfail(
    strict=True,
    reason=(
        "libpq reports a rejected password as OperationalError without a SQLSTATE, so the "
        "acquisition provider classifies it as transient provider_unavailable; the warehouse "
        "provider's startup denial probe recovers the structured 28P01 and is not yet adopted here"
    ),
)
def test_rejected_source_credentials_are_classified_as_authorization_denied() -> None:
    clock = _MutableClock(_NOW)
    with _pinned_postgresql() as bootstrap_dsn:
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=secrets.token_urlsafe(24),
            landing_password=secrets.token_urlsafe(24),
            materialization_password=secrets.token_urlsafe(24),
        )
        provider = _provider(
            _role_dsn(bootstrap_dsn, "acquisition_runtime", "not-the-password"), clock
        )
        with pytest.raises(AcquisitionProviderError) as denied:
            provider.observe_source(
                SourceObservationRequest(
                    tenant_id=_TENANT, source_binding_ref=_BINDING_REF, object_refs=("sales",)
                )
            )

    assert denied.value.classification == "authorization_denied"
