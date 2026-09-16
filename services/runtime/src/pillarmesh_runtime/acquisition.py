from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import BinaryIO, Literal, Protocol, Self

from pillarmesh_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest
from pillarmesh_contract_service import (
    ActivatedAcquisitionContract as ActivatedAcquisitionContract,
)
from pillarmesh_evidence import AcquisitionEvidenceReceipt, AcquisitionPublicReasonCode
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionCheckpointReceipt,
    AcquisitionIntent,
    AcquisitionNoValidPlan,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionProvider,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    AcquisitionSession,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    CanonicalJsonlSegmentEncoder,
    CompletedAcquisition,
    ResynchronizationRequired,
    acquisition_batch_id,
)
from pillarmesh_provider_sdk.acquisition_models import (
    AcquisitionMode,
    AcquisitionNoValidPlanReasonCode,
    AcquisitionScalar,
    ResynchronizationReasonCode,
)
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind
from pillarmesh_state import (
    AcquisitionArtifactStoreError,
    AcquisitionStateConflictError,
    AcquisitionStateNotFoundError,
    AcquisitionStatePersistenceError,
    PreparedAcquisitionState,
    PreparedAcquisitionStateStatus,
    SourceCheckpointState,
    StaleAcquisitionRevisionError,
)
from pydantic import BaseModel, ValidationError, model_validator

from .acquisition_errors import (
    AcquisitionAuthorizationError,
    AcquisitionCeilingExceeded,
    AcquisitionContractError,
    AcquisitionCursorExpiredError,
    AcquisitionDriftError,
    AcquisitionIntegrityError,
    AcquisitionOwnershipError,
    AcquisitionRuntimeError,
    AcquisitionStaleRevision,
    AcquisitionThrottledError,
    AcquisitionTransientError,
)

type BindingResolver = Callable[[str, str], SourceConnectionBinding]
type ContractResolver = Callable[[str, str], ActivatedAcquisitionContract]
type ObservationResolver = Callable[[str, str], AcquisitionSourceObservation]
type ProviderResolver = Callable[[SourceConnectionBinding], AcquisitionProvider]
type ReferenceFactory = Callable[[str], str]
type Clock = Callable[[], datetime]
type FaultHook = Callable[[str], None]
type GovernedOutcome = AcquisitionNoValidPlan | ResynchronizationRequired
type OrderScalar = bool | int | Decimal | str | datetime
type OrderAtom = tuple[str, OrderScalar]
type RecordOrder = tuple[OrderAtom, ...]


class AcquisitionPreparationResult(ArtifactModel):
    evidence: AcquisitionEvidenceReceipt
    prepared_receipt: AcquisitionPreparedReceipt | None
    batch_manifest: AcquisitionBatchManifest | None
    governed_outcome: GovernedOutcome | None

    @model_validator(mode="after")
    def requires_exact_result_shape(self) -> Self:
        prepared = self.evidence.outcome == "prepared"
        if prepared != (self.prepared_receipt is not None and self.batch_manifest is not None):
            raise ValueError("prepared result requires private preparation artifacts")
        if prepared == (self.governed_outcome is not None):
            raise ValueError("result must contain either preparation or governed outcome")
        return self


class AcquisitionArtifactStore(Protocol):
    def put_if_absent(
        self,
        *,
        tenant_id: str,
        artifact_digest: str,
        reader: BinaryIO,
    ) -> None: ...

    def open_verified(self, *, tenant_id: str, artifact_digest: str) -> BinaryIO: ...

    def exists_verified(self, *, tenant_id: str, artifact_digest: str) -> bool: ...


