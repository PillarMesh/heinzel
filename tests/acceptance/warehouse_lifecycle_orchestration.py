from __future__ import annotations

import io
import logging
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from heinzel_contract_model import ArtifactModel, digest
from heinzel_provider_clickhouse import CLICKHOUSE_SERVER_VERSION, CLICKHOUSE_WAREHOUSE_IMAGE
from heinzel_provider_clickhouse.warehouse import (
    _restore_identity as clickhouse_restore_identity,
)
from heinzel_provider_clickhouse.warehouse import (
    _warehouse_identity as clickhouse_warehouse_identity,
)
from heinzel_provider_postgresql import (
    POSTGRESQL_SERVER_VERSION_NUM,
    POSTGRESQL_WAREHOUSE_IMAGE,
)
from heinzel_provider_postgresql.warehouse import (
    _restore_identity as postgresql_restore_identity,
)
from heinzel_provider_postgresql.warehouse import (
    _warehouse_identity as postgresql_warehouse_identity,
)
from heinzel_provider_sdk import ComposeResourceKind
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBindingState,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseValidationProfile,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository
from heinzel_warehouse_control.service import WarehouseControlService

from tests.acceptance.run_warehouse_lifecycle import (
    EngineName,
    EngineWitnessResult,
    NamedCheckDisposition,
    PrivateDirectoryResourceEntry,
    PrivateDockerResourceEntry,
    WarehouseLifecycleConfig,
    WarehouseLifecycleHarnessError,
    WitnessCapture,
    register_private_resources,
)
from tests.conformance.warehouse_lifecycle import (
    WarehouseLifecycleContractObservation,
    assert_warehouse_lifecycle_contract,
)
from tests.integration.test_clickhouse_warehouse_live import (
    _cleanup_exact_driver_resources as cleanup_exact_clickhouse_resources,
)
from tests.integration.test_clickhouse_warehouse_live import (
    _new_lifecycle_driver as new_clickhouse_driver,
)
from tests.integration.test_postgresql_warehouse_live import (
    _cleanup_exact_resources as cleanup_exact_postgresql_resources,
)
from tests.integration.test_postgresql_warehouse_live import (
    _new_harness as new_postgresql_driver,
)

_CHECK_DISPOSITIONS: tuple[NamedCheckDisposition, ...] = (
    "provision:passed",
    "initial_validation:passed",
    "backup_restore:passed",
    "suspend_resume:passed",
    "retention_cleanup:passed",
    "absence:passed",
)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


class _ComposeAbsenceBoundary(Protocol):
    def resource_is_absent(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> bool | None: ...


@contextmanager
def _capture_logs() -> Iterator[_LogCapture]:
    capture = _LogCapture()
    logger = logging.getLogger()
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)


def _docker_resource_entries(
    *,
    config: WarehouseLifecycleConfig,
    engine_kind: EngineName,
    primary_container: str,
    primary_networks: tuple[str, str],
    primary_volume: str,
    restore_containers: tuple[str, ...],
    restore_networks: tuple[str, str],
    restore_volume: str,
) -> tuple[PrivateDockerResourceEntry, ...]:
    specifications: tuple[tuple[ComposeResourceKind, str], ...] = (
        *(("container", name) for name in restore_containers),
        *(("network", name) for name in restore_networks),
        ("volume", restore_volume),
        ("container", primary_container),
        *(("network", name) for name in primary_networks),
        ("volume", primary_volume),
    )
    return tuple(
        PrivateDockerResourceEntry.create(
            engine_kind=engine_kind,
            compose_resource_kind=resource_kind,
            exact_identifier=identifier,
            retention_deadline=config.retention_deadline,
        )
        for resource_kind, identifier in specifications
    )


def _validate_public_artifact(artifact: ArtifactModel) -> None:
    reconstructed = type(artifact).model_validate_json(artifact.model_dump_json())
    if reconstructed != artifact:
        raise WarehouseLifecycleHarnessError(
            "public lifecycle artifact did not survive strict reconstruction"
        )


def _require_check(condition: bool, check_name: str) -> None:
    if not condition:
        raise WarehouseLifecycleHarnessError(f"explicit lifecycle check failed: {check_name}")


