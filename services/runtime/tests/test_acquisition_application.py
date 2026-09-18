from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import ActivatedAcquisitionContractRecord
from heinzel_evidence import AcquisitionEvidenceReceipt
from heinzel_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionNoValidPlan,
    AcquisitionObjectSchema,
    acquisition_intent_key,
)
from heinzel_runtime import (
    AcquisitionApplication,
    AcquisitionContractError,
    AcquisitionOwnershipError,
    AcquisitionPreparationResult,
    ActivatedAcquisitionContract,
    acquisition_run_now_reference,
    prepare_acquisition,
)

_NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
_TENANT = "tenant-a"
_CONTRACT_REF = "contract:orders:v4"
_CONTRACT_DIGEST = "c" * 64
_BINDING_REF = "source-binding:orders"
_OBSERVATION_DIGEST = "d" * 64


def _contract() -> ActivatedAcquisitionContract:
    fields = (AcquisitionField(name="order_id", value_type="integer", nullable=False),)
    schema = AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("order_id",),
        source_updated_at_field=None,
    )
    return ActivatedAcquisitionContract(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        contract_digest=_CONTRACT_DIGEST,
        process_package_ref=ArtifactReference(
            artifact_id="process:orders", version=2, digest="1" * 64
        ),
        product_intent_ref=ArtifactReference(
            artifact_id="intent:orders", version=3, digest="2" * 64
        ),
        destination_product_ref="product:orders",
        source_binding_ref=_BINDING_REF,
        source_binding_revision=7,
        credential_revision=2,
        acknowledgement_consumer_ref="land:raw-orders",
        capability_profile_digest="a" * 64,
        source_observation_ref="observation:orders:v3",
        source_observation_digest=_OBSERVATION_DIGEST,
        lifecycle_state="activated",
        acquisition_modes=("incremental", "snapshot"),
        object_schemas=(schema,),
        record_ceiling=1_000,
        encoded_byte_ceiling=1_000_000,
        activated_by="architect-a",
        activated_at=_NOW,
    )


def _record(*, revision: int = 4, tenant_id: str = _TENANT) -> ActivatedAcquisitionContractRecord:
    contract = _contract().model_copy(update={"tenant_id": tenant_id})
    return ActivatedAcquisitionContractRecord(
        tenant_id=contract.tenant_id,
        contract_ref=contract.contract_ref,
        revision=revision,
        contract_digest=contract.contract_digest,
        contract_artifact_ref=ArtifactReference(
            artifact_id=contract.contract_ref,
            version=revision,
            digest=digest(contract),
        ),
        activated_by=contract.activated_by,
        activated_at=contract.activated_at,
        contract=contract,
    )


def _prepared_result(intent: AcquisitionIntent) -> AcquisitionPreparationResult:
    outcome = AcquisitionNoValidPlan(
        reason_codes=("acquisition_mode_not_admitted",),
        failed_constraints=("fixture",),
    )
    return AcquisitionPreparationResult(
        evidence=AcquisitionEvidenceReceipt(
            evidence_id="evidence:fixture",
            tenant_id=intent.tenant_id,
            run_intent_ref=intent.run_intent_ref,
            contract_ref=intent.contract_ref,
            source_binding_ref=intent.source_binding_ref,
            acquisition_mode=intent.acquisition_mode,
            logical_object_refs=intent.object_refs,
            prepared_receipt_ref=None,
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            resulting_checkpoint_revision=None,
            reason_codes=("acquisition_mode_not_admitted",),
            outcome="no_valid_plan",
            created_at=_NOW,
        ),
        prepared_receipt=None,
        batch_manifest=None,
        governed_outcome=outcome,
    )


class _RecordingRunner:
    def __init__(self) -> None:
        self.intents: list[AcquisitionIntent] = []
        self.acknowledgement_calls = 0

    def prepare(self, intent: AcquisitionIntent) -> AcquisitionPreparationResult:
        self.intents.append(intent)
        return _prepared_result(intent)

    def acknowledge(self, *_args: object) -> object:
        self.acknowledgement_calls += 1
        raise AssertionError("run now must wait for LAND acknowledgement")


def test_prepare_acquisition_uses_the_existing_runner_path() -> None:
    runner = _RecordingRunner()
    contract = _contract()
    run_intent_ref = "1" * 64
    intent = AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id=_TENANT,
            run_intent_ref=run_intent_ref,
            contract_digest=contract.contract_digest,
            source_binding_ref=contract.source_binding_ref,
            acquisition_mode="snapshot",
            object_refs=("orders",),
            prior_checkpoint_revision=0,
        ),
        tenant_id=_TENANT,
        run_intent_ref=run_intent_ref,
        contract_ref=contract.contract_ref,
        contract_digest=contract.contract_digest,
        source_binding_ref=contract.source_binding_ref,
        source_observation_digest=contract.source_observation_digest,
        acquisition_mode="snapshot",
        object_refs=("orders",),
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=contract.record_ceiling,
        encoded_byte_ceiling=contract.encoded_byte_ceiling,
        admitted_at=_NOW,
    )

    result = prepare_acquisition(intent=intent, runner=runner)

    assert result == _prepared_result(intent)
    assert runner.intents == [intent]


