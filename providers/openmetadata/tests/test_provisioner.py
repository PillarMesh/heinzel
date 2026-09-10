from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from typing import IO

import pillarmesh_provider_openmetadata.provisioner as provisioner_module
import pytest
from cryptography.fernet import Fernet
from pillarmesh_catalog_control import (
    CatalogBindingState,
    CatalogControlService,
    SQLiteCatalogRepository,
)
from pillarmesh_catalog_control.repository import CatalogPersistenceError
from pillarmesh_contract_model import digest
from pillarmesh_provider_openmetadata import (
    UPSTREAM_IMAGES,
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    DockerComposeController,
    OpenMetadataProvisioner,
    ProviderBuildIdentity,
    ProviderHealth,
)
from pillarmesh_provider_openmetadata.client import _OpenMetadataCredentials
from pydantic import SecretStr

_FAKE_LINEAGE_IDENTIFIER = (
    '{"fromEntity":{"id":"11111111-1111-4111-8111-111111111111",'
    '"type":"glossaryTerm"},"toEntity":{"id":"22222222-2222-4222-8222-222222222222",'
    '"type":"glossaryTerm"},"description":"validation"}'
)
_SENSITIVE_PROVISIONER_VALUE = "test-only-sensitive-provisioner-value"


class _FinishedDockerProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdin: IO[bytes] | None = None
        # The test-double process owns these pipes until its test exits.
        self.stdout = tempfile.TemporaryFile()  # noqa: SIM115
        self.stderr = tempfile.TemporaryFile()  # noqa: SIM115
        self.stdout.write(stdout)
        self.stderr.write(stderr)
        self.stdout.seek(0)
        self.stderr.seek(0)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class SecretProvisionerDriverError(RuntimeError):
    pass


def assert_provider_failure_is_sanitized(error: CatalogProviderError) -> None:
    rendered_exception = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SENSITIVE_PROVISIONER_VALUE not in rendered_exception
    assert "SecretProvisionerDriverError" not in rendered_exception
    assert _SENSITIVE_PROVISIONER_VALUE not in repr(error)
    assert "SecretProvisionerDriverError" not in repr(error)


def test_restore_step_failure_is_classified_and_sanitized() -> None:
    def fail() -> None:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)

    with pytest.raises(
        CatalogProviderError,
        match="OpenMetadata isolated restore database import failed",
    ) as captured:
        provisioner_module._run_restore_step("database import", fail)

    assert captured.value.classification == "transient"
    assert_provider_failure_is_sanitized(captured.value)


