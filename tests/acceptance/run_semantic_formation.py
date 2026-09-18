from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from heinzel_catalog_control import (
    CatalogBindingState,
    CatalogControlService,
    SQLiteCatalogRepository,
)
from heinzel_contract_model import digest
from heinzel_provider_openmetadata import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    DockerComposeController,
    EncryptedDirectoryOpenMetadataSecretStore,
    GlossaryTermPayload,
    OpenMetadataProvisioner,
    OpenMetadataPublicationProvider,
)
from heinzel_request_management import RequestManagementService, SQLiteRequestRepository
from heinzel_semantic_registry import (
    CatalogPublicationIntent,
    SemanticPublicationService,
    SQLiteCatalogPublicationRepository,
    SQLiteSemanticRepository,
    SQLiteSemanticVersionRepository,
)
from heinzel_semantic_registry.publication import publication_intent
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from tests.acceptance.semantic_formation_orchestration import (
    OfflineSemanticFormationHarness,
    legacy_refund_attribute_catalog,
    refund_entity_package,
    revenue_to_cash_package,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
EMULATOR_ROOT: Final = REPOSITORY_ROOT / "tests" / "emulators" / "openmetadata"
EVIDENCE_FILE_NAME: Final = "semantic-formation-evidence.json"

_PATH_VARIABLES: Final = (
    "HEINZEL_SEMANTIC_FORMATION_STATE_PATH",
    "HEINZEL_SEMANTIC_FORMATION_OUTPUT_DIR",
    "HEINZEL_SEMANTIC_FORMATION_CLEANUP_LEDGER_PATH",
    "HEINZEL_SEMANTIC_FORMATION_SECRET_STORE_DIR",
    "HEINZEL_SEMANTIC_FORMATION_BACKUP_PATH",
    "DOCKER_CONFIG",
)
_SECRET_VARIABLES: Final = (
    "HEINZEL_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD",
    "HEINZEL_OPENMETADATA_SECRET_STORE_KEY",
    "HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD",
    "HEINZEL_OPENMETADATA_DATABASE_PASSWORD",
    "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD",
)
_PSEUDONYM_VARIABLES: Final = (
    "HEINZEL_SEMANTIC_FORMATION_OPERATOR_PSEUDONYM",
    "HEINZEL_SEMANTIC_FORMATION_HOST_PSEUDONYM",
)
REQUIRED_VARIABLES: Final = _PATH_VARIABLES + _SECRET_VARIABLES + _PSEUDONYM_VARIABLES

_DIGEST_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_PSEUDONYM_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORBIDDEN_KEY_FRAGMENT: Final = re.compile(
    r"password|token|secret|authorization|endpoint|provider.?ref|provider.?id|"
    r"path|url|email|docker|volume|network|narrative|markdown|manifest|sql",
    re.IGNORECASE,
)
_FORBIDDEN_VALUE_FRAGMENT: Final = re.compile(
    r"(?:https?://|/Users/|/home/|/tmp/|\\\\|@open-metadata\.|"
    r"docker\.getcollate\.io|docker\.elastic\.co)",
    re.IGNORECASE,
)


class SemanticFormationHarnessError(RuntimeError):
    pass


type WitnessedPhase = Literal[
    "runtime_initialization",
    "catalog_provisioning",
    "catalog_validation",
    "semantic_formation",
    "catalog_publication",
    "independent_readback",
    "access_validation",
    "drift_observation",
    "catalog_backup",
    "isolated_restore",
    "search_rebuild",
    "runtime_credential_rotation",
    "tenant_admin_credential_rotation",
    "denial_admin_credential_rotation",
    "platform_admin_credential_rotation",
    "credential_validation",
    "restore_validation",
    "source_restart",
    "evidence_preparation",
    "exact_cleanup",
    "evidence_finalization",
]


def _failure_message(
    *,
    phase: WitnessedPhase,
    error: BaseException,
    cleanup_incomplete: bool,
) -> str:
    if isinstance(error, CatalogProviderError):
        message = f"Semantic formation witnessed lifecycle failed during {phase}: {error}"
    else:
        message = f"Semantic formation witnessed lifecycle failed during {phase}"
    if cleanup_incomplete:
        return f"{message}; exact cleanup remains incomplete"
    return message


def _runtime_read_target(
    *,
    references: Sequence[CatalogObjectRef],
    observations: Sequence[CatalogObjectSnapshot],
    logical_identity: str,
) -> tuple[CatalogObjectRef, CatalogObjectSnapshot]:
    try:
        matches = tuple(
            (reference, observation)
            for reference, observation in zip(references, observations, strict=True)
            if observation.object_kind == "glossary_term"
            and observation.logical_identity == logical_identity
        )
    except ValueError:
        raise SemanticFormationHarnessError(
            "publication references and observations differ in length"
        ) from None
    if len(matches) != 1:
        raise SemanticFormationHarnessError(
            "runtime read requires exactly one glossary term reference"
        )
    return matches[0]


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _inside(path: Path, parent: Path) -> bool:
    try:
        _absolute_lexical(path).relative_to(_absolute_lexical(parent))
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    current = _absolute_lexical(path)
    while True:
        if (current.exists() or current.is_symlink()) and current.is_symlink():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _require_private_parent(path: Path) -> None:
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise SemanticFormationHarnessError("private parent directory does not exist") from None
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise SemanticFormationHarnessError("private parent directory must be owner-only mode 0700")


def _validate_pseudonym(value: str, variable_name: str) -> str:
    if _PSEUDONYM_PATTERN.fullmatch(value) is None:
        raise SemanticFormationHarnessError(f"invalid pseudonym variable: {variable_name}")
    return value


@dataclass(frozen=True, slots=True)
class SemanticFormationConfig:
    repository_root: Path
    state_path: Path
    output_dir: Path
    cleanup_ledger_path: Path
    secret_store_dir: Path
    backup_path: Path
    docker_config: Path
    operator_pseudonym: str
    host_pseudonym: str
    environment: Mapping[str, str]

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        repository_root: Path = REPOSITORY_ROOT,
        allow_existing: bool = False,
    ) -> SemanticFormationConfig:
        missing = tuple(name for name in REQUIRED_VARIABLES if not environment.get(name))
        if missing:
            raise SemanticFormationHarnessError("missing required variables: " + ", ".join(missing))

        raw_paths = {name: Path(environment[name]) for name in _PATH_VARIABLES}
        paths = {name: _absolute_lexical(path) for name, path in raw_paths.items()}
        invalid: set[str] = set()
        for name, raw_path in raw_paths.items():
            path = paths[name]
            if not raw_path.expanduser().is_absolute() or _inside(path, repository_root):
                invalid.add(name)
            if _has_symlink_component(path):
                invalid.add(name)

        run_target_names = _PATH_VARIABLES[:-1]
        run_targets = tuple(paths[name] for name in run_target_names)
        if len(set(run_targets)) != len(run_targets):
            invalid.update(run_target_names)
        if not allow_existing:
            invalid.update(
                name
                for name in run_target_names
                if paths[name].exists() or paths[name].is_symlink()
            )

        docker_config = paths["DOCKER_CONFIG"]
        if not docker_config.is_dir():
            invalid.add("DOCKER_CONFIG")
        if invalid:
            raise SemanticFormationHarnessError(
                "invalid or reused private paths: " + ", ".join(sorted(invalid))
            )

        for target_name in run_target_names:
            _require_private_parent(paths[target_name].parent)

        return cls(
            repository_root=_absolute_lexical(repository_root),
            state_path=paths["HEINZEL_SEMANTIC_FORMATION_STATE_PATH"],
            output_dir=paths["HEINZEL_SEMANTIC_FORMATION_OUTPUT_DIR"],
            cleanup_ledger_path=paths["HEINZEL_SEMANTIC_FORMATION_CLEANUP_LEDGER_PATH"],
            secret_store_dir=paths["HEINZEL_SEMANTIC_FORMATION_SECRET_STORE_DIR"],
            backup_path=paths["HEINZEL_SEMANTIC_FORMATION_BACKUP_PATH"],
            docker_config=docker_config,
            operator_pseudonym=_validate_pseudonym(
                environment["HEINZEL_SEMANTIC_FORMATION_OPERATOR_PSEUDONYM"],
                "HEINZEL_SEMANTIC_FORMATION_OPERATOR_PSEUDONYM",
            ),
            host_pseudonym=_validate_pseudonym(
                environment["HEINZEL_SEMANTIC_FORMATION_HOST_PSEUDONYM"],
                "HEINZEL_SEMANTIC_FORMATION_HOST_PSEUDONYM",
            ),
            environment=dict(environment),
        )


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    revision: str
    tree_digest: str


class _Closable(Protocol):
    def close(self) -> None: ...


class _ResourceDiscovery(Protocol):
    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[object, ...]: ...


type CleanupState = Literal["recorded", "complete", "failed", "unknown"]


class PrivateCleanupEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_kind: str = Field(min_length=1)
    exact_identifier: str = Field(min_length=1)
    cleanup_state: CleanupState = "recorded"


class PrivateCleanupLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    run_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    integrity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_state: Literal["running", "failed", "cleanup_pending", "cleanup_complete", "complete"]
    resources: tuple[PrivateCleanupEntry, ...]


class TenantEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_pseudonym: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_resolution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    execution_occurred: bool
    publication_occurred: bool


class SemanticFormationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    evidence_package_version: Literal["semantic-formation-witness-v1"] = (
        "semantic-formation-witness-v1"
    )
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str = Field(min_length=1)
    openmetadata_version: Literal["1.13.3"] = "1.13.3"
    operator_pseudonym: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    host_pseudonym: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    successful: TenantEvidence
    no_valid_plan: TenantEvidence
    review_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_semantic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    drift_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_probe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    denial_probe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_image_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fresh_readback_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    isolated_restore_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    representative_query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    round_trip_verified: Literal[True]
    auto_applied: Literal[False]
    tenant_isolation_verified: Literal[True]
    exact_cleanup_verified: Literal[True]
    zero_residual_resources: Literal[True]


def canonical_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _cleanup_scope_digest(config: SemanticFormationConfig) -> str:
    return digest(
        {
            "domain": "heinzel-semantic-formation-cleanup-scope-v1",
            "repository_root": str(config.repository_root),
            "state_path": str(config.state_path),
            "output_dir": str(config.output_dir),
            "cleanup_ledger_path": str(config.cleanup_ledger_path),
            "secret_store_dir": str(config.secret_store_dir),
            "backup_path": str(config.backup_path),
            "operator_pseudonym": config.operator_pseudonym,
            "host_pseudonym": config.host_pseudonym,
        }
    )


def _resource_entry_digest(resource_kind: str, exact_identifier: str) -> str:
    return digest(
        {
            "domain": "heinzel-semantic-formation-cleanup-resource-v1",
            "resource_kind": resource_kind,
            "exact_identifier": exact_identifier,
        }
    )


def _restore_project_name(run_digest: str) -> str:
    return "pm2r-" + run_digest[:16]