def test_run_now_reference_is_canonical_stable_and_tenant_scoped() -> None:
    record = _record()
    first = acquisition_run_now_reference(
        record=record,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
    )
    replay = acquisition_run_now_reference(
        record=record,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
    )
    other_contract = record.model_copy(
        update={
            "tenant_id": "tenant-b",
            "contract": record.contract.model_copy(update={"tenant_id": "tenant-b"}),
        }
    )
    other_tenant = acquisition_run_now_reference(
        record=other_contract,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
    )

    assert first == replay
    assert first != other_tenant
    assert len(first) == 64


def test_run_now_composes_exact_current_authority_and_prepares_it() -> None:
    record = _record()
    runner = _RecordingRunner()
    contract_requests: list[tuple[str, str]] = []
    checkpoint_requests: list[tuple[str, str, str]] = []

    def resolve_contract(tenant_id: str, contract_ref: str) -> ActivatedAcquisitionContractRecord:
        contract_requests.append((tenant_id, contract_ref))
        return record

    def resolve_checkpoint(
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> tuple[int, str | None]:
        checkpoint_requests.append((tenant_id, contract_digest, source_binding_ref))
        return 0, None

    application = AcquisitionApplication(
        runner=runner,
        contract_resolver=resolve_contract,
        checkpoint_resolver=resolve_checkpoint,
        clock=lambda: _NOW,
    )

    result = application.run_now(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
        acquisition_mode="snapshot",
    )

    assert result == _prepared_result(runner.intents[0])
    assert contract_requests == [(_TENANT, _CONTRACT_REF)]
    assert checkpoint_requests == [(_TENANT, _CONTRACT_DIGEST, _BINDING_REF)]
    assert runner.intents == [
        AcquisitionIntent(
            intent_key=runner.intents[0].intent_key,
            tenant_id=_TENANT,
            run_intent_ref=acquisition_run_now_reference(
                record=record,
                trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
            ),
            contract_ref=_CONTRACT_REF,
            contract_digest=_CONTRACT_DIGEST,
            source_binding_ref=_BINDING_REF,
            source_observation_digest=_OBSERVATION_DIGEST,
            acquisition_mode="snapshot",
            object_refs=("orders",),
            prior_checkpoint_revision=0,
            prior_checkpoint_digest=None,
            record_ceiling=1_000,
            encoded_byte_ceiling=1_000_000,
            admitted_at=_NOW,
        )
    ]


def test_run_now_replay_prepares_once_per_call_without_acknowledging_checkpoint() -> None:
    runner = _RecordingRunner()
    current_checkpoint = (0, None)
    application = AcquisitionApplication(
        runner=runner,
        contract_resolver=lambda tenant_id, contract_ref: _record(),
        checkpoint_resolver=lambda tenant_id, contract_digest, source_binding_ref: (
            current_checkpoint
        ),
        clock=lambda: _NOW + timedelta(minutes=len(runner.intents)),
    )
    first = application.run_now(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
        acquisition_mode="snapshot",
    )
    replay = application.run_now(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
        acquisition_mode="snapshot",
    )

    assert first.evidence.run_intent_ref == replay.evidence.run_intent_ref
    assert runner.intents[0].intent_key == runner.intents[1].intent_key
    assert tuple(intent.prior_checkpoint_revision for intent in runner.intents) == (0, 0)
    assert runner.acknowledgement_calls == 0


def test_run_now_rejects_contract_authority_from_another_tenant_before_prepare() -> None:
    runner = _RecordingRunner()
    application = AcquisitionApplication(
        runner=runner,
        contract_resolver=lambda tenant_id, contract_ref: _record(tenant_id="tenant-b"),
        checkpoint_resolver=lambda tenant_id, contract_digest, source_binding_ref: (0, None),
        clock=lambda: _NOW,
    )

    with pytest.raises(AcquisitionOwnershipError, match="authority"):
        application.run_now(
            tenant_id=_TENANT,
            contract_ref=_CONTRACT_REF,
            trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
            acquisition_mode="snapshot",
        )

    assert runner.intents == []


def test_run_now_rejects_a_record_that_does_not_bind_its_exact_revision() -> None:
    runner = _RecordingRunner()
    application = AcquisitionApplication(
        runner=runner,
        contract_resolver=lambda tenant_id, contract_ref: _record().model_copy(
            update={"revision": 99}
        ),
        checkpoint_resolver=lambda tenant_id, contract_digest, source_binding_ref: (0, None),
        clock=lambda: _NOW,
    )

    with pytest.raises(AcquisitionContractError, match="contract_record_authority_invalid"):
        application.run_now(
            tenant_id=_TENANT,
            contract_ref=_CONTRACT_REF,
            trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
            acquisition_mode="snapshot",
        )

    assert runner.intents == []


def test_run_now_does_not_accept_a_caller_supplied_contract_revision() -> None:
    application = AcquisitionApplication(
        runner=_RecordingRunner(),
        contract_resolver=lambda tenant_id, contract_ref: _record(),
        checkpoint_resolver=lambda tenant_id, contract_digest, source_binding_ref: (0, None),
        clock=lambda: _NOW,
    )

    with pytest.raises(TypeError, match="contract_revision"):
        application.run_now(  # type: ignore[call-arg]
            tenant_id=_TENANT,
            contract_ref=_CONTRACT_REF,
            contract_revision=99,
            trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
            acquisition_mode="snapshot",
        )