class RecordingCompose:
    def __init__(
        self,
        *,
        fail_up: bool = False,
        fail_backup: bool = False,
        fail_restore: bool = False,
        fail_search_rebuild: bool = False,
        fail_discovery: bool = False,
        fail_down: bool = False,
        preserve_resources_after_down: bool = False,
        recreate_resources_on_restore: bool = False,
    ) -> None:
        self.project_names: list[str] = []
        self.environment_digests: list[str] = []
        self.stopped_project_names: list[str] = []
        self.started_project_names: list[str] = []
        self.down_project_names: list[str] = []
        self.search_rebuild_project_names: list[str] = []
        self.removed_resources: list[tuple[str, str]] = []
        self._fail_up = fail_up
        self._fail_backup = fail_backup
        self._fail_restore = fail_restore
        self._fail_search_rebuild = fail_search_rebuild
        self._fail_discovery = fail_discovery
        self._fail_down = fail_down
        self._preserve_resources_after_down = preserve_resources_after_down
        self._recreate_resources_on_restore = recreate_resources_on_restore
        self._resource_generation = 0
        self._present_resources: set[tuple[str, str]] = set()

    def up(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        self.project_names.append(project_name)
        if environment is not None:
            self.environment_digests.append(digest(dict(environment)))
        self._present_resources.update(self._project_resources(project_name))
        if self._fail_up:
            raise RuntimeError("compose detail must not cross the provider boundary")

    def stop(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        self.stopped_project_names.append(project_name)

    def start(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        self.started_project_names.append(project_name)

    def down(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        self.down_project_names.append(project_name)
        if self._fail_down:
            raise RuntimeError("project cleanup detail must not cross the provider boundary")
        if not self._preserve_resources_after_down:
            self._present_resources = {
                resource for resource in self._present_resources if project_name not in resource[1]
            }

    def backup(
        self,
        *,
        project_name: str,
        backup_path: Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if self._fail_backup:
            raise RuntimeError("backup detail must not cross the provider boundary")
        backup_path.write_text("backup")

    def restore(
        self,
        *,
        project_name: str,
        backup_path: Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if self._fail_restore:
            raise RuntimeError("restore detail must not cross the provider boundary")
        assert backup_path.read_text() == "backup"
        if self._recreate_resources_on_restore:
            self._resource_generation += 1
            self._present_resources = {
                resource for resource in self._present_resources if project_name not in resource[1]
            }
            self._present_resources.update(self._project_resources(project_name))

    def rebuild_search_index(
        self, *, project_name: str, environment: Mapping[str, str] | None = None
    ) -> None:
        self.search_rebuild_project_names.append(project_name)
        if self._fail_search_rebuild:
            raise RuntimeError("search rebuild detail must not cross the provider boundary")

    def verify_pinned_images(
        self, *, project_name: str, environment: Mapping[str, str] | None = None
    ) -> str:
        return digest(tuple(sorted(UPSTREAM_IMAGES)))

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(resource_kind="container", identifier=f"{project_name}-container"),
            SimpleNamespace(resource_kind="volume", identifier=f"{project_name}-volume"),
            SimpleNamespace(resource_kind="network", identifier=f"{project_name}-network"),
        )

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[SimpleNamespace, ...]:
        if self._fail_discovery:
            raise RuntimeError("discovery detail must not cross the provider boundary")
        return tuple(
            SimpleNamespace(resource_kind=resource_kind, identifier=identifier)
            for resource_kind, identifier in sorted(
                resource for resource in self._present_resources if project_name in resource[1]
            )
        )

    def remove_resource(
        self, *, resource_kind: str, identifier: str, environment: Mapping[str, str]
    ) -> None:
        self.removed_resources.append((resource_kind, identifier))
        self._present_resources.discard((resource_kind, identifier))

    def resource_is_absent(
        self, *, resource_kind: str, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        return (resource_kind, identifier) not in self._present_resources

    def active_resources(self) -> set[tuple[str, str]]:
        return set(self._present_resources)

    def _project_resources(self, project_name: str) -> set[tuple[str, str]]:
        suffix = (
            "" if self._resource_generation == 0 else f"-replacement-{self._resource_generation}"
        )
        return {
            ("container", f"{project_name}-container-id{suffix}"),
            ("volume", f"{project_name}-volume-id{suffix}"),
            ("network", f"{project_name}-network-id{suffix}"),
        }


class CredentialRejectingCompose(RecordingCompose):
    def __init__(self) -> None:
        super().__init__()
        self.received_credential_environment = False

    def up(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        assert environment is not None
        self.received_credential_environment = True
        super().up(project_name=project_name, environment=environment)
        raise RuntimeError(
            "OpenMetadata Compose rejected private credential "
            + environment["PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD"]
        )


class FlakyPlanningCompose(RecordingCompose):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self._failure = failure
        self._failed = False

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[SimpleNamespace, ...]:
        if not self._failed:
            self._failed = True
            raise self._failure
        return super().planned_resources(project_name=project_name, environment=environment)


class InterruptingUpCompose(RecordingCompose):
    def __init__(self) -> None:
        super().__init__()
        self._interrupted = False

    def up(self, *, project_name: str, environment: Mapping[str, str] | None = None) -> None:
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt("simulated interruption after operation recording")
        super().up(project_name=project_name, environment=environment)


class BlockingFirstPlanningCompose(RecordingCompose):
    def __init__(self) -> None:
        super().__init__()
        self.first_planning_entered = Event()
        self.second_planning_entered = Event()
        self.release_first_planning = Event()
        self._planning_calls = 0

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[SimpleNamespace, ...]:
        self._planning_calls += 1
        if self._planning_calls == 1:
            self.first_planning_entered.set()
            assert self.release_first_planning.wait(timeout=5)
        else:
            self.second_planning_entered.set()
        return super().planned_resources(project_name=project_name, environment=environment)


@dataclass(frozen=True, slots=True)
class _RecordedProviderResource:
    resource_kind: str
    identifier: str
    collection: str


class ReadyClient:
    def __init__(self) -> None:
        self.namespace_tenants: list[str] = []
        self.searchable_checks: list[tuple[str, str]] = []
        self.deleted_references: list[CatalogObjectRef] = []
        self.owner_assignments: list[tuple[str, str]] = []
        self.deleted_recorded_resources: list[tuple[str, str]] = []
        self._provider_resources: dict[tuple[str, str], _RecordedProviderResource] = {}

    def _remember(self, *, resource_kind: str, identifier: str, collection: str) -> None:
        self._provider_resources[(collection, identifier)] = _RecordedProviderResource(
            resource_kind=resource_kind,
            identifier=identifier,
            collection=collection,
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth()

    def build_identity(self) -> ProviderBuildIdentity:
        return ProviderBuildIdentity(
            provider_version="1.13.3",
            revision="a" * 40,
            build_timestamp=1,
        )

    def assert_object_searchable(self, *, tenant_key: str, identity: str) -> None:
        self.searchable_checks.append((tenant_key, identity))

    def ensure_tenant_namespace(self, *, tenant_key: str, idempotency_key: str) -> CatalogObjectRef:
        self.namespace_tenants.append(tenant_key)
        self._remember(
            resource_kind="namespace",
            identifier=f"namespace-provider-id-{tenant_key}",
            collection="glossaries",
        )
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=f"namespace:{tenant_key}:namespace",
            normalized_digest="a" * 64,
        )

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef:
        if identity == "runtime":
            self._remember(
                resource_kind="object",
                identifier=f"runtime-policy-provider-id-{tenant_key}",
                collection="policies",
            )
            self._remember(
                resource_kind="object",
                identifier=f"runtime-role-provider-id-{tenant_key}",
                collection="roles",
            )
        self._remember(
            resource_kind="user",
            identifier=f"{identity}-provider-id-{tenant_key}",
            collection="users",
        )
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=f"service_identity:{tenant_key}:{identity}",
            normalized_digest="b" * 64,
        )

    def ensure_glossary_term(self, **kwargs: object) -> CatalogObjectRef:
        identity = kwargs["identity"]
        tenant_key = kwargs["tenant_key"]
        assert isinstance(identity, str)
        assert isinstance(tenant_key, str)
        self._remember(
            resource_kind="object",
            identifier=f"{identity}-provider-id-{tenant_key}",
            collection="glossaryTerms",
        )
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=f"glossary_term:{tenant_key}:{identity}",
            normalized_digest="c" * 64,
        )

    def ensure_classification(self, **kwargs: object) -> CatalogObjectRef:
        identity = kwargs["identity"]
        tenant_key = kwargs["tenant_key"]
        assert isinstance(identity, str)
        assert isinstance(tenant_key, str)
        self._remember(
            resource_kind="object",
            identifier=f"{identity}-provider-id-{tenant_key}",
            collection="classifications",
        )
        self._remember(
            resource_kind="object",
            identifier=f"{identity}-tag-provider-id-{tenant_key}",
            collection="tags",
        )
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=f"classification:{tenant_key}:{identity}",
            normalized_digest="d" * 64,
        )

    def ensure_lineage(self, **kwargs: object) -> CatalogObjectRef:
        identity = kwargs["identity"]
        tenant_key = kwargs["tenant_key"]
        assert isinstance(identity, str)
        assert isinstance(tenant_key, str)
        self._remember(
            resource_kind="object",
            identifier=_FAKE_LINEAGE_IDENTIFIER,
            collection="lineage",
        )
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=f"lineage:{tenant_key}:{identity}",
            normalized_digest="e" * 64,
        )

    def get_object(self, **kwargs: object) -> CatalogObjectSnapshot:
        tenant_key = kwargs["tenant_key"]
        assert isinstance(tenant_key, str)
        normalized_payload = {"name": "validation"}
        return CatalogObjectSnapshot(
            tenant_key=tenant_key,
            stable_identity=f"glossary_term:{tenant_key}:validation-term-from",
            logical_identity="validation-term-from",
            object_kind="glossary_term",
            normalized_payload=normalized_payload,
            normalized_digest=digest(normalized_payload),
        )

    def delete_object(self, reference: CatalogObjectRef) -> None:
        self.deleted_references.append(reference)

    def assign_owner(self, reference: CatalogObjectRef, owner: CatalogObjectRef) -> None:
        self.owner_assignments.append((reference.stable_identity, owner.stable_identity))

    def rotate_admin_password(self, new_password: SecretStr) -> None:
        return None

    def discovered_resources(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self._provider_resources.values())

    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None:
        self.deleted_recorded_resources.append((collection, identifier))
        self._provider_resources.pop((collection, identifier), None)

    def recorded_resource_is_absent(self, *, collection: str, identifier: str) -> bool:
        return (collection, identifier) not in self._provider_resources


class DependencyAwareCleanupClient(ReadyClient):
    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None:
        owned_collections = {"glossaryTerms", "classifications", "glossaries"}
        if (
            collection == "users"
            and identifier.endswith("-tenant-a")
            and any(
                resource.collection in owned_collections
                for resource in self._provider_resources.values()
            )
        ):
            raise RuntimeError("service account cleanup must follow owned resources")
        super().delete_recorded_resource(collection=collection, identifier=identifier)


class PlanningAwareClient(ReadyClient):
    def __init__(self, repository: SQLiteCatalogRepository, binding_id: str) -> None:
        super().__init__()
        self._repository = repository
        self._binding_id = binding_id
        self._runtime_plan_checked = False

    def _assert_planned(self, *resource_kinds: str) -> None:
        resources = self._repository.load_resources("tenant-a", self._binding_id)
        planned_kinds = {
            resource.resource_kind.value
            for resource in resources
            if resource.creation_state == "planned"
        }
        assert set(resource_kinds) <= planned_kinds

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef:
        if identity == "runtime" and not self._runtime_plan_checked:
            self._assert_planned("service_account", "catalog_policy", "catalog_role")
            self._runtime_plan_checked = True
        return super().ensure_service_identity(
            tenant_key=tenant_key,
            identity=identity,
            idempotency_key=idempotency_key,
        )

    def ensure_classification(self, **kwargs: object) -> CatalogObjectRef:
        self._assert_planned("catalog_classification", "catalog_tag")
        return super().ensure_classification(**kwargs)

    def ensure_lineage(self, **kwargs: object) -> CatalogObjectRef:
        self._assert_planned("catalog_lineage")
        return super().ensure_lineage(**kwargs)


class ProvisionStepFailureClient(ReadyClient):
    def __init__(self, *, failing_step: str, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure
        self._failing_step = failing_step

    def ensure_tenant_namespace(self, *, tenant_key: str, idempotency_key: str) -> CatalogObjectRef:
        if self._failing_step == "namespace":
            raise self.failure
        return super().ensure_tenant_namespace(
            tenant_key=tenant_key,
            idempotency_key=idempotency_key,
        )

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef:
        if self._failing_step == identity:
            raise self.failure
        return super().ensure_service_identity(
            tenant_key=tenant_key,
            identity=identity,
            idempotency_key=idempotency_key,
        )

    def assign_owner(self, reference: CatalogObjectRef, owner: CatalogObjectRef) -> None:
        if self._failing_step == "owner assignment":
            raise self.failure
        super().assign_owner(reference, owner)


class LineageVerificationFailureClient(ReadyClient):
    def ensure_lineage(self, **kwargs: object) -> CatalogObjectRef:
        super().ensure_lineage(**kwargs)
        raise CatalogProviderError(
            "OpenMetadata lineage verification response failed provider validation",
            classification="permanent",
        )


class RestoreDivergenceClient(ReadyClient):
    def __init__(self) -> None:
        super().__init__()
        self._snapshot_reads = 0

    def get_object(self, **kwargs: object) -> CatalogObjectSnapshot:
        snapshot = super().get_object(**kwargs)
        self._snapshot_reads += 1
        if self._snapshot_reads == 1:
            return snapshot
        changed_payload = {"name": "changed-after-restore"}
        return snapshot.model_copy(
            update={
                "normalized_payload": changed_payload,
                "normalized_digest": digest(changed_payload),
            }
        )


class RotationCrashClient(ReadyClient):
    def __init__(
        self,
        *,
        supplied_password: SecretStr,
        bootstrap_password: SecretStr,
        rotated_password: SecretStr,
        state: dict[str, int | bool],
    ) -> None:
        super().__init__()
        self._supplied_password = supplied_password
        self._bootstrap_password = bootstrap_password
        self._rotated_password = rotated_password
        self._state = state

    def health(self) -> ProviderHealth:
        expected = self._rotated_password if self._state["rotated"] else self._bootstrap_password
        if self._supplied_password.get_secret_value() != expected.get_secret_value():
            raise CatalogProviderError(
                "OpenMetadata authentication failed",
                classification="authentication",
            )
        return ProviderHealth()

    def rotate_admin_password(self, new_password: SecretStr) -> None:
        self._state["rotation_calls"] = int(self._state["rotation_calls"]) + 1
        self._state["rotated"] = True
        if self._state["rotation_calls"] == 1:
            raise KeyboardInterrupt("simulated lost password rotation acknowledgement")


class RecordingSecretStore:
    def __init__(self) -> None:
        self._delegate = provisioner_module.InMemoryOpenMetadataSecretStore(
            bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
        )
        self.created_references: list[str] = []
        self.created_bundles: list[provisioner_module.OpenMetadataOperationSecrets] = []
        self.deleted_references: list[str] = []

    def create(self) -> tuple[str, provisioner_module.OpenMetadataOperationSecrets]:
        secret_reference, secrets_bundle = self._delegate.create()
        self.created_references.append(secret_reference)
        self.created_bundles.append(secrets_bundle)
        return secret_reference, secrets_bundle

    def ensure(self, secret_reference: str) -> provisioner_module.OpenMetadataOperationSecrets:
        try:
            return self._delegate.resolve(secret_reference)
        except RuntimeError:
            secrets_bundle = self._delegate.ensure(secret_reference)
            self.created_references.append(secret_reference)
            self.created_bundles.append(secrets_bundle)
            return secrets_bundle

    def resolve(self, secret_reference: str) -> provisioner_module.OpenMetadataOperationSecrets:
        return self._delegate.resolve(secret_reference)

    def bootstrap_admin_password(self) -> SecretStr:
        return self._delegate.bootstrap_admin_password()

    def delete(self, secret_reference: str) -> None:
        self.deleted_references.append(secret_reference)
        self._delegate.delete(secret_reference)


class SecretFailingSecretStore(RecordingSecretStore):
    def create(self) -> tuple[str, provisioner_module.OpenMetadataOperationSecrets]:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)

    def ensure(self, secret_reference: str) -> provisioner_module.OpenMetadataOperationSecrets:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)


class DeniedRuntimeClient:
    def __init__(self) -> None:
        self.administration_checked = False
        self.cross_tenant_checked = False

    def assert_administration_denied(self) -> None:
        self.administration_checked = True

    def assert_other_tenant_namespace_denied(self, *, tenant_key: str) -> None:
        assert tenant_key == "other-tenant"
        self.cross_tenant_checked = True


def controller(tmp_path: Path) -> DockerComposeController:
    return DockerComposeController(
        compose_file=tmp_path / "compose.yaml",
        readiness_script=tmp_path / "wait_ready.py",
        base_url="http://127.0.0.1:8585",
    )


def no_supported_catalog(*, tenant_id: str, binding_id: str) -> str:
    return "no_supported_catalog"


def compose_environment() -> dict[str, str]:
    return {
        "PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD": "test-only-root-password",
        "PILLARMESH_OPENMETADATA_DATABASE_PASSWORD": "test-only-database-password",
        "PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD": "test-only-airflow-password",
    }


def encrypted_secret_store(
    directory: Path, *, key: bytes | None = None
) -> provisioner_module.EncryptedDirectoryOpenMetadataSecretStore:
    selected_key = key or Fernet.generate_key()
    return provisioner_module.EncryptedDirectoryOpenMetadataSecretStore(
        directory=directory,
        key=SecretStr(selected_key.decode("ascii")),
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password"),
    )


def provisioner_for(
    repository: SQLiteCatalogRepository,
    compose: RecordingCompose,
    client: ReadyClient,
    *,
    availability_decision=no_supported_catalog,
    secret_store: object | None = None,
) -> OpenMetadataProvisioner:
    secret_store_type = getattr(provisioner_module, "InMemoryOpenMetadataSecretStore", None)
    assert secret_store_type is not None
    selected_secret_store = secret_store or secret_store_type(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    return OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        client_factory=lambda _: client,
        runtime_client_factory=lambda _: DeniedRuntimeClient(),
        availability_decision=availability_decision,
        secret_store=selected_secret_store,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )


def _private_secret_digests(secret_store: object, secret_reference: str) -> frozenset[str]:
    secrets_bundle = secret_store.resolve(secret_reference)
    values = (
        secrets_bundle.admin_password,
        secrets_bundle.runtime_password,
        secrets_bundle.administrator_password,
        secrets_bundle.mysql_root_password,
        secrets_bundle.database_password,
        secrets_bundle.airflow_database_password,
    )
    return frozenset(digest({"secret": value.get_secret_value()}) for value in values)


def test_secret_store_account_passwords_meet_openmetadata_policy_with_a_deficient_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioner_module.secrets, "token_urlsafe", lambda _: "a" * 52)
    secret_store = RecordingSecretStore()

    _, secrets_bundle = secret_store.create()

    for password in (
        secrets_bundle.admin_password,
        secrets_bundle.runtime_password,
        secrets_bundle.administrator_password,
    ):
        value = password.get_secret_value()
        assert 8 <= len(value) <= 56
        assert any(character.isupper() for character in value)
        assert any(character.islower() for character in value)
        assert any(character.isdigit() for character in value)
        assert any(not character.isalnum() for character in value)
        assert not any(character.isspace() for character in value)

    assert secrets_bundle.mysql_root_password.get_secret_value() == "a" * 52
    assert secrets_bundle.database_password.get_secret_value() == "a" * 52
    assert secrets_bundle.airflow_database_password.get_secret_value() == "a" * 52


def test_secret_store_generates_distinct_openmetadata_account_passwords() -> None:
    secret_store = RecordingSecretStore()

    _, secrets_bundle = secret_store.create()

    assert (
        len(
            {
                secrets_bundle.admin_password.get_secret_value(),
                secrets_bundle.runtime_password.get_secret_value(),
                secrets_bundle.administrator_password.get_secret_value(),
            }
        )
        == 3
    )


def test_encrypted_secret_store_recovers_with_new_object_and_enforces_private_modes(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private-secrets"
    key = Fernet.generate_key()
    first_store = encrypted_secret_store(directory, key=key)

    secret_reference, created = first_store.create()
    stored_file = next(directory.iterdir())
    second_store = encrypted_secret_store(directory, key=key)

    assert second_store.resolve(secret_reference) == created
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored_file.stat().st_mode) == 0o600
    encrypted_payload = stored_file.read_bytes()
    assert created.admin_password.get_secret_value().encode() not in encrypted_payload
    assert "/" not in secret_reference
    assert ".." not in secret_reference


def test_encrypted_secret_store_fails_closed_for_wrong_key_and_corrupt_payload(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private-secrets"
    key = Fernet.generate_key()
    secret_store = encrypted_secret_store(directory, key=key)
    secret_reference, _ = secret_store.create()

    with pytest.raises(RuntimeError, match="secret storage is unavailable") as wrong_key:
        encrypted_secret_store(directory, key=Fernet.generate_key()).resolve(secret_reference)

    stored_file = next(directory.iterdir())
    payload = json.loads(Fernet(key).decrypt(stored_file.read_bytes()))
    payload["unexpected"] = "must be rejected"
    stored_file.write_bytes(Fernet(key).encrypt(json.dumps(payload).encode()))
    os.chmod(stored_file, 0o600)
    with pytest.raises(RuntimeError, match="secret storage is unavailable") as corrupt:
        encrypted_secret_store(directory, key=key).resolve(secret_reference)

    assert secret_reference not in str(wrong_key.value)
    assert secret_reference not in str(corrupt.value)


def test_encrypted_secret_store_rejects_a_symlinked_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "private-secrets"
    symlink.symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="secret storage is unavailable"):
        encrypted_secret_store(symlink)


def test_encrypted_secret_store_rejects_directory_replaced_by_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "private-secrets"
    secret_store = encrypted_secret_store(directory)
    secret_reference, _ = secret_store.create()
    moved_directory = tmp_path / "moved-private-secrets"
    directory.rename(moved_directory)
    directory.symlink_to(moved_directory, target_is_directory=True)

    with pytest.raises(RuntimeError, match="secret storage is unavailable"):
        secret_store.resolve(secret_reference)


def test_provision_fails_closed_when_private_secret_store_is_missing() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioner = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        client_factory=lambda _: ReadyClient(),
        runtime_client_factory=lambda _: DeniedRuntimeClient(),
        availability_decision=no_supported_catalog,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata private credentials are unavailable"
    assert compose.project_names == []
    repository._connection.close()


def test_secret_store_failure_does_not_cross_the_provider_traceback_boundary() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(
            repository,
            RecordingCompose(),
            ReadyClient(),
            secret_store=SecretFailingSecretStore(),
        ).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata private credentials are unavailable"
    assert_provider_failure_is_sanitized(captured.value)
    repository._connection.close()


def test_availability_failure_does_not_cross_the_provider_traceback_boundary() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)

    def fail_availability(*, tenant_id: str, binding_id: str) -> str:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(
            repository,
            RecordingCompose(),
            ReadyClient(),
            availability_decision=fail_availability,
        ).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata catalog selection does not permit provisioning"
    assert_provider_failure_is_sanitized(captured.value)
    repository._connection.close()


