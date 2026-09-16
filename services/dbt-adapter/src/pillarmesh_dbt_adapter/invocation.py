from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .process_environment import build_dbt_process_environment

DBT_CORE_VERSION: Literal["1.10.13"] = "1.10.13"
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
type DbtProvider = Literal["postgresql", "clickhouse"]
type DbtQualityDisposition = Literal["not_asserted", "passed", "limited"]


class DbtColumnTest(ArtifactModel):
    column_name: Identifier
    kind: Literal["not_null", "unique"]


class DbtDecimalMagnitudeCheck(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    column_name: Identifier
    precision: Literal[57] = 57
    scale: Literal[9] = 9


class CompiledDbtModel(ArtifactModel):
    schema_version: Literal["2"] = "2"
    model_name: Identifier
    contract_digest: Digest
    provider: DbtProvider
    input_generation_digests: tuple[Digest, ...] = Field(min_length=1)
    target_schema: Identifier
    output_columns: tuple[Identifier, ...] = ()
    output_magnitude_checks: tuple[DbtDecimalMagnitudeCheck, ...] = ()
    quality_tests: tuple[DbtColumnTest, ...] = ()
    compiled_sql: str = Field(min_length=1)

    @model_validator(mode="after")
    def quality_tests_are_unique(self) -> CompiledDbtModel:
        identities = tuple((test.column_name, test.kind) for test in self.quality_tests)
        if len(identities) != len(set(identities)):
            raise ValueError("dbt column tests must be unique")
        if len(self.output_columns) != len(set(self.output_columns)):
            raise ValueError("dbt output columns must be unique")
        checked_columns = tuple(check.column_name for check in self.output_magnitude_checks)
        if len(checked_columns) != len(set(checked_columns)):
            raise ValueError("dbt output magnitude checks must be unique")
        if any(column not in self.output_columns for column in checked_columns):
            raise ValueError("dbt output magnitude check requires a declared output column")
        return self


class SignedCompiledDbtModel(ArtifactModel):
    schema_version: Literal["2"] = "2"
    model: CompiledDbtModel
    model_digest: Digest
    key_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)


def compiled_dbt_model_signing_bytes(model: CompiledDbtModel) -> bytes:
    return canonical_bytes(_validated_compiled_model(model))


def _validated_compiled_model(model: CompiledDbtModel) -> CompiledDbtModel:
    try:
        _require_declared_model_fields(model)
        for nested_model in (*model.quality_tests, *model.output_magnitude_checks):
            _require_declared_model_fields(nested_model)
        return CompiledDbtModel.model_validate(model.model_dump(mode="python"), strict=True)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("compiled dbt model is invalid") from None


def _require_declared_model_fields(model: BaseModel) -> None:
    if not set(vars(model)).issubset(type(model).model_fields):
        raise ValueError("model contains undeclared fields")


class DbtInvocationAuthority(ArtifactModel):
    contract_digest: Digest
    provider: DbtProvider
    input_generation_digests: tuple[Digest, ...] = Field(min_length=1)


@dataclass(frozen=True)
class DbtInvocationSpec:
    required_dbt_version: Literal["1.10.13"]
    arguments: tuple[str, ...]
    model: CompiledDbtModel


@dataclass(frozen=True)
class DbtProcessResult:
    return_code: int
    observed_dbt_version: str
    manifest: bytes
    test_results: bytes
    lineage: bytes


class DbtRunner(Protocol):
    def run(self, invocation: DbtInvocationSpec) -> DbtProcessResult: ...


class DbtSubprocessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    executable: Path
    profiles_directory: Path
    workspace_directory: Path
    timeout_seconds: int = Field(gt=0, le=900)
    credential_environment_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def boundaries_exist(self) -> DbtSubprocessSettings:
        if not self.executable.is_file():
            raise ValueError("dbt executable does not exist")
        if not self.profiles_directory.is_dir():
            raise ValueError("dbt profiles directory does not exist")
        if not self.workspace_directory.is_dir():
            raise ValueError("dbt workspace directory does not exist")
        return self


