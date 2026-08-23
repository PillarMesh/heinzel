from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_openmetadata import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
)
from pillarmesh_semantic_registry import SQLiteCatalogPublicationRepository

from tests.acceptance.run_plan2 import (
    Plan2Config,
    Plan2HarnessError,
    PrivateCleanupEntry,
    PrivateCleanupLedger,
    _cleanup_claims,
    _cleanup_evidence_staging,
    _cleanup_private_artifacts,
    _cleanup_scope_digest,
    _entry,
    _evidence_staging_identifier,
    _failure_message,
    _load_claimed_publication_intent,
    _load_private_ledger,
    _operation_context,
    _prepare_evidence,
    _private_artifact_entries,
    _requires_operation_secret,
    _runtime_read_target,
    _source_identity,
    _verified_readback_digest,
    _write_private_ledger,
)


class _RecordingResourceDiscovery:
    def __init__(self, *residual_projects: str) -> None:
        self.residual_projects = frozenset(residual_projects)
        self.projects: list[str] = []

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[object, ...]:
        del environment
        self.projects.append(project_name)
        return (object(),) if project_name in self.residual_projects else ()


def test_witnessed_failure_message_reports_only_the_closed_phase_and_provider_error() -> None:
    provider_error = CatalogProviderError(
        "OpenMetadata isolated restore health failed",
        classification="transient",
    )

    assert _failure_message(
        phase="isolated_restore",
        error=provider_error,
        cleanup_incomplete=False,
    ) == (
        "Plan 2 witnessed lifecycle failed during isolated_restore: "
        "OpenMetadata isolated restore health failed"
    )
    assert (
        _failure_message(
            phase="isolated_restore",
            error=RuntimeError("private provider payload"),
            cleanup_incomplete=False,
        )
        == "Plan 2 witnessed lifecycle failed during isolated_restore"
    )


def test_runtime_read_target_uses_the_exact_typed_publication_reference() -> None:
    namespace_payload = {"name": "tenant-a", "namespace": "tenant-a"}
    term_payload = {
        "name": "Customer",
        "definition": "A governed customer.",
        "owner_ref": "runtime",
        "provenance_ref": "validation",
    }
    references = (
        CatalogObjectRef(
            tenant_key="tenant-a",
            stable_identity="namespace:tenant-a:namespace",
            normalized_digest="a" * 64,
        ),
        CatalogObjectRef(
            tenant_key="tenant-a",
            stable_identity="glossary_term:tenant-a:pillarmesh:customer",
            normalized_digest="b" * 64,
        ),
    )
    observations = (
        CatalogObjectSnapshot(
            tenant_key="tenant-a",
            stable_identity=references[0].stable_identity,
            logical_identity="namespace",
            object_kind="namespace",
            normalized_payload=namespace_payload,
            normalized_digest=digest(namespace_payload),
        ),
        CatalogObjectSnapshot(
            tenant_key="tenant-a",
            stable_identity=references[1].stable_identity,
            logical_identity="pillarmesh:customer",
            object_kind="glossary_term",
            normalized_payload=term_payload,
            normalized_digest=digest(term_payload),
        ),
    )

    reference, observation = _runtime_read_target(
        references=references,
        observations=observations,
        logical_identity="pillarmesh:customer",
    )

    assert reference == references[1]
    assert observation == observations[1]
    with pytest.raises(Plan2HarnessError, match="exactly one glossary term"):
        _runtime_read_target(
            references=references,
            observations=observations,
            logical_identity="missing",
        )


@pytest.fixture
def plan2_config(tmp_path: Path) -> Plan2Config:
    private_parent = tmp_path / "private"
    state_directory = private_parent / "state"
    state_directory.mkdir(mode=0o700, parents=True)
    return Plan2Config(
        repository_root=tmp_path / "repository",
        state_path=state_directory / "plan2.sqlite",
        output_dir=private_parent / "evidence",
        cleanup_ledger_path=private_parent / "private-ledger.json",
        secret_store_dir=private_parent / "secrets",
        backup_path=private_parent / "backup.sql",
        docker_config=tmp_path / "docker",
        operator_pseudonym="operator-1",
        host_pseudonym="host-1",
        environment={"PILLARMESH_OPENMETADATA_SECRET_STORE_KEY": "test-only-ledger-key"},
    )


def _ledger(plan2_config: Plan2Config, *entries: PrivateCleanupEntry) -> PrivateCleanupLedger:
    return PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="cleanup_pending",
        resources=tuple(entries),
    )


def test_cleanup_ledger_rejects_an_altered_exact_identifier(
    plan2_config: Plan2Config,
) -> None:
    ledger = _ledger(plan2_config, _entry("backup_file", str(plan2_config.backup_path)))
    _write_private_ledger(plan2_config, ledger)
    payload = json.loads(plan2_config.cleanup_ledger_path.read_bytes())
    payload["resources"][0]["exact_identifier"] = str(
        plan2_config.backup_path.with_name("foreign-backup.sql")
    )
    plan2_config.cleanup_ledger_path.write_text(json.dumps(payload))

    with pytest.raises(Plan2HarnessError, match=r"ledger .* invalid"):
        _load_private_ledger(plan2_config)