def test_repository_driver_failure_does_not_cross_the_provider_traceback_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")

    def fail_load(tenant_id: str, binding_id: str) -> None:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)

    monkeypatch.setattr(repository, "load", fail_load)
    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, RecordingCompose(), ReadyClient()).provision(
            tenant_id="tenant-a",
            binding_id="binding-a",
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata provisioning failed"
    assert_provider_failure_is_sanitized(captured.value)
    repository._connection.close()


def test_provision_uses_independent_private_credentials_without_public_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    first_binding = provisioning_binding(repository, tenant_id="tenant-a")
    second_binding = provisioning_binding(repository, tenant_id="tenant-b")
    compose = RecordingCompose()
    captured_credentials: list[object] = []

    def create_client(*, settings: object, credentials: object) -> ReadyClient:
        captured_credentials.append(credentials)
        return ReadyClient()

    monkeypatch.setattr(provisioner_module, "OpenMetadataClient", create_client)
    secret_store = provisioner_module.InMemoryOpenMetadataSecretStore(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    provisioner = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        availability_decision=no_supported_catalog,
        secret_store=secret_store,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )

    first_handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=first_binding.binding_id,
        operation_id="operation-a",
    )
    second_handle = provisioner.provision(
        tenant_id="tenant-b",
        binding_id=second_binding.binding_id,
        operation_id="operation-b",
    )

    first_operation = repository.load_operation("tenant-a", first_binding.binding_id, "operation-a")
    second_operation = repository.load_operation(
        "tenant-b", second_binding.binding_id, "operation-b"
    )
    first_secret_digests = _private_secret_digests(secret_store, first_operation.secret_reference)
    second_secret_digests = _private_secret_digests(secret_store, second_operation.secret_reference)
    persisted_values = frozenset(
        (
            first_handle,
            second_handle,
            first_operation.project_name,
            second_operation.project_name,
            first_operation.resource_handle,
            second_operation.resource_handle,
            first_operation.secret_reference,
            second_operation.secret_reference,
        )
    )

    assert len(first_secret_digests) == 6
    assert len(second_secret_digests) == 6
    assert first_secret_digests.isdisjoint(second_secret_digests)
    assert first_secret_digests.isdisjoint(persisted_values)
    assert second_secret_digests.isdisjoint(persisted_values)
    assert len(compose.environment_digests) == 2
    assert compose.environment_digests[0] != compose.environment_digests[1]
    assert len(captured_credentials) == 2
    repository._connection.close()


