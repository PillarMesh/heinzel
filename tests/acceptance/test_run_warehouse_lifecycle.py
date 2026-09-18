from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import ComposeResourceKind
from heinzel_warehouse_control import EngineKind
from pydantic import SecretStr, ValidationError

import tests.acceptance.run_warehouse_lifecycle as run_warehouse_lifecycle_module
import tests.acceptance.warehouse_lifecycle_orchestration as lifecycle_orchestration_module
from tests.acceptance.run_warehouse_lifecycle import (
    REPOSITORY_ROOT,
    EngineName,
    EngineWitnessResult,
    PrivateDirectoryResourceEntry,
    PrivateDockerResourceEntry,
    PrivateResourceEntry,
    WarehouseLifecycleCleanupEvidence,
    WarehouseLifecycleConfig,
    WarehouseLifecycleEvidence,
    WarehouseLifecycleFaultMatrixEvidence,
    WarehouseLifecycleHarnessError,
    WarehouseLifecyclePrivateLedger,
    WitnessCapture,
    _normalize_witness_capture,
    canonical_json_bytes,
    main,
    register_private_resources,
    run_warehouse_lifecycle,
    scan_private_markers,
    teardown_warehouse_lifecycle,
    write_private_ledger,
)
from tests.acceptance.warehouse_lifecycle_fault_matrix import (
    WarehouseFaultScenarioOutcome,
    run_warehouse_lifecycle_fault_matrix,
)
from tests.acceptance.warehouse_lifecycle_orchestration import (
    _assert_control_plane_tenant_denial,
    _verify_docker_absence,
)

_POSTGRES_IMAGE = "postgres:18.6@sha256:" + "a" * 64
_CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:25.8.32.4@sha256:" + "b" * 64


@pytest.fixture
def private_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return parent