def _evidence_staging_identifier(config: SemanticFormationConfig) -> str:
    final_path = config.output_dir / EVIDENCE_FILE_NAME
    return json.dumps(
        {
            "final_path": str(final_path),
            "output_directory": str(config.output_dir),
            "temporary_path": str(final_path.with_name(final_path.name + ".temporary")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_ledger_entries(ledger: PrivateCleanupLedger) -> None:
    observed_digests: set[str] = set()
    for resource in ledger.resources:
        expected_digest = _resource_entry_digest(resource.resource_kind, resource.exact_identifier)
        if resource.resource_digest != expected_digest:
            raise SemanticFormationHarnessError("cleanup ledger resource digest is invalid")
        if resource.resource_digest in observed_digests:
            raise SemanticFormationHarnessError("cleanup ledger contains a duplicate resource")
        observed_digests.add(resource.resource_digest)


def _validate_ledger_ownership(
    config: SemanticFormationConfig, ledger: PrivateCleanupLedger
) -> None:
    expected_private_artifacts = {
        (entry.resource_kind, entry.exact_identifier) for entry in _private_artifact_entries(config)
    }
    singleton_counts: dict[str, int] = {}
    operation_claim: dict[str, str] | None = None
    operation_context: dict[str, str] | None = None
    allowed_kinds = {
        "backup_file",
        "catalog_operation",
        "catalog_operation_claim",
        "evidence_staging",
        "private_directory",
        "private_file",
        "publication_object",
        "publication_operation_claim",
        "restore_project",
        "secret_store",
    }
    for resource in ledger.resources:
        if resource.resource_kind not in allowed_kinds:
            raise SemanticFormationHarnessError("cleanup ledger resource kind is unsupported")
        if resource.resource_kind in {
            "backup_file",
            "catalog_operation",
            "catalog_operation_claim",
            "publication_operation_claim",
            "restore_project",
            "secret_store",
        }:
            singleton_counts[resource.resource_kind] = (
                singleton_counts.get(resource.resource_kind, 0) + 1
            )
        if resource.resource_kind == "backup_file" and resource.exact_identifier != str(
            config.backup_path
        ):
            raise SemanticFormationHarnessError(
                "cleanup ledger backup target is not owned by this run"
            )
        if resource.resource_kind == "secret_store" and resource.exact_identifier != str(
            config.secret_store_dir
        ):
            raise SemanticFormationHarnessError(
                "cleanup ledger secret target is not owned by this run"
            )
        if resource.resource_kind == "restore_project" and resource.exact_identifier != (
            _restore_project_name(ledger.run_digest)
        ):
            raise SemanticFormationHarnessError(
                "cleanup ledger restore project is not owned by this run"
            )
        if (
            resource.resource_kind in {"private_file", "private_directory"}
            and (
                resource.resource_kind,
                resource.exact_identifier,
            )
            not in expected_private_artifacts
        ):
            raise SemanticFormationHarnessError(
                "cleanup target is not in the private artifact inventory"
            )
        if resource.resource_kind == "catalog_operation_claim":
            operation_claim = _parse_exact_json(
                resource.exact_identifier,
                required=frozenset({"binding_id", "operation_id", "tenant_id"}),
            )
        if resource.resource_kind == "catalog_operation":
            operation_context = _parse_exact_json(
                resource.exact_identifier,
                required=frozenset(
                    {
                        "binding_id",
                        "operation_id",
                        "private_resource_handle",
                        "project_name",
                        "tenant_id",
                    }
                ),
            )
        if resource.resource_kind == "publication_operation_claim":
            publication_claim = _parse_exact_json(
                resource.exact_identifier,
                required=frozenset({"operation_id", "repository_path", "tenant_id"}),
            )
            expected_path = config.state_path.with_name(
                config.state_path.stem + "-live-publication.sqlite"
            )
            if publication_claim["repository_path"] != str(expected_path):
                raise SemanticFormationHarnessError(
                    "cleanup ledger publication state is not owned by this run"
                )
        if resource.resource_kind == "evidence_staging":
            evidence_paths = _parse_exact_json(
                resource.exact_identifier,
                required=frozenset({"final_path", "output_directory", "temporary_path"}),
            )
            if resource.exact_identifier != _evidence_staging_identifier(config) or any(
                _has_symlink_component(Path(path).parent) for path in evidence_paths.values()
            ):
                raise SemanticFormationHarnessError(
                    "cleanup ledger evidence staging is not owned by this run"
                )
    if any(count != 1 for count in singleton_counts.values()):
        raise SemanticFormationHarnessError("cleanup ledger contains duplicate singleton resources")
    if (
        operation_claim is not None
        and operation_context is not None
        and any(
            operation_claim[key] != operation_context[key]
            for key in ("binding_id", "operation_id", "tenant_id")
        )
    ):
        raise SemanticFormationHarnessError(
            "cleanup ledger operation context does not match its claim"
        )


def _ledger_integrity_digest(config: SemanticFormationConfig, ledger: PrivateCleanupLedger) -> str:
    key = config.environment.get("HEINZEL_OPENMETADATA_SECRET_STORE_KEY")
    if not key:
        raise SemanticFormationHarnessError("private cleanup integrity key is unavailable")
    payload = ledger.model_dump(mode="json", exclude={"integrity_digest"})
    return hmac.new(key.encode(), canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _verified_readback_digest(observations: tuple[object, ...], *, expected_digest: str) -> str:
    observed_digest = digest(observations)
    if observed_digest != expected_digest:
        raise SemanticFormationHarnessError("fresh publication readback differs from receipt")
    return observed_digest


def _walk_json(
    value: object, *, path: tuple[str, ...] = ()
) -> tuple[tuple[tuple[str, ...], object], ...]:
    entries: list[tuple[tuple[str, ...], object]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SemanticFormationHarnessError("evidence object keys must be strings")
            entries.extend(_walk_json(child, path=(*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            entries.extend(_walk_json(child, path=(*path, str(index))))
    else:
        entries.append((path, value))
    return tuple(entries)


def validate_sanitized_evidence(
    evidence: SemanticFormationEvidence,
    *,
    forbidden_values: Sequence[str] = (),
) -> bytes:
    encoded = canonical_json_bytes(evidence)
    decoded = json.loads(encoded)
    for path, value in _walk_json(decoded):
        if any(_FORBIDDEN_KEY_FRAGMENT.search(component) for component in path):
            raise SemanticFormationHarnessError("evidence contains a forbidden field")
        if isinstance(value, str) and _FORBIDDEN_VALUE_FRAGMENT.search(value):
            raise SemanticFormationHarnessError("evidence contains forbidden operational content")
    for forbidden_value in forbidden_values:
        if forbidden_value and forbidden_value.encode() in encoded:
            raise SemanticFormationHarnessError("evidence contains private run material")
    SemanticFormationEvidence.model_validate_json(encoded)
    return encoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.acceptance.run_semantic_formation",
        description="Run or clean up the opt-in witnessed semantic formation lifecycle.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "cleanup", "cleanup-status"),
    )
    return parser


def _availability_decision(*, tenant_id: str, binding_id: str) -> str:
    del tenant_id, binding_id
    return "no_supported_catalog"


def _entry(resource_kind: str, exact_identifier: str) -> PrivateCleanupEntry:
    if not exact_identifier or exact_identifier == "*":
        raise SemanticFormationHarnessError("cleanup resource requires an exact identifier")
    return PrivateCleanupEntry(
        resource_digest=_resource_entry_digest(resource_kind, exact_identifier),
        resource_kind=resource_kind,
        exact_identifier=exact_identifier,
    )


def _write_private_ledger(config: SemanticFormationConfig, ledger: PrivateCleanupLedger) -> None:
    _validate_ledger_entries(ledger)
    _validate_ledger_ownership(config, ledger)
    if ledger.scope_digest != _cleanup_scope_digest(config):
        raise SemanticFormationHarnessError("private cleanup ledger scope is invalid")
    ledger = ledger.model_copy(
        update={"integrity_digest": _ledger_integrity_digest(config, ledger)}
    )
    path = config.cleanup_ledger_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".temporary")
    temporary.write_bytes(canonical_json_bytes(ledger))
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _load_private_ledger(config: SemanticFormationConfig) -> PrivateCleanupLedger:
    try:
        ledger = PrivateCleanupLedger.model_validate_json(config.cleanup_ledger_path.read_bytes())
    except (OSError, ValueError):
        raise SemanticFormationHarnessError("private cleanup ledger is unavailable") from None
    _validate_ledger_entries(ledger)
    _validate_ledger_ownership(config, ledger)
    if ledger.scope_digest != _cleanup_scope_digest(config):
        raise SemanticFormationHarnessError("private cleanup ledger scope is invalid")
    expected_integrity = _ledger_integrity_digest(config, ledger)
    if not hmac.compare_digest(ledger.integrity_digest, expected_integrity):
        raise SemanticFormationHarnessError("private cleanup ledger integrity is invalid")
    return ledger


def _replace_entry(
    ledger: PrivateCleanupLedger,
    *,
    resource_digest: str,
    cleanup_state: CleanupState,
) -> PrivateCleanupLedger:
    found = False
    resources: list[PrivateCleanupEntry] = []
    for resource in ledger.resources:
        if resource.resource_digest == resource_digest:
            found = True
            resources.append(resource.model_copy(update={"cleanup_state": cleanup_state}))
        else:
            resources.append(resource)
    if not found:
        raise SemanticFormationHarnessError("cleanup ledger resource is unavailable")
    return ledger.model_copy(update={"resources": tuple(resources)})


def _append_entries(
    ledger: PrivateCleanupLedger, entries: tuple[PrivateCleanupEntry, ...]
) -> PrivateCleanupLedger:
    by_digest = {resource.resource_digest: resource for resource in ledger.resources}
    for resource in entries:
        existing = by_digest.get(resource.resource_digest)
        if existing is not None and existing != resource:
            raise SemanticFormationHarnessError("cleanup resource digest collision")
        by_digest.setdefault(resource.resource_digest, resource)
    return ledger.model_copy(update={"resources": tuple(by_digest.values())})


def _operation_context(
    *,
    tenant_id: str,
    binding_id: str,
    operation_id: str,
    private_resource_handle: str,
    project_name: str,
) -> str:
    return json.dumps(
        {
            "binding_id": binding_id,
            "operation_id": operation_id,
            "private_resource_handle": private_resource_handle,
            "project_name": project_name,
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _operation_claim(*, tenant_id: str, binding_id: str, operation_id: str) -> str:
    return json.dumps(
        {
            "binding_id": binding_id,
            "operation_id": operation_id,
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _publication_claim(*, tenant_id: str, operation_id: str, repository_path: Path) -> str:
    return json.dumps(
        {
            "operation_id": operation_id,
            "repository_path": str(repository_path),
            "tenant_id": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_exact_json(value: str, *, required: frozenset[str]) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except ValueError:
        raise SemanticFormationHarnessError("private cleanup identifier is invalid") from None
    if not isinstance(parsed, dict) or set(parsed) != required:
        raise SemanticFormationHarnessError("private cleanup identifier is invalid")
    if not all(isinstance(item, str) and item and item != "*" for item in parsed.values()):
        raise SemanticFormationHarnessError("private cleanup identifier is invalid")
    return parsed


def _load_claimed_publication_intent(
    *, repository_path: Path, tenant_id: str, operation_id: str
) -> CatalogPublicationIntent | None:
    if (
        _has_symlink_component(repository_path.parent)
        or repository_path.is_symlink()
        or not repository_path.is_file()
    ):
        raise SemanticFormationHarnessError("publication recovery state is unavailable")
    try:
        repository = SQLiteCatalogPublicationRepository(str(repository_path))
        try:
            return repository.load_intent(tenant_id=tenant_id, operation_id=operation_id)
        except KeyError:
            return None
        finally:
            repository.close()
    except SemanticFormationHarnessError:
        raise
    except Exception:
        raise SemanticFormationHarnessError("publication recovery state is unavailable") from None


def _new_runtime(
    config: SemanticFormationConfig,
) -> tuple[
    SQLiteCatalogRepository,
    CatalogControlService,
    DockerComposeController,
    EncryptedDirectoryOpenMetadataSecretStore,
    OpenMetadataProvisioner,
]:
    repository = SQLiteCatalogRepository(str(config.state_path))
    control = CatalogControlService(repository, clock=lambda: datetime_now())
    compose = DockerComposeController(
        compose_file=EMULATOR_ROOT / "compose.yaml",
        readiness_script=EMULATOR_ROOT / "wait_ready.py",
        base_url="http://127.0.0.1:8585",
    )
    secret_store = EncryptedDirectoryOpenMetadataSecretStore(
        directory=config.secret_store_dir,
        key=SecretStr(config.environment["HEINZEL_OPENMETADATA_SECRET_STORE_KEY"]),
        bootstrap_admin_password=SecretStr(
            config.environment["HEINZEL_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD"]
        ),
    )
    provisioner = OpenMetadataProvisioner(
        repository=repository,
        compose=compose,
        availability_decision=_availability_decision,
        secret_store=secret_store,
    )
    return repository, control, compose, secret_store, provisioner


def datetime_now() -> datetime:
    return datetime.now(UTC)


def _close_resources(resources: list[_Closable]) -> None:
    close_failed = False
    while resources:
        resource = resources.pop()
        try:
            resource.close()
        except Exception:
            close_failed = True
    if close_failed:
        raise SemanticFormationHarnessError("private state could not be closed for exact cleanup")


def _run_git(repository_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SemanticFormationHarnessError("source identity is unavailable")
    return result.stdout


def _source_identity(repository_root: Path) -> SourceIdentity:
    status = _run_git(repository_root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise SemanticFormationHarnessError("source checkout is not clean")
    revision = _run_git(repository_root, "rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise SemanticFormationHarnessError("source revision is unavailable")
    staged_tree = _run_git(repository_root, "ls-files", "--stage", "-z")
    return SourceIdentity(revision=revision, tree_digest=sha256_digest(staged_tree))


def _sqlite_artifact_paths(database_path: Path) -> tuple[Path, ...]:
    companion_suffixes = ("", "-journal", "-shm", "-wal")
    return tuple(Path(str(database_path) + suffix) for suffix in companion_suffixes)


def _private_artifact_entries(config: SemanticFormationConfig) -> tuple[PrivateCleanupEntry, ...]:
    journey_path = config.state_path.with_name(config.state_path.stem + "-journey.sqlite")
    database_paths = (
        config.state_path,
        journey_path,
        journey_path.with_name(journey_path.stem + "-semantic.sqlite"),
        journey_path.with_name(journey_path.stem + "-semantic-versions.sqlite"),
        journey_path.with_name(journey_path.stem + "-requests.sqlite"),
        journey_path.with_name(journey_path.stem + "-catalog.sqlite"),
        journey_path.with_name(journey_path.stem + "-publications.sqlite"),
        journey_path.with_name(journey_path.stem + "-source-observations.sqlite"),
        config.state_path.with_name(config.state_path.stem + "-live-semantic.sqlite"),
        config.state_path.with_name(config.state_path.stem + "-live-versions.sqlite"),
        config.state_path.with_name(config.state_path.stem + "-live-requests.sqlite"),
        config.state_path.with_name(config.state_path.stem + "-live-publication.sqlite"),
    )
    file_entries = tuple(
        _entry("private_file", str(path))
        for database_path in database_paths
        for path in _sqlite_artifact_paths(database_path)
    )
    replay_directory = journey_path.with_name(journey_path.stem + "-replays")
    return (*file_entries, _entry("private_directory", str(replay_directory)))


def _cleanup_claims(
    config: SemanticFormationConfig,
    ledger: PrivateCleanupLedger,
    *,
    compose: _ResourceDiscovery | None = None,
) -> tuple[bool, bool]:
    exact_cleanup_verified = ledger.run_state in {"cleanup_complete", "complete"} and all(
        resource.cleanup_state == "complete" or resource.resource_kind == "evidence_staging"
        for resource in ledger.resources
    )
    if not exact_cleanup_verified:
        return False, False
    private_artifact_identifiers = {
        entry.exact_identifier for entry in _private_artifact_entries(config)
    }
    no_private_artifacts_remain = all(
        not Path(resource.exact_identifier).exists()
        and not Path(resource.exact_identifier).is_symlink()
        for resource in ledger.resources
        if resource.exact_identifier in private_artifact_identifiers
    )
    external_resources_absent = _fresh_external_absence(config, ledger, compose=compose)
    return True, no_private_artifacts_remain and external_resources_absent


def _fresh_external_absence(
    config: SemanticFormationConfig,
    ledger: PrivateCleanupLedger,
    *,
    compose: _ResourceDiscovery | None = None,
) -> bool:
    project_names: list[str] = []
    paths_must_be_absent: list[Path] = []
    evidence_entry: PrivateCleanupEntry | None = None
    for resource in ledger.resources:
        if resource.resource_kind == "catalog_operation":
            context = _parse_exact_json(
                resource.exact_identifier,
                required=frozenset(
                    {
                        "binding_id",
                        "operation_id",
                        "private_resource_handle",
                        "project_name",
                        "tenant_id",
                    }
                ),
            )
            project_names.append(context["project_name"])
        elif resource.resource_kind == "restore_project":
            project_names.append(resource.exact_identifier)
        elif resource.resource_kind in {"backup_file", "secret_store"}:
            paths_must_be_absent.append(Path(resource.exact_identifier))
        elif resource.resource_kind == "evidence_staging":
            evidence_entry = resource

    residual_found = any(path.exists() or path.is_symlink() for path in paths_must_be_absent)
    if project_names:
        resource_discovery = compose or DockerComposeController(
            compose_file=EMULATOR_ROOT / "compose.yaml",
            readiness_script=EMULATOR_ROOT / "wait_ready.py",
            base_url="http://127.0.0.1:8585",
        )
        try:
            for project_name in project_names:
                if resource_discovery.discover_resources(
                    project_name=project_name, environment=config.environment
                ):
                    residual_found = True
        except Exception:
            raise SemanticFormationHarnessError(
                "fresh cleanup verification is unavailable"
            ) from None

    if evidence_entry is not None:
        paths = _parse_exact_json(
            evidence_entry.exact_identifier,
            required=frozenset({"final_path", "output_directory", "temporary_path"}),
        )
        output_directory = Path(paths["output_directory"])
        final_path = Path(paths["final_path"])
        temporary_path = Path(paths["temporary_path"])
        if ledger.run_state == "cleanup_complete":
            expected_children = {temporary_path}
            evidence_state_valid = (
                temporary_path.is_file()
                and not temporary_path.is_symlink()
                and not final_path.exists()
                and not final_path.is_symlink()
            )
        else:
            expected_children = {final_path}
            evidence_state_valid = (
                not temporary_path.exists()
                and not temporary_path.is_symlink()
                and (
                    not output_directory.exists()
                    or (final_path.is_file() and not final_path.is_symlink())
                )
            )
        if output_directory.exists() and (
            output_directory.is_symlink() or set(output_directory.iterdir()) != expected_children
        ):
            evidence_state_valid = False
        residual_found = residual_found or not evidence_state_valid

    return not residual_found


def _prepare_evidence(config: SemanticFormationConfig, encoded: bytes) -> None:
    paths = _parse_exact_json(
        _evidence_staging_identifier(config),
        required=frozenset({"final_path", "output_directory", "temporary_path"}),
    )
    output_directory = Path(paths["output_directory"])
    temporary_path = Path(paths["temporary_path"])
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary_path.write_bytes(encoded)
    temporary_path.chmod(0o600)
    if temporary_path.read_bytes() != encoded:
        raise SemanticFormationHarnessError("staged evidence differs from validated bytes")


def _publish_staged_evidence(config: SemanticFormationConfig, encoded: bytes) -> None:
    paths = _parse_exact_json(
        _evidence_staging_identifier(config),
        required=frozenset({"final_path", "output_directory", "temporary_path"}),
    )
    temporary_path = Path(paths["temporary_path"])
    final_path = Path(paths["final_path"])
    if temporary_path.read_bytes() != encoded or final_path.exists() or final_path.is_symlink():
        raise SemanticFormationHarnessError("staged evidence is unavailable for atomic publication")
    temporary_path.replace(final_path)
    final_path.chmod(0o600)
    if final_path.read_bytes() != encoded:
        raise SemanticFormationHarnessError("published evidence differs from validated bytes")


def _cleanup_evidence_staging(
    config: SemanticFormationConfig, ledger: PrivateCleanupLedger
) -> PrivateCleanupLedger:
    entry = next(
        (resource for resource in ledger.resources if resource.resource_kind == "evidence_staging"),
        None,
    )
    if entry is None or entry.cleanup_state == "complete":
        return ledger
    paths = _parse_exact_json(
        entry.exact_identifier,
        required=frozenset({"final_path", "output_directory", "temporary_path"}),
    )
    output_directory = Path(paths["output_directory"])
    allowed_children = {Path(paths["temporary_path"]), Path(paths["final_path"])}
    if _has_symlink_component(output_directory.parent) or output_directory.is_symlink():
        raise SemanticFormationHarnessError("evidence staging cleanup has a symlinked parent")
    if output_directory.exists():
        children = set(output_directory.iterdir())
        if not children <= allowed_children:
            raise SemanticFormationHarnessError(
                "evidence directory contains an unrecorded resource"
            )
        for child in children:
            child_status = child.stat(follow_symlinks=False)
            if not stat.S_ISREG(child_status.st_mode) and not stat.S_ISLNK(child_status.st_mode):
                raise SemanticFormationHarnessError(
                    "evidence directory contains an unrecorded resource"
                )
            child.unlink()
        output_directory.rmdir()
    if output_directory.exists() or output_directory.is_symlink():
        raise SemanticFormationHarnessError("evidence staging cleanup is not terminal")
    return _replace_entry(ledger, resource_digest=entry.resource_digest, cleanup_state="complete")


def _cleanup_private_artifacts(
    config: SemanticFormationConfig, ledger: PrivateCleanupLedger
) -> PrivateCleanupLedger:
    expected = {
        (entry.resource_kind, entry.exact_identifier) for entry in _private_artifact_entries(config)
    }
    for resource in ledger.resources:
        if resource.cleanup_state == "complete" or resource.resource_kind not in {
            "private_file",
            "private_directory",
        }:
            continue
        if (resource.resource_kind, resource.exact_identifier) not in expected:
            raise SemanticFormationHarnessError(
                "cleanup target is not in the private artifact inventory"
            )
        path = Path(resource.exact_identifier)
        if _has_symlink_component(path.parent):
            raise SemanticFormationHarnessError("private artifact cleanup has a symlinked parent")
        if resource.resource_kind == "private_file":
            if path.exists() or path.is_symlink():
                path.unlink()
        elif path.is_symlink():
            path.unlink()
        elif path.exists():
            for child in path.iterdir():
                child_status = child.stat(follow_symlinks=False)
                if not stat.S_ISREG(child_status.st_mode) and not stat.S_ISLNK(
                    child_status.st_mode
                ):
                    raise SemanticFormationHarnessError(
                        "private artifact directory contains an unrecorded resource"
                    )
                child.unlink()
            path.rmdir()
        if path.exists() or path.is_symlink():
            raise SemanticFormationHarnessError("private artifact cleanup is not terminal")
        ledger = _replace_entry(
            ledger,
            resource_digest=resource.resource_digest,
            cleanup_state="complete",
        )
    return ledger


def _sanitized_cleanup_digest(ledger: PrivateCleanupLedger) -> str:
    return sha256_digest(
        canonical_json_bytes(
            {
                "run_state": ledger.run_state,
                "resources": sorted(
                    (
                        resource.resource_kind,
                        resource.cleanup_state,
                        resource.resource_digest,
                    )
                    for resource in ledger.resources
                ),
            }
        )
    )


def _requires_operation_secret(ledger: PrivateCleanupLedger) -> bool:
    provider_access_kinds = {
        "catalog_operation",
        "publication_object",
        "publication_operation_claim",
        "restore_project",
    }
    return any(
        resource.cleanup_state != "complete" and resource.resource_kind in provider_access_kinds
        for resource in ledger.resources
    )


def _cleanup_resources(
    config: SemanticFormationConfig,
    ledger: PrivateCleanupLedger,
    *,
    preserve_evidence_staging: bool = False,
) -> PrivateCleanupLedger:
    if ledger.run_state == "complete" and all(
        resource.cleanup_state == "complete" for resource in ledger.resources
    ):
        _, zero_residual_resources = _cleanup_claims(config, ledger)
        if not zero_residual_resources:
            raise SemanticFormationHarnessError("recorded cleanup is not externally terminal")
        return ledger
    if not preserve_evidence_staging:
        ledger = _cleanup_evidence_staging(config, ledger)
        _write_private_ledger(config, ledger)
    non_evidence_resources = tuple(
        resource for resource in ledger.resources if resource.resource_kind != "evidence_staging"
    )
    if non_evidence_resources and all(
        resource.cleanup_state == "complete" for resource in non_evidence_resources
    ):
        terminal_state = "cleanup_complete" if preserve_evidence_staging else "complete"
        ledger = ledger.model_copy(update={"run_state": terminal_state})
        _write_private_ledger(config, ledger)
        _, zero_residual_resources = _cleanup_claims(config, ledger)
        if not zero_residual_resources:
            raise SemanticFormationHarnessError("recorded cleanup is not externally terminal")
        return ledger
    operation_entry = next(
        (
            resource
            for resource in ledger.resources
            if resource.resource_kind == "catalog_operation"
        ),
        None,
    )
    claim_entry = next(
        (
            resource
            for resource in ledger.resources
            if resource.resource_kind == "catalog_operation_claim"
        ),
        None,
    )
    publication_claim_entry = next(
        (
            resource
            for resource in ledger.resources
            if resource.resource_kind == "publication_operation_claim"
        ),
        None,
    )
    if operation_entry is None and claim_entry is None:
        raise SemanticFormationHarnessError("cleanup ledger has no catalog operation")
    repository, control, compose, secret_store, provisioner = _new_runtime(config)
    if operation_entry is None:
        if claim_entry is None:
            raise SemanticFormationHarnessError("cleanup ledger has no catalog operation claim")
        claim = _parse_exact_json(
            claim_entry.exact_identifier,
            required=frozenset({"binding_id", "operation_id", "tenant_id"}),
        )
        try:
            recovered = repository.load_operation(
                claim["tenant_id"], claim["binding_id"], claim["operation_id"]
            )
        except KeyError:
            provisioner.retire_unrecorded_operation_claim(
                tenant_id=claim["tenant_id"],
                binding_id=claim["binding_id"],
                operation_id=claim["operation_id"],
            )
            for resource in ledger.resources:
                if resource.cleanup_state == "complete":
                    continue
                if resource.resource_kind in {"private_file", "private_directory"}:
                    continue
                if resource.resource_kind == "restore_project" and compose.discover_resources(
                    project_name=resource.exact_identifier, environment=config.environment
                ):
                    raise SemanticFormationHarnessError(
                        "unclaimed restore project is not absent"
                    ) from None
                if resource.resource_kind == "backup_file" and (
                    Path(resource.exact_identifier).exists()
                    or Path(resource.exact_identifier).is_symlink()
                ):
                    raise SemanticFormationHarnessError("unclaimed backup is not absent") from None
                if resource.resource_kind == "secret_store" and config.secret_store_dir.exists():
                    config.secret_store_dir.rmdir()
                ledger = _replace_entry(
                    ledger,
                    resource_digest=resource.resource_digest,
                    cleanup_state="complete",
                )
            repository.close()
            ledger = _cleanup_private_artifacts(config, ledger)
            if any(resource.cleanup_state != "complete" for resource in ledger.resources):
                raise SemanticFormationHarnessError("cleanup remains incomplete") from None
            ledger = ledger.model_copy(update={"run_state": "complete"})
            _write_private_ledger(config, ledger)
            _, zero_residual_resources = _cleanup_claims(config, ledger, compose=compose)
            if not zero_residual_resources:
                raise SemanticFormationHarnessError(
                    "recorded cleanup is not externally terminal"
                ) from None
            return ledger
        context = {
            "binding_id": recovered.binding_id,
            "operation_id": recovered.operation_id,
            "private_resource_handle": recovered.resource_handle,
            "project_name": recovered.project_name,
            "tenant_id": recovered.tenant_id,
        }
        operation_entry = _entry("catalog_operation", _operation_context(**context))
        ledger = _append_entries(ledger, (operation_entry,))
        _write_private_ledger(config, ledger)
    else:
        context = _parse_exact_json(
            operation_entry.exact_identifier,
            required=frozenset(
                {
                    "binding_id",
                    "operation_id",
                    "private_resource_handle",
                    "project_name",
                    "tenant_id",
                }
            ),
        )
    operation = repository.load_operation_by_handle(context["private_resource_handle"])
    if (
        operation.tenant_id != context["tenant_id"]
        or operation.binding_id != context["binding_id"]
        or operation.operation_id != context["operation_id"]
        or operation.resource_handle != context["private_resource_handle"]
        or operation.project_name != context["project_name"]
    ):
        raise SemanticFormationHarnessError("cleanup operation is not owned by the recorded claim")
    compose_environment: Mapping[str, str] = config.environment
    if _requires_operation_secret(ledger):
        secrets_bundle = secret_store.resolve(operation.secret_reference)
        compose_environment = dict(config.environment) | secrets_bundle.compose_environment()

    for resource in ledger.resources:
        if resource.cleanup_state == "complete" or resource.resource_kind != "restore_project":
            continue
        compose.down(project_name=resource.exact_identifier, environment=compose_environment)
        if compose.discover_resources(
            project_name=resource.exact_identifier, environment=compose_environment
        ):
            raise SemanticFormationHarnessError("restore project cleanup is not terminal")
        compose.start(project_name=operation.project_name, environment=compose_environment)
        provisioner.new_administrator_client(
            private_resource_handle=context["private_resource_handle"],
            operation_id=context["operation_id"],
        ).health()
        ledger = _replace_entry(
            ledger, resource_digest=resource.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)

    publication_object_entries = tuple(
        resource for resource in ledger.resources if resource.resource_kind == "publication_object"
    )
    if publication_object_entries and publication_claim_entry is None:
        raise SemanticFormationHarnessError("publication cleanup has no owning operation claim")
    if publication_claim_entry is not None:
        publication_claim = _parse_exact_json(
            publication_claim_entry.exact_identifier,
            required=frozenset({"operation_id", "repository_path", "tenant_id"}),
        )
        publication_intent_record = _load_claimed_publication_intent(
            repository_path=Path(publication_claim["repository_path"]),
            tenant_id=publication_claim["tenant_id"],
            operation_id=publication_claim["operation_id"],
        )
        if publication_intent_record is not None:
            publication_client = provisioner.new_administrator_client(
                private_resource_handle=context["private_resource_handle"],
                operation_id=context["operation_id"],
            )
            OpenMetadataPublicationProvider(publication_client).publish(
                tenant_id=publication_claim["tenant_id"],
                intent=publication_intent_record,
            )
            provisioned_provider_identifiers = frozenset(
                resource.provider_ref
                for resource in repository.load_resources(
                    context["tenant_id"], context["binding_id"]
                )
            )
            publication_entries = tuple(
                _entry(
                    "publication_object",
                    json.dumps(
                        {"collection": resource.collection, "identifier": resource.identifier},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for resource in publication_client.discovered_resources()
                if resource.identifier not in provisioned_provider_identifiers
            )
            if not publication_entries:
                raise SemanticFormationHarnessError(
                    "publication recovery found no exact provider resources"
                )
            expected_publication_targets = {
                resource.exact_identifier for resource in publication_entries
            }
            recorded_publication_targets = {
                resource.exact_identifier for resource in publication_object_entries
            }
            if recorded_publication_targets and (
                recorded_publication_targets != expected_publication_targets
            ):
                raise SemanticFormationHarnessError(
                    "publication cleanup resources do not match the persisted intent"
                )
            ledger = _append_entries(ledger, publication_entries)
        elif publication_object_entries:
            raise SemanticFormationHarnessError("publication cleanup intent is unavailable")
        if publication_claim_entry.cleanup_state != "complete":
            ledger = _replace_entry(
                ledger,
                resource_digest=publication_claim_entry.resource_digest,
                cleanup_state="complete",
            )
            _write_private_ledger(config, ledger)

    for resource in ledger.resources:
        if resource.cleanup_state == "complete" or resource.resource_kind != "publication_object":
            continue
        identifier = _parse_exact_json(
            resource.exact_identifier, required=frozenset({"collection", "identifier"})
        )
        client = provisioner.new_administrator_client(
            private_resource_handle=context["private_resource_handle"],
            operation_id=context["operation_id"],
        )
        client.delete_recorded_resource(
            collection=identifier["collection"], identifier=identifier["identifier"]
        )
        if not client.recorded_resource_is_absent(
            collection=identifier["collection"], identifier=identifier["identifier"]
        ):
            raise SemanticFormationHarnessError("publication resource cleanup is not terminal")
        ledger = _replace_entry(
            ledger, resource_digest=resource.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)

    if operation_entry.cleanup_state != "complete":
        binding = control.get(context["tenant_id"], context["binding_id"])
        if binding.lifecycle_state is CatalogBindingState.READY:
            binding = control.transition(
                binding.tenant_id,
                binding.binding_id,
                CatalogBindingState.RETIRING,
                expected_revision=binding.revision,
            )
        provisioner.retire(
            private_resource_handle=context["private_resource_handle"],
            operation_id=context["operation_id"],
        )
        if binding.lifecycle_state is CatalogBindingState.RETIRING:
            control.transition(
                binding.tenant_id,
                binding.binding_id,
                CatalogBindingState.RETIRED,
                expected_revision=binding.revision,
            )
        resources = repository.load_resources(context["tenant_id"], context["binding_id"])
        if not resources or any(resource.cleanup_status != "complete" for resource in resources):
            raise SemanticFormationHarnessError("catalog cleanup is not terminal")
        ledger = _replace_entry(
            ledger,
            resource_digest=operation_entry.resource_digest,
            cleanup_state="complete",
        )
        _write_private_ledger(config, ledger)

    for resource in ledger.resources:
        if resource.cleanup_state == "complete" or resource.resource_kind != "backup_file":
            continue
        backup_path = Path(resource.exact_identifier)
        if backup_path.exists() or backup_path.is_symlink():
            backup_path.unlink()
        if backup_path.exists() or backup_path.is_symlink():
            raise SemanticFormationHarnessError("backup cleanup is not terminal")
        ledger = _replace_entry(
            ledger, resource_digest=resource.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)

    for resource in ledger.resources:
        if resource.cleanup_state == "complete" or resource.resource_kind != "secret_store":
            continue
        if config.secret_store_dir.exists():
            try:
                config.secret_store_dir.rmdir()
            except OSError:
                raise SemanticFormationHarnessError("secret cleanup is not terminal") from None
        ledger = _replace_entry(
            ledger, resource_digest=resource.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)

    if claim_entry is not None and claim_entry.cleanup_state != "complete":
        ledger = _replace_entry(
            ledger, resource_digest=claim_entry.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)

    repository.close()
    ledger = _cleanup_private_artifacts(config, ledger)
    _write_private_ledger(config, ledger)

    if any(
        resource.cleanup_state != "complete"
        and not (preserve_evidence_staging and resource.resource_kind == "evidence_staging")
        for resource in ledger.resources
    ):
        raise SemanticFormationHarnessError("cleanup remains incomplete")
    ledger = ledger.model_copy(
        update={"run_state": "cleanup_complete" if preserve_evidence_staging else "complete"}
    )
    _write_private_ledger(config, ledger)
    _, zero_residual_resources = _cleanup_claims(config, ledger, compose=compose)
    if not zero_residual_resources:
        raise SemanticFormationHarnessError("recorded cleanup is not externally terminal")
    return ledger


def _run(config: SemanticFormationConfig) -> int:
    source_identity = _source_identity(config.repository_root)
    run_nonce = secrets.token_bytes(32)
    run_id = sha256_digest(run_nonce)
    successful_tenant_id = "semantic-formation-success-" + secrets.token_hex(12)
    no_valid_plan_tenant_id = "semantic-formation-nvp-" + secrets.token_hex(12)
    operation_id = "semantic-formation-live-" + secrets.token_hex(12)
    restore_project = _restore_project_name(run_id)
    private_resource_handle = "pending"
    initial_resources = (
        _entry("secret_store", str(config.secret_store_dir)),
        _entry("backup_file", str(config.backup_path)),
        _entry("restore_project", restore_project),
        _entry("evidence_staging", _evidence_staging_identifier(config)),
        *_private_artifact_entries(config),
    )
    ledger = PrivateCleanupLedger(
        run_digest=run_id,
        scope_digest=_cleanup_scope_digest(config),
        integrity_digest="0" * 64,
        run_state="running",
        resources=initial_resources,
    )
    _write_private_ledger(config, ledger)
    open_resources: list[_Closable] = []
    phase: WitnessedPhase = "runtime_initialization"

    try:
        repository, control, compose, secret_store, provisioner = _new_runtime(config)
        open_resources.append(repository)
        phase = "catalog_provisioning"
        draft = control.create_draft(tenant_id=successful_tenant_id)
        provisioning = control.transition(
            successful_tenant_id,
            draft.binding_id,
            CatalogBindingState.PROVISIONING,
            expected_revision=draft.revision,
        )
        claim_entry = _entry(
            "catalog_operation_claim",
            _operation_claim(
                tenant_id=successful_tenant_id,
                binding_id=provisioning.binding_id,
                operation_id=operation_id,
            ),
        )
        ledger = _append_entries(ledger, (claim_entry,))
        _write_private_ledger(config, ledger)
        private_resource_handle = provisioner.provision(
            tenant_id=successful_tenant_id,
            binding_id=provisioning.binding_id,
            operation_id=operation_id,
        )
        operation = repository.load_operation(
            successful_tenant_id,
            provisioning.binding_id,
            operation_id,
        )
        operation_entry = _entry(
            "catalog_operation",
            _operation_context(
                tenant_id=successful_tenant_id,
                binding_id=provisioning.binding_id,
                operation_id=operation_id,
                private_resource_handle=private_resource_handle,
                project_name=operation.project_name,
            ),
        )
        ledger = _append_entries(ledger, (operation_entry,))
        ledger = _replace_entry(
            ledger, resource_digest=claim_entry.resource_digest, cleanup_state="complete"
        )
        _write_private_ledger(config, ledger)
        phase = "catalog_validation"
        validating = control.transition(
            successful_tenant_id,
            provisioning.binding_id,
            CatalogBindingState.VALIDATING,
            expected_revision=provisioning.revision,
        )
        validation = provisioner.validate(
            tenant_id=successful_tenant_id,
            binding_id=validating.binding_id,
            private_resource_handle=private_resource_handle,
        )
        ready = control.record_validation(
            tenant_id=successful_tenant_id,
            binding_id=validating.binding_id,
            expected_revision=validating.revision,
            evidence=validation,
        )

        phase = "semantic_formation"
        offline_catalog = legacy_refund_attribute_catalog()
        offline = OfflineSemanticFormationHarness(
            database_path=config.state_path.with_name(config.state_path.stem + "-journey.sqlite"),
            catalog=offline_catalog,
        )
        open_resources.append(offline)
        successful = offline.run(
            tenant_id=successful_tenant_id, process_package=revenue_to_cash_package()
        )
        no_valid_plan = offline.run(
            tenant_id=no_valid_plan_tenant_id,
            process_package=refund_entity_package(),
            owner_decisions=(),
        )
        if successful.semantic_version is None or successful.contract is None:
            raise SemanticFormationHarnessError("successful semantic journey is incomplete")
        if successful.candidate_set is None or no_valid_plan.candidate_set is None:
            raise SemanticFormationHarnessError("candidate journey is incomplete")

        live_semantic_repository = SQLiteSemanticRepository(
            str(config.state_path.with_name(config.state_path.stem + "-live-semantic.sqlite"))
        )
        open_resources.append(live_semantic_repository)
        live_semantic_versions = SQLiteSemanticVersionRepository(
            str(config.state_path.with_name(config.state_path.stem + "-live-versions.sqlite"))
        )
        open_resources.append(live_semantic_versions)
        persisted_semantic = live_semantic_versions.store(successful.semantic_version)
        if persisted_semantic != successful.semantic_version:
            raise SemanticFormationHarnessError(
                "live semantic persistence changed approved identity"
            )
        live_request_repository = SQLiteRequestRepository.open(
            str(config.state_path.with_name(config.state_path.stem + "-live-requests.sqlite"))
        )
        open_resources.append(live_request_repository)
        live_requests = RequestManagementService(
            live_request_repository,
            clock=datetime_now,
        )
        publication_repository_path = config.state_path.with_name(
            config.state_path.stem + "-live-publication.sqlite"
        )
        publication_repository = SQLiteCatalogPublicationRepository(
            str(publication_repository_path)
        )
        open_resources.append(publication_repository)
        client = provisioner.new_administrator_client(
            private_resource_handle=private_resource_handle, operation_id=operation_id
        )
        provider = OpenMetadataPublicationProvider(client)
        publication_service = SemanticPublicationService(
            repository=publication_repository,
            provider=provider,
            semantic_repository=live_semantic_repository,
            semantic_version_repository=live_semantic_versions,
            request_service=live_requests,
            clock=datetime_now,
        )
        expected_publication_intent = publication_intent(
            binding=ready,
            semantic_version=persisted_semantic,
            contract=successful.contract,
        )
        publication_claim_entry = _entry(
            "publication_operation_claim",
            _publication_claim(
                tenant_id=successful_tenant_id,
                operation_id=expected_publication_intent.operation_id,
                repository_path=publication_repository_path,
            ),
        )
        ledger = _append_entries(ledger, (publication_claim_entry,))
        _write_private_ledger(config, ledger)
        phase = "catalog_publication"
        receipt = publication_service.publish(
            binding=ready,
            semantic_version=persisted_semantic,
            contract=successful.contract,
        )
        intent, _, references = publication_repository.load_publication(
            tenant_id=successful_tenant_id, publication_id=receipt.publication_id
        )
        if intent != expected_publication_intent:
            raise SemanticFormationHarnessError(
                "persisted publication intent differs from the planned effect"
            )
        successful_publication_occurred = (
            publication_repository.effect_count(tenant_id=successful_tenant_id) == 2
        )
        no_valid_plan_publication_occurred = (
            publication_repository.effect_count(tenant_id=no_valid_plan_tenant_id) != 0
        )
        if not successful_publication_occurred or no_valid_plan_publication_occurred:
            raise SemanticFormationHarnessError(
                "publication effect journal differs from the journey result"
            )
        provisioned_provider_identifiers = frozenset(
            resource.provider_ref
            for resource in repository.load_resources(
                successful_tenant_id,
                provisioning.binding_id,
            )
        )
        created_resources = tuple(
            resource
            for resource in client.discovered_resources()
            if resource.identifier not in provisioned_provider_identifiers
        )
        if not created_resources:
            raise SemanticFormationHarnessError("publication produced no exact cleanup resources")
        ledger = _append_entries(
            ledger,
            tuple(
                _entry(
                    "publication_object",
                    json.dumps(
                        {"collection": resource.collection, "identifier": resource.identifier},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for resource in created_resources
            ),
        )
        ledger = _replace_entry(
            ledger,
            resource_digest=publication_claim_entry.resource_digest,
            cleanup_state="complete",
        )
        _write_private_ledger(config, ledger)

        phase = "independent_readback"
        fresh_client = provisioner.new_administrator_client(
            private_resource_handle=private_resource_handle, operation_id=operation_id
        )
        fresh_provider = OpenMetadataPublicationProvider(fresh_client)
        fresh_observations = tuple(
            fresh_provider.observe(tenant_id=successful_tenant_id, reference=reference)
            for reference in references
        )
        for reference, expected in zip(references, fresh_observations, strict=True):
            if provider.observe(tenant_id=successful_tenant_id, reference=reference) != expected:
                raise SemanticFormationHarnessError("fresh publication readback differs")
        fresh_readback_digest = _verified_readback_digest(
            fresh_observations,
            expected_digest=receipt.round_trip_observation_digest,
        )
        client.health()
        client.assert_object_absent(
            tenant_key=no_valid_plan_tenant_id,
            identity=intent.semantic_identities[0],
        )
        operation = repository.load_operation_by_handle(private_resource_handle)
        operation_secrets = secret_store.resolve(operation.secret_reference)
        phase = "access_validation"
        runtime_client = provisioner.new_service_identity_client(
            private_resource_handle=private_resource_handle,
            operation_id=operation_id,
            tenant_key=successful_tenant_id,
            identity="runtime",
            password=operation_secrets.runtime_password,
        )
        runtime_reference, expected_runtime_snapshot = _runtime_read_target(
            references=references,
            observations=fresh_observations,
            logical_identity=intent.semantic_identities[0],
        )
        runtime_snapshot = runtime_client.get_object(
            tenant_key=successful_tenant_id,
            identity=runtime_reference.stable_identity,
        )
        if runtime_snapshot != expected_runtime_snapshot:
            raise SemanticFormationHarnessError(
                "runtime identity could not read its published semantic object"
            )
        runtime_client.assert_administration_denied()
        runtime_client.assert_other_tenant_namespace_denied(tenant_key="other-tenant")
        # Reaching this line is the proof: both denial assertions raise otherwise.
        tenant_isolation_verified: Literal[True] = True

        phase = "drift_observation"
        client.ensure_glossary_term(
            tenant_key=successful_tenant_id,
            identity=intent.semantic_identities[0],
            payload=GlossaryTermPayload(
                name=persisted_semantic.entities[0].name,
                definition="A materially narrowed definition for drift verification.",
                owner_ref="runtime",
                provenance_ref=intent.contract_reference.digest,
            ),
            idempotency_key="drift-" + secrets.token_hex(12),
        )
        drift = publication_service.observe_drift(
            tenant_id=successful_tenant_id, publication_id=receipt.publication_id
        )
        if drift is None or drift.auto_applied is not False:
            raise SemanticFormationHarnessError("material catalog drift was not routed for review")
        source_backup_observations = tuple(
            fresh_provider.observe(tenant_id=successful_tenant_id, reference=reference)
            for reference in references
        )

        compose_environment = dict(config.environment) | operation_secrets.compose_environment()
        restore_environment = dict(compose_environment) | {
            "HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD": secrets.token_urlsafe(32),
            "HEINZEL_OPENMETADATA_DATABASE_PASSWORD": secrets.token_urlsafe(32),
            "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD": secrets.token_urlsafe(32),
        }
        phase = "catalog_backup"
        compose.backup(
            project_name=operation.project_name,
            backup_path=config.backup_path,
            environment=compose_environment,
        )
        compose.stop(project_name=operation.project_name, environment=compose_environment)
        phase = "isolated_restore"
        compose.restore(
            project_name=restore_project,
            backup_path=config.backup_path,
            environment=restore_environment,
        )
        phase = "search_rebuild"
        compose.rebuild_search_index(
            project_name=restore_project,
            environment=restore_environment,
        )
        restore_admin_password = SecretStr("Aa0!" + secrets.token_urlsafe(32))
        restore_runtime_password = SecretStr("Aa0!" + secrets.token_urlsafe(32))
        restore_tenant_admin_password = SecretStr("Aa0!" + secrets.token_urlsafe(32))
        restore_other_admin_password = SecretStr("Aa0!" + secrets.token_urlsafe(32))
        restore_bootstrap_client = provisioner.new_administrator_client(
            private_resource_handle=private_resource_handle, operation_id=operation_id
        )
        phase = "runtime_credential_rotation"
        restore_bootstrap_client.rotate_service_identity_password(
            tenant_key=successful_tenant_id,
            identity="runtime",
            new_password=restore_runtime_password,
        )
        phase = "tenant_admin_credential_rotation"
        restore_bootstrap_client.rotate_service_identity_password(
            tenant_key=successful_tenant_id,
            identity="administrator",
            new_password=restore_tenant_admin_password,
        )
        phase = "denial_admin_credential_rotation"
        restore_bootstrap_client.rotate_service_identity_password(
            tenant_key="other-tenant",
            identity="administrator",
            new_password=restore_other_admin_password,
        )
        phase = "platform_admin_credential_rotation"
        restore_bootstrap_client.rotate_admin_password(restore_admin_password)
        restored_client = provisioner.new_administrator_client(
            private_resource_handle=private_resource_handle,
            operation_id=operation_id,
            password=restore_admin_password,
        )
        restored_identity_clients = (
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key=successful_tenant_id,
                identity="runtime",
                password=restore_runtime_password,
            ),
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key=successful_tenant_id,
                identity="administrator",
                password=restore_tenant_admin_password,
            ),
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key="other-tenant",
                identity="administrator",
                password=restore_other_admin_password,
            ),
        )
        retired_identity_clients = (
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key=successful_tenant_id,
                identity="runtime",
                password=operation_secrets.runtime_password,
            ),
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key=successful_tenant_id,
                identity="administrator",
                password=operation_secrets.administrator_password,
            ),
            provisioner.new_service_identity_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                tenant_key="other-tenant",
                identity="administrator",
                password=operation_secrets.administrator_password,
            ),
            provisioner.new_administrator_client(
                private_resource_handle=private_resource_handle,
                operation_id=operation_id,
                password=operation_secrets.admin_password,
            ),
        )
        credential_observations: list[tuple[str, str]] = [("platform_admin", "authenticated")]
        phase = "credential_validation"
        restored_client.health()
        for index, identity_client in enumerate(restored_identity_clients, start=1):
            identity_client.health()
            credential_observations.append((f"service_identity_{index}", "authenticated"))
        for index, retired_client in enumerate(retired_identity_clients, start=1):
            try:
                retired_client.health()
            except CatalogProviderError as error:
                if error.classification != "authentication":
                    raise SemanticFormationHarnessError(
                        "isolated restore retired credential denial was not authentication"
                    ) from None
                credential_observations.append((f"retired_identity_{index}", error.classification))
            else:
                raise SemanticFormationHarnessError(
                    "isolated restore accepted a retired credential"
                )
        phase = "restore_validation"
        restored_provider = OpenMetadataPublicationProvider(restored_client)
        restored_observations = tuple(
            restored_provider.observe(tenant_id=successful_tenant_id, reference=reference)
            for reference in references
        )
        if restored_observations != source_backup_observations:
            raise SemanticFormationHarnessError(
                "isolated restore differs from the backed-up catalog state"
            )
        search_observations: list[tuple[str, str]] = []
        for semantic_identity in intent.semantic_identities:
            restored_client.assert_object_searchable(
                tenant_key=successful_tenant_id,
                identity=semantic_identity,
            )
            search_observations.append((semantic_identity, "searchable"))
        restored_client.assert_object_absent(
            tenant_key=no_valid_plan_tenant_id,
            identity=intent.semantic_identities[0],
        )
        backup_result_digest = sha256_digest(config.backup_path.read_bytes())
        compose.down(project_name=restore_project, environment=restore_environment)
        phase = "source_restart"
        compose.start(project_name=operation.project_name, environment=compose_environment)

        if _source_identity(config.repository_root) != source_identity:
            raise SemanticFormationHarnessError(
                "source checkout changed during witnessed execution"
            )
        terminal_ledger = ledger.model_copy(
            update={
                "run_state": "complete",
                "resources": tuple(
                    resource.model_copy(update={"cleanup_state": "complete"})
                    for resource in ledger.resources
                ),
            }
        )
        phase = "evidence_preparation"
        evidence = SemanticFormationEvidence(
            run_id=run_id,
            source_revision=source_identity.revision,
            source_tree_digest=source_identity.tree_digest,
            lock_digest=sha256_digest((config.repository_root / "uv.lock").read_bytes()),
            python_version=sys.version.split()[0],
            operator_pseudonym=config.operator_pseudonym,
            host_pseudonym=config.host_pseudonym,
            successful=TenantEvidence(
                tenant_pseudonym="successful-" + run_id[:12],
                package_digest=successful.candidate_set.original_digest,
                candidate_set_digest=digest(successful.candidate_set),
                authority_resolution_digest=digest(successful.authority_resolution),
                lifecycle_state=successful.request.state.value,
                reason_code=successful.authority_resolution.reason_code.value,
                execution_occurred=successful.execution_occurred,
                publication_occurred=successful_publication_occurred,
            ),
            no_valid_plan=TenantEvidence(
                tenant_pseudonym="no-valid-plan-" + run_id[12:24],
                package_digest=no_valid_plan.candidate_set.original_digest,
                candidate_set_digest=digest(no_valid_plan.candidate_set),
                authority_resolution_digest=digest(no_valid_plan.authority_resolution),
                lifecycle_state=no_valid_plan.request.state.value,
                reason_code=no_valid_plan.authority_resolution.reason_code.value,
                execution_occurred=no_valid_plan.execution_occurred,
                publication_occurred=no_valid_plan_publication_occurred,
            ),
            review_bundle_digest=successful.semantic_version.review_bundle_digest,
            approved_semantic_digest=digest(persisted_semantic),
            contract_digest=digest(successful.contract),
            publication_intent_digest=digest(intent),
            publication_receipt_digest=digest(receipt),
            drift_request_digest=digest(drift.request),
            positive_probe_digest=validation.positive_probe_digest,
            denial_probe_digest=validation.denial_probe_digest,
            provider_build_digest=validation.provider_build_digest,
            provider_image_set_digest=validation.provider_image_set_digest,
            fresh_readback_digest=fresh_readback_digest,
            backup_result_digest=backup_result_digest,
            isolated_restore_result_digest=digest(
                {
                    "source_observations": source_backup_observations,
                    "restored_observations": restored_observations,
                    "isolated_project": restore_project,
                    "source_project": operation.project_name,
                }
            ),
            search_result_digest=digest(tuple(search_observations)),
            credential_result_digest=digest(tuple(credential_observations)),
            representative_query_digest=digest(restored_observations),
            cleanup_result_digest=_sanitized_cleanup_digest(terminal_ledger),
            round_trip_verified=receipt.round_trip_verified,
            auto_applied=drift.auto_applied,
            tenant_isolation_verified=tenant_isolation_verified,
            exact_cleanup_verified=True,
            zero_residual_resources=True,
        )
        private_secret_values = tuple(
            secret.get_secret_value()
            for secret in (
                operation_secrets.admin_password,
                operation_secrets.runtime_password,
                operation_secrets.administrator_password,
                operation_secrets.mysql_root_password,
                operation_secrets.database_password,
                operation_secrets.airflow_database_password,
                restore_admin_password,
                restore_runtime_password,
                restore_tenant_admin_password,
                restore_other_admin_password,
            )
        )
        forbidden_values = (
            *tuple(
                config.environment[name]
                for name in _SECRET_VARIABLES
                if config.environment.get(name)
            ),
            successful_tenant_id,
            no_valid_plan_tenant_id,
            operation_id,
            restore_project,
            private_resource_handle,
            *(
                str(getattr(config, field))
                for field in (
                    "state_path",
                    "output_dir",
                    "cleanup_ledger_path",
                    "secret_store_dir",
                    "backup_path",
                    "docker_config",
                )
            ),
            *(resource.exact_identifier for resource in ledger.resources),
            *provisioned_provider_identifiers,
            *(resource.identifier for resource in created_resources),
            *private_secret_values,
            *(
                restore_environment[name]
                for name in (
                    "HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD",
                    "HEINZEL_OPENMETADATA_DATABASE_PASSWORD",
                    "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD",
                )
            ),
        )
        encoded = validate_sanitized_evidence(evidence, forbidden_values=forbidden_values)
        _prepare_evidence(config, encoded)
        ledger = ledger.model_copy(update={"run_state": "cleanup_pending"})
        _write_private_ledger(config, ledger)
        _close_resources(open_resources)
        phase = "exact_cleanup"
        ledger = _cleanup_resources(config, ledger, preserve_evidence_staging=True)
        exact_cleanup_verified, zero_residual_resources = _cleanup_claims(config, ledger)
        if not exact_cleanup_verified or not zero_residual_resources:
            raise SemanticFormationHarnessError("exact cleanup verification failed")
        if _source_identity(config.repository_root) != source_identity:
            raise SemanticFormationHarnessError("source checkout changed during witnessed cleanup")
        evidence_entry = next(
            resource
            for resource in ledger.resources
            if resource.resource_kind == "evidence_staging"
        )
        completed_ledger = _replace_entry(
            ledger,
            resource_digest=evidence_entry.resource_digest,
            cleanup_state="complete",
        ).model_copy(update={"run_state": "complete"})
        if _sanitized_cleanup_digest(completed_ledger) != evidence.cleanup_result_digest:
            raise SemanticFormationHarnessError("terminal cleanup differs from staged evidence")
        phase = "evidence_finalization"
        _publish_staged_evidence(config, encoded)
        _write_private_ledger(config, completed_ledger)
        ledger = completed_ledger
        print(json.dumps({"evidence_digest": sha256_digest(encoded), "status": "complete"}))
        return 0
    except BaseException as error:
        close_error: SemanticFormationHarnessError | None = None
        try:
            _close_resources(open_resources)
        except SemanticFormationHarnessError as resource_close_error:
            close_error = resource_close_error
        cleanup_error: Exception | None = None
        try:
            ledger = _load_private_ledger(config)
            if ledger.run_state != "complete":
                ledger = ledger.model_copy(update={"run_state": "failed"})
                _write_private_ledger(config, ledger)
                _cleanup_resources(config, ledger)
        except Exception as resource_cleanup_error:
            cleanup_error = resource_cleanup_error
        if close_error is not None or cleanup_error is not None:
            raise SemanticFormationHarnessError(
                _failure_message(phase=phase, error=error, cleanup_incomplete=True)
            ) from None
        if isinstance(error, SemanticFormationHarnessError):
            raise
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SemanticFormationHarnessError(
            _failure_message(phase=phase, error=error, cleanup_incomplete=False)
        ) from None


def _cleanup(config: SemanticFormationConfig) -> int:
    ledger = _cleanup_resources(config, _load_private_ledger(config))
    print(
        json.dumps(
            {"cleanup_digest": _sanitized_cleanup_digest(ledger), "status": ledger.run_state}
        )
    )
    return 0


def _cleanup_status(config: SemanticFormationConfig) -> int:
    ledger = _load_private_ledger(config)
    exact_cleanup_verified, zero_residual_resources = _cleanup_claims(config, ledger)
    print(
        json.dumps(
            {
                "cleanup_digest": _sanitized_cleanup_digest(ledger),
                "complete": ledger.run_state == "complete"
                and exact_cleanup_verified
                and zero_residual_resources,
                "status": ledger.run_state,
                "zero_residual_resources": zero_residual_resources,
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = SemanticFormationConfig.from_environment(
            os.environ,
            allow_existing=arguments.command in {"cleanup", "cleanup-status"},
        )
        if arguments.command == "cleanup":
            return _cleanup(config)
        if arguments.command == "cleanup-status":
            return _cleanup_status(config)
        return _run(config)
    except SemanticFormationHarnessError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