def test_compose_credential_startup_failure_is_sanitized_at_the_provider_boundary() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = CredentialRejectingCompose()
    secret_store = RecordingSecretStore()
    provisioner = provisioner_for(
        repository,
        compose,
        ReadyClient(),
        secret_store=secret_store,
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    secrets_bundle = secret_store.created_bundles[0]
    credential_leaked = any(
        secret.get_secret_value() in str(captured.value)
        for secret in (
            secrets_bundle.admin_password,
            secrets_bundle.runtime_password,
            secrets_bundle.administrator_password,
            secrets_bundle.mysql_root_password,
            secrets_bundle.database_password,
            secrets_bundle.airflow_database_password,
        )
    )
    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata provisioning failed"
    assert compose.received_credential_environment
    assert not credential_leaked
    assert secret_store.deleted_references == secret_store.created_references
    repository._connection.close()


def provisioning_binding(repository: SQLiteCatalogRepository, tenant_id: str = "tenant-a"):
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    draft = control.create_draft(tenant_id=tenant_id)
    return control.transition(
        tenant_id,
        draft.binding_id,
        CatalogBindingState.PROVISIONING,
        expected_revision=draft.revision,
    )


def test_backup_uses_the_root_password_inside_the_mysql_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        assert kwargs["stdin"] is None
        return _FinishedDockerProcess(stdout=b"backup")

    monkeypatch.setattr(subprocess, "Popen", record_popen)

    controller(tmp_path).backup(
        project_name="project-a",
        backup_path=tmp_path / "backup.sql",
        environment=compose_environment(),
    )

    assert "util.dumpSchemas" in commands[0][-1]
    assert "--socket=/var/lib/mysql/mysql.sock" in commands[0][-1]
    assert "tar --create --directory=/tmp pillarmesh-openmetadata-dump" in commands[0][-1]


def test_restore_uses_the_root_password_inside_the_mysql_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    compose = controller(tmp_path)
    backup_path = tmp_path / "backup.sql"
    backup_path.write_bytes(b"backup")

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        source = kwargs["stdin"]
        assert hasattr(source, "read")
        return _FinishedDockerProcess()

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    monkeypatch.setattr(compose, "down", lambda **_: None)
    monkeypatch.setattr(compose, "_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(compose, "_wait_for_mysql", lambda *args, **kwargs: None)
    monkeypatch.setattr(compose, "_run_readiness", lambda: None)

    compose.restore(
        project_name="project-a",
        backup_path=backup_path,
        environment=compose_environment(),
    )

    restore_command = commands[0][-1]
    assert "tar --extract --directory=/tmp" in restore_command
    assert "DROP DATABASE IF EXISTS openmetadata_db" in restore_command
    assert "SET GLOBAL local_infile=ON" in restore_command
    assert "util.loadDump" in restore_command
    assert "SET GLOBAL local_infile=OFF" in restore_command


def test_restore_readiness_waits_for_an_authenticated_mysql_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        return _FinishedDockerProcess()

    monkeypatch.setattr(subprocess, "Popen", record_popen)

    controller(tmp_path)._wait_for_mysql("project-a", environment=compose_environment())

    assert commands[0][-1] == (
        'exec mysql --user=root --password="$MYSQL_ROOT_PASSWORD" --silent --execute "SELECT 1"'
    )


def test_search_rebuild_runs_the_pinned_openmetadata_cli_to_terminal_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        return _FinishedDockerProcess(stdout=b"complete")

    monkeypatch.setattr(subprocess, "Popen", record_popen)

    controller(tmp_path).rebuild_search_index(
        project_name="project-a",
        environment=compose_environment(),
    )

    assert commands == [
        [
            "docker",
            "compose",
            "--project-name",
            "project-a",
            "--file",
            str(tmp_path / "compose.yaml"),
            "exec",
            "-T",
            "openmetadata-server",
            "./bootstrap/openmetadata-ops.sh",
            "reindex",
            "--force",
            "--entities=glossary,glossaryTerm,classification,tag,user",
        ],
    ]


def test_search_rebuild_retries_one_transient_cli_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts = 0

    def flaky_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        nonlocal attempts
        if "reindex" in command:
            attempts += 1
        return _FinishedDockerProcess(
            returncode=1 if "reindex" in command and attempts == 1 else 0,
            stdout=b"",
            stderr=b"transient search startup",
        )

    monkeypatch.setattr(subprocess, "Popen", flaky_popen)

    controller(tmp_path).rebuild_search_index(
        project_name="project-a",
        environment=compose_environment(),
    )

    assert attempts == 2


def test_search_rebuild_fails_closed_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts = 0
    commands: list[list[str]] = []

    def rejected_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        nonlocal attempts
        commands.append(command)
        if "reindex" in command:
            attempts += 1
            return _FinishedDockerProcess(returncode=1, stderr=b"private detail")
        return _FinishedDockerProcess()

    monkeypatch.setattr(subprocess, "Popen", rejected_popen)

    with pytest.raises(RuntimeError, match="OpenMetadata search index rebuild failed"):
        controller(tmp_path).rebuild_search_index(
            project_name="project-a",
            environment=compose_environment(),
        )

    assert attempts == 2
    assert "reindex" in commands[-1]
    assert all("ingestion" not in command for command in commands)


def test_compose_controller_plans_and_discovers_only_exact_project_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        if "config" in command:
            return _FinishedDockerProcess(
                stdout=json.dumps(
                    {
                        "services": {"server": {}, "mysql": {}},
                        "volumes": {"database": {}},
                        "networks": {"default": {}},
                    }
                ).encode(),
                stderr=b"",
            )
        outputs = {
            "container": b"container-b\ncontainer-a\n",
            "volume": b"volume-a\n",
            "network": b"network-a\n",
        }
        return _FinishedDockerProcess(stdout=outputs[command[1]])

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    compose = controller(tmp_path)

    planned = compose.planned_resources(
        project_name="project-a",
        environment=compose_environment(),
    )
    discovered = compose.discover_resources(
        project_name="project-a",
        environment=compose_environment(),
    )

    assert [(resource.resource_kind, resource.identifier) for resource in planned] == [
        ("container", "planned:container:mysql"),
        ("container", "planned:container:server"),
        ("volume", "planned:volume:database"),
        ("network", "planned:network:default"),
    ]
    assert {(resource.resource_kind, resource.identifier) for resource in discovered} == {
        ("container", "container-a"),
        ("container", "container-b"),
        ("volume", "volume-a"),
        ("network", "network-a"),
    }
    assert all("label=com.docker.compose.project=project-a" in command for command in commands[1:])


def test_compose_controller_observes_the_exact_pinned_image_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose = controller(tmp_path)
    images = tuple(sorted(UPSTREAM_IMAGES))
    monkeypatch.setattr(
        compose,
        "discover_resources",
        lambda **_: tuple(
            SimpleNamespace(resource_kind="container", identifier=f"container-{index}")
            for index, _ in enumerate(images)
        ),
    )
    by_container = {f"container-{index}": image for index, image in enumerate(images)}

    def inspect_image(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        return _FinishedDockerProcess(stdout=json.dumps(by_container[command[-1]]).encode())

    monkeypatch.setattr(subprocess, "Popen", inspect_image)

    observed_digest = compose.verify_pinned_images(
        project_name="project-a", environment=compose_environment()
    )

    assert observed_digest == digest(images)


def test_compose_controller_preserves_explicit_docker_caller_config_without_ambient_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker"
    captured_environment = tmp_path / "captured-environment"
    docker.write_text(
        "#!/bin/sh\n"
        'capture_path="${0%/*}/captured-environment"\n'
        "{\n"
        '  printf "PATH=%s\\n" "${PATH-}"\n'
        '  printf "DOCKER_CONFIG=%s\\n" "${DOCKER_CONFIG-}"\n'
        '  printf "DOCKER_HOST=%s\\n" "${DOCKER_HOST-}"\n'
        '  printf "MYSQL_ROOT=%s\\n" "${PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD-}"\n'
        '  printf "DATABASE=%s\\n" "${PILLARMESH_OPENMETADATA_DATABASE_PASSWORD-}"\n'
        '  printf "AIRFLOW=%s\\n" "${PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD-}"\n'
        '  printf "AMBIENT_SECRET=%s\\n" "${PILLARMESH_AMBIENT_SECRET-}"\n'
        '  printf "TASK_SECRET=%s\\n" "${PILLARMESH_UNRELATED_TASK_SECRET-}"\n'
        '} > "$capture_path"\n'
        "printf '%s\\n' "
        '\'{"services":{"server":{}},"volumes":{"database":{}},'
        '"networks":{"default":{}}}\'\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONFIG", "/tmp/test-docker-config")
    monkeypatch.setenv("DOCKER_HOST", "tcp://caller.example.test:2376")
    monkeypatch.setenv("PILLARMESH_AMBIENT_SECRET", "must-not-cross-boundary")

    try:
        resources = controller(tmp_path).planned_resources(
            project_name="project-a",
            environment=compose_environment()
            | {
                "DOCKER_HOST": "tcp://operation.example.test:2376",
                "PILLARMESH_UNRELATED_TASK_SECRET": "must-not-cross-boundary",
            },
        )
    except FileNotFoundError:
        pytest.fail("Docker caller PATH was not preserved")

    assert len(resources) == 3
    assert captured_environment.read_text().splitlines() == [
        f"PATH={tmp_path}",
        "DOCKER_CONFIG=/tmp/test-docker-config",
        "DOCKER_HOST=tcp://caller.example.test:2376",
        "MYSQL_ROOT=test-only-root-password",
        "DATABASE=test-only-database-password",
        "AIRFLOW=test-only-airflow-password",
        "AMBIENT_SECRET=",
        "TASK_SECRET=",
    ]


def test_compose_controller_removes_and_verifies_only_the_exact_identifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        if "inspect" in command:
            return _FinishedDockerProcess(
                stdout=b"",
                stderr=b"Error: No such volume: exact-volume-id",
                returncode=1,
            )
        return _FinishedDockerProcess()

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    compose = controller(tmp_path)

    compose.remove_resource(
        resource_kind="volume",
        identifier="exact-volume-id",
        environment=compose_environment(),
    )
    absent = compose.resource_is_absent(
        resource_kind="volume",
        identifier="exact-volume-id",
        environment=compose_environment(),
    )

    assert absent is True
    assert commands == [
        ["docker", "volume", "rm", "--", "exact-volume-id"],
        ["docker", "volume", "inspect", "--", "exact-volume-id"],
    ]


def test_compose_controller_preserves_compose_command_order_for_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    compose = controller(tmp_path)
    monkeypatch.setattr(compose, "_run_readiness", lambda: None)

    def record_popen(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        commands.append(command)
        return _FinishedDockerProcess()

    monkeypatch.setattr(subprocess, "Popen", record_popen)

    compose.up(project_name="project-a", environment=compose_environment())

    assert commands == [
        [
            "docker",
            "compose",
            "--project-name",
            "project-a",
            "--file",
            str(tmp_path / "compose.yaml"),
            "up",
            "--detach",
        ]
    ]


def test_compose_controller_treats_an_already_absent_resource_as_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def absent_resource(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        return _FinishedDockerProcess(
            stdout=b"private stdout",
            stderr=b"Error: No such volume: exact-volume-id",
            returncode=1,
        )

    monkeypatch.setattr(subprocess, "Popen", absent_resource)

    controller(tmp_path).remove_resource(
        resource_kind="volume",
        identifier="exact-volume-id",
        environment=compose_environment(),
    )


def test_compose_controller_reports_an_unknown_resource_inspection_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unknown_inspection(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        return _FinishedDockerProcess(
            stdout=b"private stdout",
            stderr=b"Docker daemon refused inspection",
            returncode=1,
        )

    monkeypatch.setattr(subprocess, "Popen", unknown_inspection)

    absent = controller(tmp_path).resource_is_absent(
        resource_kind="network",
        identifier="exact-network-id",
        environment=compose_environment(),
    )

    assert absent is None


def test_compose_controller_propagates_interrupts_from_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def interrupt(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", interrupt)

    with pytest.raises(KeyboardInterrupt):
        controller(tmp_path).stop(project_name="project-a", environment=compose_environment())


def test_compose_controller_failure_is_stable_and_does_not_expose_process_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_stdout = "stdout-with-password"
    private_stderr = "stderr-with-password"

    def rejected(command: list[str], **kwargs: object) -> _FinishedDockerProcess:
        return _FinishedDockerProcess(
            stdout=private_stdout.encode(),
            stderr=private_stderr.encode(),
            returncode=1,
        )

    monkeypatch.setattr(subprocess, "Popen", rejected)

    with pytest.raises(RuntimeError) as captured:
        controller(tmp_path).stop(project_name="project-a", environment=compose_environment())

    assert str(captured.value) == "OpenMetadata Compose operation failed"
    assert private_stdout not in str(captured.value)
    assert private_stderr not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_replayed_provisioning_returns_the_same_private_resource_handle() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)

    first = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    replay = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    assert replay == first
    assert first != binding.binding_id
    assert compose.project_names == [compose.project_names[0]]
    resources = repository.load_resources("tenant-a", binding.binding_id)
    assert {resource.resource_kind.value for resource in resources} == {
        "compose_container",
        "compose_volume",
        "compose_network",
        "catalog_policy",
        "catalog_role",
        "service_account",
        "tenant_namespace",
    }
    repository._connection.close()


def test_concurrent_same_operation_has_one_executor_and_preserves_its_secret(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "catalog.sqlite"
    setup_repository = SQLiteCatalogRepository(str(database_path))
    binding = provisioning_binding(setup_repository)
    setup_repository._connection.close()
    compose = BlockingFirstPlanningCompose()
    client = ReadyClient()
    secret_store = RecordingSecretStore()
    outcomes: list[str] = []
    failures: list[BaseException] = []
    repositories_ready = Barrier(3)

    def provision() -> None:
        repository = SQLiteCatalogRepository(str(database_path))
        try:
            repositories_ready.wait(timeout=5)
            outcomes.append(
                provisioner_for(
                    repository,
                    compose,
                    client,
                    secret_store=secret_store,
                ).provision(
                    tenant_id="tenant-a",
                    binding_id=binding.binding_id,
                    operation_id="operation-a",
                )
            )
        except BaseException as error:
            failures.append(error)
        finally:
            repository._connection.close()

    first = Thread(target=provision)
    second = Thread(target=provision)
    first.start()
    second.start()
    repositories_ready.wait(timeout=5)
    assert compose.first_planning_entered.wait(timeout=5)
    compose.second_planning_entered.wait(timeout=0.2)
    compose.release_first_planning.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == [], (outcomes, compose.project_names, secret_store.deleted_references)
    assert len(set(outcomes)) == 1
    assert len(compose.project_names) == 1
    assert len(secret_store.created_references) == 1
    assert secret_store.deleted_references == []


def test_same_operation_retry_recovers_a_claim_left_before_operation_recording() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = FlakyPlanningCompose(KeyboardInterrupt("simulated process interruption"))
    client = ReadyClient()
    secret_store = RecordingSecretStore()
    provisioner = provisioner_for(
        repository,
        compose,
        client,
        secret_store=secret_store,
    )

    with pytest.raises(KeyboardInterrupt, match="process interruption"):
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
    with pytest.raises(KeyError, match="was not recorded"):
        repository.load_operation("tenant-a", binding.binding_id, "operation-a")

    recovered_handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    recovered_operation = repository.load_operation("tenant-a", binding.binding_id, "operation-a")

    assert recovered_handle == recovered_operation.resource_handle
    assert secret_store.created_references == [recovered_operation.secret_reference]
    assert secret_store.deleted_references == []
    assert compose.project_names
    repository._connection.close()


def test_fresh_cleanup_retires_a_claimed_secret_left_before_operation_recording(
    tmp_path: Path,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    key = Fernet.generate_key()
    secret_directory = tmp_path / "private-secrets"
    interrupted = provisioner_for(
        repository,
        FlakyPlanningCompose(KeyboardInterrupt("simulated process interruption")),
        ReadyClient(),
        secret_store=encrypted_secret_store(secret_directory, key=key),
    )

    with pytest.raises(KeyboardInterrupt, match="process interruption"):
        interrupted.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
    assert tuple(secret_directory.iterdir())

    recovered = provisioner_for(
        repository,
        RecordingCompose(),
        ReadyClient(),
        secret_store=encrypted_secret_store(secret_directory, key=key),
    )
    recovered.retire_unrecorded_operation_claim(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    assert tuple(secret_directory.iterdir()) == ()
    with pytest.raises(KeyError):
        repository.load_operation("tenant-a", binding.binding_id, "operation-a")
    repository.close()


def test_fresh_retry_resumes_an_operation_recorded_before_provider_completion(
    tmp_path: Path,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = InterruptingUpCompose()
    key = Fernet.generate_key()
    secret_directory = tmp_path / "private-secrets"
    initial_store = encrypted_secret_store(secret_directory, key=key)
    initial = provisioner_for(
        repository,
        compose,
        ReadyClient(),
        secret_store=initial_store,
    )

    with pytest.raises(KeyboardInterrupt, match="operation recording"):
        initial.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
    recorded_before_retry = repository.load_resources("tenant-a", binding.binding_id)
    assert recorded_before_retry
    assert all(resource.creation_state == "planned" for resource in recorded_before_retry)

    recovered_store = encrypted_secret_store(secret_directory, key=key)
    recovered = provisioner_for(
        repository,
        compose,
        ReadyClient(),
        secret_store=recovered_store,
    )
    handle = recovered.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    operation = repository.load_operation("tenant-a", binding.binding_id, "operation-a")
    resources = repository.load_resources("tenant-a", binding.binding_id)
    assert handle == operation.resource_handle
    assert len(resources) == len(recorded_before_retry) + 5
    assert len({resource.resource_id for resource in resources}) == len(resources)
    assert all(resource.creation_state == "created" for resource in resources)
    assert compose.project_names == [operation.project_name]
    assert recovered_store.resolve(operation.secret_reference)
    repository._connection.close()


def test_fresh_retry_repairs_owner_assignment_after_process_interruption() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    secret_store = RecordingSecretStore()
    interrupted_client = ProvisionStepFailureClient(
        failing_step="owner assignment",
        failure=KeyboardInterrupt("simulated interruption before owner assignment"),
    )
    initial = provisioner_for(
        repository,
        compose,
        interrupted_client,
        secret_store=secret_store,
    )

    with pytest.raises(KeyboardInterrupt, match="before owner assignment"):
        initial.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    recovery_client = ReadyClient()
    recovered = provisioner_for(
        repository,
        compose,
        recovery_client,
        secret_store=secret_store,
    )
    recovered.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    assert recovery_client.owner_assignments == [
        (
            "namespace:tenant-a:namespace",
            "service_identity:tenant-a:runtime",
        )
    ]
    repository._connection.close()


def test_fresh_retry_uses_rotated_password_after_lost_rotation_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    secret_store = RecordingSecretStore()
    bootstrap_password = secret_store.bootstrap_admin_password()
    state: dict[str, int | bool] = {"rotated": False, "rotation_calls": 0}

    def client_factory(
        *, settings: object, credentials: _OpenMetadataCredentials
    ) -> RotationCrashClient:
        assert settings is not None
        return RotationCrashClient(
            supplied_password=credentials.password,
            bootstrap_password=bootstrap_password,
            rotated_password=secret_store.created_bundles[0].admin_password,
            state=state,
        )

    monkeypatch.setattr(provisioner_module, "OpenMetadataClient", client_factory)
    initial = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        availability_decision=no_supported_catalog,
        secret_store=secret_store,
    )

    with pytest.raises(KeyboardInterrupt, match="lost password rotation acknowledgement"):
        initial.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    recovered = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        availability_decision=no_supported_catalog,
        secret_store=secret_store,
    )
    handle = recovered.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    assert handle
    assert state == {"rotated": True, "rotation_calls": 1}
    repository._connection.close()


def test_pre_record_planning_failure_deletes_credentials_and_allows_safe_retry() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = FlakyPlanningCompose(RuntimeError("secret-bearing planning failure"))
    secret_store = RecordingSecretStore()
    provisioner = provisioner_for(
        repository,
        compose,
        ReadyClient(),
        secret_store=secret_store,
    )

    with pytest.raises(CatalogProviderError, match="resource discovery"):
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert secret_store.deleted_references == secret_store.created_references
    with pytest.raises(RuntimeError, match="reference is unavailable"):
        secret_store.resolve(secret_store.created_references[0])

    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    operation = repository.load_operation("tenant-a", binding.binding_id, "operation-a")

    assert handle == operation.resource_handle
    assert secret_store.created_references == [
        operation.secret_reference,
        operation.secret_reference,
    ]
    repository._connection.close()


def test_second_operation_fails_before_secret_creation_or_first_operation_retirement() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    secret_store = RecordingSecretStore()
    provisioner = provisioner_for(
        repository,
        compose,
        client,
        secret_store=secret_store,
    )
    first_handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    resources_before_conflict = repository.load_resources("tenant-a", binding.binding_id)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-b",
        )

    assert captured.value.classification == "conflict"
    assert str(captured.value) == "OpenMetadata binding already has another operation"
    assert len(secret_store.created_references) == 1
    assert secret_store.deleted_references == []
    assert compose.down_project_names == []
    assert repository.load_resources("tenant-a", binding.binding_id) == resources_before_conflict
    assert (
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
        == first_handle
    )
    repository._connection.close()


def test_provision_binds_only_exact_discovered_provider_identifiers() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)

    provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    project_name = compose.project_names[0]
    resources = repository.load_resources("tenant-a", binding.binding_id)
    assert {resource.provider_ref for resource in resources} == {
        f"{project_name}-container-id",
        f"{project_name}-volume-id",
        f"{project_name}-network-id",
        "namespace-provider-id-tenant-a",
        "runtime-policy-provider-id-tenant-a",
        "runtime-role-provider-id-tenant-a",
        "runtime-provider-id-tenant-a",
        "administrator-provider-id-tenant-a",
    }
    assert all(resource.creation_state == "created" for resource in resources)
    repository._connection.close()


def test_discovery_failure_is_sanitized_without_broad_project_cleanup() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(fail_discovery=True)
    provisioner = provisioner_for(repository, compose, ReadyClient())

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    resources = repository.load_resources("tenant-a", binding.binding_id)
    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata resource discovery failed"
    assert "discovery detail" not in str(captured.value)
    assert compose.down_project_names == compose.project_names
    assert compose.active_resources() == set()
    assert compose.removed_resources == []
    assert resources
    assert all(resource.cleanup_status == "not_started" for resource in resources)
    repository._connection.close()


def test_compose_down_failure_does_not_advance_project_cleanup_rows() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(fail_down=True)
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    with pytest.raises(CatalogProviderError) as captured:
        provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    resources = repository.load_resources("tenant-a", binding.binding_id)
    compose_resources = tuple(
        resource for resource in resources if resource.resource_kind.value.startswith("compose_")
    )
    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata exact resource cleanup failed"
    assert compose.down_project_names == compose.project_names
    assert compose.active_resources()
    assert all(resource.cleanup_status == "not_started" for resource in compose_resources)
    assert compose.removed_resources == []
    assert set(client.deleted_recorded_resources) == {
        ("glossaries", "namespace-provider-id-tenant-a"),
        ("policies", "runtime-policy-provider-id-tenant-a"),
        ("roles", "runtime-role-provider-id-tenant-a"),
        ("users", "runtime-provider-id-tenant-a"),
        ("users", "administrator-provider-id-tenant-a"),
    }
    repository._connection.close()


def test_nonempty_project_discovery_does_not_advance_compose_cleanup_rows() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(preserve_resources_after_down=True)
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    with pytest.raises(CatalogProviderError) as captured:
        provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    resources = repository.load_resources("tenant-a", binding.binding_id)
    compose_resources = tuple(
        resource for resource in resources if resource.resource_kind.value.startswith("compose_")
    )
    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata exact resource cleanup failed"
    assert compose.down_project_names == compose.project_names
    assert compose.active_resources()
    assert all(resource.cleanup_status == "not_started" for resource in compose_resources)
    repository._connection.close()


def test_terminal_compose_cleanup_is_not_retried() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    compose_resource = next(
        resource
        for resource in repository.load_resources("tenant-a", binding.binding_id)
        if resource.resource_kind.value == "compose_container"
    )
    repository.begin_cleanup("tenant-a", compose_resource.resource_id)
    repository.fail_cleanup(
        "tenant-a",
        compose_resource.resource_id,
        status="failed",
        failure_classification="transient",
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata resource cleanup is already terminal"
    assert compose.down_project_names == []
    assert compose.active_resources()
    repository._connection.close()


def test_validation_records_positive_and_denial_evidence_before_exact_cleanup() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    provisioning = provisioning_binding(repository)
    compose = RecordingCompose()
    runtime = DeniedRuntimeClient()
    client = ReadyClient()
    secret_store = provisioner_module.InMemoryOpenMetadataSecretStore(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    provisioner = provisioner_for(repository, compose, client, secret_store=secret_store)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=provisioning.binding_id,
        operation_id="operation-a",
    )
    validating = control.transition(
        "tenant-a",
        provisioning.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=provisioning.revision,
    )
    provisioner = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        client_factory=lambda _: client,
        runtime_client_factory=lambda _: runtime,
        availability_decision=no_supported_catalog,
        secret_store=secret_store,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )

    evidence = provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )

    assert evidence.tenant_id == "tenant-a"
    assert runtime.administration_checked
    assert runtime.cross_tenant_checked
    assert "other-tenant" in client.namespace_tenants
    assert client.deleted_recorded_resources == []
    assert (
        "namespace:other-tenant:namespace",
        "service_identity:other-tenant:administrator",
    ) in client.owner_assignments
    assert any(
        resource.provider_ref == "administrator-provider-id-other-tenant"
        for resource in repository.load_resources("tenant-a", validating.binding_id)
    )
    resources_before_retire = repository.load_resources("tenant-a", validating.binding_id)
    assert {
        resource.provider_ref
        for resource in resources_before_retire
        if resource.resource_kind.value
        in {
            "catalog_glossary_term",
            "catalog_classification",
            "catalog_policy",
            "catalog_role",
            "catalog_tag",
            "catalog_lineage",
        }
    } == {
        "validation-term-from-provider-id-tenant-a",
        "validation-term-to-provider-id-tenant-a",
        "validation-classification-provider-id-tenant-a",
        "runtime-policy-provider-id-tenant-a",
        "runtime-role-provider-id-tenant-a",
        "validation-classification-tag-provider-id-tenant-a",
        _FAKE_LINEAGE_IDENTIFIER,
    }
    assert {resource.resource_kind.value for resource in resources_before_retire} == {
        "backup_artifact",
        "catalog_classification",
        "catalog_glossary_term",
        "catalog_lineage",
        "catalog_policy",
        "catalog_role",
        "catalog_tag",
        "compose_container",
        "compose_network",
        "compose_volume",
        "service_account",
        "tenant_namespace",
    }
    backup_resource = next(
        resource
        for resource in resources_before_retire
        if resource.resource_kind.value == "backup_artifact"
    )
    assert backup_resource.creation_state == "created"
    assert Path(backup_resource.provider_ref).is_file()
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")
    assert (
        "glossaries",
        "namespace-provider-id-other-tenant",
    ) in client.deleted_recorded_resources
    assert (
        "users",
        "administrator-provider-id-other-tenant",
    ) in client.deleted_recorded_resources
    statuses = repository._connection.execute(
        "SELECT cleanup_status FROM private_catalog_resources WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchall()
    assert statuses
    assert all(status[0] == "complete" for status in statuses)
    assert not Path(backup_resource.provider_ref).exists()
    repository._connection.close()


def test_nested_provider_side_effects_are_planned_before_each_ensure_call() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    provisioning = provisioning_binding(repository)
    compose = RecordingCompose()
    client = PlanningAwareClient(repository, provisioning.binding_id)
    provisioner = provisioner_for(repository, compose, client)

    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=provisioning.binding_id,
        operation_id="operation-a",
    )
    validating = control.transition(
        "tenant-a",
        provisioning.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=provisioning.revision,
    )
    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )

    resources = repository.load_resources("tenant-a", validating.binding_id)
    exact_nested_resources = {
        resource.resource_kind.value: resource.provider_ref
        for resource in resources
        if resource.resource_kind.value
        in {"catalog_policy", "catalog_role", "catalog_tag", "catalog_lineage"}
    }
    assert exact_nested_resources == {
        "catalog_policy": "runtime-policy-provider-id-tenant-a",
        "catalog_role": "runtime-role-provider-id-tenant-a",
        "catalog_tag": "validation-classification-tag-provider-id-tenant-a",
        "catalog_lineage": _FAKE_LINEAGE_IDENTIFIER,
    }
    assert all(
        not resource.provider_ref.startswith("planned:")
        for resource in resources
        if resource.resource_kind.value
        in {"catalog_policy", "catalog_role", "catalog_tag", "catalog_lineage"}
    )
    repository._connection.close()


def test_validation_retry_cannot_recreate_resources_behind_terminal_cleanup_rows() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    provisioning = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    runtime = DeniedRuntimeClient()
    secret_store = provisioner_module.InMemoryOpenMetadataSecretStore(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    provisioner = provisioner_for(repository, compose, client, secret_store=secret_store)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=provisioning.binding_id,
        operation_id="operation-a",
    )
    validating = control.transition(
        "tenant-a",
        provisioning.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=provisioning.revision,
    )
    provisioner = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        client_factory=lambda _: client,
        runtime_client_factory=lambda _: runtime,
        availability_decision=no_supported_catalog,
        secret_store=secret_store,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )

    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )
    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert client.discovered_resources() == ()
    repository._connection.close()


def test_failed_lineage_verification_still_ledgers_the_created_edge_for_retirement() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    provisioning = provisioning_binding(repository)
    compose = RecordingCompose()
    client = LineageVerificationFailureClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=provisioning.binding_id,
        operation_id="operation-a",
    )
    validating = control.transition(
        "tenant-a",
        provisioning.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=provisioning.revision,
    )

    with pytest.raises(CatalogProviderError, match="lineage verification"):
        provisioner.validate(
            tenant_id="tenant-a",
            binding_id=validating.binding_id,
            private_resource_handle=handle,
        )

    lineage = next(
        resource
        for resource in repository.load_resources("tenant-a", validating.binding_id)
        if resource.resource_kind.value == "catalog_lineage"
    )
    assert lineage.creation_state == "created"
    assert lineage.provider_ref == _FAKE_LINEAGE_IDENTIFIER
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")
    assert client.discovered_resources() == ()
    repository._connection.close()


def test_retire_removes_the_exact_project_after_restore_recreates_compose_ids() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    provisioning = provisioning_binding(repository)
    compose = RecordingCompose(recreate_resources_on_restore=True)
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=provisioning.binding_id,
        operation_id="operation-a",
    )
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    validating = control.transition(
        "tenant-a",
        provisioning.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=provisioning.revision,
    )
    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )
    resources = repository.load_resources("tenant-a", validating.binding_id)
    recorded_compose_ids = {
        resource.provider_ref
        for resource in resources
        if resource.resource_kind.value.startswith("compose_")
    }
    active_compose_ids = {identifier for _, identifier in compose.active_resources()}
    assert recorded_compose_ids.isdisjoint(active_compose_ids)

    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert compose.down_project_names == compose.project_names
    assert compose.active_resources() == set()
    assert compose.removed_resources == []
    assert all(
        resource.cleanup_status == "complete"
        for resource in repository.load_resources("tenant-a", validating.binding_id)
    )
    repository._connection.close()