class SubprocessDbtRunner:
    def __init__(self, settings: DbtSubprocessSettings) -> None:
        self._settings = settings

    def run(self, invocation: DbtInvocationSpec) -> DbtProcessResult:
        with tempfile.TemporaryDirectory(
            prefix="pillarmesh-dbt-",
            dir=self._settings.workspace_directory,
        ) as operation_directory_text:
            operation_directory = Path(operation_directory_text)
            environment = build_dbt_process_environment(
                source_environment=os.environ,
                credential_names=self._settings.credential_environment_names,
                temporary_directory=operation_directory,
            )
            observed_version = self._observed_version(environment)
            models_directory = operation_directory / "models"
            target_directory = operation_directory / "target"
            logs_directory = operation_directory / "logs"
            models_directory.mkdir(mode=0o700)
            target_directory.mkdir(mode=0o700)
            logs_directory.mkdir(mode=0o700)
            (operation_directory / "dbt_project.yml").write_text(
                "name: pillarmesh_materialization\n"
                "version: '1.0.0'\n"
                "config-version: 2\n"
                "profile: pillarmesh_materialization\n"
                "model-paths: ['models']\n"
                "models:\n"
                "  pillarmesh_materialization:\n"
                "    +materialized: table\n",
                encoding="utf-8",
            )
            (models_directory / f"{invocation.model.model_name}.sql").write_text(
                invocation.model.compiled_sql + "\n",
                encoding="utf-8",
            )
            if invocation.model.quality_tests:
                (models_directory / "schema.yml").write_text(
                    _quality_test_schema(invocation.model),
                    encoding="utf-8",
                )
            command = (
                str(self._settings.executable),
                *invocation.arguments[1:],
                "--project-dir",
                str(operation_directory),
                "--profiles-dir",
                str(self._settings.profiles_directory),
                "--target-path",
                str(target_directory),
                "--log-path",
                str(logs_directory),
            )
            result = subprocess.run(
                command,
                cwd=operation_directory,
                env=environment,
                capture_output=True,
                timeout=self._settings.timeout_seconds,
                check=False,
            )
            manifest = _read_artifact(target_directory / "manifest.json")
            test_results = _read_artifact(target_directory / "run_results.json")
            lineage = _lineage_artifact(manifest)
            return DbtProcessResult(
                return_code=result.returncode,
                observed_dbt_version=observed_version,
                manifest=manifest,
                test_results=test_results,
                lineage=lineage,
            )

    def _observed_version(self, environment: dict[str, str]) -> str:
        result = subprocess.run(
            (str(self._settings.executable), "--version"),
            env=environment,
            capture_output=True,
            timeout=self._settings.timeout_seconds,
            check=False,
            text=True,
        )
        output = result.stdout + result.stderr
        match = re.search(r"installed:\s*([0-9]+\.[0-9]+\.[0-9]+)", output)
        return "unavailable" if match is None else match.group(1)


