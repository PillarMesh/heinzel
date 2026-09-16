from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pillarmesh_contract_service import (
    AcquisitionContractLifecycleNotFoundError,
    ActivatedAcquisitionContractRecord,
)
from pillarmesh_state import (
    AcquisitionStateNotFoundError,
    AcquisitionStatePersistenceError,
    SourceCheckpointState,
)
from pydantic import ValidationError

from .acquisition import (
    AcquisitionArtifactStore,
    AcquisitionEvidenceWriter,
    AcquisitionRunner,
    AcquisitionStateStore,
    ObservationResolver,
    ProviderResolver,
    ReferenceFactory,
)
from .acquisition_application import (
    AcquisitionApplication,
    CheckpointResolver,
    ContractRecordResolver,
)
from .acquisition_errors import (
    AcquisitionContractError,
    AcquisitionIntegrityError,
    AcquisitionRuntimeError,
    AcquisitionTransientError,
)
from .binding_resolution import SourceBindingReader, source_binding_resolver


class ActivatedContractReader(Protocol):
    def list_contracts(self, tenant_id: str) -> tuple[ActivatedAcquisitionContractRecord, ...]: ...


class AcquisitionCheckpointReader(Protocol):
    def load_checkpoint(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> SourceCheckpointState: ...


class ComposedAcquisitionStateStore(
    AcquisitionStateStore,
    AcquisitionCheckpointReader,
    Protocol,
):
    pass


def activated_contract_resolver(repository: ActivatedContractReader) -> ContractRecordResolver:
    def resolve(tenant_id: str, contract_ref: str) -> ActivatedAcquisitionContractRecord:
        try:
            untrusted_records = repository.list_contracts(tenant_id)
        except AcquisitionContractLifecycleNotFoundError:
            raise AcquisitionContractError("contract_not_activated") from None
        except AcquisitionRuntimeError:
            raise
        except Exception:
            raise AcquisitionTransientError("contract_authority_store_unavailable") from None
        try:
            records = tuple(
                ActivatedAcquisitionContractRecord.model_validate(
                    record.model_dump(mode="python"), strict=True
                )
                for record in untrusted_records
                if record.contract_ref == contract_ref
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise AcquisitionIntegrityError("contract_authority_stored_state_invalid") from None
        if not records:
            raise AcquisitionContractError("contract_not_activated")
        revisions = tuple(record.revision for record in records)
        if len(revisions) != len(set(revisions)):
            raise AcquisitionIntegrityError("contract_authority_revision_conflict")
        current = max(records, key=lambda record: record.revision)
        if current.contract.lifecycle_state != "activated":
            raise AcquisitionContractError("contract_not_activated")
        return current

    return resolve


def acquisition_checkpoint_resolver(repository: AcquisitionCheckpointReader) -> CheckpointResolver:
    def resolve(
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> tuple[int, str | None]:
        try:
            untrusted_checkpoint = repository.load_checkpoint(
                tenant_id,
                contract_digest,
                source_binding_ref,
            )
        except AcquisitionStateNotFoundError:
            return (0, None)
        except AcquisitionStatePersistenceError:
            raise AcquisitionTransientError("checkpoint_store_unavailable") from None
        try:
            checkpoint = SourceCheckpointState.model_validate(
                untrusted_checkpoint.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise AcquisitionIntegrityError("checkpoint_stored_state_invalid") from None
        if (
            checkpoint.tenant_id != tenant_id
            or checkpoint.contract_digest != contract_digest
            or checkpoint.source_binding_ref != source_binding_ref
        ):
            raise AcquisitionIntegrityError("checkpoint_authority_mismatch")
        return checkpoint.revision, checkpoint.cursor_digest

    return resolve


def compose_acquisition_application(
    *,
    contract_repository: ActivatedContractReader,
    binding_repository: SourceBindingReader,
    observation_resolver: ObservationResolver,
    provider_resolver: ProviderResolver,
    state_store: ComposedAcquisitionStateStore,
    artifact_store: AcquisitionArtifactStore,
    evidence_writer: AcquisitionEvidenceWriter,
    reference_factory: ReferenceFactory,
    clock: Callable[[], datetime],
) -> AcquisitionApplication:
    resolve_record = activated_contract_resolver(contract_repository)
    runner = AcquisitionRunner(
        binding_resolver=source_binding_resolver(binding_repository),
        contract_resolver=lambda tenant_id, contract_ref: (
            resolve_record(tenant_id, contract_ref).contract
        ),
        observation_resolver=observation_resolver,
        provider_resolver=provider_resolver,
        state_store=state_store,
        artifact_store=artifact_store,
        evidence_writer=evidence_writer,
        reference_factory=reference_factory,
        clock=clock,
    )
    return AcquisitionApplication(
        runner=runner,
        contract_resolver=resolve_record,
        checkpoint_resolver=acquisition_checkpoint_resolver(state_store),
        clock=clock,
    )