def test_retire_completes_in_progress_compose_cleanup_after_reverifying_absence() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    compose_resources = tuple(
        resource
        for resource in repository.load_resources("tenant-a", binding.binding_id)
        if resource.resource_kind.value.startswith("compose_")
    )
    for resource in compose_resources:
        repository.begin_cleanup("tenant-a", resource.resource_id)
    project_name = compose.project_names[0]
    compose.down(project_name=project_name, environment=compose_environment())

    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert compose.down_project_names == [project_name, project_name]
    assert compose.active_resources() == set()
    assert all(
        resource.cleanup_status == "complete"
        for resource in repository.load_resources("tenant-a", binding.binding_id)
        if resource.resource_kind.value.startswith("compose_")
    )
    repository._connection.close()


def test_retire_replays_in_progress_provider_and_backup_cleanup(tmp_path: Path) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    secret_store = encrypted_secret_store(tmp_path / "secrets")
    provisioner = provisioner_for(
        repository,
        compose,
        client,
        secret_store=secret_store,
    )
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    assert provisioner.backup_restore_and_probe(
        private_resource_handle=handle,
        operation_id="operation-a",
    )
    resources = repository.load_resources("tenant-a", binding.binding_id)
    replayed_resources = tuple(
        resource
        for resource in resources
        if resource.resource_kind.value in {"tenant_namespace", "backup_artifact"}
    )
    for resource in replayed_resources:
        repository.begin_cleanup("tenant-a", resource.resource_id)

    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert replayed_resources
    assert all(
        repository.load_resource("tenant-a", resource.resource_id).cleanup_status == "complete"
        for resource in replayed_resources
    )
    repository._connection.close()