def _environment(private_parent: Path) -> dict[str, str]:
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH": str(private_parent / "state"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_SECRET_DIRECTORY": str(private_parent / "secrets"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_BACKUP_DIRECTORY": str(private_parent / "backups"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY": str(private_parent / "evidence"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_RESERVATION_PATH": str(private_parent / "reservation.json"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_SOURCE_COMMIT": source_commit,
        "HEINZEL_WAREHOUSE_LIFECYCLE_POSTGRES_IMAGE": _POSTGRES_IMAGE,
        "HEINZEL_WAREHOUSE_LIFECYCLE_CLICKHOUSE_IMAGE": _CLICKHOUSE_IMAGE,
        "HEINZEL_WAREHOUSE_LIFECYCLE_RETENTION_DEADLINE": "2026-08-28T12:00:00Z",
        "HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES": json.dumps(
            [f"credential-private-marker-{index}" for index in range(8)]
        ),
        "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_ENCRYPTION_KEY": "encryption-private-marker",
        "HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_SIGNING_KEY": "signing-private-marker",
    }


def _config(private_parent: Path) -> WarehouseLifecycleConfig:
    return WarehouseLifecycleConfig.from_environment(_environment(private_parent))


def test_private_ledger_write_recovers_from_a_stale_fixed_temporary_file(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    config.state_path.mkdir(mode=0o700)
    stale_temporary = config.state_path / "private-ledger.json.encrypted.temporary"
    stale_temporary.write_bytes(b"interrupted private ledger write")
    stale_temporary.chmod(0o600)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )

    write_private_ledger(config, ledger)

    reloaded = run_warehouse_lifecycle_module._load_private_ledger(config)
    assert reloaded.run_id == ledger.run_id


def test_private_ledger_write_never_follows_a_stale_temporary_symlink(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    config.state_path.mkdir(mode=0o700)
    sentinel = private_parent / "sentinel"
    sentinel.write_bytes(b"must remain unchanged")
    stale_temporary = config.state_path / "private-ledger.json.encrypted.temporary"
    stale_temporary.symlink_to(sentinel)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )

    write_private_ledger(config, ledger)

    assert sentinel.read_bytes() == b"must remain unchanged"
    assert stale_temporary.is_symlink()
    assert run_warehouse_lifecycle_module._load_private_ledger(config).run_id == ledger.run_id


def test_cost_scope_loads_only_hmac_authenticated_exact_docker_resources(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            PrivateDockerResourceEntry.create(
                engine_kind="postgresql",
                compose_resource_kind="container",
                exact_identifier="pm-exact-container",
                retention_deadline=now,
            ),
            PrivateDockerResourceEntry.create(
                engine_kind="postgresql",
                compose_resource_kind="volume",
                exact_identifier="pm-exact-volume",
                retention_deadline=now,
            ),
            PrivateDirectoryResourceEntry.create(
                engine_kind="postgresql",
                exact_path=config.state_path / "postgresql-witness",
                retention_deadline=now,
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    resources = run_warehouse_lifecycle_module.load_authenticated_docker_resources(config)

    assert tuple(
        (resource.compose_resource_kind, resource.exact_identifier) for resource in resources
    ) == (
        ("container", "pm-exact-container"),
        ("volume", "pm-exact-volume"),
    )

    ledger_path = config.state_path / "private-ledger.json.encrypted"
    encrypted = ledger_path.read_bytes()
    ledger_path.write_bytes(b"x" + encrypted[1:])
    with pytest.raises(WarehouseLifecycleHarnessError, match="missing or corrupt"):
        run_warehouse_lifecycle_module.load_authenticated_docker_resources(config)


def test_private_ledger_rejects_trailing_ciphertext_bytes(private_parent: Path) -> None:
    config = _config(private_parent)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    ledger_path = config.state_path / "private-ledger.json.encrypted"

    ledger_path.write_bytes(ledger_path.read_bytes() + b"corrupt")

    with pytest.raises(WarehouseLifecycleHarnessError, match="missing or corrupt"):
        run_warehouse_lifecycle_module._load_private_ledger(config)


def test_configuration_is_factory_only_and_binds_source_commit(
    private_parent: Path,
) -> None:
    with pytest.raises(TypeError):
        WarehouseLifecycleConfig()

    environment = _environment(private_parent)
    environment["HEINZEL_WAREHOUSE_LIFECYCLE_SOURCE_COMMIT"] = "d" * 40
    with pytest.raises(WarehouseLifecycleHarnessError, match="checked-out commit"):
        WarehouseLifecycleConfig.from_environment(environment)


@pytest.mark.parametrize("engine_kind", tuple(EngineKind))
def test_control_plane_tenant_probe_observes_denial(engine_kind: EngineKind) -> None:
    _assert_control_plane_tenant_denial(engine_kind)


def test_explicit_absence_check_cannot_be_disabled_with_python_assertions() -> None:
    class RecordingCompose:
        absent = False

        def resource_is_absent(
            self,
            *,
            resource_kind: ComposeResourceKind,
            identifier: str,
            environment: Mapping[str, str],
        ) -> bool:
            assert resource_kind == "volume"
            assert identifier == "exact-volume"
            assert environment == {}
            return self.absent

    compose = RecordingCompose()
    with pytest.raises(
        WarehouseLifecycleHarnessError, match="explicit lifecycle check failed: absence"
    ):
        _verify_docker_absence(compose, (("volume", "exact-volume"),))

    compose.absent = True

    assert _verify_docker_absence(compose, (("volume", "exact-volume"),)) == "absence:passed"


def _engine_result(engine_kind: EngineName) -> EngineWitnessResult:
    return EngineWitnessResult(
        engine_kind=engine_kind,
        binding_digest=("1" if engine_kind == "postgresql" else "6") * 64,
        validation_evidence_digest="2" * 64,
        resume_evidence_digest="3" * 64,
        retirement_evidence_digest="4" * 64,
        cleanup_evidence_digest="5" * 64,
        terminal_state="retired",
        engine_version="18.6" if engine_kind == "postgresql" else "25.8.32.4",
        engine_image=_POSTGRES_IMAGE if engine_kind == "postgresql" else _CLICKHOUSE_IMAGE,
        check_dispositions=(
            "provision:passed",
            "initial_validation:passed",
            "backup_restore:passed",
            "suspend_resume:passed",
            "retention_cleanup:passed",
            "absence:passed",
        ),
        duration_seconds=1.25,
        residual_resource_count=0,
    )


def test_fault_outcome_contract_binds_sanitized_terminal_replay_and_cleanup_proof(
    tmp_path: Path,
) -> None:
    outcome = run_warehouse_lifecycle_fault_matrix(tmp_path / "fault-matrix")[0]
    encoded = outcome.model_dump_json()

    assert set(WarehouseFaultScenarioOutcome.model_fields) == {
        "schema_version",
        "engine_kind",
        "scenario_id",
        "checkpoint",
        "checkpoint_occurrence",
        "provider_operation",
        "failure_classification",
        "expected_fault_state",
        "observed_fault_state",
        "expected_terminal_state",
        "observed_terminal_state",
        "replay_disposition",
        "cleanup_proof",
        "cleanup_proof_digest",
        "disposition",
    }
    assert set(type(outcome.cleanup_proof).model_fields) == {
        "schema_version",
        "repository_reopen_count",
        "repository_session_count",
        "provider_session_count",
        "distinct_session_generation_count",
        "session_generation_digest",
        "provision_effect_count",
        "unique_resource_count",
        "residual_resource_count",
        "suspend_effect_count",
        "resume_effect_count",
        "retire_effect_count",
        "terminal_operation_count",
        "terminal_resource_count",
    }
    assert outcome.cleanup_proof.schema_version == "2"
    assert outcome.cleanup_proof_digest == digest(
        {
            "domain": "heinzel-warehouse-lifecycle-offline-control-plane-cleanup-proof-v2",
            "engine_kind": outcome.engine_kind,
            "scenario_id": outcome.scenario_id,
            "proof": outcome.cleanup_proof.model_dump(mode="json"),
        }
    )
    assert "/private/fault/path" not in encoded
    assert "postgresql://private-endpoint" not in encoded
    with pytest.raises(ValidationError):
        WarehouseFaultScenarioOutcome.model_validate(
            {**outcome.model_dump(), "private_path": "/private/fault/path"}
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "duplicate",
        "unexpected",
        "skipped",
        "failed",
        "wrong_replay_disposition",
        "forged_cleanup_digest",
        "zero_provision_effect",
        "duplicate_provision_effect",
        "nonzero_residue",
        "wrong_effect_counts",
        "wrong_terminal_counts",
        "all_zero_proof",
    ),
)
def test_run_fails_closed_on_incomplete_or_unsuccessful_fault_matrix(
    private_parent: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _config(private_parent)
    outcomes = list(run_warehouse_lifecycle_fault_matrix(tmp_path / f"fault-matrix-{mutation}"))
    checkpoint_index = next(
        index
        for index, outcome in enumerate(outcomes)
        if outcome.engine_kind == "postgresql"
        and outcome.scenario_id == "checkpoint:after_operation_claim:1"
    )
    checkpoint_outcome = outcomes[checkpoint_index]
    if mutation == "missing":
        outcomes.pop()
    elif mutation == "duplicate":
        outcomes.append(outcomes[-1])
    elif mutation == "unexpected":
        outcomes.append(
            outcomes[-1].model_copy(update={"scenario_id": "checkpoint:after_operation_claim:5"})
        )
    elif mutation == "wrong_replay_disposition":
        outcomes[checkpoint_index] = checkpoint_outcome.model_copy(
            update={"replay_disposition": "remained_resumable_after_restart"}
        )
    elif mutation == "forged_cleanup_digest":
        outcomes[checkpoint_index] = checkpoint_outcome.model_copy(
            update={"cleanup_proof_digest": "0" * 64}
        )
    elif mutation in {
        "zero_provision_effect",
        "duplicate_provision_effect",
        "nonzero_residue",
        "wrong_effect_counts",
        "wrong_terminal_counts",
        "all_zero_proof",
    }:
        proof_updates: dict[str, int | str]
        if mutation == "zero_provision_effect":
            proof_updates = {
                "provision_effect_count": 0,
                "unique_resource_count": 0,
                "terminal_resource_count": 0,
            }
        elif mutation == "duplicate_provision_effect":
            proof_updates = {"provision_effect_count": 2}
        elif mutation == "nonzero_residue":
            proof_updates = {"residual_resource_count": 1}
        elif mutation == "wrong_effect_counts":
            proof_updates = {
                "suspend_effect_count": 2,
                "resume_effect_count": 0,
                "retire_effect_count": 2,
            }
        elif mutation == "wrong_terminal_counts":
            proof_updates = {
                "terminal_operation_count": 0,
                "terminal_resource_count": 0,
            }
        else:
            proof_updates = {
                field_name: "0" * 64 if field_name == "session_generation_digest" else 0
                for field_name in type(checkpoint_outcome.cleanup_proof).model_fields
                if field_name != "schema_version"
            }
        proof = checkpoint_outcome.cleanup_proof.model_copy(update=proof_updates)
        proof_digest = digest(
            {
                "domain": "heinzel-warehouse-lifecycle-offline-control-plane-cleanup-proof-v2",
                "engine_kind": checkpoint_outcome.engine_kind,
                "scenario_id": checkpoint_outcome.scenario_id,
                "proof": proof.model_dump(mode="json"),
            }
        )
        outcomes[checkpoint_index] = checkpoint_outcome.model_copy(
            update={"cleanup_proof": proof, "cleanup_proof_digest": proof_digest}
        )
    else:
        outcomes[-1] = outcomes[-1].model_copy(update={"disposition": mutation})

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    with pytest.raises(WarehouseLifecycleHarnessError, match="fault matrix"):
        run_warehouse_lifecycle(
            config,
            witness=witness,
            fault_matrix=lambda _: tuple(outcomes),
            cleanup_authorized=True,
        )


def test_fault_matrix_workspace_is_cleaned_after_fatal_interruption(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    matrix_root = config.state_path / "offline-control-plane-fault-matrix"

    def interrupted_fault_matrix(root: Path) -> tuple[WarehouseFaultScenarioOutcome, ...]:
        root.mkdir(mode=0o700)
        (root / "durable-state").write_text("interrupted")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_warehouse_lifecycle(
            config,
            fault_matrix=interrupted_fault_matrix,
            cleanup_authorized=True,
        )

    assert not matrix_root.exists()
    ledger = run_warehouse_lifecycle_module._load_private_ledger(config)
    assert ledger.cleanup_evidence is None
    assert (
        sum(
            isinstance(
                resource, run_warehouse_lifecycle_module.PrivateFaultMatrixDirectoryResourceEntry
            )
            for resource in ledger.resources
        )
        == 1
    )

    cleanup = teardown_warehouse_lifecycle(
        config,
        run_id=ledger.run_id,
        now=datetime(2026, 8, 29, 12, tzinfo=UTC),
        authorized=True,
    )
    replayed = teardown_warehouse_lifecycle(
        config,
        run_id=ledger.run_id,
        now=datetime(2026, 8, 29, 12, tzinfo=UTC),
        authorized=True,
    )

    assert cleanup.zero_residual_resources is True
    assert replayed == cleanup


def test_fault_matrix_cleanup_cancellation_is_recovered_from_the_exact_private_ledger(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(private_parent)
    matrix_root = config.state_path / "offline-control-plane-fault-matrix"
    original_rmtree = run_warehouse_lifecycle_module.shutil.rmtree

    def cancelled_cleanup(path: Path) -> None:
        if Path(path) == matrix_root:
            raise KeyboardInterrupt
        original_rmtree(path)

    monkeypatch.setattr(run_warehouse_lifecycle_module.shutil, "rmtree", cancelled_cleanup)

    with pytest.raises(KeyboardInterrupt):
        run_warehouse_lifecycle(config, cleanup_authorized=True)

    ledger = run_warehouse_lifecycle_module._load_private_ledger(config)
    fault_resources = tuple(
        resource
        for resource in ledger.resources
        if isinstance(
            resource, run_warehouse_lifecycle_module.PrivateFaultMatrixDirectoryResourceEntry
        )
    )

    assert matrix_root.is_dir()
    assert len(fault_resources) == 1
    assert fault_resources[0].exact_path == str(matrix_root)
    assert ledger.cleanup_evidence is None

    unrelated = config.state_path / "unrelated-private-directory"
    unrelated.mkdir(mode=0o700)
    monkeypatch.setattr(run_warehouse_lifecycle_module.shutil, "rmtree", original_rmtree)

    cleanup = teardown_warehouse_lifecycle(
        config,
        run_id=ledger.run_id,
        now=datetime(2026, 8, 29, 12, tzinfo=UTC),
        authorized=True,
    )

    assert not matrix_root.exists()
    assert unrelated.is_dir()
    assert cleanup.completed_resource_count == 1
    assert cleanup.retained_resource_count == 0
    assert cleanup.failed_resource_count == 0
    assert cleanup.zero_residual_resources is True


def test_witnessed_fault_matrix_requires_exact_task_8_scenarios(
    tmp_path: Path,
) -> None:
    outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / "fault-matrix")
    by_engine = {
        engine_kind: tuple(outcome for outcome in outcomes if outcome.engine_kind == engine_kind)
        for engine_kind in ("postgresql", "clickhouse")
    }

    assert len(outcomes) == 64
    assert all(len(engine_outcomes) == 32 for engine_outcomes in by_engine.values())
    assert all(
        sum(outcome.scenario_id.startswith("checkpoint:") for outcome in engine_outcomes) == 20
        for engine_outcomes in by_engine.values()
    )
    assert all(
        sum(outcome.scenario_id.startswith("classification:") for outcome in engine_outcomes) == 12
        for engine_outcomes in by_engine.values()
    )


def test_evidence_contracts_are_strict_and_omit_private_identifiers(tmp_path: Path) -> None:
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    fault_outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / "strict-evidence-fault-matrix")
    failure_matrix = WarehouseLifecycleFaultMatrixEvidence(outcomes=fault_outcomes)
    cleanup = WarehouseLifecycleCleanupEvidence(
        run_id="6" * 64,
        resource_inventory_digest="7" * 64,
        completed_resource_count=12,
        retained_resource_count=0,
        failed_resource_count=0,
        zero_residual_resources=True,
        verified_at=now,
    )
    evidence = WarehouseLifecycleEvidence(
        run_id=cleanup.run_id,
        source_commit="c" * 40,
        engine_results=(_engine_result("postgresql"), _engine_result("clickhouse")),
        cross_engine_conformance_digest="8" * 64,
        tenant_isolation_digest="9" * 64,
        failure_matrix=failure_matrix,
        failure_matrix_digest=digest(
            {
                "domain": "heinzel-warehouse-lifecycle-offline-control-plane-failure-matrix-v1",
                "outcomes": tuple(
                    outcome.model_dump(mode="json") for outcome in failure_matrix.outcomes
                ),
            }
        ),
        privacy_scan_digest="b" * 64,
        local_encryption_limitation="deferred_local_acceptance",
        production_readiness="not_proven",
        lifecycle_conformance="proven",
        cleanup_digest="d" * 64,
        started_at=now,
        completed_at=now + timedelta(seconds=3),
    )

    encoded = canonical_json_bytes(evidence)

    assert cleanup.zero_residual_resources is True
    assert evidence.schema_version == "2"
    assert b"tenant-private-marker" not in encoded
    assert b"/private/warehouse/path" not in encoded
    with pytest.raises(ValidationError):
        WarehouseLifecycleEvidence.model_validate({**evidence.model_dump(), "endpoint": "private"})


@pytest.mark.parametrize(
    ("variable", "replacement"),
    (
        ("HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH", "relative/state"),
        ("HEINZEL_WAREHOUSE_LIFECYCLE_POSTGRES_IMAGE", "postgres:latest"),
        ("HEINZEL_WAREHOUSE_LIFECYCLE_STATE_ENCRYPTION_KEY", ""),
        ("HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_SIGNING_KEY", ""),
    ),
)
def test_configuration_rejects_unsafe_paths_floating_images_and_missing_keys(
    private_parent: Path,
    variable: str,
    replacement: str,
) -> None:
    environment = _environment(private_parent)
    environment[variable] = replacement

    with pytest.raises(WarehouseLifecycleHarnessError):
        WarehouseLifecycleConfig.from_environment(
            environment,
            repository_root=REPOSITORY_ROOT,
        )


def test_configuration_rejects_duplicate_nonempty_and_symlink_targets(
    private_parent: Path,
) -> None:
    environment = _environment(private_parent)
    environment["HEINZEL_WAREHOUSE_LIFECYCLE_BACKUP_DIRECTORY"] = environment[
        "HEINZEL_WAREHOUSE_LIFECYCLE_SECRET_DIRECTORY"
    ]
    with pytest.raises(WarehouseLifecycleHarnessError, match="duplicate"):
        WarehouseLifecycleConfig.from_environment(
            environment,
            repository_root=REPOSITORY_ROOT,
        )

    environment = _environment(private_parent)
    evidence = Path(environment["HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY"])
    evidence.mkdir(mode=0o700)
    (evidence / "existing").write_text("foreign")
    with pytest.raises(WarehouseLifecycleHarnessError, match="nonempty"):
        WarehouseLifecycleConfig.from_environment(
            environment,
            repository_root=REPOSITORY_ROOT,
        )

    for child in evidence.iterdir():
        child.unlink()
    evidence.rmdir()
    target = private_parent / "target"
    target.mkdir()
    evidence.symlink_to(target, target_is_directory=True)
    with pytest.raises(WarehouseLifecycleHarnessError, match="symlink"):
        WarehouseLifecycleConfig.from_environment(
            environment,
            repository_root=REPOSITORY_ROOT,
        )


def test_configuration_rejects_group_accessible_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o750)
    os.chmod(parent, 0o750)

    with pytest.raises(WarehouseLifecycleHarnessError, match="0700"):
        WarehouseLifecycleConfig.from_environment(
            _environment(parent),
            repository_root=REPOSITORY_ROOT,
        )


def test_teardown_configuration_reopens_only_owner_only_existing_targets(
    private_parent: Path,
) -> None:
    environment = _environment(private_parent)
    for name in (
        "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH",
        "HEINZEL_WAREHOUSE_LIFECYCLE_SECRET_DIRECTORY",
        "HEINZEL_WAREHOUSE_LIFECYCLE_BACKUP_DIRECTORY",
        "HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY",
    ):
        path = Path(environment[name])
        path.mkdir(mode=0o700)
        (path / "owned").write_text("state")
    reservation = Path(environment["HEINZEL_WAREHOUSE_LIFECYCLE_RESERVATION_PATH"])
    reservation.write_text("state")
    reservation.chmod(0o600)

    config = WarehouseLifecycleConfig.from_environment(
        environment,
        repository_root=REPOSITORY_ROOT,
        allow_existing=True,
    )

    assert config.reservation_path == reservation


def test_privacy_scanner_checks_json_stdout_stderr_and_logs() -> None:
    marker = "private-endpoint-marker"
    safe = WitnessCapture(stdout="terminal\n", stderr="", logs=("engine complete",))

    assert len(scan_private_markers(b"{}\n", safe, (marker,))) == 64
    for capture in (
        WitnessCapture(stdout=marker, stderr="", logs=()),
        WitnessCapture(stdout="", stderr=marker, logs=()),
        WitnessCapture(stdout="", stderr="", logs=(marker,)),
    ):
        with pytest.raises(WarehouseLifecycleHarnessError, match="privacy scan"):
            scan_private_markers(b"{}\n", capture, (marker,))
    with pytest.raises(WarehouseLifecycleHarnessError, match="privacy scan"):
        scan_private_markers(json.dumps({"value": marker}).encode(), safe, (marker,))


@pytest.mark.parametrize("channel", ("evidence", "stdout", "stderr", "logs"))
@pytest.mark.parametrize("marker_name", ("secret_directory", "authority_key"))
def test_clickhouse_witness_scans_each_harness_private_marker_in_every_channel(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    marker_name: str,
) -> None:
    config = _config(private_parent)
    secret_directory = private_parent / "clickhouse-secret-directory-private-marker"
    authority_key_marker = "clickhouse-fernet-authority-private-marker"
    binding = SimpleNamespace(
        tenant_id="clickhouse-tenant-private-marker",
        binding_id="whb-clickhouse-private-marker",
    )
    driver = SimpleNamespace(
        binding=binding,
        provisioning_binding=binding,
        provision_operation=object(),
        compose=object(),
        private_directory=private_parent / "clickhouse-private-directory-marker",
        secret_directory=secret_directory,
        configuration=SimpleNamespace(authority_key=SecretStr(authority_key_marker)),
        private_markers=("existing-clickhouse-private-marker",),
    )
    identity = SimpleNamespace(
        container_name="primary-container",
        private_network_name="primary-network",
        loopback_network_name="primary-loopback-network",
        data_volume_name="primary-volume",
    )
    restore = SimpleNamespace(
        container_name="restore-container",
        isolation_probe_container_name="restore-probe-container",
        private_network_name="restore-network",
        loopback_network_name="restore-loopback-network",
        data_volume_name="restore-volume",
    )
    observation = SimpleNamespace(initial_resources=())

    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "CLICKHOUSE_WAREHOUSE_IMAGE",
        config.clickhouse_image,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "new_clickhouse_driver",
        lambda *args, **kwargs: driver,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "clickhouse_warehouse_identity",
        lambda *args: identity,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "clickhouse_restore_identity",
        lambda *args: restore,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module, "register_private_resources", lambda *args: None
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "assert_warehouse_lifecycle_contract",
        lambda *args: observation,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "_validate_observation",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "_verify_docker_absence",
        lambda *args: "absence:passed",
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "_assert_control_plane_tenant_denial",
        lambda *args: None,
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "_result",
        lambda **kwargs: _engine_result("clickhouse"),
    )
    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "cleanup_exact_clickhouse_resources",
        lambda *args: None,
    )

    capture = lifecycle_orchestration_module._witness_clickhouse(
        config,
        private_parent / "clickhouse-witness",
    )
    marker = {
        "secret_directory": str(secret_directory),
        "authority_key": authority_key_marker,
    }[marker_name]
    encoded_evidence = json.dumps({"value": marker}).encode() if channel == "evidence" else b"{}"
    leaked_capture = WitnessCapture(
        result=capture.result,
        stdout=marker if channel == "stdout" else "",
        stderr=marker if channel == "stderr" else "",
        logs=(marker,) if channel == "logs" else (),
        private_markers=capture.private_markers,
    )

    with pytest.raises(WarehouseLifecycleHarnessError, match="privacy scan"):
        scan_private_markers(encoded_evidence, leaked_capture, ())


