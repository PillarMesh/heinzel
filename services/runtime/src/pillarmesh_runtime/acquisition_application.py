from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pillarmesh_contract_model import ArtifactModel, digest
from pillarmesh_contract_service import (
    ActivatedAcquisitionContractRecord,
)
from pillarmesh_provider_sdk import AcquisitionIntent, acquisition_intent_key
from pillarmesh_provider_sdk.acquisition_models import AcquisitionMode

from .acquisition import AcquisitionPreparationResult
from .acquisition_errors import AcquisitionContractError, AcquisitionOwnershipError

type CheckpointResolver = Callable[[str, str, str], tuple[int, str | None]]
type Clock = Callable[[], datetime]
type ContractRecordResolver = Callable[[str, str], ActivatedAcquisitionContractRecord]


class AcquisitionRunPreparation(ArtifactModel):
    """A prepared batch with the intent it was admitted under, which LAND needs to land it."""

    intent: AcquisitionIntent
    result: AcquisitionPreparationResult


class AcquisitionPreparer(Protocol):
    def prepare(self, intent: AcquisitionIntent) -> AcquisitionPreparationResult: ...


def prepare_acquisition(
    *,
    intent: AcquisitionIntent,
    runner: AcquisitionPreparer,
) -> AcquisitionPreparationResult:
    return runner.prepare(intent)


def acquisition_run_now_reference(
    *,
    record: ActivatedAcquisitionContractRecord,
    trigger_window: str,
) -> str:
    if not trigger_window:
        raise ValueError("trigger_window must be non-empty")
    return digest(
        {
            "domain": "pillarmesh-acquisition-run-now-v1",
            "tenant_id": record.tenant_id,
            "contract_ref": record.contract_ref,
            "contract_revision": record.revision,
            "trigger_window": trigger_window,
        }
    )


class AcquisitionApplication:
    def __init__(
        self,
        *,
        runner: AcquisitionPreparer,
        contract_resolver: ContractRecordResolver,
        checkpoint_resolver: CheckpointResolver,
        clock: Clock,
    ) -> None:
        self._runner = runner
        self._contract_resolver = contract_resolver
        self._checkpoint_resolver = checkpoint_resolver
        self._clock = clock

    def run_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        trigger_window: str,
        acquisition_mode: AcquisitionMode,
    ) -> AcquisitionPreparationResult:
        return self.prepare_now(
            tenant_id=tenant_id,
            contract_ref=contract_ref,
            trigger_window=trigger_window,
            acquisition_mode=acquisition_mode,
        ).result

    def prepare_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        trigger_window: str,
        acquisition_mode: AcquisitionMode,
    ) -> AcquisitionRunPreparation:
        """Prepare the batch and keep the admitted intent, which LAND binds its targets to."""
        untrusted_record = self._contract_resolver(tenant_id, contract_ref)
        try:
            record = ActivatedAcquisitionContractRecord.model_validate(
                untrusted_record.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise AcquisitionContractError("contract_record_authority_invalid") from None
        if record.tenant_id != tenant_id:
            raise AcquisitionOwnershipError("contract_authority_mismatch")
        if record.contract_ref != contract_ref:
            raise AcquisitionContractError("contract_authority_mismatch")
        contract = record.contract

        checkpoint_revision, checkpoint_digest = self._checkpoint_resolver(
            tenant_id,
            contract.contract_digest,
            contract.source_binding_ref,
        )
        run_intent_ref = acquisition_run_now_reference(
            record=record,
            trigger_window=trigger_window,
        )
        object_refs = tuple(schema.logical_object_ref for schema in contract.object_schemas)
        intent = AcquisitionIntent(
            intent_key=acquisition_intent_key(
                tenant_id=tenant_id,
                run_intent_ref=run_intent_ref,
                contract_digest=contract.contract_digest,
                source_binding_ref=contract.source_binding_ref,
                acquisition_mode=acquisition_mode,
                object_refs=object_refs,
                prior_checkpoint_revision=checkpoint_revision,
            ),
            tenant_id=tenant_id,
            run_intent_ref=run_intent_ref,
            contract_ref=contract_ref,
            contract_digest=contract.contract_digest,
            source_binding_ref=contract.source_binding_ref,
            source_observation_digest=contract.source_observation_digest,
            acquisition_mode=acquisition_mode,
            object_refs=object_refs,
            prior_checkpoint_revision=checkpoint_revision,
            prior_checkpoint_digest=checkpoint_digest,
            record_ceiling=contract.record_ceiling,
            encoded_byte_ceiling=contract.encoded_byte_ceiling,
            admitted_at=self._clock(),
        )
        return AcquisitionRunPreparation(
            intent=intent, result=prepare_acquisition(intent=intent, runner=self._runner)
        )