def test_restore_rejects_a_representative_object_that_differs_from_the_backup() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = RestoreDivergenceClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.backup_restore_and_probe(
            private_resource_handle=handle,
            operation_id="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata restored metadata failed exact verification"
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")
    repository._connection.close()


def test_restore_rebuilds_catalog_search_before_reporting_success() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    assert provisioner.backup_restore_and_probe(
        private_resource_handle=handle,
        operation_id="operation-a",
    )

    assert compose.search_rebuild_project_names == compose.project_names
    assert client.searchable_checks == [("tenant-a", "validation-term-from")]
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")
    repository._connection.close()


def test_restore_reports_search_rebuild_failure_without_driver_details() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(fail_search_rebuild=True)
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.backup_restore_and_probe(
            private_resource_handle=handle,
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata backup and restore failed"
    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")
    repository._connection.close()


@pytest.mark.parametrize("terminal_status", ["failed", "unknown"])
def test_retire_keeps_failed_and_unknown_provider_cleanup_terminal(
    terminal_status: str,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    namespace = next(
        resource
        for resource in repository.load_resources("tenant-a", binding.binding_id)
        if resource.resource_kind.value == "tenant_namespace"
    )
    repository.begin_cleanup("tenant-a", namespace.resource_id)
    repository.fail_cleanup(
        "tenant-a",
        namespace.resource_id,
        status=terminal_status,
        failure_classification="transient" if terminal_status == "failed" else "unknown",
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata resource cleanup is already terminal"
    assert ("glossaries", namespace.provider_ref) not in client.deleted_recorded_resources
    repository._connection.close()


def test_retire_deletes_owned_provider_resources_before_service_accounts_and_skips_replay() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = DependencyAwareCleanupClient()
    provisioner = provisioner_for(repository, compose, client)
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    validating = control.transition(
        "tenant-a",
        binding.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=binding.revision,
    )
    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )
    cleanup_attempts_before_retirement = len(client.deleted_recorded_resources)

    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    first_cleanup = tuple(client.deleted_recorded_resources[cleanup_attempts_before_retirement:])
    assert [collection for collection, _ in first_cleanup] == [
        "lineage",
        "glossaryTerms",
        "glossaryTerms",
        "tags",
        "classifications",
        "glossaries",
        "glossaries",
        "users",
        "users",
        "users",
        "roles",
        "policies",
    ]

    provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    assert len(client.deleted_recorded_resources) == cleanup_attempts_before_retirement + len(
        first_cleanup
    )
    repository._connection.close()


def test_fresh_cleanup_client_uses_exact_nested_resource_ids_from_the_ledger() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioning_client = ReadyClient()
    secret_store = provisioner_module.InMemoryOpenMetadataSecretStore(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    provisioner = provisioner_for(
        repository,
        compose,
        provisioning_client,
        secret_store=secret_store,
    )
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    validating = control.transition(
        "tenant-a",
        binding.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=binding.revision,
    )
    provisioner.validate(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        private_resource_handle=handle,
    )
    expected_nested_ids = {
        (resource.resource_kind.value, resource.provider_ref)
        for resource in repository.load_resources("tenant-a", binding.binding_id)
        if resource.resource_kind.value
        in {"catalog_policy", "catalog_role", "catalog_tag", "catalog_lineage"}
    }
    cleanup_client = ReadyClient()
    restarted = provisioner_for(
        repository,
        compose,
        cleanup_client,
        secret_store=secret_store,
    )

    restarted.retire(private_resource_handle=handle, operation_id="operation-a")

    assert expected_nested_ids == {
        ("catalog_policy", "runtime-policy-provider-id-tenant-a"),
        ("catalog_role", "runtime-role-provider-id-tenant-a"),
        ("catalog_tag", "validation-classification-tag-provider-id-tenant-a"),
        ("catalog_lineage", _FAKE_LINEAGE_IDENTIFIER),
    }
    assert {
        (collection, identifier)
        for collection, identifier in cleanup_client.deleted_recorded_resources
        if collection in {"policies", "roles", "tags", "lineage"}
    } == {
        ("policies", "runtime-policy-provider-id-tenant-a"),
        ("roles", "runtime-role-provider-id-tenant-a"),
        ("tags", "validation-classification-tag-provider-id-tenant-a"),
        ("lineage", _FAKE_LINEAGE_IDENTIFIER),
    }
    repository._connection.close()


def test_fresh_provisioner_recovers_operation_and_handle_for_every_lifecycle_operation() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    secret_store = provisioner_module.InMemoryOpenMetadataSecretStore(
        bootstrap_admin_password=SecretStr("test-only-bootstrap-password")
    )
    initial = provisioner_for(repository, compose, client, secret_store=secret_store)

    handle = initial.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    recovered = provisioner_for(
        repository,
        compose,
        client,
        secret_store=secret_store,
    )

    assert (
        recovered.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
        == handle
    )

    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    validating = control.transition(
        "tenant-a",
        binding.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=binding.revision,
    )

    assert (
        recovered.validate(
            tenant_id="tenant-a",
            binding_id=validating.binding_id,
            private_resource_handle=handle,
        ).binding_id
        == binding.binding_id
    )

    recovered.suspend(private_resource_handle=handle, operation_id="operation-a")
    recovered.resume(private_resource_handle=handle, operation_id="operation-a")
    recovered.retire(private_resource_handle=handle, operation_id="operation-a")

    assert compose.stopped_project_names == compose.project_names
    assert compose.started_project_names == compose.project_names
    assert compose.down_project_names == compose.project_names
    assert compose.removed_resources == []
    assert all(
        resource.cleanup_status == "complete"
        for resource in repository.load_resources("tenant-a", binding.binding_id)
    )
    repository._connection.close()


def test_new_encrypted_store_object_recovers_and_retires_without_docker(tmp_path: Path) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    directory = tmp_path / "private-secrets"
    key = Fernet.generate_key()
    first_store = encrypted_secret_store(directory, key=key)
    initial = provisioner_for(repository, compose, client, secret_store=first_store)
    handle = initial.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    operation = repository.load_operation("tenant-a", binding.binding_id, "operation-a")

    second_store = encrypted_secret_store(directory, key=key)
    recovered = provisioner_for(repository, compose, client, secret_store=second_store)
    assert second_store.resolve(operation.secret_reference)
    assert (
        recovered.provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )
        == handle
    )

    recovered.retire(private_resource_handle=handle, operation_id="operation-a")

    with pytest.raises(RuntimeError, match="secret storage is unavailable"):
        second_store.resolve(operation.secret_reference)
    assert all(
        resource.cleanup_status == "complete"
        for resource in repository.load_resources("tenant-a", binding.binding_id)
    )
    repository._connection.close()


def test_failed_resource_cleanup_retains_restart_credentials(tmp_path: Path) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(fail_down=True)
    client = ReadyClient()
    directory = tmp_path / "private-secrets"
    key = Fernet.generate_key()
    secret_store = encrypted_secret_store(directory, key=key)
    provisioner = provisioner_for(
        repository,
        compose,
        client,
        secret_store=secret_store,
    )
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    operation = repository.load_operation("tenant-a", binding.binding_id, "operation-a")

    with pytest.raises(CatalogProviderError):
        provisioner.retire(private_resource_handle=handle, operation_id="operation-a")

    recovered_store = encrypted_secret_store(directory, key=key)
    assert recovered_store.resolve(operation.secret_reference)
    repository._connection.close()


def test_provision_denies_a_missing_binding_before_compose() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    compose = RecordingCompose()

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, compose, ReadyClient()).provision(
            tenant_id="tenant-a",
            binding_id="missing-binding",
            operation_id="operation-a",
        )

    assert captured.value.classification == "invalid_request"
    assert str(captured.value) == "OpenMetadata provisioning request is invalid"
    assert compose.project_names == []
    repository._connection.close()


def test_provision_denies_a_cross_tenant_binding_before_compose() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, compose, ReadyClient()).provision(
            tenant_id="tenant-b",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "invalid_request"
    assert str(captured.value) == "OpenMetadata provisioning request is invalid"
    assert compose.project_names == []
    repository._connection.close()


def test_provision_denies_a_binding_outside_provisioning_before_compose() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = repository.create_draft("tenant-a", datetime(2026, 8, 19, tzinfo=UTC))
    compose = RecordingCompose()

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, compose, ReadyClient()).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "invalid_request"
    assert str(captured.value) == "OpenMetadata provisioning request is invalid"
    assert compose.project_names == []
    repository._connection.close()


def test_validate_classifies_a_wrong_binding_lifecycle() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    with pytest.raises(CatalogProviderError) as captured:
        provisioner.validate(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            private_resource_handle=handle,
        )

    assert captured.value.classification == "invalid_request"
    assert str(captured.value) == "OpenMetadata validation request is invalid"
    repository._connection.close()


def test_validate_classifies_repository_driver_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    provisioner = provisioner_for(repository, compose, ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    def fail_load(tenant_id: str, binding_id: str) -> None:
        raise CatalogPersistenceError(operation="test validation load")

    monkeypatch.setattr(repository, "load", fail_load)
    with pytest.raises(CatalogProviderError) as captured:
        provisioner.validate(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            private_resource_handle=handle,
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata validation failed"
    repository._connection.close()


def test_validation_driver_failure_does_not_cross_the_provider_traceback_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    provisioner = provisioner_for(repository, RecordingCompose(), ReadyClient())
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )

    def fail_load(tenant_id: str, binding_id: str) -> None:
        raise SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)

    monkeypatch.setattr(repository, "load", fail_load)
    with pytest.raises(CatalogProviderError) as captured:
        provisioner.validate(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            private_resource_handle=handle,
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata validation failed"
    assert_provider_failure_is_sanitized(captured.value)
    repository._connection.close()


def test_validate_classifies_unrecoverable_restart_credentials(tmp_path: Path) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()
    client = ReadyClient()
    directory = tmp_path / "private-secrets"
    initial_store = encrypted_secret_store(directory)
    provisioner = provisioner_for(
        repository,
        compose,
        client,
        secret_store=initial_store,
    )
    handle = provisioner.provision(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        operation_id="operation-a",
    )
    control = CatalogControlService(repository, clock=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    control.transition(
        "tenant-a",
        binding.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=binding.revision,
    )
    wrong_store = encrypted_secret_store(directory)
    restarted = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        availability_decision=no_supported_catalog,
        secret_store=wrong_store,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )

    with pytest.raises(CatalogProviderError) as captured:
        restarted.validate(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            private_resource_handle=handle,
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata private credentials are unavailable"
    repository._connection.close()


@pytest.mark.parametrize("decision", ["supported_catalog", "unknown"])
def test_provision_denies_any_catalog_selection_except_explicit_no_supported_catalog(
    decision: str,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose()

    def unavailable_catalog_decision(*, tenant_id: str, binding_id: str) -> str:
        assert tenant_id == "tenant-a"
        assert binding_id == binding.binding_id
        return decision

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(
            repository,
            compose,
            ReadyClient(),
            availability_decision=unavailable_catalog_decision,
        ).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata catalog selection does not permit provisioning"
    assert compose.project_names == []
    repository._connection.close()


def test_partial_startup_failure_retires_the_exact_project_before_compose_discovery() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    compose = RecordingCompose(fail_up=True)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, compose, ReadyClient()).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == "OpenMetadata provisioning failed"
    assert "compose detail" not in str(captured.value)
    resources = repository.load_resources("tenant-a", binding.binding_id)
    assert compose.down_project_names == compose.project_names
    assert compose.active_resources() == set()
    assert all(
        resource.creation_state == "planned" and resource.cleanup_status == "complete"
        for resource in resources
    )
    repository._connection.close()


@pytest.mark.parametrize(
    ("failing_step", "expected_message"),
    [
        ("namespace", "OpenMetadata namespace provisioning failed"),
        ("runtime", "OpenMetadata runtime identity provisioning failed"),
        ("administrator", "OpenMetadata administrator identity provisioning failed"),
        ("owner assignment", "OpenMetadata owner assignment failed"),
    ],
)
def test_provision_sanitizes_unexpected_fixed_step_failures(
    failing_step: str,
    expected_message: str,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    failure = SecretProvisionerDriverError(_SENSITIVE_PROVISIONER_VALUE)
    client = ProvisionStepFailureClient(failing_step=failing_step, failure=failure)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, RecordingCompose(), client).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "transient"
    assert str(captured.value) == expected_message
    assert_provider_failure_is_sanitized(captured.value)
    repository._connection.close()


def test_provision_replaces_a_typed_runtime_failure_message_and_preserves_its_classification() -> (
    None
):
    repository = SQLiteCatalogRepository(":memory:")
    binding = provisioning_binding(repository)
    failure = CatalogProviderError(
        "OpenMetadata resource discovery failed", classification="authorization"
    )
    client = ProvisionStepFailureClient(failing_step="runtime", failure=failure)

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, RecordingCompose(), client).provision(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            operation_id="operation-a",
        )

    assert captured.value.classification == "authorization"
    assert str(captured.value) == "OpenMetadata runtime identity provisioning failed"
    assert "resource discovery" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    repository._connection.close()


def test_recovery_failure_is_sanitized_at_the_provider_boundary() -> None:
    repository = SQLiteCatalogRepository(":memory:")

    with pytest.raises(CatalogProviderError) as captured:
        provisioner_for(repository, RecordingCompose(), ReadyClient()).suspend(
            private_resource_handle="missing-private-resource-handle",
            operation_id="operation-a",
        )

    assert captured.value.classification == "invalid_request"
    assert str(captured.value) == "OpenMetadata private resource is unavailable"
    repository._connection.close()
