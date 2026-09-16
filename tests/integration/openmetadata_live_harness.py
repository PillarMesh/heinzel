from __future__ import annotations

import os
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest
from pillarmesh_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogControlService,
    CatalogValidationEvidence,
    SQLiteCatalogRepository,
)
from pillarmesh_provider_openmetadata import (
    DockerComposeController,
    EncryptedDirectoryOpenMetadataSecretStore,
    OpenMetadataClient,
    OpenMetadataProvisioner,
)
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
EMULATOR_ROOT = ROOT / "tests" / "emulators" / "openmetadata"


@dataclass(frozen=True, slots=True)
class LocalBinding:
    binding: CatalogBinding
    private_resource_handle: str
    operation_id: str
    validation_evidence: CatalogValidationEvidence | None = None


@dataclass(frozen=True, slots=True)
class RestoreProbe:
    representative_objects_verified: bool


@dataclass(frozen=True, slots=True)
class ResourceLedger:
    resource_kind_counts: tuple[tuple[str, int], ...]
    provider_ids: frozenset[str]
    all_exact_resources_created: bool
    all_cleaned: bool


class TrackedOpenMetadataResource(Protocol):
    @property
    def collection(self) -> str: ...

    @property
    def identifier(self) -> str: ...


class TrackedOpenMetadataClient(Protocol):
    def discovered_resources(self) -> tuple[TrackedOpenMetadataResource, ...]: ...

    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None: ...

    def recorded_resource_is_absent(self, *, collection: str, identifier: str) -> bool: ...


def no_supported_catalog(*, tenant_id: str, binding_id: str) -> str:
    return "no_supported_catalog"


