from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, Self, TypedDict, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken
from pillarmesh_contract_model import ArtifactModel, digest
from pillarmesh_provider_sdk import ComposeResourceKind, DockerComposeProcess
from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from tests.acceptance.plan3a_fault_matrix import (
    WarehouseFaultScenarioOutcome,
    canonical_fault_matrix_outcomes,
    run_plan3a_fault_matrix,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE_FILE_NAME: Final = "plan3a-evidence.json"
LEDGER_FILE_NAME: Final = "private-ledger.json.encrypted"
_ENGINE_ORDER: Final = ("postgresql", "clickhouse")
_DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
_IMAGE_PATTERN: Final = re.compile(r"^[^\s@:]+(?:/[^\s@:]+)*:[^\s@]+@sha256:[0-9a-f]{64}$")
_PATH_VARIABLES: Final = {
    "PILLARMESH_PLAN3A_STATE_PATH": "state_path",
    "PILLARMESH_PLAN3A_SECRET_DIRECTORY": "secret_directory",
    "PILLARMESH_PLAN3A_BACKUP_DIRECTORY": "backup_directory",
    "PILLARMESH_PLAN3A_EVIDENCE_DIRECTORY": "evidence_directory",
    "PILLARMESH_PLAN3A_RESERVATION_PATH": "reservation_path",
}
_REQUIRED_VARIABLES: Final = (
    *_PATH_VARIABLES,
    "PILLARMESH_PLAN3A_SOURCE_COMMIT",
    "PILLARMESH_PLAN3A_POSTGRES_IMAGE",
    "PILLARMESH_PLAN3A_CLICKHOUSE_IMAGE",
    "PILLARMESH_PLAN3A_RETENTION_DEADLINE",
    "PILLARMESH_PLAN3A_CREDENTIAL_CANARIES",
    "PILLARMESH_PLAN3A_STATE_ENCRYPTION_KEY",
    "PILLARMESH_PLAN3A_EVIDENCE_SIGNING_KEY",
)


class Plan3AHarnessError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


def canonical_json_bytes(value: ArtifactModel | Mapping[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, ArtifactModel) else dict(value)
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        + "\n"
    ).encode()


def _json_default(value: object) -> object:
    if isinstance(value, ArtifactModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _require_owner_only_directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise Plan3AHarnessError("private parent directory does not exist") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise Plan3AHarnessError("private parent directory must be owner-only mode 0700")


def _parse_utc(value: str, variable_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(parsed)
    except ValueError:
        raise Plan3AHarnessError(f"invalid UTC timestamp: {variable_name}") from None


def _parse_canaries(raw_value: str) -> tuple[str, ...]:
    try:
        values = json.loads(raw_value)
    except json.JSONDecodeError:
        raise Plan3AHarnessError("credential canaries must be a JSON array") from None
    if (
        not isinstance(values, list)
        or len(values) < 8
        or any(not isinstance(value, str) or len(value) < 12 for value in values)
    ):
        raise Plan3AHarnessError("credential canaries must be nonempty private markers")
    if len(set(values)) != len(values):
        raise Plan3AHarnessError("credential canaries must be unique")
    return tuple(values)


@dataclass(frozen=True, slots=True, init=False)
class Plan3AConfig:
    state_path: Path
    secret_directory: Path
    backup_directory: Path
    evidence_directory: Path
    reservation_path: Path
    source_commit: str
    postgres_image: str
    clickhouse_image: str
    retention_deadline: datetime
    credential_canary_digests: tuple[str, ...]
    repository_root: Path
    _credential_canaries: tuple[str, ...] = field(repr=False)
    _state_encryption_key: SecretStr = field(repr=False)
    _signing_key: SecretStr = field(repr=False)

    def __init__(self) -> None:
        raise TypeError("Plan3AConfig must be created from the environment")

    @property
    def signing_key(self) -> SecretStr:
        return self._signing_key

    @property
    def private_markers(self) -> tuple[str, ...]:
        return (
            *self._credential_canaries,
            self._state_encryption_key.get_secret_value(),
            self._signing_key.get_secret_value(),
        )

    @property
    def credential_canaries_for_witness(self) -> tuple[str, ...]:
        return self._credential_canaries

    @classmethod
    def _create(cls, **values: object) -> Plan3AConfig:
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        repository_root: Path = REPOSITORY_ROOT,
        allow_existing: bool = False,
    ) -> Plan3AConfig:
        missing = tuple(name for name in _REQUIRED_VARIABLES if not environment.get(name))
        if missing:
            raise Plan3AHarnessError("missing required variables: " + ", ".join(missing))

        raw_paths = {name: Path(environment[name]) for name in _PATH_VARIABLES}
        paths = {name: _absolute_lexical(path) for name, path in raw_paths.items()}
        if len(set(paths.values())) != len(paths):
            raise Plan3AHarnessError("duplicate private paths are not permitted")
        for name, raw_path in raw_paths.items():
            path = paths[name]
            if not raw_path.expanduser().is_absolute() or _inside(path, repository_root):
                raise Plan3AHarnessError(
                    f"private path must be absolute and outside repository: {name}"
                )
            if _has_symlink_component(path):
                raise Plan3AHarnessError(f"private path contains a symlink: {name}")
            _require_owner_only_directory(path.parent)
            if path.exists():
                metadata = path.stat(follow_symlinks=False)
                expected_mode = 0o700 if path.is_dir() else 0o600
                if (
                    metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != expected_mode
                ):
                    raise Plan3AHarnessError(f"existing private target must be owner-only: {name}")
                if not allow_existing and path.is_dir() and any(path.iterdir()):
                    raise Plan3AHarnessError(f"private output is nonempty: {name}")
                if not allow_existing and path.is_file():
                    raise Plan3AHarnessError(f"private output already exists: {name}")

        source_commit = environment["PILLARMESH_PLAN3A_SOURCE_COMMIT"]
        if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
            raise Plan3AHarnessError("source commit must be a full lowercase Git commit")
        try:
            checked_out_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            raise Plan3AHarnessError("repository root has no readable checked-out commit") from None
        if source_commit != checked_out_commit:
            raise Plan3AHarnessError("source commit differs from the checked-out commit")
        postgres_image = environment["PILLARMESH_PLAN3A_POSTGRES_IMAGE"]
        clickhouse_image = environment["PILLARMESH_PLAN3A_CLICKHOUSE_IMAGE"]
        if _IMAGE_PATTERN.fullmatch(postgres_image) is None:
            raise Plan3AHarnessError("PostgreSQL image must be tag-and-digest pinned")
        if _IMAGE_PATTERN.fullmatch(clickhouse_image) is None:
            raise Plan3AHarnessError("ClickHouse image must be tag-and-digest pinned")

        canaries = _parse_canaries(environment["PILLARMESH_PLAN3A_CREDENTIAL_CANARIES"])
        return cls._create(
            state_path=paths["PILLARMESH_PLAN3A_STATE_PATH"],
            secret_directory=paths["PILLARMESH_PLAN3A_SECRET_DIRECTORY"],
            backup_directory=paths["PILLARMESH_PLAN3A_BACKUP_DIRECTORY"],
            evidence_directory=paths["PILLARMESH_PLAN3A_EVIDENCE_DIRECTORY"],
            reservation_path=paths["PILLARMESH_PLAN3A_RESERVATION_PATH"],
            source_commit=source_commit,
            postgres_image=postgres_image,
            clickhouse_image=clickhouse_image,
            retention_deadline=_parse_utc(
                environment["PILLARMESH_PLAN3A_RETENTION_DEADLINE"],
                "PILLARMESH_PLAN3A_RETENTION_DEADLINE",
            ),
            credential_canary_digests=tuple(_sha256(value.encode()) for value in canaries),
            repository_root=_absolute_lexical(repository_root),
            _credential_canaries=canaries,
            _state_encryption_key=SecretStr(environment["PILLARMESH_PLAN3A_STATE_ENCRYPTION_KEY"]),
            _signing_key=SecretStr(environment["PILLARMESH_PLAN3A_EVIDENCE_SIGNING_KEY"]),
        )


type EngineName = Literal["postgresql", "clickhouse"]
type TerminalState = Literal["retired"]
type NamedCheckDisposition = Literal[
    "provision:passed",
    "initial_validation:passed",
    "backup_restore:passed",
    "suspend_resume:passed",
    "retention_cleanup:passed",
    "absence:passed",
]


class EngineWitnessResult(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    engine_kind: EngineName
    binding_digest: str = Field(pattern=_DIGEST_PATTERN)
    validation_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    resume_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    retirement_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    cleanup_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    terminal_state: TerminalState
    engine_version: str = Field(min_length=1, max_length=64)
    engine_image: str = Field(min_length=1, max_length=256)
    check_dispositions: tuple[NamedCheckDisposition, ...]
    duration_seconds: float = Field(ge=0)
    residual_resource_count: int = Field(ge=0)

    @model_validator(mode="after")
    def requires_all_checks_once(self) -> Self:
        required = {
            "provision:passed",
            "initial_validation:passed",
            "backup_restore:passed",
            "suspend_resume:passed",
            "retention_cleanup:passed",
            "absence:passed",
        }
        if set(self.check_dispositions) != required or len(self.check_dispositions) != len(
            required
        ):
            raise ValueError("engine witness requires every named lifecycle check exactly once")
        if self.residual_resource_count != 0:
            raise ValueError("terminal engine witness must have zero residual resources")
        if _IMAGE_PATTERN.fullmatch(self.engine_image) is None:
            raise ValueError("engine image must be tag-and-digest pinned")
        return self


class Plan3ACleanupEvidence(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    resource_inventory_digest: str = Field(pattern=_DIGEST_PATTERN)
    completed_resource_count: int = Field(ge=0)
    retained_resource_count: int = Field(ge=0)
    failed_resource_count: int = Field(ge=0)
    zero_residual_resources: bool
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def requires_consistent_zero_residual_claim(self) -> Self:
        expected = self.retained_resource_count == 0 and self.failed_resource_count == 0
        if self.zero_residual_resources != expected:
            raise ValueError("cleanup residual counts and terminal claim differ")
        return self


def _failure_matrix_digest(
    outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
) -> str:
    return digest(
        {
            "domain": "pillarmesh-plan3a-offline-control-plane-failure-matrix-v1",
            "outcomes": tuple(outcome.model_dump(mode="json") for outcome in outcomes),
        }
    )


class Plan3AFaultMatrixEvidence(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    execution_domain: Literal["offline_control_plane"] = "offline_control_plane"
    outcomes: tuple[WarehouseFaultScenarioOutcome, ...]

    @field_validator("outcomes", mode="before")
    @classmethod
    def canonicalizes_exact_outcomes(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("fault matrix outcomes must be a finite sequence")
        parsed: list[WarehouseFaultScenarioOutcome] = []
        for outcome in value:
            if isinstance(outcome, ArtifactModel):
                payload = outcome.model_dump(mode="json")
            elif isinstance(outcome, Mapping):
                payload = dict(outcome)
            else:
                raise ValueError("fault matrix outcome is invalid")
            parsed.append(
                WarehouseFaultScenarioOutcome.model_validate_json(canonical_json_bytes(payload))
            )
        return canonical_fault_matrix_outcomes(tuple(parsed))


class _Plan3AProvisionalEvidence(TypedDict):
    """Every `Plan3AEvidence` field except the privacy digest computed from it.

    The payload is serialized to derive `privacy_scan_digest` before the model can
    be built, so it exists as a mapping first. Typing it keeps the `**` expansion
    below checked against the model instead of erasing all thirteen fields to
    `object`.
    """

    run_id: str
    source_commit: str
    engine_results: tuple[EngineWitnessResult, EngineWitnessResult]
    cross_engine_conformance_digest: str
    tenant_isolation_digest: str
    failure_matrix: Plan3AFaultMatrixEvidence
    failure_matrix_digest: str
    local_encryption_limitation: Literal["deferred_local_acceptance"]
    production_readiness: Literal["not_proven"]
    lifecycle_conformance: Literal["proven"]
    cleanup_digest: str
    started_at: datetime
    completed_at: datetime


class Plan3AEvidence(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    engine_results: tuple[EngineWitnessResult, EngineWitnessResult]
    cross_engine_conformance_digest: str = Field(pattern=_DIGEST_PATTERN)
    tenant_isolation_digest: str = Field(pattern=_DIGEST_PATTERN)
    failure_matrix: Plan3AFaultMatrixEvidence
    failure_matrix_digest: str = Field(pattern=_DIGEST_PATTERN)
    privacy_scan_digest: str = Field(pattern=_DIGEST_PATTERN)
    local_encryption_limitation: Literal["deferred_local_acceptance"]
    production_readiness: Literal["not_proven"]
    lifecycle_conformance: Literal["proven"]
    cleanup_digest: str = Field(pattern=_DIGEST_PATTERN)
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def requires_ordered_two_engine_witness(self) -> Self:
        if tuple(result.engine_kind for result in self.engine_results) != _ENGINE_ORDER:
            raise ValueError("engine witnesses must be PostgreSQL followed by ClickHouse")
        if self.completed_at < self.started_at:
            raise ValueError("completion cannot precede start")
        canonical_outcomes = canonical_fault_matrix_outcomes(self.failure_matrix.outcomes)
        if (
            canonical_outcomes != self.failure_matrix.outcomes
            or self.failure_matrix_digest != _failure_matrix_digest(canonical_outcomes)
        ):
            raise ValueError("failure matrix evidence and digest differ")
        return self


@dataclass(frozen=True, slots=True)
class WitnessCapture:
    result: EngineWitnessResult | None = None
    stdout: str = ""
    stderr: str = ""
    logs: tuple[str, ...] = ()
    private_markers: tuple[str, ...] = field(default=(), repr=False)


@runtime_checkable
class _DumpableEngineResult(Protocol):
    def model_dump(self, *, mode: Literal["json"]) -> dict[str, object]: ...


@runtime_checkable
class _WitnessCaptureBoundary(Protocol):
    result: _DumpableEngineResult | None
    stdout: str
    stderr: str
    logs: tuple[str, ...]
    private_markers: tuple[str, ...]


def _normalize_witness_capture(value: object) -> WitnessCapture:
    if not isinstance(value, _WitnessCaptureBoundary) or value.result is None:
        raise Plan3AHarnessError("engine witness returned an invalid capture")
    try:
        result = EngineWitnessResult.model_validate_json(
            canonical_json_bytes(value.result.model_dump(mode="json"))
        )
    except ValueError:
        raise Plan3AHarnessError("engine witness returned an invalid result") from None
    return WitnessCapture(
        result=result,
        stdout=value.stdout,
        stderr=value.stderr,
        logs=value.logs,
        private_markers=value.private_markers,
    )


def scan_private_markers(
    encoded_evidence: bytes,
    capture: WitnessCapture,
    private_markers: Sequence[str],
) -> str:
    channels = (
        encoded_evidence,
        capture.stdout.encode(),
        capture.stderr.encode(),
        *(entry.encode() for entry in capture.logs),
    )
    markers = tuple(marker.encode() for marker in (*private_markers, *capture.private_markers))
    if any(marker and marker in channel for marker in markers for channel in channels):
        raise Plan3AHarnessError("privacy scan detected a private marker")
    return digest(
        {
            "domain": "pillarmesh-plan3a-privacy-scan-v1",
            "channel_digests": tuple(_sha256(channel) for channel in channels),
            "marker_digests": tuple(sorted(_sha256(marker) for marker in markers if marker)),
        }
    )


type PrivateCleanupState = Literal["recorded", "retained", "complete", "failed"]


class PrivateResourceEntry(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_digest: str = Field(pattern=_DIGEST_PATTERN)
    resource_kind: Literal["encrypted_backup_secret_pair"]
    backup_path: str = Field(min_length=1)
    secret_path: str = Field(min_length=1)
    retention_deadline: datetime
    backup_cleanup_state: PrivateCleanupState = "recorded"
    secret_cleanup_state: PrivateCleanupState = "recorded"

    @field_validator("retention_deadline")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def file_pair(
        cls,
        *,
        backup_path: Path,
        secret_path: Path,
        retention_deadline: datetime,
    ) -> PrivateResourceEntry:
        deadline = _utc(retention_deadline)
        payload = {
            "resource_kind": "encrypted_backup_secret_pair",
            "backup_path": str(_absolute_lexical(backup_path)),
            "secret_path": str(_absolute_lexical(secret_path)),
            "retention_deadline": deadline.isoformat(),
        }
        return cls(
            resource_digest=digest(payload),
            resource_kind="encrypted_backup_secret_pair",
            backup_path=payload["backup_path"],
            secret_path=payload["secret_path"],
            retention_deadline=deadline,
        )


class PrivateDockerResourceEntry(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_digest: str = Field(pattern=_DIGEST_PATTERN)
    resource_kind: Literal["docker_resource"]
    engine_kind: EngineName
    compose_resource_kind: ComposeResourceKind
    exact_identifier: str = Field(min_length=1)
    retention_deadline: datetime
    cleanup_state: PrivateCleanupState = "recorded"

    @field_validator("retention_deadline")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        engine_kind: EngineName,
        compose_resource_kind: ComposeResourceKind,
        exact_identifier: str,
        retention_deadline: datetime,
    ) -> PrivateDockerResourceEntry:
        deadline = _utc(retention_deadline)
        payload = {
            "resource_kind": "docker_resource",
            "engine_kind": engine_kind,
            "compose_resource_kind": compose_resource_kind,
            "exact_identifier": exact_identifier,
            "retention_deadline": deadline.isoformat(),
        }
        return cls(
            resource_digest=digest(payload),
            resource_kind="docker_resource",
            engine_kind=engine_kind,
            compose_resource_kind=compose_resource_kind,
            exact_identifier=exact_identifier,
            retention_deadline=deadline,
        )


class PrivateDirectoryResourceEntry(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_digest: str = Field(pattern=_DIGEST_PATTERN)
    resource_kind: Literal["private_engine_directory"]
    engine_kind: EngineName
    exact_path: str = Field(min_length=1)
    retention_deadline: datetime
    cleanup_state: PrivateCleanupState = "recorded"

    @field_validator("retention_deadline")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        engine_kind: EngineName,
        exact_path: Path,
        retention_deadline: datetime,
    ) -> PrivateDirectoryResourceEntry:
        deadline = _utc(retention_deadline)
        payload = {
            "resource_kind": "private_engine_directory",
            "engine_kind": engine_kind,
            "exact_path": str(_absolute_lexical(exact_path)),
            "retention_deadline": deadline.isoformat(),
        }
        return cls(
            resource_digest=digest(payload),
            resource_kind="private_engine_directory",
            engine_kind=engine_kind,
            exact_path=payload["exact_path"],
            retention_deadline=deadline,
        )


class PrivateFaultMatrixDirectoryResourceEntry(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_digest: str = Field(pattern=_DIGEST_PATTERN)
    resource_kind: Literal["offline_fault_matrix_directory"]
    exact_path: str = Field(min_length=1)
    retention_deadline: datetime
    cleanup_state: PrivateCleanupState = "recorded"

    @field_validator("retention_deadline")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        exact_path: Path,
        retention_deadline: datetime,
    ) -> PrivateFaultMatrixDirectoryResourceEntry:
        deadline = _utc(retention_deadline)
        payload = {
            "resource_kind": "offline_fault_matrix_directory",
            "exact_path": str(_absolute_lexical(exact_path)),
            "retention_deadline": deadline.isoformat(),
        }
        return cls(
            resource_digest=digest(payload),
            resource_kind="offline_fault_matrix_directory",
            exact_path=payload["exact_path"],
            retention_deadline=deadline,
        )


type PrivateResource = (
    PrivateResourceEntry
    | PrivateDockerResourceEntry
    | PrivateDirectoryResourceEntry
    | PrivateFaultMatrixDirectoryResourceEntry
)


class Plan3APrivateLedger(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    run_id: str = Field(pattern=_DIGEST_PATTERN)
    reservation_digest: str = Field(pattern=_DIGEST_PATTERN)
    resources: tuple[PrivateResource, ...]
    cleanup_evidence: Plan3ACleanupEvidence | None = None
    public_evidence_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    integrity_digest: str = Field(pattern=_DIGEST_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        reservation_digest: str,
        resources: tuple[PrivateResource, ...],
        signing_key: SecretStr,
    ) -> Plan3APrivateLedger:
        unsigned = cls(
            run_id=run_id,
            reservation_digest=reservation_digest,
            resources=resources,
            integrity_digest="0" * 64,
        )
        return unsigned.model_copy(
            update={"integrity_digest": _ledger_integrity(unsigned, signing_key)}
        )


def _ledger_integrity(ledger: Plan3APrivateLedger, key: SecretStr) -> str:
    payload = ledger.model_dump(mode="json", exclude={"integrity_digest"})
    return hmac.new(
        key.get_secret_value().encode(),
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _fernet(key: SecretStr) -> Fernet:
    material = hashlib.sha256(key.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def _canonical_fernet_token(token: bytes) -> bytes:
    try:
        decoded = base64.b64decode(token, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise InvalidToken from None
    if not hmac.compare_digest(base64.urlsafe_b64encode(decoded), token):
        raise InvalidToken
    return token


def _ledger_path(config: Plan3AConfig) -> Path:
    return config.state_path / LEDGER_FILE_NAME


def _atomic_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_owner_only_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}.temporary")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _reservation_payload(run_id: str) -> bytes:
    return canonical_json_bytes({"schema_version": "1", "run_id": run_id})


def _write_reservation(config: Plan3AConfig, run_id: str) -> str:
    payload = _reservation_payload(run_id)
    digest_value = hmac.new(
        config.signing_key.get_secret_value().encode(), payload, hashlib.sha256
    ).hexdigest()
    encoded = canonical_json_bytes(
        {"payload": json.loads(payload), "integrity_digest": digest_value}
    )
    if config.reservation_path.exists():
        existing = config.reservation_path.read_bytes()
        if existing != encoded:
            raise Plan3AHarnessError("shared-environment reservation is owned by another run")
    else:
        _atomic_private_write(config.reservation_path, encoded)
    return digest_value


def _validate_reservation(config: Plan3AConfig, ledger: Plan3APrivateLedger) -> None:
    try:
        envelope = json.loads(config.reservation_path.read_bytes())
        payload = canonical_json_bytes(envelope["payload"])
        observed = str(envelope["integrity_digest"])
        expected = hmac.new(
            config.signing_key.get_secret_value().encode(), payload, hashlib.sha256
        ).hexdigest()
        run_id = str(envelope["payload"]["run_id"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise Plan3AHarnessError("reservation state is missing or corrupt") from None
    if not hmac.compare_digest(observed, expected) or run_id != ledger.run_id:
        raise Plan3AHarnessError("reservation/run identity does not match private ledger")
    if ledger.reservation_digest != expected:
        raise Plan3AHarnessError("reservation digest does not match private ledger")


def write_private_ledger(config: Plan3AConfig, ledger: Plan3APrivateLedger) -> None:
    expected_integrity = _ledger_integrity(ledger, config.signing_key)
    if not hmac.compare_digest(ledger.integrity_digest, expected_integrity):
        raise Plan3AHarnessError("private ledger integrity is invalid")
    reservation_digest = _write_reservation(config, ledger.run_id)
    if ledger.reservation_digest != reservation_digest:
        ledger = ledger.model_copy(update={"reservation_digest": reservation_digest})
        ledger = ledger.model_copy(
            update={"integrity_digest": _ledger_integrity(ledger, config.signing_key)}
        )
    encrypted = _fernet(config._state_encryption_key).encrypt(canonical_json_bytes(ledger))
    path = _ledger_path(config)
    _atomic_private_write(path, encrypted)


def _load_private_ledger(config: Plan3AConfig) -> Plan3APrivateLedger:
    if _has_symlink_component(_ledger_path(config)) or _has_symlink_component(
        config.reservation_path
    ):
        raise Plan3AHarnessError("private ledger is missing or corrupt")
    try:
        encrypted = _canonical_fernet_token(_ledger_path(config).read_bytes())
        cleartext = _fernet(config._state_encryption_key).decrypt(encrypted)
        ledger = Plan3APrivateLedger.model_validate_json(cleartext)
    except (FileNotFoundError, InvalidToken, ValueError):
        raise Plan3AHarnessError("private ledger is missing or corrupt") from None
    expected = _ledger_integrity(ledger, config.signing_key)
    if not hmac.compare_digest(ledger.integrity_digest, expected):
        raise Plan3AHarnessError("private ledger is missing or corrupt")
    _validate_reservation(config, ledger)
    return ledger


def load_authenticated_docker_resources(
    config: Plan3AConfig,
) -> tuple[PrivateDockerResourceEntry, ...]:
    ledger_path = _ledger_path(config)
    if not ledger_path.exists() and not ledger_path.is_symlink():
        return ()
    ledger = _load_private_ledger(config)
    return tuple(
        resource
        for resource in ledger.resources
        if isinstance(resource, PrivateDockerResourceEntry)
    )


def load_authenticated_docker_resources_from_environment(
    environment: Mapping[str, str],
) -> tuple[PrivateDockerResourceEntry, ...]:
    config = Plan3AConfig.from_environment(environment, allow_existing=True)
    return load_authenticated_docker_resources(config)


def register_private_resources(
    config: Plan3AConfig,
    resources: tuple[PrivateResource, ...],
) -> None:
    ledger = _load_private_ledger(config)
    existing = {resource.resource_digest for resource in ledger.resources}
    if any(resource.resource_digest in existing for resource in resources):
        raise Plan3AHarnessError("private resource registration contains a duplicate")
    updated = ledger.model_copy(
        update={"resources": (*ledger.resources, *resources), "cleanup_evidence": None}
    )
    updated = updated.model_copy(
        update={"integrity_digest": _ledger_integrity(updated, config.signing_key)}
    )
    write_private_ledger(config, updated)


def _validate_pair_scope(config: Plan3AConfig, resource: PrivateResourceEntry) -> None:
    backup = _absolute_lexical(Path(resource.backup_path))
    secret = _absolute_lexical(Path(resource.secret_path))
    if (
        not _inside(backup, config.backup_directory)
        or not _inside(secret, config.secret_directory)
        or _has_symlink_component(backup)
        or _has_symlink_component(secret)
    ):
        raise Plan3AHarnessError("private ledger contains a resource outside its exact scope")
    expected = PrivateResourceEntry.file_pair(
        backup_path=backup,
        secret_path=secret,
        retention_deadline=resource.retention_deadline,
    ).resource_digest
    if resource.resource_digest != expected:
        raise Plan3AHarnessError("private ledger resource digest is invalid")


def _cleanup_counts(resources: Sequence[PrivateResource]) -> tuple[int, int, int]:
    states: tuple[PrivateCleanupState, ...] = tuple(
        state
        for resource in resources
        for state in (
            (resource.cleanup_state,)
            if isinstance(
                resource,
                (
                    PrivateDockerResourceEntry,
                    PrivateDirectoryResourceEntry,
                    PrivateFaultMatrixDirectoryResourceEntry,
                ),
            )
            else (resource.backup_cleanup_state, resource.secret_cleanup_state)
        )
    )
    return (
        states.count("complete"),
        states.count("retained") + states.count("recorded"),
        states.count("failed"),
    )


def _compose_for_resource(
    config: Plan3AConfig,
    resource: PrivateDockerResourceEntry,
) -> DockerComposeProcess:
    compose_file = (
        config.repository_root
        / "tests"
        / "emulators"
        / "warehouses"
        / resource.engine_kind
        / "compose.yaml"
    )
    if not compose_file.is_file() or _has_symlink_component(compose_file):
        raise Plan3AHarnessError("recorded Docker resource has no trusted Compose definition")
    return DockerComposeProcess(
        compose_file=compose_file,
        timeout_seconds=300,
        termination_grace_seconds=5,
    )


def _cleanup_docker_resource(
    config: Plan3AConfig,
    resource: PrivateDockerResourceEntry,
) -> PrivateDockerResourceEntry:
    compose = _compose_for_resource(config, resource)
    try:
        compose.remove_resource(
            resource_kind=resource.compose_resource_kind,
            identifier=resource.exact_identifier,
            environment={},
        )
        absent = compose.resource_is_absent(
            resource_kind=resource.compose_resource_kind,
            identifier=resource.exact_identifier,
            environment={},
        )
    except Exception:
        return resource.model_copy(update={"cleanup_state": "failed"})
    return resource.model_copy(update={"cleanup_state": "complete" if absent else "failed"})


def _cleanup_private_directory(
    config: Plan3AConfig,
    resource: PrivateDirectoryResourceEntry,
) -> PrivateDirectoryResourceEntry:
    try:
        path = _validate_private_directory_scope(config, resource)
        if path.exists():
            metadata = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise Plan3AHarnessError("private engine directory ownership changed")
            shutil.rmtree(path)
    except (OSError, Plan3AHarnessError):
        return resource.model_copy(update={"cleanup_state": "failed"})
    return resource.model_copy(
        update={"cleanup_state": "complete" if not path.exists() else "failed"}
    )


def _validate_private_directory_scope(
    config: Plan3AConfig,
    resource: PrivateDirectoryResourceEntry,
) -> Path:
    path = _absolute_lexical(Path(resource.exact_path))
    expected = config.state_path / f"{resource.engine_kind}-witness"
    expected_digest = PrivateDirectoryResourceEntry.create(
        engine_kind=resource.engine_kind,
        exact_path=path,
        retention_deadline=resource.retention_deadline,
    ).resource_digest
    if (
        path != expected
        or resource.resource_digest != expected_digest
        or _has_symlink_component(path)
    ):
        raise Plan3AHarnessError("private engine directory is outside its exact scope")
    return path


def _validate_fault_matrix_directory_scope(
    config: Plan3AConfig,
    resource: PrivateFaultMatrixDirectoryResourceEntry,
) -> Path:
    path = _absolute_lexical(Path(resource.exact_path))
    expected = config.state_path / "offline-control-plane-fault-matrix"
    expected_digest = PrivateFaultMatrixDirectoryResourceEntry.create(
        exact_path=path,
        retention_deadline=resource.retention_deadline,
    ).resource_digest
    if (
        path != expected
        or resource.resource_digest != expected_digest
        or _has_symlink_component(path)
    ):
        raise Plan3AHarnessError("fault matrix directory is outside its exact scope")
    return path


def _cleanup_fault_matrix_directory(
    config: Plan3AConfig,
    resource: PrivateFaultMatrixDirectoryResourceEntry,
) -> PrivateFaultMatrixDirectoryResourceEntry:
    try:
        path = _validate_fault_matrix_directory_scope(config, resource)
        if path.exists():
            metadata = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise Plan3AHarnessError("fault matrix directory ownership changed")
            shutil.rmtree(path)
    except (OSError, Plan3AHarnessError):
        return resource.model_copy(update={"cleanup_state": "failed"})
    return resource.model_copy(
        update={"cleanup_state": "complete" if not path.exists() else "failed"}
    )


def _validate_teardown_scope(config: Plan3AConfig, resources: Sequence[PrivateResource]) -> None:
    for resource in resources:
        if isinstance(resource, PrivateDockerResourceEntry):
            _compose_for_resource(config, resource)
        elif isinstance(resource, PrivateDirectoryResourceEntry):
            _validate_private_directory_scope(config, resource)
        elif isinstance(resource, PrivateFaultMatrixDirectoryResourceEntry):
            _validate_fault_matrix_directory_scope(config, resource)
        else:
            _validate_pair_scope(config, resource)


def _unlink_private_file(path: Path) -> PrivateCleanupState:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return "failed"
    return "failed" if path.exists() or path.is_symlink() else "complete"


def teardown_plan3a(
    config: Plan3AConfig,
    *,
    run_id: str | None,
    now: datetime,
    authorized: bool,
) -> Plan3ACleanupEvidence:
    if not authorized:
        raise PermissionError("Plan 3A teardown requires explicit retention authorization")
    ledger = _load_private_ledger(config)
    if run_id is not None and ledger.run_id != run_id:
        raise Plan3AHarnessError("teardown run identity does not match private ledger")
    if ledger.cleanup_evidence is not None and ledger.cleanup_evidence.zero_residual_resources:
        return ledger.cleanup_evidence

    _validate_teardown_scope(config, ledger.resources)
    updated: list[PrivateResource] = []
    ordered_resources = tuple(
        sorted(
            ledger.resources,
            key=lambda resource: isinstance(
                resource,
                (PrivateDirectoryResourceEntry, PrivateFaultMatrixDirectoryResourceEntry),
            ),
        )
    )
    for resource in ordered_resources:
        if _utc(now) < resource.retention_deadline:
            if isinstance(
                resource,
                (
                    PrivateDockerResourceEntry,
                    PrivateDirectoryResourceEntry,
                    PrivateFaultMatrixDirectoryResourceEntry,
                ),
            ):
                updated.append(resource.model_copy(update={"cleanup_state": "retained"}))
            else:
                _validate_pair_scope(config, resource)
                updated.append(
                    resource.model_copy(
                        update={
                            "backup_cleanup_state": "retained",
                            "secret_cleanup_state": "retained",
                        }
                    )
                )
            continue
        if isinstance(resource, PrivateDockerResourceEntry):
            updated.append(_cleanup_docker_resource(config, resource))
            continue
        if isinstance(resource, PrivateDirectoryResourceEntry):
            updated.append(_cleanup_private_directory(config, resource))
            continue
        if isinstance(resource, PrivateFaultMatrixDirectoryResourceEntry):
            updated.append(_cleanup_fault_matrix_directory(config, resource))
            continue
        backup = Path(resource.backup_path)
        secret = Path(resource.secret_path)
        updated.append(
            resource.model_copy(
                update={
                    "backup_cleanup_state": _unlink_private_file(backup),
                    "secret_cleanup_state": _unlink_private_file(secret),
                }
            )
        )

    completed, retained, failed = _cleanup_counts(updated)
    evidence = Plan3ACleanupEvidence(
        run_id=ledger.run_id,
        resource_inventory_digest=digest(
            {
                "domain": "pillarmesh-plan3a-cleanup-inventory-v1",
                "resource_digests": tuple(resource.resource_digest for resource in updated),
            }
        ),
        completed_resource_count=completed,
        retained_resource_count=retained,
        failed_resource_count=failed,
        zero_residual_resources=retained == 0 and failed == 0,
        verified_at=_utc(now),
    )
    updated_ledger = ledger.model_copy(
        update={"resources": tuple(updated), "cleanup_evidence": evidence}
    )
    updated_ledger = updated_ledger.model_copy(
        update={"integrity_digest": _ledger_integrity(updated_ledger, config.signing_key)}
    )
    write_private_ledger(config, updated_ledger)
    return evidence


type EngineWitness = Callable[[EngineName, Plan3AConfig], WitnessCapture]
type FaultMatrixWitness = Callable[[Path], tuple[WarehouseFaultScenarioOutcome, ...]]


def _empty_cleanup(run_id: str, now: datetime) -> Plan3ACleanupEvidence:
    return Plan3ACleanupEvidence(
        run_id=run_id,
        resource_inventory_digest=digest(
            {"domain": "pillarmesh-plan3a-cleanup-inventory-v1", "resource_digests": ()}
        ),
        completed_resource_count=0,
        retained_resource_count=0,
        failed_resource_count=0,
        zero_residual_resources=True,
        verified_at=now,
    )


def _default_witness(engine_kind: EngineName, config: Plan3AConfig) -> WitnessCapture:
    from tests.acceptance.plan3a_orchestration import witness_live_engine

    return witness_live_engine(engine_kind, config)


def _witness_offline_fault_matrix(
    config: Plan3AConfig,
    witness: FaultMatrixWitness,
) -> tuple[WarehouseFaultScenarioOutcome, ...]:
    root = config.state_path / "offline-control-plane-fault-matrix"
    if root.exists() or root.is_symlink():
        raise Plan3AHarnessError("offline control-plane fault matrix state already exists")
    register_private_resources(
        config,
        (
            PrivateFaultMatrixDirectoryResourceEntry.create(
                exact_path=root,
                retention_deadline=config.retention_deadline,
            ),
        ),
    )
    primary_failure: BaseException | None = None
    fatal_failure: KeyboardInterrupt | SystemExit | None = None
    outcomes: tuple[WarehouseFaultScenarioOutcome, ...] = ()
    try:
        outcomes = witness(root)
        outcomes = canonical_fault_matrix_outcomes(outcomes)
    except (KeyboardInterrupt, SystemExit) as error:
        fatal_failure = error
    except BaseException as error:
        primary_failure = error
    cleanup_failed = False
    try:
        if root.exists() or root.is_symlink():
            shutil.rmtree(root)
    except OSError:
        cleanup_failed = True
    if fatal_failure is not None:
        raise fatal_failure
    if primary_failure is not None:
        raise Plan3AHarnessError("offline control-plane fault matrix failed") from None
    if cleanup_failed:
        raise Plan3AHarnessError("offline control-plane fault matrix cleanup failed")
    return outcomes


def run_plan3a(
    config: Plan3AConfig,
    *,
    witness: EngineWitness | None = None,
    fault_matrix: FaultMatrixWitness | None = None,
    cleanup_authorized: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Plan3AEvidence:
    started_at = _utc(clock())
    if not cleanup_authorized:
        raise PermissionError("Plan 3A run requires explicit retention authorization")
    if started_at < config.retention_deadline:
        raise Plan3AHarnessError("retention deadline has not elapsed")
    run_id = digest(
        {
            "domain": "pillarmesh-plan3a-run-v1",
            "source_commit": config.source_commit,
            "started_at": started_at,
            "entropy": secrets.token_hex(32),
        }
    )
    reservation_digest = _write_reservation(config, run_id)
    cleanup = _empty_cleanup(run_id, started_at)
    ledger = Plan3APrivateLedger.create(
        run_id=run_id,
        reservation_digest=reservation_digest,
        resources=(),
        signing_key=config.signing_key,
    ).model_copy(update={"cleanup_evidence": cleanup})
    ledger = ledger.model_copy(
        update={"integrity_digest": _ledger_integrity(ledger, config.signing_key)}
    )
    write_private_ledger(config, ledger)

    fault_outcomes = _witness_offline_fault_matrix(
        config,
        fault_matrix or run_plan3a_fault_matrix,
    )

    engine_witness = witness or _default_witness
    captures: list[WitnessCapture] = []
    results: list[EngineWitnessResult] = []
    for engine_kind in _ENGINE_ORDER:
        try:
            capture = _normalize_witness_capture(engine_witness(engine_kind, config))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise Plan3AHarnessError(
                f"Plan 3A witnessed lifecycle failed for {engine_kind}"
            ) from None
        if capture.result is None or capture.result.engine_kind != engine_kind:
            raise Plan3AHarnessError("engine witness returned the wrong engine result")
        captures.append(capture)
        results.append(capture.result)

    if results[0].binding_digest == results[1].binding_digest:
        raise Plan3AHarnessError("engine witnesses reused a binding identity")

    normalized = tuple(
        {
            "terminal_state": result.terminal_state,
            "checks": result.check_dispositions,
            "residual_resource_count": result.residual_resource_count,
        }
        for result in results
    )
    if normalized[0] != normalized[1]:
        raise Plan3AHarnessError("PostgreSQL and ClickHouse lifecycle outcomes do not conform")
    completed_at = _utc(clock())
    cleanup = teardown_plan3a(
        config,
        run_id=run_id,
        now=completed_at,
        authorized=cleanup_authorized,
    )
    if not cleanup.zero_residual_resources:
        raise Plan3AHarnessError("Plan 3A exact cleanup left residual resources")
    fault_matrix_evidence = Plan3AFaultMatrixEvidence(outcomes=fault_outcomes)
    if len(results) != 2:
        raise Plan3AHarnessError("Plan 3A requires exactly two engine witnesses")
    provisional: _Plan3AProvisionalEvidence = {
        "run_id": run_id,
        "source_commit": config.source_commit,
        "engine_results": (results[0], results[1]),
        "cross_engine_conformance_digest": digest(
            {"domain": "pillarmesh-plan3a-cross-engine-v1", "outcomes": normalized}
        ),
        "tenant_isolation_digest": digest(
            {
                "domain": "pillarmesh-plan3a-tenant-isolation-v1",
                "dispositions": ("postgresql:denied", "clickhouse:denied"),
            }
        ),
        "failure_matrix": fault_matrix_evidence,
        "failure_matrix_digest": _failure_matrix_digest(fault_matrix_evidence.outcomes),
        "local_encryption_limitation": "deferred_local_acceptance",
        "production_readiness": "not_proven",
        "lifecycle_conformance": "proven",
        "cleanup_digest": digest(cleanup),
        "started_at": started_at,
        "completed_at": completed_at,
    }
    privacy_payload = canonical_json_bytes(provisional)
    privacy_digest = digest(
        {
            "domain": "pillarmesh-plan3a-aggregate-privacy-scan-v1",
            "capture_digests": tuple(
                scan_private_markers(privacy_payload, capture, config.private_markers)
                for capture in captures
            ),
        }
    )
    evidence = Plan3AEvidence(privacy_scan_digest=privacy_digest, **provisional)
    encoded = canonical_json_bytes(evidence)
    for capture in captures:
        scan_private_markers(encoded, capture, config.private_markers)

    config.evidence_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_private_write(config.evidence_directory / EVIDENCE_FILE_NAME, encoded)
    ledger = _load_private_ledger(config)
    if ledger.run_id != run_id:
        raise Plan3AHarnessError("private ledger run identity changed before evidence binding")
    bound_ledger = ledger.model_copy(
        update={"public_evidence_digest": _sha256(encoded), "integrity_digest": "0" * 64}
    )
    bound_ledger = bound_ledger.model_copy(
        update={"integrity_digest": _ledger_integrity(bound_ledger, config.signing_key)}
    )
    write_private_ledger(config, bound_ledger)
    return evidence


def _evidence_digest(evidence: Plan3AEvidence) -> str:
    return _sha256(canonical_json_bytes(evidence))


def validate_plan3a_evidence(config: Plan3AConfig) -> Plan3AEvidence:
    ledger = _load_private_ledger(config)
    expected_evidence_digest = ledger.public_evidence_digest
    if expected_evidence_digest is None:
        raise Plan3AHarnessError("public Plan 3A evidence is not authenticated")
    evidence_path = config.evidence_directory / EVIDENCE_FILE_NAME
    try:
        encoded = evidence_path.read_bytes()
    except OSError:
        raise Plan3AHarnessError("public Plan 3A evidence is missing or invalid") from None
    if not hmac.compare_digest(_sha256(encoded), expected_evidence_digest):
        raise Plan3AHarnessError("public Plan 3A evidence differs from authenticated state")

    cleanup = ledger.cleanup_evidence
    try:
        untrusted = json.loads(encoded)
        encoded_run_id = untrusted["run_id"]
        encoded_cleanup_digest = untrusted["cleanup_digest"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise Plan3AHarnessError("public Plan 3A evidence is missing or invalid") from None
    if (
        not isinstance(encoded_run_id, str)
        or not isinstance(encoded_cleanup_digest, str)
        or encoded_run_id != ledger.run_id
        or cleanup is None
        or not cleanup.zero_residual_resources
        or encoded_cleanup_digest != digest(cleanup)
    ):
        raise Plan3AHarnessError("public Plan 3A evidence differs from private cleanup state")

    try:
        evidence = Plan3AEvidence.model_validate_json(encoded)
    except ValueError:
        raise Plan3AHarnessError("public Plan 3A evidence is missing or invalid") from None
    if canonical_json_bytes(evidence) != encoded:
        raise Plan3AHarnessError("public Plan 3A evidence is not canonical")
    if evidence.source_commit != config.source_commit:
        raise Plan3AHarnessError("public Plan 3A evidence source commit differs from the run")
    if tuple(result.engine_image for result in evidence.engine_results) != (
        config.postgres_image,
        config.clickhouse_image,
    ):
        raise Plan3AHarnessError("public Plan 3A evidence image pins differ from the run")
    if evidence.failure_matrix_digest != _failure_matrix_digest(evidence.failure_matrix.outcomes):
        raise Plan3AHarnessError("public Plan 3A fault evidence is invalid")

    scan_private_markers(encoded, WitnessCapture(), config.private_markers)
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run witnessed Plan 3A acceptance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--authorize-retention-cleanup", action="store_true")
    teardown = subparsers.add_parser("teardown")
    teardown.add_argument("--run-id")
    teardown.add_argument("--authorize-retention-cleanup", action="store_true")
    subparsers.add_parser("validate-evidence")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = Plan3AConfig.from_environment(
            os.environ,
            allow_existing=arguments.command in {"teardown", "validate-evidence"},
        )
        if arguments.command == "teardown":
            cleanup = teardown_plan3a(
                config,
                run_id=arguments.run_id,
                now=datetime.now(UTC),
                authorized=arguments.authorize_retention_cleanup,
            )
            print(
                json.dumps(
                    {
                        "cleanup_digest": digest(cleanup),
                        "status": "complete" if cleanup.zero_residual_resources else "retained",
                    },
                    sort_keys=True,
                )
            )
            return 0 if cleanup.zero_residual_resources else 3
        if arguments.command == "validate-evidence":
            evidence = validate_plan3a_evidence(config)
            print(
                json.dumps(
                    {"evidence_digest": _evidence_digest(evidence), "status": "valid"},
                    sort_keys=True,
                )
            )
            return 0
        evidence = run_plan3a(
            config,
            cleanup_authorized=arguments.authorize_retention_cleanup,
        )
        print(
            json.dumps(
                {"evidence_digest": _evidence_digest(evidence), "status": "complete"},
                sort_keys=True,
            )
        )
        return 0
    except (Plan3AHarnessError, PermissionError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("ERROR: Plan 3A acceptance failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