def test_cleanup_ledger_rejects_a_scope_from_another_run(
    plan2_config: Plan2Config,
) -> None:
    ledger = _ledger(plan2_config, _entry("backup_file", str(plan2_config.backup_path)))
    _write_private_ledger(plan2_config, ledger)
    foreign_config = replace(
        plan2_config,
        state_path=plan2_config.state_path.with_name("foreign.sqlite"),
    )

    with pytest.raises(Plan2HarnessError, match="scope"):
        _load_private_ledger(foreign_config)


def test_cleanup_ledger_rejects_a_mismatched_resource_digest(
    plan2_config: Plan2Config,
) -> None:
    entry = _entry("backup_file", str(plan2_config.backup_path)).model_copy(
        update={"resource_digest": "b" * 64}
    )

    with pytest.raises(Plan2HarnessError, match="resource digest"):
        _write_private_ledger(plan2_config, _ledger(plan2_config, entry))


def test_cleanup_ledger_rejects_a_foreign_restore_project(
    plan2_config: Plan2Config,
) -> None:
    ledger = _ledger(plan2_config, _entry("restore_project", "foreign-project"))

    with pytest.raises(Plan2HarnessError, match="restore project"):
        _write_private_ledger(plan2_config, ledger)


def test_cleanup_ledger_rejects_a_foreign_backup_path(
    plan2_config: Plan2Config,
) -> None:
    ledger = _ledger(
        plan2_config,
        _entry("backup_file", str(plan2_config.backup_path.with_name("foreign.sql"))),
    )

    with pytest.raises(Plan2HarnessError, match="backup target"):
        _write_private_ledger(plan2_config, ledger)


def test_post_teardown_recovery_removes_only_registered_staged_evidence(
    plan2_config: Plan2Config,
) -> None:
    evidence_entry = _entry("evidence_staging", _evidence_staging_identifier(plan2_config))
    ledger = _ledger(plan2_config, evidence_entry).model_copy(
        update={"run_state": "cleanup_complete"}
    )
    _prepare_evidence(plan2_config, b"validated evidence\n")

    cleaned = _cleanup_evidence_staging(plan2_config, ledger)

    assert not plan2_config.output_dir.exists()
    assert not plan2_config.state_path.exists()
    assert cleaned.resources[0].cleanup_state == "complete"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def test_source_identity_rejects_a_dirty_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "plan2@example.invalid")
    _git(repository, "config", "user.name", "Plan 2 Test")
    source = repository / "source.txt"
    source.write_text("committed\n")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-m", "test: create source")
    source.write_text("dirty\n")

    with pytest.raises(Plan2HarnessError, match="source checkout is not clean"):
        _source_identity(repository)


def test_fresh_readback_must_match_the_committed_receipt_digest() -> None:
    observations = ({"identity": "invoice"},)

    with pytest.raises(Plan2HarnessError, match="fresh publication readback differs from receipt"):
        _verified_readback_digest(observations, expected_digest="a" * 64)


def test_empty_claimed_publication_ledger_proves_no_provider_effect(tmp_path: Path) -> None:
    repository_path = tmp_path / "publication.sqlite"
    repository = SQLiteCatalogPublicationRepository(str(repository_path))
    repository.close()

    intent = _load_claimed_publication_intent(
        repository_path=repository_path,
        tenant_id="tenant-a",
        operation_id="a" * 64,
    )

    assert intent is None


def test_missing_claimed_publication_ledger_is_indeterminate(tmp_path: Path) -> None:
    with pytest.raises(Plan2HarnessError, match="publication recovery state is unavailable"):
        _load_claimed_publication_intent(
            repository_path=tmp_path / "missing.sqlite",
            tenant_id="tenant-a",
            operation_id="a" * 64,
        )


def test_private_artifact_inventory_covers_every_runner_database_and_replay_directory(
    plan2_config: Plan2Config,
) -> None:
    entries = _private_artifact_entries(plan2_config)

    file_names = {
        Path(entry.exact_identifier).name
        for entry in entries
        if entry.resource_kind == "private_file"
    }
    directory_names = {
        Path(entry.exact_identifier).name
        for entry in entries
        if entry.resource_kind == "private_directory"
    }

    assert {
        "plan2.sqlite",
        "plan2-journey.sqlite",
        "plan2-journey-semantic.sqlite",
        "plan2-journey-semantic-versions.sqlite",
        "plan2-journey-requests.sqlite",
        "plan2-journey-catalog.sqlite",
        "plan2-journey-publications.sqlite",
        "plan2-journey-source-observations.sqlite",
        "plan2-live-semantic.sqlite",
        "plan2-live-versions.sqlite",
        "plan2-live-requests.sqlite",
        "plan2-live-publication.sqlite",
    } <= file_names
    assert "plan2-journey-replays" in directory_names