class AcquisitionStateStore(Protocol):
    def admit_authority(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
    ) -> tuple[int, int]: ...

    def load_checkpoint_with_cursor(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> tuple[SourceCheckpointState, bytes]: ...

    def prepare_exact(
        self,
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState: ...

    def load_preparation(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]: ...

    def load_preparation_for_replay(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]: ...

    def acknowledge_exact(
        self,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        expected_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        cursor_version: str,
    ) -> AcquisitionCheckpointReceipt: ...

    def load_acknowledgement_replay(
        self,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt | None: ...

    def record_governed_outcome(
        self,
        *,
        tenant_id: str,
        outcome_id: str,
        outcome: GovernedOutcome,
        created_at: datetime,
    ) -> object: ...


class AcquisitionEvidenceWriter(Protocol):
    def append(self, receipt: AcquisitionEvidenceReceipt) -> None: ...


@dataclass
class _TemporarySegment:
    schema: AcquisitionObjectSchema
    reader: BinaryIO
    encoder: CanonicalJsonlSegmentEncoder


class AcquisitionRunner:
    def __init__(
        self,
        *,
        binding_resolver: BindingResolver,
        contract_resolver: ContractResolver,
        observation_resolver: ObservationResolver,
        provider_resolver: ProviderResolver,
        state_store: AcquisitionStateStore,
        artifact_store: AcquisitionArtifactStore,
        evidence_writer: AcquisitionEvidenceWriter,
        reference_factory: ReferenceFactory,
        clock: Clock,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self._binding_resolver = binding_resolver
        self._contract_resolver = contract_resolver
        self._observation_resolver = observation_resolver
        self._provider_resolver = provider_resolver
        self._state_store = state_store
        self._artifact_store = artifact_store
        self._evidence_writer = evidence_writer
        self._reference_factory = reference_factory
        self._clock = clock
        self._fault_hook = fault_hook

    def prepare(self, intent: AcquisitionIntent) -> AcquisitionPreparationResult:
        admitted = _revalidate(AcquisitionIntent, intent, "intent")
        try:
            contract = self._resolve_contract(admitted)
            no_valid_plan = self._contract_no_valid_plan(admitted, contract)
            if no_valid_plan is not None:
                return self._record_governed_outcome(admitted, no_valid_plan)
            binding = self._resolve_binding(admitted, contract)
            binding_no_valid_plan = self._binding_no_valid_plan(contract, binding)
            if binding_no_valid_plan is not None:
                return self._record_governed_outcome(admitted, binding_no_valid_plan)
            try:
                binding_epoch, contract_epoch = self._state_store.admit_authority(
                    tenant_id=admitted.tenant_id,
                    contract_digest=admitted.contract_digest,
                    source_binding_ref=admitted.source_binding_ref,
                    source_binding_revision=binding.revision,
                )
            except (AcquisitionStateConflictError, StaleAcquisitionRevisionError):
                raise AcquisitionStaleRevision("acquisition_authority_is_stale") from None
            except AcquisitionStatePersistenceError:
                raise AcquisitionIntegrityError("acquisition_authority_state_failed") from None
            observation = self._resolve_observation(admitted, contract, binding)
            schemas = self._admitted_schemas(admitted, contract, observation)
            private_cursor, prior_cursor_version = self._load_exact_cursor(admitted, binding)
            replay = self._load_preparation_replay(admitted)
            if replay is not None:
                return replay
            provider = self._resolve_provider(binding)
            try:
                untrusted_session = provider.open_acquisition(admitted, schemas, private_cursor)
            except AcquisitionProviderError as error:
                raise _map_provider_error(error, binding.provider_kind) from None
            except Exception:
                raise AcquisitionIntegrityError("provider_open_failed") from None
            if not isinstance(untrusted_session, AcquisitionSession):
                raise AcquisitionIntegrityError("invalid_provider_session")
            session = untrusted_session
            with ExitStack() as temporary_files:
                segments = tuple(
                    self._open_temporary_segment(
                        schema,
                        temporary_files=temporary_files,
                        intent=admitted,
                    )
                    for schema in schemas
                )
                record_counts, completed = self._consume_session(
                    admitted,
                    segments,
                    session,
                    binding.provider_kind,
                    prior_cursor_version,
                )
                return self._publish_preparation(
                    admitted,
                    segments,
                    record_counts,
                    completed,
                    contract=contract,
                    binding=binding,
                    binding_authority_epoch=binding_epoch,
                    contract_authority_epoch=contract_epoch,
                )
        except AcquisitionCursorExpiredError as error:
            try:
                return self._record_resynchronization(admitted, error.reason_code)
            except AcquisitionRuntimeError as outcome_error:
                self._record_failed_evidence(admitted, _public_failure_reason(outcome_error))
                raise
        except AcquisitionOwnershipError:
            raise
        except AcquisitionRuntimeError as error:
            self._record_failed_evidence(admitted, _public_failure_reason(error))
            raise

    def acknowledge(
        self,
        intent: AcquisitionIntent,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt:
        admitted = _revalidate(AcquisitionIntent, intent, "intent")
        accepted = _revalidate(
            AcquisitionAcknowledgement,
            acknowledgement,
            "acquisition acknowledgement",
        )
        try:
            state, prepared_receipt = self._load_preparation(admitted)
            self._assert_preparation_matches_intent(admitted, state, prepared_receipt)
            self._assert_acknowledgement_matches_preparation(
                accepted,
                admitted,
                state,
                prepared_receipt,
                expected_consumer_ref=state.acknowledgement_consumer_ref,
            )
            replay = self._state_store.load_acknowledgement_replay(accepted)
            if replay is not None:
                return self._complete_acknowledgement(
                    admitted,
                    accepted,
                    prepared_receipt,
                    replay,
                )
            contract = self._resolve_contract(admitted)
            if self._contract_no_valid_plan(admitted, contract) is not None:
                raise AcquisitionContractError("acknowledgement_contract_not_admitted")
            binding = self._resolve_binding(admitted, contract)
            if self._binding_no_valid_plan(contract, binding) is not None:
                raise AcquisitionStaleRevision("acknowledgement_binding_authority_stale")
            observation = self._resolve_observation(admitted, contract, binding)
            self._admitted_schemas(admitted, contract, observation)
            if (
                state.acknowledgement_consumer_ref != contract.acknowledgement_consumer_ref
                or state.source_binding_revision != binding.revision
                or state.credential_revision != binding.credential_revision
                or state.provider_kind != binding.provider_kind
            ):
                raise AcquisitionStaleRevision("acknowledgement_authority_changed")
            checkpoint_receipt = self._state_store.acknowledge_exact(
                accepted,
                expected_consumer_ref=contract.acknowledgement_consumer_ref,
                provider_kind=binding.provider_kind,
                cursor_version=state.cursor_version,
            )
            return self._complete_acknowledgement(
                admitted,
                accepted,
                prepared_receipt,
                checkpoint_receipt,
            )
        except AcquisitionStateNotFoundError:
            state_error = AcquisitionStaleRevision("prepared_acquisition_not_found")
            self._record_failed_evidence(admitted, _public_failure_reason(state_error))
            raise state_error from None
        except StaleAcquisitionRevisionError:
            state_error = AcquisitionStaleRevision("acknowledgement_compare_and_set_failed")
            self._record_failed_evidence(admitted, _public_failure_reason(state_error))
            raise state_error from None
        except AcquisitionStateConflictError:
            integrity_error = AcquisitionIntegrityError("contradictory_acknowledgement")
            self._record_failed_evidence(admitted, _public_failure_reason(integrity_error))
            raise integrity_error from None
        except AcquisitionStatePersistenceError:
            integrity_error = AcquisitionIntegrityError("acknowledgement_state_failed")
            self._record_failed_evidence(admitted, _public_failure_reason(integrity_error))
            raise integrity_error from None
        except AcquisitionOwnershipError:
            raise
        except AcquisitionRuntimeError as error:
            self._record_failed_evidence(admitted, _public_failure_reason(error))
            raise
        raise AssertionError("acknowledgement must return or raise")

    def _complete_acknowledgement(
        self,
        intent: AcquisitionIntent,
        acknowledgement: AcquisitionAcknowledgement,
        prepared_receipt: AcquisitionPreparedReceipt,
        untrusted_checkpoint_receipt: AcquisitionCheckpointReceipt,
    ) -> AcquisitionCheckpointReceipt:
        checkpoint_receipt = _revalidate(
            AcquisitionCheckpointReceipt,
            untrusted_checkpoint_receipt,
            "checkpoint receipt",
        )
        if (
            checkpoint_receipt.tenant_id != intent.tenant_id
            or checkpoint_receipt.contract_digest != intent.contract_digest
            or checkpoint_receipt.source_binding_ref != intent.source_binding_ref
            or checkpoint_receipt.previous_revision != intent.prior_checkpoint_revision
            or checkpoint_receipt.committed_revision != intent.prior_checkpoint_revision + 1
            or checkpoint_receipt.cursor_digest != prepared_receipt.candidate_checkpoint_digest
            or checkpoint_receipt.batch_id != prepared_receipt.batch_id
            or checkpoint_receipt.acknowledgement_id != acknowledgement.acknowledgement_id
            or checkpoint_receipt.committed_at != acknowledgement.acknowledged_at
        ):
            raise AcquisitionIntegrityError("checkpoint_receipt_authority_mismatch")
        evidence = AcquisitionEvidenceReceipt(
            evidence_id=self._new_reference("evidence"),
            tenant_id=intent.tenant_id,
            run_intent_ref=intent.run_intent_ref,
            contract_ref=intent.contract_ref,
            source_binding_ref=intent.source_binding_ref,
            acquisition_mode=intent.acquisition_mode,
            logical_object_refs=intent.object_refs,
            prepared_receipt_ref=prepared_receipt.prepared_receipt_id,
            checkpoint_receipt_ref=checkpoint_receipt.checkpoint_receipt_id,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            resulting_checkpoint_revision=checkpoint_receipt.committed_revision,
            reason_codes=(),
            outcome="acknowledged",
            created_at=checkpoint_receipt.committed_at,
        )
        self._append_evidence(evidence)
        return checkpoint_receipt

    def _resolve_contract(self, intent: AcquisitionIntent) -> ActivatedAcquisitionContract:
        try:
            value = self._contract_resolver(intent.tenant_id, intent.contract_ref)
            contract = _revalidate(ActivatedAcquisitionContract, value, "contract")
        except AcquisitionRuntimeError:
            raise
        except Exception:
            raise AcquisitionContractError("contract_authority_unavailable") from None
        if contract.tenant_id != intent.tenant_id:
            raise AcquisitionOwnershipError("contract_authority_mismatch")
        if (
            contract.contract_ref != intent.contract_ref
            or contract.contract_digest != intent.contract_digest
        ):
            raise AcquisitionContractError("contract_authority_mismatch")
        return contract

    @staticmethod
    def _contract_no_valid_plan(
        intent: AcquisitionIntent,
        contract: ActivatedAcquisitionContract,
    ) -> AcquisitionNoValidPlan | None:
        reasons: list[AcquisitionNoValidPlanReasonCode] = []
        constraints: list[str] = []
        if contract.lifecycle_state != "activated":
            reasons.append("contract_not_activated")
            constraints.append("contract.lifecycle_state")
        if contract.source_binding_ref != intent.source_binding_ref:
            reasons.append("source_binding_not_admitted")
            constraints.append("contract.source_binding_ref")
        if contract.source_observation_digest != intent.source_observation_digest:
            reasons.append("source_observation_not_admitted")
            constraints.append("contract.source_observation_digest")
        if intent.acquisition_mode not in contract.acquisition_modes:
            reasons.append("acquisition_mode_not_admitted")
            constraints.append("contract.acquisition_modes")
        contract_objects = {schema.logical_object_ref for schema in contract.object_schemas}
        if not set(intent.object_refs).issubset(contract_objects):
            reasons.append("logical_object_not_admitted")
            constraints.append("contract.object_schemas")
        if intent.record_ceiling > contract.record_ceiling:
            reasons.append("record_ceiling_not_admitted")
            constraints.append("contract.record_ceiling")
        if intent.encoded_byte_ceiling > contract.encoded_byte_ceiling:
            reasons.append("encoded_byte_ceiling_not_admitted")
            constraints.append("contract.encoded_byte_ceiling")
        if not reasons:
            return None
        return AcquisitionNoValidPlan(
            reason_codes=tuple(sorted(reasons)),
            failed_constraints=tuple(sorted(constraints)),
        )

    def _resolve_binding(
        self,
        intent: AcquisitionIntent,
        contract: ActivatedAcquisitionContract,
    ) -> SourceConnectionBinding:
        try:
            value = self._binding_resolver(intent.tenant_id, intent.source_binding_ref)
            binding = _revalidate(SourceConnectionBinding, value, "source binding")
        except AcquisitionRuntimeError:
            raise
        except Exception:
            raise AcquisitionAuthorizationError("source_binding_unavailable") from None
        if binding.tenant_id != intent.tenant_id or binding.binding_id != intent.source_binding_ref:
            raise AcquisitionOwnershipError("source_binding_authority_mismatch")
        if binding.lifecycle_state is not SourceConnectionBindingState.READY:
            raise AcquisitionAuthorizationError("source_binding_not_ready")
        if binding.provider_kind not in {"postgresql", "stripe"}:
            raise AcquisitionContractError("provider_kind_not_admitted")
        if not set(intent.object_refs).issubset(binding.approved_object_refs):
            raise AcquisitionAuthorizationError("source_object_not_approved")
        if contract.source_binding_ref != binding.binding_id:
            raise AcquisitionContractError("source_binding_contract_mismatch")
        return binding

    @staticmethod
    def _binding_no_valid_plan(
        contract: ActivatedAcquisitionContract,
        binding: SourceConnectionBinding,
    ) -> AcquisitionNoValidPlan | None:
        constraints: list[str] = []
        if binding.revision != contract.source_binding_revision:
            constraints.append("contract.source_binding_revision")
        if binding.credential_revision != contract.credential_revision:
            constraints.append("contract.credential_revision")
        if binding.capability_profile_digest != contract.capability_profile_digest:
            constraints.append("contract.capability_profile_digest")
        if binding.source_observation_ref != contract.source_observation_ref:
            constraints.append("contract.source_observation_ref")
        if not constraints:
            return None
        return AcquisitionNoValidPlan(
            reason_codes=("source_binding_authority_stale",),
            failed_constraints=tuple(sorted(constraints)),
        )

    def _resolve_observation(
        self,
        intent: AcquisitionIntent,
        contract: ActivatedAcquisitionContract,
        binding: SourceConnectionBinding,
    ) -> AcquisitionSourceObservation:
        observation_ref = binding.source_observation_ref
        if observation_ref is None:
            raise AcquisitionIntegrityError("ready_binding_missing_source_observation")
        try:
            value = self._observation_resolver(
                intent.tenant_id,
                observation_ref,
            )
            observation = _revalidate(AcquisitionSourceObservation, value, "source observation")
        except AcquisitionRuntimeError:
            raise
        except Exception:
            raise AcquisitionDriftError("source_observation_unavailable") from None
        if (
            digest(observation) != intent.source_observation_digest
            or contract.source_observation_digest != intent.source_observation_digest
            or observation.tenant_id != intent.tenant_id
            or observation.source_binding_ref != binding.binding_id
            or observation.provider_kind != binding.provider_kind
            or any(
                item.provider_observation.connection_handle != binding.connection_handle
                for item in observation.object_observations
            )
        ):
            raise AcquisitionDriftError("source_observation_authority_mismatch")
        return observation

    @staticmethod
    def _admitted_schemas(
        intent: AcquisitionIntent,
        contract: ActivatedAcquisitionContract,
        observation: AcquisitionSourceObservation,
    ) -> tuple[AcquisitionObjectSchema, ...]:
        schemas_by_ref = {schema.logical_object_ref: schema for schema in contract.object_schemas}
        schemas = tuple(schemas_by_ref[object_ref] for object_ref in intent.object_refs)
        observed_refs = tuple(item.logical_object_ref for item in observation.object_observations)
        if observed_refs != intent.object_refs:
            raise AcquisitionDriftError("source_observation_object_mismatch")
        for schema, item in zip(schemas, observation.object_observations, strict=True):
            provider_observation = item.provider_observation
            capabilities = provider_observation.capabilities
            if (
                provider_observation.schema_digest != schema.schema_digest
                or capabilities is None
                or intent.acquisition_mode not in capabilities
                or provider_observation.observed_at is None
                or provider_observation.observed_at > intent.admitted_at
            ):
                raise AcquisitionDriftError("source_observation_schema_mismatch")
        return schemas

    def _load_exact_cursor(
        self,
        intent: AcquisitionIntent,
        binding: SourceConnectionBinding,
    ) -> tuple[bytes | None, str | None]:
        try:
            checkpoint, cursor = self._state_store.load_checkpoint_with_cursor(
                intent.tenant_id,
                intent.contract_digest,
                intent.source_binding_ref,
            )
        except AcquisitionStateNotFoundError:
            if intent.prior_checkpoint_revision == 0 and intent.prior_checkpoint_digest is None:
                return None, None
            raise AcquisitionStaleRevision("checkpoint_not_found") from None
        except (AcquisitionStateConflictError, AcquisitionStatePersistenceError):
            raise AcquisitionIntegrityError("checkpoint_load_failed") from None
        if intent.prior_checkpoint_revision == 0:
            raise AcquisitionStaleRevision("unexpected_existing_checkpoint")
        if (
            checkpoint.tenant_id != intent.tenant_id
            or checkpoint.contract_digest != intent.contract_digest
            or checkpoint.source_binding_ref != intent.source_binding_ref
        ):
            raise AcquisitionOwnershipError("checkpoint_authority_mismatch")
        if (
            checkpoint.provider_kind != binding.provider_kind
            or checkpoint.revision != intent.prior_checkpoint_revision
            or checkpoint.cursor_digest != intent.prior_checkpoint_digest
        ):
            raise AcquisitionStaleRevision("checkpoint_revision_mismatch")
        return cursor, checkpoint.cursor_version

    def _load_preparation(
        self,
        intent: AcquisitionIntent,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        state, receipt = self._state_store.load_preparation(
            intent.tenant_id,
            intent.contract_digest,
            intent.source_binding_ref,
            intent.prior_checkpoint_revision,
        )
        return (
            _revalidate(PreparedAcquisitionState, state, "prepared acquisition state"),
            _revalidate(AcquisitionPreparedReceipt, receipt, "prepared receipt"),
        )

    def _load_preparation_replay(
        self,
        intent: AcquisitionIntent,
    ) -> AcquisitionPreparationResult | None:
        try:
            state, receipt = self._state_store.load_preparation_for_replay(
                intent.tenant_id,
                intent.contract_digest,
                intent.source_binding_ref,
                intent.prior_checkpoint_revision,
            )
        except AcquisitionStateNotFoundError:
            return None
        except (AcquisitionStateConflictError, StaleAcquisitionRevisionError):
            raise AcquisitionStaleRevision("prepared_replay_is_stale") from None
        except AcquisitionStatePersistenceError:
            raise AcquisitionIntegrityError("prepared_replay_state_failed") from None
        state = _revalidate(PreparedAcquisitionState, state, "prepared acquisition state")
        receipt = _revalidate(AcquisitionPreparedReceipt, receipt, "prepared receipt")
        self._assert_preparation_matches_intent(intent, state, receipt)
        if state.state is not PreparedAcquisitionStateStatus.PREPARED:
            raise AcquisitionStaleRevision("prepared_replay_is_not_pending")
        try:
            reader = self._artifact_store.open_verified(
                tenant_id=intent.tenant_id,
                artifact_digest=receipt.batch_manifest_digest,
            )
            try:
                manifest_payload = reader.read()
            finally:
                with suppress(Exception):
                    reader.close()
            if not isinstance(manifest_payload, bytes):
                raise TypeError
            manifest = AcquisitionBatchManifest.model_validate_json(manifest_payload, strict=True)
            if canonical_bytes(manifest) != manifest_payload:
                raise ValueError
            if any(
                not self._artifact_store.exists_verified(
                    tenant_id=intent.tenant_id,
                    artifact_digest=segment.content_digest,
                )
                for segment in manifest.segment_manifests
            ):
                raise AcquisitionArtifactStoreError(
                    operation="verify replay",
                    detail="referenced segment does not exist",
                )
        except (AcquisitionArtifactStoreError, ValidationError, TypeError, ValueError):
            raise AcquisitionIntegrityError("prepared_replay_artifact_failed") from None
        except Exception:
            raise AcquisitionIntegrityError("prepared_replay_artifact_failed") from None
        if (
            digest(manifest) != receipt.batch_manifest_digest
            or manifest.batch_id != receipt.batch_id
            or manifest.intent_key != intent.intent_key
            or manifest.tenant_id != intent.tenant_id
            or manifest.contract_ref != intent.contract_ref
            or manifest.contract_digest != intent.contract_digest
            or manifest.source_binding_ref != intent.source_binding_ref
            or manifest.source_observation_digest != intent.source_observation_digest
            or manifest.acquisition_mode != intent.acquisition_mode
            or manifest.prior_checkpoint_revision != intent.prior_checkpoint_revision
            or manifest.prior_checkpoint_digest != intent.prior_checkpoint_digest
            or manifest.candidate_checkpoint_digest != receipt.candidate_checkpoint_digest
            or tuple(item.logical_object_ref for item in manifest.segment_manifests)
            != intent.object_refs
        ):
            raise AcquisitionIntegrityError("prepared_replay_authority_mismatch")
        evidence = AcquisitionEvidenceReceipt(
            evidence_id=self._new_reference("evidence"),
            tenant_id=intent.tenant_id,
            run_intent_ref=intent.run_intent_ref,
            contract_ref=intent.contract_ref,
            source_binding_ref=intent.source_binding_ref,
            acquisition_mode=intent.acquisition_mode,
            logical_object_refs=intent.object_refs,
            prepared_receipt_ref=receipt.prepared_receipt_id,
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            resulting_checkpoint_revision=None,
            reason_codes=(),
            outcome="prepared",
            created_at=self._clock(),
        )
        self._append_evidence(evidence)
        return AcquisitionPreparationResult(
            evidence=evidence,
            prepared_receipt=receipt,
            batch_manifest=manifest,
            governed_outcome=None,
        )

    @staticmethod
    def _assert_preparation_matches_intent(
        intent: AcquisitionIntent,
        state: PreparedAcquisitionState,
        receipt: AcquisitionPreparedReceipt,
    ) -> None:
        if (
            state.tenant_id != intent.tenant_id
            or state.intent_key != intent.intent_key
            or state.contract_digest != intent.contract_digest
            or state.source_binding_ref != intent.source_binding_ref
            or state.prior_checkpoint_revision != intent.prior_checkpoint_revision
            or receipt.tenant_id != state.tenant_id
            or receipt.intent_key != state.intent_key
            or receipt.batch_id != state.batch_id
            or receipt.batch_manifest_digest != state.batch_manifest_digest
            or receipt.prior_checkpoint_revision != state.prior_checkpoint_revision
            or receipt.candidate_checkpoint_digest != state.candidate_checkpoint_digest
            or receipt.cursor_version != state.cursor_version
            or receipt.prepared_at != state.created_at
        ):
            raise AcquisitionIntegrityError("prepared_acquisition_authority_mismatch")

    @staticmethod
    def _assert_acknowledgement_matches_preparation(
        acknowledgement: AcquisitionAcknowledgement,
        intent: AcquisitionIntent,
        state: PreparedAcquisitionState,
        receipt: AcquisitionPreparedReceipt,
        *,
        expected_consumer_ref: str,
    ) -> None:
        if acknowledgement.tenant_id != intent.tenant_id:
            raise AcquisitionOwnershipError("acknowledgement_tenant_mismatch")
        if acknowledgement.consumer_ref != expected_consumer_ref:
            raise AcquisitionAuthorizationError("acknowledgement_consumer_not_authorized")
        if (
            acknowledgement.contract_digest != intent.contract_digest
            or acknowledgement.source_binding_ref != intent.source_binding_ref
            or acknowledgement.batch_id != state.batch_id
            or acknowledgement.batch_manifest_digest != state.batch_manifest_digest
            or acknowledgement.prior_checkpoint_revision != state.prior_checkpoint_revision
            or acknowledgement.candidate_checkpoint_digest != state.candidate_checkpoint_digest
            or receipt.batch_id != acknowledgement.batch_id
        ):
            raise AcquisitionIntegrityError("acknowledgement_authority_mismatch")

    def _resolve_provider(self, binding: SourceConnectionBinding) -> AcquisitionProvider:
        try:
            provider = self._provider_resolver(binding)
        except AcquisitionProviderError as error:
            raise _map_provider_error(error, binding.provider_kind) from None
        # A resolver that already classified its own failure keeps that
        # classification, exactly as contract, binding and observation resolution do.
        # Resolving a provider reads the private capability from connection-broker,
        # whose transient and corrupt-row failures are distinguishable; replacing
        # them here recorded a permanent denial for a momentary one.
        except AcquisitionRuntimeError:
            raise
        except Exception:
            raise AcquisitionAuthorizationError("private_capability_resolution_failed") from None
        if not isinstance(provider, AcquisitionProvider):
            raise AcquisitionIntegrityError("invalid_provider_capability")
        return provider

    def _consume_session(
        self,
        intent: AcquisitionIntent,
        segments: tuple[_TemporarySegment, ...],
        session: AcquisitionSession,
        provider_kind: AcquisitionProviderKind,
        prior_cursor_version: str | None,
    ) -> tuple[dict[str, int], CompletedAcquisition]:
        segment_by_ref = {segment.schema.logical_object_ref: segment for segment in segments}
        object_position = {object_ref: index for index, object_ref in enumerate(intent.object_refs)}
        record_counts = dict.fromkeys(intent.object_refs, 0)
        previous_object_position = -1
        previous_order: dict[str, RecordOrder] = {}
        seen_identities: set[tuple[str, str]] = set()
        record_count = 0
        encoded_bytes = 0
        try:
            for untrusted_record in session:
                record = _revalidate(AcquisitionRecord, untrusted_record, "provider record")
                segment = segment_by_ref.get(record.logical_object_ref)
                if segment is None:
                    raise AcquisitionIntegrityError("record_object_not_admitted")
                position = object_position[record.logical_object_ref]
                if position < previous_object_position:
                    raise AcquisitionIntegrityError("record_order_is_not_deterministic")
                previous_object_position = position
                _validate_record_schema(record, segment.schema)
                identity = (record.logical_object_ref, record.record_key)
                if identity in seen_identities:
                    raise AcquisitionIntegrityError("duplicate_record_identity")
                seen_identities.add(identity)
                order = _record_order(
                    record,
                    segment.schema,
                    provider_kind,
                    intent.acquisition_mode,
                )
                prior_order = previous_order.get(record.logical_object_ref)
                if prior_order is not None and not _is_strictly_after(order, prior_order):
                    raise AcquisitionIntegrityError("record_order_is_not_deterministic")
                previous_order[record.logical_object_ref] = order
                record_count += 1
                if record_count > intent.record_ceiling:
                    raise AcquisitionCeilingExceeded(
                        logical_object_ref=record.logical_object_ref,
                        limit_kind="records",
                        ceiling=intent.record_ceiling,
                    )
                encoded_bytes += len(canonical_bytes(record)) + 1
                if encoded_bytes > intent.encoded_byte_ceiling:
                    raise AcquisitionCeilingExceeded(
                        logical_object_ref=record.logical_object_ref,
                        limit_kind="encoded_bytes",
                        ceiling=intent.encoded_byte_ceiling,
                    )
                try:
                    segment.encoder.append(record)
                except OSError:
                    raise AcquisitionIntegrityError("temporary_encoding_failed") from None
                record_counts[record.logical_object_ref] += 1
            completed = _revalidate(
                CompletedAcquisition,
                session.complete(),
                "completed acquisition",
            )
            if (
                prior_cursor_version is not None
                and completed.cursor_version != prior_cursor_version
            ):
                raise AcquisitionIntegrityError("cursor_version_mismatch")
            _validate_boundaries(intent, segments, record_counts, completed)
            return record_counts, completed
        except AcquisitionProviderError as error:
            _abort_safely(session)
            raise _map_provider_error(error, provider_kind) from None
        except AcquisitionCeilingExceeded:
            _abort_safely(session)
            raise
        except AcquisitionSessionIncomplete:
            _abort_safely(session)
            raise AcquisitionIntegrityError("provider_session_incomplete") from None
        except AcquisitionRuntimeError:
            _abort_safely(session)
            raise
        except (ValidationError, TypeError, ValueError):
            _abort_safely(session)
            raise AcquisitionIntegrityError("invalid_provider_response") from None
        except Exception:
            _abort_safely(session)
            raise AcquisitionIntegrityError("provider_stream_failed") from None

    def _publish_preparation(
        self,
        intent: AcquisitionIntent,
        segments: tuple[_TemporarySegment, ...],
        record_counts: dict[str, int],
        completed: CompletedAcquisition,
        *,
        contract: ActivatedAcquisitionContract,
        binding: SourceConnectionBinding,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
    ) -> AcquisitionPreparationResult:
        boundaries = {item.logical_object_ref: item for item in completed.boundaries}
        prepared_at = self._clock()
        segment_manifests: list[AcquisitionSegmentManifest] = []
        for ordinal, segment in enumerate(segments):
            encoded = segment.encoder.finish()
            segment.reader.seek(0)
            if _digest_reader(segment.reader) != encoded.content_digest:
                raise AcquisitionIntegrityError("segment_content_digest_mismatch")
            if encoded.record_count != record_counts[segment.schema.logical_object_ref]:
                raise AcquisitionIntegrityError("segment_record_count_mismatch")
            segment.reader.seek(0)
            segment_manifests.append(
                AcquisitionSegmentManifest(
                    segment_name=(
                        f"{ordinal:04d}-{digest(segment.schema.logical_object_ref)}.jsonl"
                    ),
                    logical_object_ref=segment.schema.logical_object_ref,
                    record_schema_digest=segment.schema.schema_digest,
                    boundary_digest=digest(boundaries[segment.schema.logical_object_ref]),
                    content_digest=encoded.content_digest,
                    record_set_digest=encoded.record_set_digest,
                    record_count=encoded.record_count,
                    encoded_bytes=encoded.encoded_bytes,
                )
            )
        candidate_checkpoint_digest = completed.candidate_cursor_digest
        manifests = tuple(segment_manifests)
        batch_id = acquisition_batch_id(
            intent_key=intent.intent_key,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            segment_manifests=manifests,
        )
        batch_manifest = AcquisitionBatchManifest(
            batch_id=batch_id,
            intent_key=intent.intent_key,
            tenant_id=intent.tenant_id,
            contract_ref=intent.contract_ref,
            contract_digest=intent.contract_digest,
            source_binding_ref=intent.source_binding_ref,
            source_observation_digest=intent.source_observation_digest,
            acquisition_mode=intent.acquisition_mode,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            prior_checkpoint_digest=intent.prior_checkpoint_digest,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            segment_manifests=manifests,
            total_record_count=sum(item.record_count for item in manifests),
            total_encoded_bytes=sum(item.encoded_bytes for item in manifests),
            prepared_at=prepared_at,
        )
        try:
            for ordinal, (manifest, segment) in enumerate(zip(manifests, segments, strict=True)):
                segment.reader.seek(0)
                self._invoke_fault(f"segment_artifact_{ordinal}_before")
                self._artifact_store.put_if_absent(
                    tenant_id=intent.tenant_id,
                    artifact_digest=manifest.content_digest,
                    reader=segment.reader,
                )
                self._invoke_fault(f"segment_artifact_{ordinal}_after")
            manifest_payload = canonical_bytes(batch_manifest)
            manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
            self._invoke_fault("manifest_artifact_before")
            self._artifact_store.put_if_absent(
                tenant_id=intent.tenant_id,
                artifact_digest=manifest_digest,
                reader=io.BytesIO(manifest_payload),
            )
            self._invoke_fault("manifest_artifact_after")
        except AcquisitionArtifactStoreError:
            raise AcquisitionIntegrityError("artifact_publication_failed") from None
        except Exception:
            raise AcquisitionIntegrityError("artifact_publication_failed") from None
        prepared_receipt = AcquisitionPreparedReceipt(
            prepared_receipt_id=self._new_reference("prepared_receipt"),
            tenant_id=intent.tenant_id,
            intent_key=intent.intent_key,
            batch_id=batch_manifest.batch_id,
            batch_manifest_digest=digest(batch_manifest),
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            candidate_checkpoint_digest=batch_manifest.candidate_checkpoint_digest,
            cursor_version=completed.cursor_version,
            prepared_at=prepared_at,
        )
        try:
            self._state_store.prepare_exact(
                prepared_receipt,
                contract_digest=intent.contract_digest,
                source_binding_ref=intent.source_binding_ref,
                source_binding_revision=binding.revision,
                credential_revision=binding.credential_revision,
                binding_authority_epoch=binding_authority_epoch,
                contract_authority_epoch=contract_authority_epoch,
                acknowledgement_consumer_ref=contract.acknowledgement_consumer_ref,
                provider_kind=binding.provider_kind,
                candidate_cursor_plaintext=completed.candidate_cursor_payload,
            )
        except StaleAcquisitionRevisionError:
            raise AcquisitionStaleRevision("prepared_authority_compare_and_set_failed") from None
        except AcquisitionStateConflictError:
            winner = self._load_preparation_replay(intent)
            if (
                winner is not None
                and winner.prepared_receipt is not None
                and winner.batch_manifest is not None
                and winner.prepared_receipt.cursor_version == completed.cursor_version
                and winner.batch_manifest.batch_id == batch_manifest.batch_id
                and winner.batch_manifest.candidate_checkpoint_digest
                == batch_manifest.candidate_checkpoint_digest
                and winner.batch_manifest.segment_manifests == batch_manifest.segment_manifests
                and winner.batch_manifest.total_record_count == batch_manifest.total_record_count
                and winner.batch_manifest.total_encoded_bytes == batch_manifest.total_encoded_bytes
            ):
                return winner
            raise AcquisitionIntegrityError("prepared_state_write_failed") from None
        except AcquisitionStatePersistenceError:
            raise AcquisitionIntegrityError("prepared_state_write_failed") from None
        except Exception:
            raise AcquisitionIntegrityError("prepared_state_write_failed") from None
        evidence = AcquisitionEvidenceReceipt(
            evidence_id=self._new_reference("evidence"),
            tenant_id=intent.tenant_id,
            run_intent_ref=intent.run_intent_ref,
            contract_ref=intent.contract_ref,
            source_binding_ref=intent.source_binding_ref,
            acquisition_mode=intent.acquisition_mode,
            logical_object_refs=intent.object_refs,
            prepared_receipt_ref=prepared_receipt.prepared_receipt_id,
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            resulting_checkpoint_revision=None,
            reason_codes=(),
            outcome="prepared",
            created_at=prepared_at,
        )
        self._append_evidence(evidence)
        return AcquisitionPreparationResult(
            evidence=evidence,
            prepared_receipt=prepared_receipt,
            batch_manifest=batch_manifest,
            governed_outcome=None,
        )

    @staticmethod
    def _open_temporary_segment(
        schema: AcquisitionObjectSchema,
        *,
        temporary_files: ExitStack,
        intent: AcquisitionIntent,
    ) -> _TemporarySegment:
        try:
            reader = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - owned by the ExitStack
            temporary_files.callback(_close_safely, reader)
        except OSError:
            raise AcquisitionIntegrityError("temporary_encoding_failed") from None
        return _TemporarySegment(
            schema=schema,
            reader=reader,
            encoder=CanonicalJsonlSegmentEncoder(
                reader,
                record_ceiling=intent.record_ceiling,
                encoded_byte_ceiling=intent.encoded_byte_ceiling,
            ),
        )

    def _invoke_fault(self, checkpoint: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(checkpoint)

    def _record_governed_outcome(
        self,
        intent: AcquisitionIntent,
        outcome: GovernedOutcome,
    ) -> AcquisitionPreparationResult:
        created_at = (
            outcome.created_at if isinstance(outcome, ResynchronizationRequired) else self._clock()
        )
        outcome_id = self._new_reference("governed_outcome")
        try:
            self._state_store.record_governed_outcome(
                tenant_id=intent.tenant_id,
                outcome_id=outcome_id,
                outcome=outcome,
                created_at=created_at,
            )
        except (AcquisitionStateConflictError, AcquisitionStatePersistenceError):
            raise AcquisitionIntegrityError("governed_outcome_write_failed") from None
        if isinstance(outcome, AcquisitionNoValidPlan):
            evidence_outcome: Literal["no_valid_plan", "resynchronization_required"] = (
                "no_valid_plan"
            )
            reason_codes: tuple[AcquisitionPublicReasonCode, ...] = outcome.reason_codes
        else:
            evidence_outcome = "resynchronization_required"
            reason_codes = (outcome.reason_code,)
        evidence = AcquisitionEvidenceReceipt(
            evidence_id=self._new_reference("evidence"),
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
            reason_codes=reason_codes,
            outcome=evidence_outcome,
            created_at=created_at,
        )
        self._append_evidence(evidence)
        return AcquisitionPreparationResult(
            evidence=evidence,
            prepared_receipt=None,
            batch_manifest=None,
            governed_outcome=outcome,
        )

    def _record_resynchronization(
        self,
        intent: AcquisitionIntent,
        reason_code: ResynchronizationReasonCode,
    ) -> AcquisitionPreparationResult:
        checkpoint_digest = intent.prior_checkpoint_digest
        if checkpoint_digest is None:
            raise AcquisitionIntegrityError("resynchronization_without_checkpoint")
        outcome = ResynchronizationRequired(
            reason_code=reason_code,
            source_binding_ref=intent.source_binding_ref,
            affected_object_refs=intent.object_refs,
            last_proven_checkpoint_digest=checkpoint_digest,
            required_scope="full_reconciliation",
            created_at=self._clock(),
        )
        return self._record_governed_outcome(intent, outcome)

    def _append_evidence(self, evidence: AcquisitionEvidenceReceipt) -> None:
        try:
            self._evidence_writer.append(evidence)
        except Exception:
            raise AcquisitionIntegrityError("evidence_write_failed") from None

    def _record_failed_evidence(
        self,
        intent: AcquisitionIntent,
        reason_code: AcquisitionPublicReasonCode,
    ) -> None:
        with suppress(AcquisitionRuntimeError):
            evidence = AcquisitionEvidenceReceipt(
                evidence_id=self._new_reference("evidence"),
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
                reason_codes=(reason_code,),
                outcome="failed",
                created_at=self._clock(),
            )
            self._append_evidence(evidence)

    def _new_reference(self, kind: str) -> str:
        try:
            reference = self._reference_factory(kind)
        except Exception:
            raise AcquisitionIntegrityError("reference_allocation_failed") from None
        if not reference:
            raise AcquisitionIntegrityError("reference_allocation_failed")
        return reference


def _revalidate[Model: BaseModel](model: type[Model], value: object, label: str) -> Model:
    if not isinstance(value, model):
        raise AcquisitionIntegrityError(f"invalid_{label.replace(' ', '_')}")
    try:
        return model.model_validate(value.model_dump(), strict=True)
    except (ValidationError, TypeError, ValueError):
        raise AcquisitionIntegrityError(f"invalid_{label.replace(' ', '_')}") from None


def _validate_record_schema(record: AcquisitionRecord, schema: AcquisitionObjectSchema) -> None:
    if tuple(value.name for value in record.fields) != tuple(field.name for field in schema.fields):
        raise AcquisitionIntegrityError("record_schema_mismatch")
    for value, field in zip(record.fields, schema.fields, strict=True):
        if not _value_matches_field(value.value, field.value_type, field.nullable):
            raise AcquisitionIntegrityError("record_schema_mismatch")


def _value_matches_field(value: AcquisitionScalar, value_type: str, nullable: bool) -> bool:
    if value is None:
        return nullable or value_type == "null"
    if value_type == "boolean":
        return type(value) is bool
    if value_type == "integer":
        return type(value) is int
    if value_type == "decimal":
        return isinstance(value, Decimal)
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "timestamp":
        return isinstance(value, datetime) and value.tzinfo is not None
    return False


def _record_order(
    record: AcquisitionRecord,
    schema: AcquisitionObjectSchema,
    provider_kind: AcquisitionProviderKind,
    mode: AcquisitionMode,
) -> RecordOrder:
    order: list[OrderAtom] = []
    if provider_kind == "postgresql" and mode == "incremental":
        order.append(_timestamp_order_atom(record.source_updated_at))
    elif provider_kind == "stripe":
        timestamp = (
            record.source_created_at
            if mode in {"snapshot", "reconciliation"}
            else record.source_updated_at or record.source_created_at
        )
        order.append(_timestamp_order_atom(timestamp))

    values_by_name = {value.name: value.value for value in record.fields}
    fields_by_name = {field.name: field for field in schema.fields}
    for field_name in schema.record_key_fields:
        field = fields_by_name[field_name]
        order.append(_order_atom(values_by_name[field_name], field.value_type))
    return tuple(order)


def _timestamp_order_atom(value: datetime | None) -> OrderAtom:
    if value is None:
        raise AcquisitionIntegrityError("record_order_key_missing")
    return "timestamp", value


def _order_atom(value: AcquisitionScalar, value_type: str) -> OrderAtom:
    if value is None or not _value_matches_field(value, value_type, False):
        raise AcquisitionIntegrityError("record_order_key_invalid")
    return value_type, value


def _is_strictly_after(current: RecordOrder, previous: RecordOrder) -> bool:
    if len(current) != len(previous):
        raise AcquisitionIntegrityError("record_order_shape_mismatch")
    for (current_type, current_value), (previous_type, previous_value) in zip(
        current,
        previous,
        strict=True,
    ):
        if current_type != previous_type:
            raise AcquisitionIntegrityError("record_order_shape_mismatch")
        if current_value == previous_value:
            continue
        return _order_scalar_is_less(previous_type, previous_value, current_value)
    return False


def _order_scalar_is_less(
    value_type: str,
    left: OrderScalar,
    right: OrderScalar,
) -> bool:
    if value_type == "boolean" and type(left) is bool and type(right) is bool:
        return not left and right
    if value_type == "integer" and type(left) is int and type(right) is int:
        return left < right
    if value_type == "decimal" and isinstance(left, Decimal) and isinstance(right, Decimal):
        return left < right
    if value_type == "string" and isinstance(left, str) and isinstance(right, str):
        return left < right
    if value_type == "timestamp" and isinstance(left, datetime) and isinstance(right, datetime):
        return left < right
    raise AcquisitionIntegrityError("record_order_shape_mismatch")


def _validate_boundaries(
    intent: AcquisitionIntent,
    segments: tuple[_TemporarySegment, ...],
    record_counts: dict[str, int],
    completed: CompletedAcquisition,
) -> None:
    boundary_refs = tuple(item.logical_object_ref for item in completed.boundaries)
    if boundary_refs != intent.object_refs:
        raise AcquisitionIntegrityError("acquisition_boundary_mismatch")
    for segment, boundary in zip(segments, completed.boundaries, strict=True):
        if (
            boundary.acquisition_mode != intent.acquisition_mode
            or boundary.schema_digest != segment.schema.schema_digest
            or boundary.lower_cursor_digest != intent.prior_checkpoint_digest
            or boundary.record_count != record_counts[segment.schema.logical_object_ref]
        ):
            raise AcquisitionIntegrityError("acquisition_boundary_mismatch")


def _abort_safely(session: AcquisitionSession) -> None:
    with suppress(BaseException):
        session.abort()


def _close_safely(reader: BinaryIO) -> None:
    with suppress(BaseException):
        reader.close()


def _digest_reader(reader: BinaryIO, *, chunk_size: int = 64 * 1024) -> str:
    hasher = hashlib.sha256()
    while chunk := reader.read(chunk_size):
        hasher.update(chunk)
    return hasher.hexdigest()


def _map_provider_error(
    error: AcquisitionProviderError,
    expected_provider_kind: AcquisitionProviderKind,
) -> AcquisitionRuntimeError:
    if error.provider_kind != expected_provider_kind:
        return AcquisitionIntegrityError("provider_error_authority_mismatch")
    if error.classification == "resynchronization_required":
        if error.reason_code == "stripe_event_cursor_expired":
            return AcquisitionCursorExpiredError("stripe_event_cursor_expired")
        if error.reason_code == "stripe_event_overlap_gap":
            return AcquisitionCursorExpiredError("stripe_event_overlap_gap")
        return AcquisitionIntegrityError("invalid_resynchronization_reason")
    if error.classification == "authorization_denied":
        return AcquisitionAuthorizationError(error.reason_code)
    if error.classification == "throttled":
        return AcquisitionThrottledError(error.reason_code)
    if error.classification in {"transient_transport", "transient_unavailable"}:
        return AcquisitionTransientError(error.reason_code)
    if error.classification in {"statement_rejected", "permanent_configuration"}:
        return AcquisitionContractError(error.reason_code)
    return AcquisitionIntegrityError(error.reason_code)


def _public_failure_reason(error: AcquisitionRuntimeError) -> AcquisitionPublicReasonCode:
    if isinstance(error, AcquisitionAuthorizationError):
        return "authorization_denied"
    if isinstance(error, AcquisitionThrottledError):
        return "rate_limited"
    if isinstance(error, AcquisitionTransientError):
        return "provider_unavailable"
    if isinstance(error, AcquisitionStaleRevision):
        return "stale_checkpoint"
    if isinstance(error, AcquisitionCeilingExceeded):
        if error.limit_kind == "records":
            return "record_ceiling_exceeded"
        return "encoded_byte_ceiling_exceeded"
    if isinstance(error, AcquisitionContractError):
        return "contract_invalid"
    if isinstance(error, AcquisitionDriftError):
        return "source_drift"
    return "integrity_failure"
