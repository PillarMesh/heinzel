from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .models import CatalogBinding, CatalogBindingState, CatalogValidationEvidence
from .repository import CatalogRepository, CatalogValidationConflictError, StaleRevisionError

_TRANSITIONS: dict[CatalogBindingState, frozenset[CatalogBindingState]] = {
    CatalogBindingState.DRAFT: frozenset(
        {CatalogBindingState.PROVISIONING, CatalogBindingState.RETIRED}
    ),
    CatalogBindingState.PROVISIONING: frozenset(
        {CatalogBindingState.VALIDATING, CatalogBindingState.FAILED}
    ),
    CatalogBindingState.VALIDATING: frozenset(
        {CatalogBindingState.READY, CatalogBindingState.FAILED}
    ),
    CatalogBindingState.READY: frozenset(
        {CatalogBindingState.SUSPENDED, CatalogBindingState.RETIRING}
    ),
    CatalogBindingState.SUSPENDED: frozenset(
        {CatalogBindingState.READY, CatalogBindingState.RETIRING}
    ),
    CatalogBindingState.RETIRING: frozenset({CatalogBindingState.RETIRED}),
    CatalogBindingState.FAILED: frozenset({CatalogBindingState.RETIRED}),
    CatalogBindingState.RETIRED: frozenset(),
}


class CatalogControlService:
    def __init__(self, repository: CatalogRepository, *, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def create_draft(self, *, tenant_id: str) -> CatalogBinding:
        return self._repository.create_draft(tenant_id, self._clock())

    def transition(
        self,
        tenant_id: str,
        binding_id: str,
        state: CatalogBindingState,
        *,
        expected_revision: int,
    ) -> CatalogBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if state not in _TRANSITIONS[binding.lifecycle_state]:
            raise ValueError(
                f"transition {binding.lifecycle_state.value} -> {state.value} is not allowed"
            )
        if (
            binding.lifecycle_state is CatalogBindingState.VALIDATING
            and state is CatalogBindingState.READY
        ):
            raise ValueError("ready transition requires validation evidence")
        transitioned = self._rebuild_binding(
            binding,
            lifecycle_state=state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        self._append_transition(transitioned, expected_revision=binding.revision)
        return transitioned

    def record_validation(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        expected_revision: int,
        evidence: CatalogValidationEvidence,
    ) -> CatalogBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state is not CatalogBindingState.VALIDATING:
            raise ValueError("validation evidence requires a validating binding")
        if (
            evidence.tenant_id != tenant_id
            or evidence.binding_id != binding_id
            or evidence.binding_revision != binding.revision
        ):
            raise ValueError("validation evidence does not match the current binding")
        now = self._clock()
        ready = self._rebuild_binding(
            binding,
            lifecycle_state=CatalogBindingState.READY,
            revision=binding.revision + 1,
            updated_at=now,
            provisioned_at=now,
        )
        try:
            self._repository.record_validation(ready, evidence, expected_revision=binding.revision)
        except StaleRevisionError as error:
            raise ValueError("binding revision is stale") from error
        except CatalogValidationConflictError as error:
            raise ValueError("validation evidence was not recorded") from error
        return ready

    def get(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        binding = self._repository.load(tenant_id, binding_id)
        if binding.tenant_id != tenant_id:
            raise KeyError(f"binding {binding_id} belongs to another tenant")
        return binding

    @staticmethod
    def _assert_current_revision(binding: CatalogBinding, expected_revision: int) -> None:
        if binding.revision != expected_revision:
            raise ValueError("binding revision is stale")

    def _append_transition(self, binding: CatalogBinding, *, expected_revision: int) -> None:
        try:
            self._repository.append_transition(binding, expected_revision=expected_revision)
        except StaleRevisionError as error:
            raise ValueError("binding revision is stale") from error

    @staticmethod
    def _rebuild_binding(
        binding: CatalogBinding,
        *,
        lifecycle_state: CatalogBindingState,
        revision: int,
        updated_at: datetime,
        provisioned_at: datetime | None,
    ) -> CatalogBinding:
        return CatalogBinding(
            schema_version=binding.schema_version,
            binding_id=binding.binding_id,
            tenant_id=binding.tenant_id,
            provider_kind=binding.provider_kind,
            deployment_mode=binding.deployment_mode,
            capability_profile_digest=binding.capability_profile_digest,
            lifecycle_state=lifecycle_state,
            revision=revision,
            created_at=binding.created_at,
            updated_at=updated_at,
            provisioned_at=provisioned_at,
        )