def test_cleanup_claims_require_observed_private_artifact_absence(
    plan2_config: Plan2Config,
) -> None:
    private_entry = _private_artifact_entries(plan2_config)[0]
    private_path = Path(private_entry.exact_identifier)
    private_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_path.write_text("residual")
    ledger = PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="complete",
        resources=(private_entry.model_copy(update={"cleanup_state": "complete"}),),
    )

    exact_cleanup_verified, zero_residual_resources = _cleanup_claims(plan2_config, ledger)

    assert exact_cleanup_verified is True
    assert zero_residual_resources is False


def test_cleanup_claims_require_fresh_source_and_restore_project_absence(
    plan2_config: Plan2Config,
) -> None:
    source_project = "plan2-source-project"
    restore_project = "plan2-restore-project"
    discovery = _RecordingResourceDiscovery(source_project)
    entries = (
        _entry(
            "catalog_operation",
            _operation_context(
                tenant_id="tenant-1",
                binding_id="binding-1",
                operation_id="operation-1",
                private_resource_handle="handle-1",
                project_name=source_project,
            ),
        ).model_copy(update={"cleanup_state": "complete"}),
        _entry("restore_project", restore_project).model_copy(update={"cleanup_state": "complete"}),
    )
    ledger = PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="complete",
        resources=entries,
    )

    exact_cleanup_verified, zero_residual_resources = _cleanup_claims(
        plan2_config, ledger, compose=discovery
    )

    assert exact_cleanup_verified is True
    assert zero_residual_resources is False
    assert discovery.projects == [source_project, restore_project]


def test_post_retirement_cleanup_does_not_require_the_deleted_operation_secret(
    plan2_config: Plan2Config,
) -> None:
    completed_operation = _entry(
        "catalog_operation",
        _operation_context(
            tenant_id="tenant-1",
            binding_id="binding-1",
            operation_id="operation-1",
            private_resource_handle="handle-1",
            project_name="source-project",
        ),
    ).model_copy(update={"cleanup_state": "complete"})
    pending_backup = _entry("backup_file", str(plan2_config.backup_path))
    ledger = _ledger(plan2_config, completed_operation, pending_backup)

    assert _requires_operation_secret(ledger) is False
    assert (
        _requires_operation_secret(
            ledger.model_copy(
                update={
                    "resources": (
                        completed_operation.model_copy(update={"cleanup_state": "recorded"}),
                        pending_backup,
                    )
                }
            )
        )
        is True
    )


def test_private_artifact_cleanup_removes_only_exact_inventory(
    plan2_config: Plan2Config,
) -> None:
    entries = _private_artifact_entries(plan2_config)
    database_entry = next(entry for entry in entries if entry.resource_kind == "private_file")
    directory_entry = next(entry for entry in entries if entry.resource_kind == "private_directory")
    database_path = Path(database_entry.exact_identifier)
    replay_directory = Path(directory_entry.exact_identifier)
    database_path.write_text("database")
    replay_directory.mkdir(mode=0o700)
    (replay_directory / "record.json").write_text("{}")
    ledger = PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="cleanup_pending",
        resources=(database_entry, directory_entry),
    )

    cleaned = _cleanup_private_artifacts(plan2_config, ledger)

    assert not database_path.exists()
    assert not replay_directory.exists()
    assert all(resource.cleanup_state == "complete" for resource in cleaned.resources)


def test_private_artifact_cleanup_rejects_an_unrecorded_target(
    plan2_config: Plan2Config,
    tmp_path: Path,
) -> None:
    unrecorded = tmp_path / "must-remain.sqlite"
    unrecorded.write_text("customer data")
    ledger = PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="cleanup_pending",
        resources=(_entry("private_file", str(unrecorded)),),
    )

    with pytest.raises(Plan2HarnessError, match="not in the private artifact inventory"):
        _cleanup_private_artifacts(plan2_config, ledger)

    assert unrecorded.read_text() == "customer data"


def test_private_artifact_cleanup_rejects_a_swapped_parent_symlink(
    plan2_config: Plan2Config,
    tmp_path: Path,
) -> None:
    database_entry = next(
        entry
        for entry in _private_artifact_entries(plan2_config)
        if Path(entry.exact_identifier) == plan2_config.state_path
    )
    external_directory = tmp_path / "external"
    external_directory.mkdir()
    external_database = external_directory / plan2_config.state_path.name
    external_database.write_text("must remain")
    plan2_config.state_path.parent.rmdir()
    plan2_config.state_path.parent.symlink_to(external_directory, target_is_directory=True)
    ledger = PrivateCleanupLedger(
        run_digest="a" * 64,
        scope_digest=_cleanup_scope_digest(plan2_config),
        integrity_digest="0" * 64,
        run_state="cleanup_pending",
        resources=(database_entry,),
    )

    with pytest.raises(Plan2HarnessError, match="symlinked parent"):
        _cleanup_private_artifacts(plan2_config, ledger)

    assert external_database.read_text() == "must remain"