def _validate_observation(
    observation: WarehouseLifecycleContractObservation,
    *,
    expected_engine: EngineKind,
) -> tuple[NamedCheckDisposition, ...]:
    artifacts: tuple[ArtifactModel, ...] = (
        observation.ready,
        observation.suspended,
        observation.resumed,
        observation.retired,
        observation.initial_validation.evidence,
        observation.initial_validation.restore_verification,
        observation.resume_validation.evidence,
        observation.retained_retirement,
        observation.deletion_retirement,
        *observation.initial_resources,
        *observation.final_resources,
    )
    for artifact in artifacts:
        _validate_public_artifact(artifact)

    _require_check(
        observation.ready.lifecycle_state is WarehouseBindingState.READY
        and observation.ready.engine_kind is expected_engine
        and observation.ready.provisioned_at is not None,
        "provision",
    )
    initial = observation.initial_validation
    _require_check(
        initial.evidence.engine_kind is expected_engine
        and initial.evidence.validation_profile is WarehouseValidationProfile.LOCAL_ACCEPTANCE
        and initial.evidence.encryption_at_rest_disposition
        is EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE
        and initial.evidence.positive_probe_digest != initial.evidence.denial_probe_digest
        and initial.evidence.backup_artifact_digest
        == initial.restore_verification.source_backup_artifact_digest
        and initial.evidence.restore_verification_digest == digest(initial.restore_verification),
        "initial_validation",
    )

    resource_counts = Counter(resource.resource_kind for resource in observation.initial_resources)
    primary_kinds = (
        WarehouseResourceKind.COMPOSE_PROJECT,
        WarehouseResourceKind.WAREHOUSE_CONTAINER,
        WarehouseResourceKind.PRIVATE_NETWORK,
        WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
        WarehouseResourceKind.PRIVATE_DIRECTORY,
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.TLS_PRIVATE_KEY,
        WarehouseResourceKind.TLS_CERTIFICATE,
        WarehouseResourceKind.BACKUP_ARTIFACT,
        WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
    )
    restore_kinds = (
        WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
        WarehouseResourceKind.RESTORE_CONTAINER,
        WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
        WarehouseResourceKind.RESTORE_DATA_VOLUME,
    )
    restore_resources = tuple(
        resource
        for resource in observation.initial_resources
        if resource.resource_kind in restore_kinds
    )
    backup_resource = observation.backup_resource
    backup_keys = tuple(
        resource
        for resource in observation.initial_resources
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY
    )
    _require_check(
        all(resource_counts[kind] >= 1 for kind in primary_kinds)
        and all(resource_counts[kind] >= 1 for kind in restore_kinds)
        and len(backup_keys) == 1
        and backup_keys[0].retention_deadline == backup_resource.retention_deadline
        and all(
            resource.creation_state is WarehouseResourceCreationState.CREATED
            and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in restore_resources
        ),
        "backup_restore",
    )
    _require_check(
        observation.suspended.lifecycle_state is WarehouseBindingState.SUSPENDED
        and observation.resumed.lifecycle_state is WarehouseBindingState.READY
        and observation.resume_validation.evidence.engine_kind is expected_engine
        and observation.resume_validation.evidence.observed_at > initial.evidence.observed_at
        and observation.resume_validation.evidence.engine_image_digest
        == initial.evidence.engine_image_digest,
        "suspend_resume",
    )
    _require_check(
        observation.retired.lifecycle_state is WarehouseBindingState.RETIRED
        and observation.retained_retirement.retained_resource_count > 0
        and observation.retained_retirement.cleanup_failed_resource_count == 0
        and observation.deletion_retirement.retained_resource_count == 0
        and observation.deletion_retirement.cleanup_failed_resource_count == 0
        and all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in observation.final_resources
        ),
        "retention_cleanup",
    )
    return _CHECK_DISPOSITIONS[:-1]


def _verify_docker_absence(
    compose: _ComposeAbsenceBoundary,
    specifications: tuple[tuple[ComposeResourceKind, str], ...],
) -> NamedCheckDisposition:
    _require_check(
        all(
            compose.resource_is_absent(
                resource_kind=resource_kind,
                identifier=identifier,
                environment={},
            )
            is True
            for resource_kind, identifier in specifications
        ),
        "absence",
    )
    return "absence:passed"