class LocalOpenMetadata:
    def __init__(self, temporary_path: Path) -> None:
        secret_store_key = os.environ.get("PILLARMESH_OPENMETADATA_SECRET_STORE_KEY")
        bootstrap_admin_password = os.environ.get(
            "PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD"
        )
        if secret_store_key is None or bootstrap_admin_password is None:
            pytest.skip("OpenMetadata live credentials are required")
        self._repository = SQLiteCatalogRepository(str(temporary_path / "catalog.sqlite"))
        self._control = CatalogControlService(
            self._repository,
            clock=lambda: datetime.now(UTC),
        )
        self._compose = DockerComposeController(
            compose_file=EMULATOR_ROOT / "compose.yaml",
            readiness_script=EMULATOR_ROOT / "wait_ready.py",
            base_url="http://127.0.0.1:8585",
        )
        self._secret_store_directory = temporary_path / "openmetadata-operation-secrets"
        self._secret_store_key = SecretStr(secret_store_key)
        self._bootstrap_admin_password = SecretStr(bootstrap_admin_password)
        self._provisioner = self._new_provisioner()
        self._bindings: list[LocalBinding] = []
        self._supplemental_clients: list[TrackedOpenMetadataClient] = []

    def provision_and_validate(self, tenant_id: str) -> LocalBinding:
        draft = self._control.create_draft(tenant_id=tenant_id)
        provisioning = self._control.transition(
            tenant_id,
            draft.binding_id,
            CatalogBindingState.PROVISIONING,
            expected_revision=draft.revision,
        )
        operation_id = f"live-{secrets.token_hex(12)}"
        private_resource_handle = self._provisioner.provision(
            tenant_id=tenant_id,
            binding_id=provisioning.binding_id,
            operation_id=operation_id,
        )
        self._restart_provisioner()
        cleanup_binding = LocalBinding(provisioning, private_resource_handle, operation_id)
        self._bindings.append(cleanup_binding)
        validating = self._control.transition(
            tenant_id,
            provisioning.binding_id,
            CatalogBindingState.VALIDATING,
            expected_revision=provisioning.revision,
        )
        evidence = self._provisioner.validate(
            tenant_id=tenant_id,
            binding_id=validating.binding_id,
            private_resource_handle=private_resource_handle,
        )
        ready = self._control.record_validation(
            tenant_id=tenant_id,
            binding_id=validating.binding_id,
            expected_revision=validating.revision,
            evidence=evidence,
        )
        binding = LocalBinding(ready, private_resource_handle, operation_id, evidence)
        self._replace_binding(binding)
        return binding

    def suspend(self, binding: LocalBinding) -> LocalBinding:
        self._provisioner.suspend(
            private_resource_handle=binding.private_resource_handle,
            operation_id=binding.operation_id,
        )
        suspended = self._control.transition(
            binding.binding.tenant_id,
            binding.binding.binding_id,
            CatalogBindingState.SUSPENDED,
            expected_revision=binding.binding.revision,
        )
        updated = LocalBinding(
            suspended,
            binding.private_resource_handle,
            binding.operation_id,
            binding.validation_evidence,
        )
        self._replace_binding(updated)
        return updated

    def resume(self, binding: LocalBinding) -> LocalBinding:
        self._provisioner.resume(
            private_resource_handle=binding.private_resource_handle,
            operation_id=binding.operation_id,
        )
        ready = self._control.transition(
            binding.binding.tenant_id,
            binding.binding.binding_id,
            CatalogBindingState.READY,
            expected_revision=binding.binding.revision,
        )
        updated = LocalBinding(
            ready,
            binding.private_resource_handle,
            binding.operation_id,
            binding.validation_evidence,
        )
        self._replace_binding(updated)
        return updated

    def backup_restore_and_probe(self, binding: LocalBinding) -> RestoreProbe:
        return RestoreProbe(
            representative_objects_verified=self._provisioner.backup_restore_and_probe(
                private_resource_handle=binding.private_resource_handle,
                operation_id=binding.operation_id,
            )
        )

    def retire(self, binding: LocalBinding) -> LocalBinding:
        retiring = self._control.transition(
            binding.binding.tenant_id,
            binding.binding.binding_id,
            CatalogBindingState.RETIRING,
            expected_revision=binding.binding.revision,
        )
        self._restart_provisioner()
        self._provisioner.retire(
            private_resource_handle=binding.private_resource_handle,
            operation_id=binding.operation_id,
        )
        retired = self._control.transition(
            retiring.tenant_id,
            retiring.binding_id,
            CatalogBindingState.RETIRED,
            expected_revision=retiring.revision,
        )
        updated = LocalBinding(
            retired,
            binding.private_resource_handle,
            binding.operation_id,
            binding.validation_evidence,
        )
        self._replace_binding(updated)
        return updated

    def resource_ledger(self, binding: LocalBinding) -> ResourceLedger:
        resources = self._repository.load_resources(
            binding.binding.tenant_id,
            binding.binding.binding_id,
        )
        return ResourceLedger(
            resource_kind_counts=tuple(
                sorted(Counter(resource.resource_kind.value for resource in resources).items())
            ),
            provider_ids=frozenset(resource.provider_ref for resource in resources),
            all_exact_resources_created=bool(resources)
            and all(
                resource.creation_state in {"created", "validated"}
                and not resource.provider_ref.startswith("planned:")
                for resource in resources
            ),
            all_cleaned=bool(resources)
            and all(
                resource.cleanup_status == "complete" and resource.cleaned_at is not None
                for resource in resources
            ),
        )

    def administrator_client(self, binding: LocalBinding) -> OpenMetadataClient:
        client = self._provisioner.new_administrator_client(
            private_resource_handle=binding.private_resource_handle,
            operation_id=binding.operation_id,
        )
        self._supplemental_clients.append(client)
        return client

    def cleanup_client_resources(
        self, client: TrackedOpenMetadataClient
    ) -> tuple[tuple[str, str], ...]:
        resources = tuple(
            (resource.collection, resource.identifier) for resource in client.discovered_resources()
        )
        for collection, identifier in reversed(resources):
            client.delete_recorded_resource(collection=collection, identifier=identifier)
        for collection, identifier in resources:
            if not client.recorded_resource_is_absent(collection=collection, identifier=identifier):
                raise RuntimeError("OpenMetadata supplemental resource cleanup is not terminal")
        return resources

    def cleanup(self) -> None:
        for client in reversed(self._supplemental_clients):
            self.cleanup_client_resources(client)
        for binding in self._bindings:
            if not self.resource_ledger(binding).all_cleaned:
                self._restart_provisioner()
                self._provisioner.retire(
                    private_resource_handle=binding.private_resource_handle,
                    operation_id=binding.operation_id,
                )

    def _new_provisioner(self) -> OpenMetadataProvisioner:
        secret_store = EncryptedDirectoryOpenMetadataSecretStore(
            directory=self._secret_store_directory,
            key=self._secret_store_key,
            bootstrap_admin_password=self._bootstrap_admin_password,
        )
        return OpenMetadataProvisioner(
            repository=self._repository,
            compose=self._compose,
            availability_decision=no_supported_catalog,
            secret_store=secret_store,
        )

    def _restart_provisioner(self) -> None:
        self._provisioner = self._new_provisioner()

    def _replace_binding(self, binding: LocalBinding) -> None:
        self._bindings = [
            binding
            if existing.private_resource_handle == binding.private_resource_handle
            else existing
            for existing in self._bindings
        ]