def _read_artifact(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _quality_test_schema(model: CompiledDbtModel) -> str:
    tests_by_column: dict[str, list[str]] = {}
    for test in model.quality_tests:
        tests_by_column.setdefault(test.column_name, []).append(test.kind)
    lines = ["version: 2", "models:", f"  - name: {model.model_name}", "    columns:"]
    for column_name, tests in tests_by_column.items():
        lines.extend((f"      - name: {column_name}", "        data_tests:"))
        lines.extend(f"          - {test}" for test in tests)
    return "\n".join(lines) + "\n"


def _lineage_artifact(manifest: bytes) -> bytes:
    try:
        parsed = json.loads(manifest)
        nodes = parsed["nodes"]
        parent_map = parsed["parent_map"]
        if not isinstance(nodes, dict) or not isinstance(parent_map, dict):
            raise ValueError
        node_names = sorted(str(name) for name in nodes)
        edges = sorted(
            (str(parent), str(child))
            for child, parents in parent_map.items()
            if isinstance(parents, list)
            for parent in parents
        )
        return canonical_bytes({"nodes": node_names, "edges": edges})
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return b""


class DbtFailureClassification(StrEnum):
    INVALID_SIGNATURE = "invalid_signature"
    AUTHORITY_MISMATCH = "authority_mismatch"
    INVOCATION_FAILED = "invocation_failed"
    NONZERO_EXIT = "nonzero_exit"
    MALFORMED_OUTPUT = "malformed_output"


class DbtInvocationError(RuntimeError):
    def __init__(self, classification: DbtFailureClassification, message: str) -> None:
        super().__init__(message)
        self.classification = classification


class DbtInvocationReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    model_digest: Digest
    contract_digest: Digest
    provider: DbtProvider
    input_generation_digests: tuple[Digest, ...]
    dbt_version: Literal["1.10.13"]
    manifest_digest: Digest
    run_results_digest: Digest
    lineage_digest: Digest
    quality_assertion_count: int = Field(ge=0)
    quality_disposition: DbtQualityDisposition


class DbtInvoker:
    def __init__(
        self,
        *,
        trusted_compiler_keys: dict[str, Ed25519PublicKey],
        runner: DbtRunner,
    ) -> None:
        self._trusted_compiler_keys = dict(trusted_compiler_keys)
        self._runner = runner

    def invoke(
        self,
        *,
        signed_model: SignedCompiledDbtModel,
        authority: DbtInvocationAuthority,
    ) -> DbtInvocationReceipt:
        signed_model = self._validated_signed_model(signed_model)
        self._verify_signature(signed_model)
        self._verify_authority(signed_model.model, authority)
        invocation = DbtInvocationSpec(
            required_dbt_version=DBT_CORE_VERSION,
            arguments=(
                "dbt",
                "build",
                "--no-use-colors",
                "--select",
                signed_model.model.model_name,
                "--target",
                signed_model.model.provider,
            ),
            model=signed_model.model,
        )
        try:
            result = self._runner.run(invocation)
        except Exception:
            raise DbtInvocationError(
                DbtFailureClassification.INVOCATION_FAILED,
                "dbt invocation failed before an exit status was observed",
            ) from None
        if result.return_code != 0:
            raise DbtInvocationError(
                DbtFailureClassification.NONZERO_EXIT,
                f"dbt invocation exited with status {result.return_code}",
            )
        self._validate_output(result)
        quality_assertion_count, quality_disposition = _quality_observation(result.test_results)
        return DbtInvocationReceipt(
            model_digest=signed_model.model_digest,
            contract_digest=signed_model.model.contract_digest,
            provider=signed_model.model.provider,
            input_generation_digests=signed_model.model.input_generation_digests,
            dbt_version=DBT_CORE_VERSION,
            manifest_digest=sha256(result.manifest).hexdigest(),
            run_results_digest=sha256(result.test_results).hexdigest(),
            lineage_digest=sha256(result.lineage).hexdigest(),
            quality_assertion_count=quality_assertion_count,
            quality_disposition=quality_disposition,
        )

    @staticmethod
    def _validated_signed_model(
        signed_model: SignedCompiledDbtModel,
    ) -> SignedCompiledDbtModel:
        try:
            _require_declared_model_fields(signed_model)
            _validated_compiled_model(signed_model.model)
            return SignedCompiledDbtModel.model_validate(
                signed_model.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise DbtInvocationError(
                DbtFailureClassification.INVALID_SIGNATURE,
                "compiled model payload is invalid",
            ) from None

    def _verify_signature(self, signed_model: SignedCompiledDbtModel) -> None:
        if digest(signed_model.model) != signed_model.model_digest:
            raise DbtInvocationError(
                DbtFailureClassification.INVALID_SIGNATURE,
                "compiled model digest does not match its signed payload",
            )
        public_key = self._trusted_compiler_keys.get(signed_model.key_id)
        if public_key is None:
            raise DbtInvocationError(
                DbtFailureClassification.INVALID_SIGNATURE,
                "compiled model was not signed by a trusted compiler key",
            )
        try:
            signature = base64.b64decode(signed_model.signature, validate=True)
            public_key.verify(signature, compiled_dbt_model_signing_bytes(signed_model.model))
        except (binascii.Error, InvalidSignature, ValueError) as error:
            raise DbtInvocationError(
                DbtFailureClassification.INVALID_SIGNATURE,
                "compiled model signature is invalid",
            ) from error

    @staticmethod
    def _verify_authority(
        model: CompiledDbtModel,
        authority: DbtInvocationAuthority,
    ) -> None:
        expected_schema = f"contract_{authority.contract_digest[:54]}"
        if (
            model.contract_digest != authority.contract_digest
            or model.provider != authority.provider
            or model.input_generation_digests != authority.input_generation_digests
            or model.target_schema != expected_schema
        ):
            raise DbtInvocationError(
                DbtFailureClassification.AUTHORITY_MISMATCH,
                "compiled model does not match invocation authority",
            )

    @staticmethod
    def _validate_output(result: DbtProcessResult) -> None:
        if result.observed_dbt_version != DBT_CORE_VERSION:
            raise DbtInvocationError(
                DbtFailureClassification.MALFORMED_OUTPUT,
                "dbt invocation did not report the required pinned version",
            )
        required_keys = (
            (result.manifest, {"metadata", "nodes"}),
            (result.test_results, {"results"}),
            (result.lineage, {"nodes", "edges"}),
        )
        try:
            for payload, keys in required_keys:
                parsed = json.loads(payload)
                if not isinstance(parsed, dict) or not keys.issubset(parsed):
                    raise ValueError("required dbt artifact keys are absent")
            _quality_observation(result.test_results)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise DbtInvocationError(
                DbtFailureClassification.MALFORMED_OUTPUT,
                "dbt invocation returned malformed artifacts",
            ) from error


def _quality_observation(run_results: bytes) -> tuple[int, DbtQualityDisposition]:
    parsed = json.loads(run_results)
    results = parsed["results"]
    if not isinstance(results, list):
        raise ValueError("dbt run results are invalid")
    statuses: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("dbt run result is invalid")
        unique_id = result.get("unique_id")
        if isinstance(unique_id, str) and unique_id.startswith("test."):
            status = result.get("status")
            if not isinstance(status, str):
                raise ValueError("dbt test result status is invalid")
            statuses.append(status)
    if not statuses:
        return 0, "not_asserted"
    return len(statuses), "passed" if all(status == "pass" for status in statuses) else "limited"