def _assert_control_plane_tenant_denial(engine_kind: EngineKind) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    try:
        control = WarehouseControlService(repository, clock=lambda: datetime.now(UTC))
        binding = control.create_draft(
            tenant_id=f"warehouse-lifecycle-{engine_kind.value}-owner",
            engine_kind=engine_kind,
            region="local",
            capacity_profile="mvp-fixed",
        )
        try:
            control.get(f"warehouse-lifecycle-{engine_kind.value}-counterparty", binding.binding_id)
        except KeyError:
            return
        raise WarehouseLifecycleHarnessError("cross-tenant warehouse read was not denied")
    finally:
        repository.close()


def _result(
    *,
    engine_kind: EngineName,
    observation: WarehouseLifecycleContractObservation,
    engine_version: str,
    engine_image: str,
    duration_seconds: float,
    check_dispositions: tuple[NamedCheckDisposition, ...],
) -> EngineWitnessResult:
    return EngineWitnessResult(
        engine_kind=engine_kind,
        binding_digest=digest(observation.retired),
        validation_evidence_digest=digest(observation.initial_validation.evidence),
        resume_evidence_digest=digest(observation.resume_validation.evidence),
        retirement_evidence_digest=digest(observation.retained_retirement),
        cleanup_evidence_digest=digest(observation.deletion_retirement),
        terminal_state="retired",
        engine_version=engine_version,
        engine_image=engine_image,
        check_dispositions=check_dispositions,
        duration_seconds=duration_seconds,
        residual_resource_count=sum(
            resource.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE
            for resource in observation.final_resources
        ),
    )


def _witness_postgresql(config: WarehouseLifecycleConfig, root: Path) -> WitnessCapture:
    if config.postgres_image != POSTGRESQL_WAREHOUSE_IMAGE:
        raise WarehouseLifecycleHarnessError(
            "configured PostgreSQL image differs from provider pin"
        )
    driver = new_postgresql_driver(
        root,
        credential_canaries=config.credential_canaries_for_witness,
    )
    identity = postgresql_warehouse_identity(driver.binding, driver.private_directory)
    operation = PrivateWarehouseOperation(
        tenant_id=driver.binding.tenant_id,
        binding_id=driver.binding.binding_id,
        binding_revision=driver.binding.revision + 1,
        operation_id=driver.provision_operation_id,
        operation_kind=WarehouseOperationKind.PROVISION,
        engine_kind=EngineKind.POSTGRESQL,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=driver.clock(),
        updated_at=driver.clock(),
    )
    restore = postgresql_restore_identity(driver.binding, operation)
    register_private_resources(
        config,
        _docker_resource_entries(
            config=config,
            engine_kind="postgresql",
            primary_container=identity.container_name,
            primary_networks=(identity.network_name, identity.loopback_network_name),
            primary_volume=identity.data_volume_name,
            restore_containers=(restore.container_name,),
            restore_networks=(restore.network_name, restore.loopback_network_name),
            restore_volume=restore.data_volume_name,
        ),
    )
    started = time.monotonic()
    try:
        tls_private_marker = (
            driver.authority.operation_capability(
                driver.secret_reference,
                operation_id=driver.provision_operation_id,
                purpose="tls_private_key",
            )
            .resolve()
            .get_secret_value()
        )
        tls_certificate_marker = (
            driver.authority.operation_capability(
                driver.secret_reference,
                operation_id=driver.provision_operation_id,
                purpose="tls_certificate",
            )
            .resolve()
            .get_secret_value()
        )
        observation = assert_warehouse_lifecycle_contract(lambda: driver)
        checks = _validate_observation(
            observation,
            expected_engine=EngineKind.POSTGRESQL,
        )
        absence = _verify_docker_absence(
            driver.compose,
            (
                ("container", restore.container_name),
                ("network", restore.network_name),
                ("network", restore.loopback_network_name),
                ("volume", restore.data_volume_name),
                ("container", identity.container_name),
                ("network", identity.network_name),
                ("network", identity.loopback_network_name),
                ("volume", identity.data_volume_name),
            ),
        )
        _assert_control_plane_tenant_denial(EngineKind.POSTGRESQL)
        result = _result(
            engine_kind="postgresql",
            observation=observation,
            engine_version=(
                f"{POSTGRESQL_SERVER_VERSION_NUM[:2]}.{int(POSTGRESQL_SERVER_VERSION_NUM[2:])}"
            ),
            engine_image=config.postgres_image,
            duration_seconds=time.monotonic() - started,
            check_dispositions=(*checks, absence),
        )
        private_markers = (
            driver.binding.tenant_id,
            driver.binding.binding_id,
            str(driver.private_directory),
            str(driver.secret_directory),
            *driver.credential_canaries,
            tls_private_marker,
            tls_certificate_marker,
            *(resource.provider_resource_handle for resource in observation.initial_resources),
        )
        return WitnessCapture(
            result=result,
            private_markers=private_markers,
        )
    finally:
        cleanup_exact_postgresql_resources(driver)


