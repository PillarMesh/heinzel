from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind

from .models import (
    SOURCE_BINDING_TRANSITIONS,
    SourceAccountMode,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from .private_state import PrivateSourceCapability
from .protocols import SourceCapabilityProbe, SourceSecretResolver
from .repository import SourceBindingRepository, StaleSourceBindingRevisionError

_TRANSITIONS = SOURCE_BINDING_TRANSITIONS


class SourceAcquisitionAuthorityInvalidator(Protocol):
    def invalidate_source_binding_authority(
        self,
        tenant_id: str,
        source_binding_ref: str,
        *,
        invalidated_revision: int,
    ) -> None: ...


class SourceBindingBoundaryError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"source binding private boundary failed during {operation}")


class SourceBindingService:
    def __init__(
        self,
        repository: SourceBindingRepository,
        *,
        secret_resolver: SourceSecretResolver,
        capability_probes: Mapping[AcquisitionProviderKind, SourceCapabilityProbe],
        clock: Callable[[], datetime],
        authority_invalidator: SourceAcquisitionAuthorityInvalidator | None = None,
    ) -> None:
        self._repository = repository
        self._secret_resolver = secret_resolver
        self._capability_probes = dict(capability_probes)
        self._clock = clock
        self._authority_invalidator = authority_invalidator

    def create_draft(
        self,
        *,
        tenant_id: str,
        provider_kind: AcquisitionProviderKind,
        connection_handle: str,
        account_mode: SourceAccountMode,
        approved_object_refs: tuple[str, ...],
    ) -> SourceConnectionBinding:
        now = self._clock()
        binding_id = (
            "src-"
            + digest(
                {
                    "domain": "pillarmesh-source-binding-v1",
                    "tenant_id": tenant_id,
                    "provider_kind": provider_kind,
                    "connection_handle": connection_handle,
                }
            )[:24]
        )
        binding = SourceConnectionBinding(
            binding_id=binding_id,
            tenant_id=tenant_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            lifecycle_state=SourceConnectionBindingState.DRAFT,
            approved_object_refs=approved_object_refs,
            capability_profile_digest=None,
            source_observation_ref=None,
            credential_revision=1,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        capability = self._resolve_capability(binding)
        self._repository.create(binding, capability)
        return binding

    def get(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        return self._repository.load(tenant_id, binding_id)

    def transition(
        self,
        tenant_id: str,
        binding_id: str,
        state: SourceConnectionBindingState,
        *,
        expected_revision: int,
    ) -> SourceConnectionBinding:
        binding = self.get(tenant_id, binding_id)
        self._require_revision(binding, expected_revision)
        if state not in _TRANSITIONS[binding.lifecycle_state]:
            raise ValueError(
                f"transition {binding.lifecycle_state.value} -> {state.value} is not allowed"
            )
        if state is SourceConnectionBindingState.READY:
            raise ValueError("ready transition requires validation evidence")
        transitioned = self._rebuild(
            binding,
            lifecycle_state=state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            capability_profile_digest=None,
            source_observation_ref=None,
            credential_revision=binding.credential_revision,
        )
        if binding.lifecycle_state is SourceConnectionBindingState.READY:
            self._invalidate_acquisition_authority(binding)
        self._append(transitioned, expected_revision=binding.revision)
        return transitioned

    def validate(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> SourceConnectionBinding:
        binding = self.get(tenant_id, binding_id)
        self._require_revision(binding, expected_revision)
        if binding.lifecycle_state is not SourceConnectionBindingState.VALIDATING:
            raise ValueError("validation evidence requires a validating binding")
        capability = self._validated_capability(
            self._repository.load_capability(
                tenant_id,
                binding_id,
                binding.credential_revision,
            ),
            binding=binding,
        )
        now = self._clock()
        try:
            probe = self._capability_probes[binding.provider_kind]
            untrusted_evidence = probe.validate(
                binding=binding,
                capability=capability,
                observed_at=now,
            )
        except Exception:
            raise SourceBindingBoundaryError(operation="probe source capability") from None
        evidence = self._validated_evidence(untrusted_evidence)
        if (
            evidence.tenant_id != binding.tenant_id
            or evidence.binding_id != binding.binding_id
            or evidence.binding_revision != binding.revision
            or evidence.credential_revision != binding.credential_revision
            or evidence.provider_kind != binding.provider_kind
            or evidence.observed_at < binding.updated_at
            or evidence.observed_at > now
        ):
            raise SourceBindingBoundaryError(operation="validate probe evidence")
        ready = self._rebuild(
            binding,
            lifecycle_state=SourceConnectionBindingState.READY,
            revision=binding.revision + 1,
            updated_at=now,
            capability_profile_digest=evidence.capability_profile_digest,
            source_observation_ref=evidence.source_observation_ref,
            credential_revision=binding.credential_revision,
        )
        try:
            self._repository.record_validation(ready, evidence, expected_revision=binding.revision)
        except StaleSourceBindingRevisionError:
            raise StaleSourceBindingRevisionError() from None
        return ready

    def rotate_credentials(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> SourceConnectionBinding:
        binding = self.get(tenant_id, binding_id)
        self._require_revision(binding, expected_revision)
        if binding.lifecycle_state in {
            SourceConnectionBindingState.DRAFT,
            SourceConnectionBindingState.RETIRED,
        }:
            raise ValueError("credential rotation is not allowed in the current state")
        credential_revision = binding.credential_revision + 1
        rotated = self._rebuild(
            binding,
            lifecycle_state=SourceConnectionBindingState.VALIDATING,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            capability_profile_digest=None,
            source_observation_ref=None,
            credential_revision=credential_revision,
        )
        if binding.lifecycle_state is SourceConnectionBindingState.READY:
            self._invalidate_acquisition_authority(binding)
        capability = self._resolve_capability(rotated)
        try:
            self._repository.rotate(rotated, capability, expected_revision=binding.revision)
        except StaleSourceBindingRevisionError:
            raise StaleSourceBindingRevisionError() from None
        return rotated

    @staticmethod
    def _require_revision(binding: SourceConnectionBinding, expected_revision: int) -> None:
        if binding.revision != expected_revision:
            raise StaleSourceBindingRevisionError()

    def _append(self, binding: SourceConnectionBinding, *, expected_revision: int) -> None:
        try:
            self._repository.append(binding, expected_revision=expected_revision)
        except StaleSourceBindingRevisionError:
            raise StaleSourceBindingRevisionError() from None

    def _invalidate_acquisition_authority(self, binding: SourceConnectionBinding) -> None:
        if self._authority_invalidator is None:
            raise SourceBindingBoundaryError(operation="invalidate acquisition authority")
        try:
            self._authority_invalidator.invalidate_source_binding_authority(
                binding.tenant_id,
                binding.binding_id,
                invalidated_revision=binding.revision,
            )
        except Exception:
            raise SourceBindingBoundaryError(operation="invalidate acquisition authority") from None

    def _resolve_capability(
        self,
        binding: SourceConnectionBinding,
    ) -> PrivateSourceCapability:
        try:
            untrusted_capability = self._secret_resolver.resolve(
                tenant_id=binding.tenant_id,
                binding_id=binding.binding_id,
                provider_kind=binding.provider_kind,
                connection_handle=binding.connection_handle,
                account_mode=binding.account_mode,
                credential_revision=binding.credential_revision,
            )
        except Exception:
            raise SourceBindingBoundaryError(operation="resolve private capability") from None
        return self._validated_capability(untrusted_capability, binding=binding)

    @staticmethod
    def _validated_capability(
        capability: object, *, binding: SourceConnectionBinding
    ) -> PrivateSourceCapability:
        if not isinstance(capability, PrivateSourceCapability):
            raise SourceBindingBoundaryError(operation="validate private capability")
        try:
            validated = PrivateSourceCapability.model_validate_json(capability.model_dump_json())
        except Exception:
            raise SourceBindingBoundaryError(operation="validate private capability") from None
        if (
            validated.tenant_id != binding.tenant_id
            or validated.binding_id != binding.binding_id
            or validated.provider_kind != binding.provider_kind
            or validated.connection_handle != binding.connection_handle
            or validated.account_mode != binding.account_mode
            or validated.credential_revision != binding.credential_revision
        ):
            raise SourceBindingBoundaryError(operation="validate private capability")
        return validated

    @staticmethod
    def _validated_evidence(evidence: object) -> SourceBindingValidationEvidence:
        if not isinstance(evidence, SourceBindingValidationEvidence):
            raise SourceBindingBoundaryError(operation="validate probe evidence")
        try:
            return SourceBindingValidationEvidence.model_validate_json(evidence.model_dump_json())
        except Exception:
            raise SourceBindingBoundaryError(operation="validate probe evidence") from None

    @staticmethod
    def _rebuild(
        binding: SourceConnectionBinding,
        *,
        lifecycle_state: SourceConnectionBindingState,
        revision: int,
        updated_at: datetime,
        capability_profile_digest: str | None,
        source_observation_ref: str | None,
        credential_revision: int,
    ) -> SourceConnectionBinding:
        return SourceConnectionBinding(
            schema_version=binding.schema_version,
            binding_id=binding.binding_id,
            tenant_id=binding.tenant_id,
            provider_kind=binding.provider_kind,
            connection_handle=binding.connection_handle,
            account_mode=binding.account_mode,
            lifecycle_state=lifecycle_state,
            approved_object_refs=binding.approved_object_refs,
            capability_profile_digest=capability_profile_digest,
            source_observation_ref=source_observation_ref,
            credential_revision=credential_revision,
            revision=revision,
            created_at=binding.created_at,
            updated_at=updated_at,
        )