def test_live_engine_capture_encloses_adapter_construction_and_cleanup(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(private_parent)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    marker = "constructor-cleanup-private-marker"

    def fake_witness(_: WarehouseLifecycleConfig, root: Path) -> WitnessCapture:
        print(marker)
        logging.getLogger("warehouse-lifecycle-test").warning(marker)
        assert root.name == "postgresql-witness"
        return WitnessCapture(result=_engine_result("postgresql"), private_markers=(marker,))

    monkeypatch.setattr(
        lifecycle_orchestration_module,
        "_witness_postgresql",
        fake_witness,
    )

    capture = lifecycle_orchestration_module.witness_live_engine("postgresql", config)

    assert marker in capture.stdout
    assert any(marker in message for message in capture.logs)
    with pytest.raises(WarehouseLifecycleHarnessError, match="privacy scan"):
        scan_private_markers(b"{}\n", capture, ())


def test_witness_boundary_reconstructs_result_from_another_module_identity() -> None:
    source = _engine_result("postgresql")

    class ForeignResult:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return source.model_dump(mode="json")

    class ForeignCapture:
        result = ForeignResult()
        stdout = "terminal"
        stderr = ""
        logs = ("sanitized",)
        private_markers: tuple[str, ...] = ()

    normalized = _normalize_witness_capture(ForeignCapture())

    assert type(normalized.result) is EngineWitnessResult
    assert normalized.result == source


def test_run_warehouse_lifecycle_orders_engines_and_returns_only_sanitized_evidence(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    observed: list[str] = []

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        observed.append(engine_kind)
        return WitnessCapture(
            result=_engine_result(engine_kind),
            stdout="terminal",
            stderr="",
            logs=("sanitized lifecycle log",),
        )

    evidence = run_warehouse_lifecycle(config, witness=witness, cleanup_authorized=True)

    assert observed == ["postgresql", "clickhouse"]
    assert evidence.production_readiness == "not_proven"
    assert evidence.lifecycle_conformance == "proven"
    assert evidence.local_encryption_limitation == "deferred_local_acceptance"
    assert tuple(result.engine_kind for result in evidence.engine_results) == (
        "postgresql",
        "clickhouse",
    )


def test_exported_evidence_retains_canonical_fault_outcomes(
    private_parent: Path,
    tmp_path: Path,
) -> None:
    config = _config(private_parent)
    outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / "retained-fault-matrix")

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    evidence = run_warehouse_lifecycle(
        config,
        witness=witness,
        fault_matrix=lambda _: outcomes,
        cleanup_authorized=True,
    )
    encoded = canonical_json_bytes(evidence)
    reloaded = WarehouseLifecycleEvidence.model_validate_json(encoded)
    stored = (config.evidence_directory / "warehouse-lifecycle-evidence.json").read_bytes()

    assert stored == encoded
    assert evidence.failure_matrix.outcomes == outcomes
    assert reloaded.failure_matrix.outcomes == outcomes
    assert len(reloaded.failure_matrix.outcomes) == 64
    assert b'"checkpoint":"after_operation_claim"' in encoded
    assert b'"observed_at"' not in encoded


@pytest.mark.parametrize(
    "mutation",
    (
        "forged_failure_matrix_digest",
        "dropped_outcome",
        "mutated_outcome",
        "cleanup_schema_downgrade",
        "cleanup_domain_downgrade",
    ),
)
def test_warehouse_lifecycle_evidence_load_rejects_fault_matrix_mutations(
    private_parent: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _config(private_parent)
    outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / f"evidence-mutation-{mutation}")

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    evidence = run_warehouse_lifecycle(
        config,
        witness=witness,
        fault_matrix=lambda _: outcomes,
        cleanup_authorized=True,
    )
    payload = json.loads(canonical_json_bytes(evidence))
    retained_outcomes = payload["failure_matrix"]["outcomes"]
    checkpoint = next(
        outcome
        for outcome in retained_outcomes
        if outcome["engine_kind"] == "postgresql"
        and outcome["scenario_id"] == "checkpoint:after_operation_claim:1"
    )
    if mutation == "forged_failure_matrix_digest":
        payload["failure_matrix_digest"] = "0" * 64
    elif mutation == "dropped_outcome":
        retained_outcomes.pop()
    elif mutation == "mutated_outcome":
        checkpoint["replay_disposition"] = "remained_resumable_after_restart"
    else:
        proof = checkpoint["cleanup_proof"]
        if mutation == "cleanup_schema_downgrade":
            proof["schema_version"] = "1"
            proof_domain = "heinzel-warehouse-lifecycle-offline-control-plane-cleanup-proof-v2"
        else:
            proof_domain = "heinzel-warehouse-lifecycle-offline-control-plane-cleanup-proof-v1"
        checkpoint["cleanup_proof_digest"] = digest(
            {
                "domain": proof_domain,
                "engine_kind": checkpoint["engine_kind"],
                "scenario_id": checkpoint["scenario_id"],
                "proof": proof,
            }
        )
    if mutation != "forged_failure_matrix_digest":
        payload["failure_matrix_digest"] = digest(
            {
                "domain": "heinzel-warehouse-lifecycle-offline-control-plane-failure-matrix-v1",
                "outcomes": tuple(retained_outcomes),
            }
        )

    with pytest.raises(ValidationError):
        WarehouseLifecycleEvidence.model_validate_json(json.dumps(payload, sort_keys=True))


def test_aggregate_privacy_scan_covers_retained_fault_outcomes(
    private_parent: Path,
    tmp_path: Path,
) -> None:
    environment = _environment(private_parent)
    marker = "checkpoint:after_operation_claim:1"
    canaries = json.loads(environment["HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES"])
    canaries[0] = marker
    environment["HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES"] = json.dumps(canaries)
    config = WarehouseLifecycleConfig.from_environment(environment)
    outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / "privacy-fault-matrix")

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    with pytest.raises(WarehouseLifecycleHarnessError, match="privacy scan"):
        run_warehouse_lifecycle(
            config,
            witness=witness,
            fault_matrix=lambda _: outcomes,
            cleanup_authorized=True,
        )


