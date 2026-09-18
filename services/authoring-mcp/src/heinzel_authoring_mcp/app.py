from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import IntegrationContract, digest
from heinzel_contract_service import ContractService
from heinzel_evidence import (
    PackageMetadata,
    PackageResult,
    ScanInput,
    SQLiteStore,
    export_package,
    verify_package,
)
from heinzel_execution_graph import GraphSigner, GraphVerifier
from heinzel_provider_postgresql import PostgresProvider, PostgresSettings
from heinzel_provider_snowflake import (
    SnowflakeProvider,
    SnowflakeSettings,
    encode_segment,
)
from heinzel_runtime import Runtime
from pydantic import BaseModel

from .settings import AppSettings


def structured(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, tuple):
        return [structured(item) for item in value]
    return value


class AuthoringApplication:
    def __init__(
        self,
        contract_service: ContractService,
        runtime: Runtime,
        store: SQLiteStore | None = None,
        verifier: GraphVerifier | None = None,
    ) -> None:
        self._contract_service = contract_service
        self._runtime = runtime
        self._store = store
        self._verifier = verifier

    def create_draft(self, value: dict[str, object]) -> object:
        contract = self._contract_service.create_draft(IntegrationContract.model_validate(value))
        return {"contract": structured(contract), "contract_digest": digest(contract)}

    def get_draft(self, contract_id: str, version: int) -> object:
        return structured(self._contract_service.get_draft(contract_id, version))

    def verify(self, contract_id: str, version: int) -> object:
        result = self._contract_service.verify(contract_id, version)
        if hasattr(result, "signed_graph"):
            return {"activation_summary": structured(result), "summary_digest": digest(result)}
        return {"legality_decision": structured(result), "decision_digest": digest(result)}

    def get_activation_summary(self, summary_digest: str) -> object:
        summary = self._contract_service.get_activation_summary(summary_digest)
        return {"activation_summary": structured(summary), "summary_digest": digest(summary)}

    def activate(self, contract_digest: str, summary_digest: str, acceptance_key: int) -> object:
        run = self._contract_service.activate(contract_digest, summary_digest, acceptance_key)
        if run.state == "created":
            summary = self._contract_service.get_activation_summary(summary_digest)
            self._runtime.execute(run.run_id, summary.signed_graph, acceptance_key)
        elif run.state == "running":
            self._runtime.resume(run.run_id)
        return structured(self._contract_service.get_run(run.run_id))

    def get_run(self, run_id: str) -> object:
        return structured(self._contract_service.get_run(run_id))

    def get_trace(self, run_id: str) -> object:
        return structured(self._contract_service.get_trace(run_id))

    @staticmethod
    def _package_result(result: PackageResult) -> object:
        return {
            "package_index_digest": result.package_index_digest,
            "verification_result_digest": result.verification_result_digest,
            "checks": structured(result.checks),
        }

    def export_evidence(
        self,
        run_id: str,
        package_dir: Path,
        metadata: PackageMetadata,
        scan_input: ScanInput,
    ) -> object:
        if self._store is None or self._verifier is None:
            raise RuntimeError("evidence package support is not configured")
        result = export_package(
            self._store,
            run_id,
            package_dir,
            metadata,
            scan_input,
            self._verifier,
        )
        return self._package_result(result)

    def verify_evidence(self, package_dir: Path, scan_input: ScanInput) -> object:
        if self._verifier is None:
            raise RuntimeError("evidence package support is not configured")
        return self._package_result(verify_package(package_dir, self._verifier, scan_input))


def build_application(settings: AppSettings) -> AuthoringApplication:
    key_bytes = base64.b64decode(settings.signing_private_key_b64.get_secret_value(), validate=True)
    if len(key_bytes) != 32:
        raise ValueError("Ed25519 private key must encode exactly 32 bytes")
    signer = GraphSigner(settings.signing_key_id, Ed25519PrivateKey.from_private_bytes(key_bytes))
    postgres = PostgresProvider(
        PostgresSettings(
            dsn=settings.postgres_dsn,
            connection_handle=settings.postgres_connection_handle,
            schema_name=settings.postgres_schema,
            table_name=settings.postgres_table,
        )
    )
    snowflake = SnowflakeProvider(
        SnowflakeSettings(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            password=settings.snowflake_password,
            role=settings.snowflake_role,
            warehouse=settings.snowflake_warehouse,
            database=settings.snowflake_database,
            schema_name=settings.snowflake_schema,
            stage=settings.snowflake_stage,
            target_table=settings.snowflake_target_table,
            ledger_table=settings.snowflake_ledger_table,
            connection_handle=settings.snowflake_connection_handle,
        )
    )
    store = SQLiteStore.open(settings.state_path)
    contract_service = ContractService(
        store=store,
        signer=signer,
        source_resolver=lambda handle: _resolve(
            handle, settings.postgres_connection_handle, postgres
        ),
        destination_resolver=lambda handle: _resolve(
            handle, settings.snowflake_connection_handle, snowflake
        ),
    )
    verifier = GraphVerifier({settings.signing_key_id: signer.public_key})
    runtime = Runtime(
        store=store,
        verifier=verifier,
        source_resolver=lambda handle: _resolve(
            handle, settings.postgres_connection_handle, postgres
        ),
        destination_resolver=lambda handle: _resolve(
            handle, settings.snowflake_connection_handle, snowflake
        ),
        segment_encoder=encode_segment,
        output_dir=settings.output_dir,
    )
    return AuthoringApplication(contract_service, runtime, store, verifier)


def _resolve(handle: str, expected: str, provider: Any) -> Any:
    if handle != expected:
        raise KeyError(f"unknown connection handle: {handle}")
    return provider
