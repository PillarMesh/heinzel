from __future__ import annotations

import os
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
    OpenMetadataProvisioner,
)
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
EMULATOR_ROOT = ROOT / "tests" / "emulators" / "openmetadata"

_EXPECTED_PRIVATE_RESOURCE_COUNTS = (
    ("backup_artifact", 1),
    ("catalog_classification", 1),
    ("catalog_glossary_term", 2),
    ("catalog_lineage", 1),
    ("catalog_policy", 1),
    ("catalog_role", 1),
    ("catalog_tag", 1),
    ("compose_container", 6),
    ("compose_network", 1),
    ("compose_volume", 2),
    ("service_account", 3),
    ("tenant_namespace", 2),
)


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


def no_supported_catalog(*, tenant_id: str, binding_id: str) -> str:
    return "no_supported_catalog"


class LocalOpenMetadata:
    def __init__(self, temporary_path: Path) -> None:
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
        self._secret_store_key = SecretStr(os.environ["PILLARMESH_OPENMETADATA_SECRET_STORE_KEY"])
        self._bootstrap_admin_password = SecretStr(
            os.environ["PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD"]
        )
        self._provisioner = self._new_provisioner()
        self._bindings: list[LocalBinding] = []

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

    def cleanup(self) -> None:
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


@pytest.fixture
def local_openmetadata(tmp_path: Path) -> LocalOpenMetadata:
    local = LocalOpenMetadata(tmp_path)
    try:
        yield local
    finally:
        local.cleanup()


@pytest.mark.live
@pytest.mark.emulator
def test_real_catalog_lifecycle_round_trip(local_openmetadata: LocalOpenMetadata) -> None:
    binding = local_openmetadata.provision_and_validate("tenant-a")
    assert binding.binding.lifecycle_state is CatalogBindingState.READY
    assert binding.validation_evidence is not None
    assert len(binding.validation_evidence.positive_probe_digest) == 64
    assert len(binding.validation_evidence.denial_probe_digest) == 64
    ledger = local_openmetadata.resource_ledger(binding)
    assert ledger.resource_kind_counts == _EXPECTED_PRIVATE_RESOURCE_COUNTS
    assert ledger.all_exact_resources_created
    assert len(ledger.provider_ids) == sum(count for _, count in _EXPECTED_PRIVATE_RESOURCE_COUNTS)

    binding = local_openmetadata.suspend(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.SUSPENDED
    binding = local_openmetadata.resume(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.READY
    restored = local_openmetadata.backup_restore_and_probe(binding)
    assert restored.representative_objects_verified is True

    binding = local_openmetadata.retire(binding)
    assert binding.binding.lifecycle_state is CatalogBindingState.RETIRED
    ledger = local_openmetadata.resource_ledger(binding)
    assert ledger.all_cleaned
