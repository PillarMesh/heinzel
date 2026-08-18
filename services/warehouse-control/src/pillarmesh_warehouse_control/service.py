from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pillarmesh_contract_model import digest

from .models import EngineKind, WarehouseBinding, WarehouseBindingState
from .repository import StaleRevisionError, WarehouseRepository

_TRANSITIONS: dict[WarehouseBindingState, frozenset[WarehouseBindingState]] = {
    WarehouseBindingState.DRAFT: frozenset(
        {WarehouseBindingState.PROVISIONING, WarehouseBindingState.RETIRED}
    ),
    WarehouseBindingState.PROVISIONING: frozenset(
        {WarehouseBindingState.VALIDATING, WarehouseBindingState.FAILED}
    ),
    WarehouseBindingState.VALIDATING: frozenset(
        {WarehouseBindingState.READY, WarehouseBindingState.FAILED}
    ),
    WarehouseBindingState.READY: frozenset(
        {WarehouseBindingState.SUSPENDED, WarehouseBindingState.RETIRING}
    ),
    WarehouseBindingState.FAILED: frozenset({WarehouseBindingState.RETIRED}),
    WarehouseBindingState.SUSPENDED: frozenset(
        {WarehouseBindingState.READY, WarehouseBindingState.RETIRING}
    ),
    WarehouseBindingState.RETIRING: frozenset({WarehouseBindingState.RETIRED}),
    WarehouseBindingState.RETIRED: frozenset(),
}


class WarehouseControlService:
    def __init__(self, repository: WarehouseRepository, *, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def create_draft(
        self,
        *,
        tenant_id: str,
        engine_kind: EngineKind,
        region: str,
        capacity_profile: Literal["mvp-fixed"],
    ) -> WarehouseBinding:
        sequence = self._repository.next_sequence(tenant_id)
        binding_id = (
            "whb-"
            + digest(
                {
                    "domain": "pillarmesh-warehouse-binding-v1",
                    "tenant_id": tenant_id,
                    "sequence": sequence,
                }
            )[:24]
        )
        now = self._clock()
        binding = WarehouseBinding(
            binding_id=binding_id,
            tenant_id=tenant_id,
            engine_kind=engine_kind,
            region=region,
            capacity_profile=capacity_profile,
            capability_profile_digest=self._capability_profile_digest(
                capacity_profile, engine_kind, "pillarmesh_cloud"
            ),
            lifecycle_state=WarehouseBindingState.DRAFT,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self._repository.save(binding)
        return binding

    def revise_draft(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        engine_kind: EngineKind | None = None,
        region: str | None = None,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state is not WarehouseBindingState.DRAFT:
            raise ValueError("binding is immutable after draft")
        revised_engine_kind = engine_kind if engine_kind is not None else binding.engine_kind
        revised = self._rebuild_binding(
            binding,
            engine_kind=revised_engine_kind,
            region=region if region is not None else binding.region,
            lifecycle_state=binding.lifecycle_state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
        )
        self._save_advanced_binding(revised)
        return revised

    def transition(
        self,
        tenant_id: str,
        binding_id: str,
        lifecycle_state: WarehouseBindingState,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if lifecycle_state not in _TRANSITIONS[binding.lifecycle_state]:
            raise ValueError(
                "transition "
                f"{binding.lifecycle_state.value} -> {lifecycle_state.value} is not allowed"
            )
        transitioned = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=lifecycle_state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
        )
        self._save_advanced_binding(transitioned)
        return transitioned

    def get(self, tenant_id: str, binding_id: str) -> WarehouseBinding:
        binding = self._repository.load(tenant_id, binding_id)
        if binding is None or binding.tenant_id != tenant_id:
            # A missing binding and another tenant's binding report identically on
            # purpose: distinguishing them would answer "does this identifier exist"
            # for any caller holding a leaked identifier.
            raise KeyError(f"binding {binding_id} belongs to another tenant")
        return binding

    @staticmethod
    def _capability_profile_digest(
        capacity_profile: Literal["mvp-fixed"],
        engine_kind: EngineKind,
        deployment_mode: Literal["pillarmesh_cloud"],
    ) -> str:
        return digest(
            {
                "capacity_profile": capacity_profile,
                "engine_kind": engine_kind,
                "deployment_mode": deployment_mode,
            }
        )

    @staticmethod
    def _assert_current_revision(binding: WarehouseBinding, expected_revision: int) -> None:
        if binding.revision != expected_revision:
            raise ValueError("binding revision is stale")

    def _save_advanced_binding(self, binding: WarehouseBinding) -> None:
        try:
            self._repository.save(binding)
        except StaleRevisionError as error:
            raise ValueError("binding revision is stale") from error

    def _rebuild_binding(
        self,
        binding: WarehouseBinding,
        *,
        engine_kind: EngineKind,
        region: str,
        lifecycle_state: WarehouseBindingState,
        revision: int,
        updated_at: datetime,
    ) -> WarehouseBinding:
        return WarehouseBinding(
            schema_version=binding.schema_version,
            binding_id=binding.binding_id,
            tenant_id=binding.tenant_id,
            engine_kind=engine_kind,
            deployment_mode=binding.deployment_mode,
            region=region,
            capacity_profile=binding.capacity_profile,
            capability_profile_digest=self._capability_profile_digest(
                binding.capacity_profile, engine_kind, binding.deployment_mode
            ),
            lifecycle_state=lifecycle_state,
            revision=revision,
            created_at=binding.created_at,
            updated_at=updated_at,
            provisioned_at=binding.provisioned_at,
        )
