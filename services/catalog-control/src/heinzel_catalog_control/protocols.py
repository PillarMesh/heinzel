from __future__ import annotations

from typing import Protocol

from .models import CatalogValidationEvidence


class CatalogProvisioner(Protocol):
    def provision(self, *, tenant_id: str, binding_id: str, operation_id: str) -> str: ...

    def suspend(self, *, private_resource_handle: str, operation_id: str) -> None: ...

    def resume(self, *, private_resource_handle: str, operation_id: str) -> None: ...

    def retire(self, *, private_resource_handle: str, operation_id: str) -> None: ...


class CatalogValidator(Protocol):
    def validate(
        self, *, tenant_id: str, binding_id: str, private_resource_handle: str
    ) -> CatalogValidationEvidence: ...