def test_failure_matrix_digest_is_not_derived_from_normal_lifecycle_checks(
    private_parent: Path,
    tmp_path: Path,
) -> None:
    config = _config(private_parent)
    outcomes = run_warehouse_lifecycle_fault_matrix(tmp_path / "fault-matrix-digest")

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    evidence = run_warehouse_lifecycle(
        config,
        witness=witness,
        fault_matrix=lambda _: outcomes,
        cleanup_authorized=True,
    )
    legacy_check_digest = digest(
        {
            "domain": "heinzel-warehouse-lifecycle-failure-matrix-v1",
            "checks": tuple(result.check_dispositions for result in evidence.engine_results),
        }
    )

    expected_fault_digest = digest(
        {
            "domain": "heinzel-warehouse-lifecycle-offline-control-plane-failure-matrix-v1",
            "outcomes": tuple(outcome.model_dump(mode="json") for outcome in outcomes),
        }
    )

    assert evidence.failure_matrix_digest == expected_fault_digest
    assert evidence.failure_matrix_digest != legacy_check_digest


def test_run_requires_explicit_cleanup_authorization_before_engine_effects(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    called = False

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        nonlocal called
        called = True
        return WitnessCapture(result=_engine_result(engine_kind))

    with pytest.raises(PermissionError, match="explicit retention authorization"):
        run_warehouse_lifecycle(config, witness=witness)

    assert called is False


def test_run_refuses_cleanup_authorization_before_real_deadline(
    private_parent: Path,
) -> None:
    environment = _environment(private_parent)
    environment["HEINZEL_WAREHOUSE_LIFECYCLE_RETENTION_DEADLINE"] = "2099-08-29T12:00:00Z"
    config = WarehouseLifecycleConfig.from_environment(environment)
    called = False

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        nonlocal called
        called = True
        return WitnessCapture(result=_engine_result(engine_kind))

    with pytest.raises(WarehouseLifecycleHarnessError, match="retention deadline has not elapsed"):
        run_warehouse_lifecycle(config, witness=witness, cleanup_authorized=True)

    assert called is False


def test_teardown_retains_secret_while_backup_is_not_authorized(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    config.secret_directory.mkdir(mode=0o700)
    config.backup_directory.mkdir(mode=0o700)
    secret = config.secret_directory / "engine.secret"
    backup = config.backup_directory / "engine.backup"
    secret.write_text("encrypted secret")
    backup.write_text("encrypted backup")
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            PrivateResourceEntry.file_pair(
                backup_path=backup,
                secret_path=secret,
                retention_deadline=now + timedelta(hours=1),
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    retained = teardown_warehouse_lifecycle(config, run_id=ledger.run_id, now=now, authorized=True)

    assert retained.retained_resource_count == 2
    assert secret.exists()
    assert backup.exists()

    cleaned = teardown_warehouse_lifecycle(
        config,
        run_id=ledger.run_id,
        now=now + timedelta(hours=2),
        authorized=True,
    )
    replayed = teardown_warehouse_lifecycle(
        config,
        run_id=ledger.run_id,
        now=now + timedelta(hours=2),
        authorized=True,
    )

    assert cleaned == replayed
    assert cleaned.zero_residual_resources is True
    assert not secret.exists()
    assert not backup.exists()


def test_teardown_refuses_missing_corrupt_or_foreign_ledger(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)

    with pytest.raises(WarehouseLifecycleHarnessError, match="ledger"):
        teardown_warehouse_lifecycle(config, run_id="6" * 64, now=now, authorized=True)

    config.state_path.mkdir(mode=0o700)
    (config.state_path / "private-ledger.json").write_text("{not-json")
    with pytest.raises(WarehouseLifecycleHarnessError, match="ledger"):
        teardown_warehouse_lifecycle(config, run_id="6" * 64, now=now, authorized=True)

    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="7" * 64,
        reservation_digest="8" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    with pytest.raises(WarehouseLifecycleHarnessError, match="run identity"):
        teardown_warehouse_lifecycle(config, run_id="6" * 64, now=now, authorized=True)


def test_interrupted_run_teardown_removes_only_exact_registered_docker_resource(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(private_parent)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    resource = PrivateDockerResourceEntry.create(
        engine_kind="postgresql",
        compose_resource_kind="volume",
        exact_identifier="pm-exact-volume",
        retention_deadline=now,
    )
    register_private_resources(config, (resource,))

    class RecordingCompose:
        def __init__(self) -> None:
            self.removed: list[tuple[str, str]] = []

        def remove_resource(
            self,
            *,
            resource_kind: str,
            identifier: str,
            environment: dict[str, str],
        ) -> None:
            assert environment == {}
            self.removed.append((resource_kind, identifier))

        def resource_is_absent(
            self,
            *,
            resource_kind: str,
            identifier: str,
            environment: dict[str, str],
        ) -> bool:
            assert environment == {}
            return (resource_kind, identifier) in self.removed

    compose = RecordingCompose()
    monkeypatch.setattr(run_warehouse_lifecycle_module, "_compose_for_resource", lambda *_: compose)

    cleanup = teardown_warehouse_lifecycle(config, run_id=ledger.run_id, now=now, authorized=True)

    assert compose.removed == [("volume", "pm-exact-volume")]
    assert cleanup.completed_resource_count == 1
    assert cleanup.zero_residual_resources is True


def test_teardown_cli_recovers_exact_run_without_public_evidence(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = _environment(private_parent)
    config = WarehouseLifecycleConfig.from_environment(environment)
    deadline = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    resource = PrivateDockerResourceEntry.create(
        engine_kind="postgresql",
        compose_resource_kind="volume",
        exact_identifier="pm-exact-recovery-volume",
        retention_deadline=deadline,
    )
    register_private_resources(config, (resource,))

    class RecordingCompose:
        def __init__(self) -> None:
            self.removed: list[tuple[str, str]] = []

        def remove_resource(
            self,
            *,
            resource_kind: str,
            identifier: str,
            environment: dict[str, str],
        ) -> None:
            assert environment == {}
            self.removed.append((resource_kind, identifier))

        def resource_is_absent(
            self,
            *,
            resource_kind: str,
            identifier: str,
            environment: dict[str, str],
        ) -> bool:
            assert environment == {}
            return (resource_kind, identifier) in self.removed

    compose = RecordingCompose()
    monkeypatch.setattr(run_warehouse_lifecycle_module, "_compose_for_resource", lambda *_: compose)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    exit_code = main(["teardown", "--authorize-retention-cleanup"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "complete"
    assert compose.removed == [("volume", "pm-exact-recovery-volume")]
    assert not (config.evidence_directory / "warehouse-lifecycle-evidence.json").exists()


def test_teardown_attempts_both_files_when_one_exact_unlink_fails(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    config.backup_directory.mkdir(mode=0o700)
    config.secret_directory.mkdir(mode=0o700)
    backup = config.backup_directory / "undeletable-backup"
    backup.mkdir(mode=0o700)
    secret = config.secret_directory / "deletable-secret"
    secret.write_text("encrypted secret", encoding="utf-8")
    deadline = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            PrivateResourceEntry.file_pair(
                backup_path=backup,
                secret_path=secret,
                retention_deadline=deadline,
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    cleanup = teardown_warehouse_lifecycle(config, run_id=None, now=deadline, authorized=True)

    assert backup.exists()
    assert not secret.exists()
    assert cleanup.completed_resource_count == 1
    assert cleanup.failed_resource_count == 1
    assert cleanup.zero_residual_resources is False


def test_teardown_attempts_later_directories_after_one_cannot_be_removed(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    config.state_path.mkdir(mode=0o700)
    postgres_root = config.state_path / "postgresql-witness"
    clickhouse_root = config.state_path / "clickhouse-witness"
    postgres_root.mkdir(mode=0o750)
    os.chmod(postgres_root, 0o750)
    clickhouse_root.mkdir(mode=0o700)
    deadline = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            PrivateDirectoryResourceEntry.create(
                engine_kind="postgresql",
                exact_path=postgres_root,
                retention_deadline=deadline,
            ),
            PrivateDirectoryResourceEntry.create(
                engine_kind="clickhouse",
                exact_path=clickhouse_root,
                retention_deadline=deadline,
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    cleanup = teardown_warehouse_lifecycle(config, run_id=None, now=deadline, authorized=True)

    assert postgres_root.exists()
    assert not clickhouse_root.exists()
    assert cleanup.completed_resource_count == 1
    assert cleanup.failed_resource_count == 1


def test_teardown_validates_every_recorded_scope_before_any_cleanup_effect(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(private_parent)
    outside = private_parent / "outside"
    outside.mkdir(mode=0o700)
    backup = outside / "backup"
    secret = outside / "secret"
    backup.write_text("encrypted backup", encoding="utf-8")
    secret.write_text("encrypted secret", encoding="utf-8")
    deadline = datetime(2026, 8, 28, 12, tzinfo=UTC)
    docker_resource = PrivateDockerResourceEntry.create(
        engine_kind="postgresql",
        compose_resource_kind="volume",
        exact_identifier="pm-exact-volume",
        retention_deadline=deadline,
    )
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            docker_resource,
            PrivateResourceEntry.file_pair(
                backup_path=backup,
                secret_path=secret,
                retention_deadline=deadline,
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    class RecordingCompose:
        def __init__(self) -> None:
            self.removed: list[tuple[str, str]] = []

        def remove_resource(self, **kwargs: str) -> None:
            self.removed.append((kwargs["resource_kind"], kwargs["identifier"]))

        def resource_is_absent(self, **kwargs: str) -> bool:
            return True

    compose = RecordingCompose()
    monkeypatch.setattr(run_warehouse_lifecycle_module, "_compose_for_resource", lambda *_: compose)

    with pytest.raises(WarehouseLifecycleHarnessError, match="outside its exact scope"):
        teardown_warehouse_lifecycle(config, run_id=None, now=deadline, authorized=True)

    assert compose.removed == []
    assert backup.exists()
    assert secret.exists()


def test_validate_evidence_cli_strictly_reloads_fault_proof_and_rescans_privacy(
    private_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = _environment(private_parent)
    config = WarehouseLifecycleConfig.from_environment(environment)

    def witness(engine_kind: EngineName, _: WarehouseLifecycleConfig) -> WitnessCapture:
        return WitnessCapture(result=_engine_result(engine_kind))

    run_warehouse_lifecycle(config, witness=witness, cleanup_authorized=True)
    evidence_path = config.evidence_directory / "warehouse-lifecycle-evidence.json"
    original = evidence_path.read_bytes()
    ledger_path = config.state_path / "private-ledger.json.encrypted"
    original_ledger = ledger_path.read_bytes()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    exit_code = main(["validate-evidence"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {
        "evidence_digest": hashlib.sha256(original).hexdigest(),
        "status": "valid",
    }

    private_marker = json.loads(environment["HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES"])[0]
    mutations: tuple[tuple[str, object], ...] = (
        ("privacy_scan_digest", "0" * 64),
        ("run_id", "f" * 64),
        ("cleanup_digest", "0" * 64),
        ("failure_matrix_digest", "0" * 64),
        ("engine_validation_digest", "0" * 64),
        ("private_marker", private_marker),
        ("tenant_private_marker_field", private_marker),
    )
    for mutation, value in mutations:
        malformed = json.loads(original)
        if mutation == "engine_validation_digest":
            malformed["engine_results"][0]["validation_evidence_digest"] = value
        elif mutation == "private_marker":
            malformed["engine_results"][0]["engine_version"] = value
        elif mutation == "tenant_private_marker_field":
            malformed["tenant_id"] = value
        else:
            malformed[mutation] = value
        evidence_path.write_bytes(canonical_json_bytes(malformed))

        assert main(["validate-evidence"]) == 2, mutation
        error = capsys.readouterr().err
        assert "ERROR:" in error
        assert private_marker not in error
        assert str(private_parent) not in error

    evidence_path.write_bytes(original)
    ledger = run_warehouse_lifecycle_module._load_private_ledger(config)
    assert ledger.public_evidence_digest == hashlib.sha256(original).hexdigest()
    unsigned_ledger = ledger.model_copy(
        update={"public_evidence_digest": None, "integrity_digest": "0" * 64}
    )
    missing_digest_ledger = unsigned_ledger.model_copy(
        update={
            "integrity_digest": run_warehouse_lifecycle_module._ledger_integrity(
                unsigned_ledger, config.signing_key
            )
        }
    )
    write_private_ledger(config, missing_digest_ledger)

    assert main(["validate-evidence"]) == 2
    error = capsys.readouterr().err
    assert "ERROR:" in error
    assert private_marker not in error
    assert str(private_parent) not in error

    ledger_path.write_bytes(original_ledger + b"corrupt")

    assert main(["validate-evidence"]) == 2
    error = capsys.readouterr().err
    assert "ERROR:" in error
    assert private_marker not in error
    assert str(private_parent) not in error


def test_clickhouse_acceptance_inventory_includes_both_restore_containers(
    private_parent: Path,
) -> None:
    config = _config(private_parent)

    resources = lifecycle_orchestration_module._docker_resource_entries(
        config=config,
        engine_kind="clickhouse",
        primary_container="pm-clickhouse-primary",
        primary_networks=("pm-clickhouse-primary-private", "pm-clickhouse-primary-loopback"),
        primary_volume="pm-clickhouse-primary-data",
        restore_containers=(
            "pm-clickhouse-restore-database",
            "pm-clickhouse-restore-isolation-probe",
        ),
        restore_networks=("pm-clickhouse-restore-private", "pm-clickhouse-restore-loopback"),
        restore_volume="pm-clickhouse-restore-data",
    )

    container_identifiers = {
        resource.exact_identifier
        for resource in resources
        if resource.compose_resource_kind == "container"
    }
    assert container_identifiers == {
        "pm-clickhouse-primary",
        "pm-clickhouse-restore-database",
        "pm-clickhouse-restore-isolation-probe",
    }


def test_pre_driver_directory_registration_recovers_constructor_artifacts(
    private_parent: Path,
) -> None:
    config = _config(private_parent)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = WarehouseLifecyclePrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)
    engine_root = config.state_path / "postgresql-witness"
    resource = PrivateDirectoryResourceEntry.create(
        engine_kind="postgresql",
        exact_path=engine_root,
        retention_deadline=now,
    )
    register_private_resources(config, (resource,))
    engine_root.mkdir(mode=0o700)
    private_secret = engine_root / "operation-secrets" / "secret.fernet"
    private_secret.parent.mkdir(mode=0o700, parents=True)
    private_secret.write_text("encrypted constructor artifact")

    cleanup = teardown_warehouse_lifecycle(config, run_id=ledger.run_id, now=now, authorized=True)

    assert not engine_root.exists()
    assert cleanup.completed_resource_count == 1
    assert cleanup.zero_residual_resources is True