def _witness_clickhouse(config: WarehouseLifecycleConfig, root: Path) -> WitnessCapture:
    if config.clickhouse_image != CLICKHOUSE_WAREHOUSE_IMAGE:
        raise WarehouseLifecycleHarnessError(
            "configured ClickHouse image differs from provider pin"
        )
    driver = new_clickhouse_driver(
        root,
        credential_canaries=config.credential_canaries_for_witness,
    )
    identity = clickhouse_warehouse_identity(driver.provisioning_binding, driver.private_directory)
    restore = clickhouse_restore_identity(driver.provisioning_binding, driver.provision_operation)
    register_private_resources(
        config,
        _docker_resource_entries(
            config=config,
            engine_kind="clickhouse",
            primary_container=identity.container_name,
            primary_networks=(identity.private_network_name, identity.loopback_network_name),
            primary_volume=identity.data_volume_name,
            restore_containers=(
                restore.container_name,
                restore.isolation_probe_container_name,
            ),
            restore_networks=(restore.private_network_name, restore.loopback_network_name),
            restore_volume=restore.data_volume_name,
        ),
    )
    started = time.monotonic()
    try:
        observation = assert_warehouse_lifecycle_contract(lambda: driver)
        checks = _validate_observation(
            observation,
            expected_engine=EngineKind.CLICKHOUSE,
        )
        absence = _verify_docker_absence(
            driver.compose,
            (
                ("container", restore.container_name),
                ("container", restore.isolation_probe_container_name),
                ("network", restore.private_network_name),
                ("network", restore.loopback_network_name),
                ("volume", restore.data_volume_name),
                ("container", identity.container_name),
                ("network", identity.private_network_name),
                ("network", identity.loopback_network_name),
                ("volume", identity.data_volume_name),
            ),
        )
        _assert_control_plane_tenant_denial(EngineKind.CLICKHOUSE)
        result = _result(
            engine_kind="clickhouse",
            observation=observation,
            engine_version=CLICKHOUSE_SERVER_VERSION,
            engine_image=config.clickhouse_image,
            duration_seconds=time.monotonic() - started,
            check_dispositions=(*checks, absence),
        )
        return WitnessCapture(
            result=result,
            private_markers=(
                driver.binding.tenant_id,
                driver.binding.binding_id,
                str(driver.private_directory),
                str(driver.secret_directory),
                driver.configuration.authority_key.get_secret_value(),
                "127.0.0.1",
                *driver.private_markers,
                *(resource.provider_resource_handle for resource in observation.initial_resources),
            ),
        )
    finally:
        cleanup_exact_clickhouse_resources(driver)


def witness_live_engine(
    engine_kind: EngineName, config: WarehouseLifecycleConfig
) -> WitnessCapture:
    output = io.StringIO()
    error = io.StringIO()
    with redirect_stdout(output), redirect_stderr(error), _capture_logs() as logs:
        root = config.state_path / f"{engine_kind}-witness"
        register_private_resources(
            config,
            (
                PrivateDirectoryResourceEntry.create(
                    engine_kind=engine_kind,
                    exact_path=root,
                    retention_deadline=config.retention_deadline,
                ),
            ),
        )
        root.mkdir(mode=0o700, parents=True)
        capture = (
            _witness_postgresql(config, root)
            if engine_kind == "postgresql"
            else _witness_clickhouse(config, root)
        )
    return WitnessCapture(
        result=capture.result,
        stdout=output.getvalue(),
        stderr=error.getvalue(),
        logs=tuple(logs.messages),
        private_markers=capture.private_markers,
    )


__all__ = ["witness_live_engine"]
