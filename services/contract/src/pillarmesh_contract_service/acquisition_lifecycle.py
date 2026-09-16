from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from pillarmesh_contract_model import ArtifactReference, canonical_bytes, digest

from .models import AcquisitionContractLifecycleState
from .process_models import (
    AcquisitionActivationApproval,
    ActivatedAcquisitionContract,
    ActivatedAcquisitionContractRecord,
)
from .source_observation import ValidatedSourceBinding


class AcquisitionContractLifecycleNotFoundError(LookupError):
    pass


class StaleAcquisitionContractLifecycleError(RuntimeError):
    pass


class AcquisitionContractActivationDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("activation authority is not validated")


class AcquisitionContractActivationConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("idempotency key has conflicting activation inputs")


class AcquisitionContractLifecycleRepository(Protocol):
    def activate_contract(
        self,
        *,
        idempotency_key: str,
        contract: ActivatedAcquisitionContract | Mapping[str, object],
        approval: AcquisitionActivationApproval | Mapping[str, object],
        source_validation: ValidatedSourceBinding | Mapping[str, object],
    ) -> ActivatedAcquisitionContractRecord: ...

    def get_contract(
        self, tenant_id: str, contract_ref: str, revision: int
    ) -> ActivatedAcquisitionContractRecord: ...

    def list_contracts(self, tenant_id: str) -> tuple[ActivatedAcquisitionContractRecord, ...]: ...

    def activate(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        activated_at: datetime,
    ) -> AcquisitionContractLifecycleState: ...

    def get(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState: ...

    def list_activated(
        self,
        tenant_id: str,
    ) -> tuple[AcquisitionContractLifecycleState, ...]: ...

    def retire(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        expected_revision: int,
        retired_at: datetime,
    ) -> AcquisitionContractLifecycleState: ...


class SQLiteAcquisitionContractLifecycleRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_contract_lifecycles ("
            "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, revision INTEGER NOT NULL, "
            "lifecycle_state TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, contract_digest))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS activated_acquisition_contracts ("
            "tenant_id TEXT NOT NULL, contract_ref TEXT NOT NULL, revision INTEGER NOT NULL, "
            "contract_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, contract_ref, revision), "
            "UNIQUE (tenant_id, contract_digest))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_activation_replays ("
            "tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, contract_ref TEXT NOT NULL, "
            "contract_revision INTEGER NOT NULL, PRIMARY KEY (tenant_id, idempotency_key), "
            "FOREIGN KEY (tenant_id, contract_ref, contract_revision) REFERENCES "
            "activated_acquisition_contracts(tenant_id, contract_ref, revision))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_activation_approvals ("
            "tenant_id TEXT NOT NULL, contract_ref TEXT NOT NULL, "
            "contract_revision INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, contract_ref, contract_revision), "
            "FOREIGN KEY (tenant_id, contract_ref, contract_revision) REFERENCES "
            "activated_acquisition_contracts(tenant_id, contract_ref, revision))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_source_validations ("
            "tenant_id TEXT NOT NULL, contract_ref TEXT NOT NULL, "
            "contract_revision INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, contract_ref, contract_revision), "
            "FOREIGN KEY (tenant_id, contract_ref, contract_revision) REFERENCES "
            "activated_acquisition_contracts(tenant_id, contract_ref, revision))"
        )
        self._connection.commit()

    def activate_contract(
        self,
        *,
        idempotency_key: str,
        contract: ActivatedAcquisitionContract | Mapping[str, object],
        approval: AcquisitionActivationApproval | Mapping[str, object],
        source_validation: ValidatedSourceBinding | Mapping[str, object],
    ) -> ActivatedAcquisitionContractRecord:
        candidate = ActivatedAcquisitionContract.model_validate(contract, strict=True)
        approved = AcquisitionActivationApproval.model_validate(approval, strict=True)
        validated_source = ValidatedSourceBinding.model_validate(source_validation, strict=True)
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                replay = self._load_contract_replay(
                    candidate.tenant_id,
                    idempotency_key,
                    candidate,
                    approved,
                    validated_source,
                )
                if replay is not None:
                    self._connection.commit()
                    return replay
                self._require_validated_authority(candidate, approved, validated_source)
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM activated_acquisition_contracts "
                    "WHERE tenant_id = ? AND contract_ref = ?",
                    (candidate.tenant_id, candidate.contract_ref),
                ).fetchone()
                if row is None:
                    raise RuntimeError("contract revision allocation did not return a revision")
                revision = int(row[0])
                self._connection.execute(
                    "INSERT INTO activated_acquisition_contracts "
                    "(tenant_id, contract_ref, revision, contract_digest, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.tenant_id,
                        candidate.contract_ref,
                        revision,
                        candidate.contract_digest,
                        canonical_bytes(candidate),
                    ),
                )
                authority_key = (candidate.tenant_id, candidate.contract_ref, revision)
                self._connection.execute(
                    "INSERT INTO acquisition_activation_approvals "
                    "(tenant_id, contract_ref, contract_revision, payload) VALUES (?, ?, ?, ?)",
                    (*authority_key, canonical_bytes(approved)),
                )
                self._connection.execute(
                    "INSERT INTO acquisition_source_validations "
                    "(tenant_id, contract_ref, contract_revision, payload) VALUES (?, ?, ?, ?)",
                    (*authority_key, canonical_bytes(validated_source)),
                )
                self._connection.execute(
                    "INSERT INTO acquisition_activation_replays "
                    "(tenant_id, idempotency_key, contract_ref, contract_revision) "
                    "VALUES (?, ?, ?, ?)",
                    (candidate.tenant_id, idempotency_key, candidate.contract_ref, revision),
                )
                self._connection.commit()
                return self._activation_record(revision, candidate)
            except BaseException:
                with suppress(sqlite3.Error):
                    self._connection.rollback()
                raise

    def get_contract(
        self, tenant_id: str, contract_ref: str, revision: int
    ) -> ActivatedAcquisitionContractRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT revision, payload FROM activated_acquisition_contracts "
                "WHERE tenant_id = ? AND contract_ref = ? AND revision = ?",
                (tenant_id, contract_ref, revision),
            ).fetchone()
        if row is None:
            raise AcquisitionContractLifecycleNotFoundError((tenant_id, contract_ref, revision))
        contract = ActivatedAcquisitionContract.model_validate_json(row[1], strict=True)
        return self._activation_record(int(row[0]), contract)

    def list_contracts(self, tenant_id: str) -> tuple[ActivatedAcquisitionContractRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision, payload FROM activated_acquisition_contracts WHERE tenant_id = ? "
                "ORDER BY contract_ref, revision",
                (tenant_id,),
            ).fetchall()
        return tuple(
            self._activation_record(
                int(row[0]),
                ActivatedAcquisitionContract.model_validate_json(row[1], strict=True),
            )
            for row in rows
        )

    def _load_contract_replay(
        self,
        tenant_id: str,
        idempotency_key: str,
        candidate: ActivatedAcquisitionContract,
        candidate_approval: AcquisitionActivationApproval,
        candidate_source: ValidatedSourceBinding,
    ) -> ActivatedAcquisitionContractRecord | None:
        row = self._connection.execute(
            "SELECT contract.revision, contract.payload, approval.payload, source.payload "
            "FROM acquisition_activation_replays AS replay "
            "INNER JOIN activated_acquisition_contracts AS contract "
            "ON contract.tenant_id = replay.tenant_id "
            "AND contract.contract_ref = replay.contract_ref "
            "AND contract.revision = replay.contract_revision "
            "INNER JOIN acquisition_activation_approvals AS approval "
            "ON approval.tenant_id = replay.tenant_id "
            "AND approval.contract_ref = replay.contract_ref "
            "AND approval.contract_revision = replay.contract_revision "
            "INNER JOIN acquisition_source_validations AS source "
            "ON source.tenant_id = replay.tenant_id "
            "AND source.contract_ref = replay.contract_ref "
            "AND source.contract_revision = replay.contract_revision "
            "WHERE replay.tenant_id = ? AND replay.idempotency_key = ?",
            (tenant_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        contract = ActivatedAcquisitionContract.model_validate_json(row[1], strict=True)
        approval = AcquisitionActivationApproval.model_validate_json(row[2], strict=True)
        source = ValidatedSourceBinding.model_validate_json(row[3], strict=True)
        self._require_validated_authority(contract, approval, source)
        if (
            canonical_bytes(candidate) != canonical_bytes(contract)
            or canonical_bytes(candidate_approval) != canonical_bytes(approval)
            or canonical_bytes(candidate_source) != canonical_bytes(source)
        ):
            raise AcquisitionContractActivationConflictError()
        return self._activation_record(int(row[0]), contract)

    @staticmethod
    def _activation_record(
        revision: int, contract: ActivatedAcquisitionContract
    ) -> ActivatedAcquisitionContractRecord:
        return ActivatedAcquisitionContractRecord(
            tenant_id=contract.tenant_id,
            contract_ref=contract.contract_ref,
            revision=revision,
            contract_digest=contract.contract_digest,
            contract_artifact_ref=ArtifactReference(
                artifact_id=contract.contract_ref,
                version=revision,
                digest=digest(contract),
            ),
            activated_by=contract.activated_by,
            activated_at=contract.activated_at,
            contract=contract,
        )

    @staticmethod
    def _require_validated_authority(
        contract: ActivatedAcquisitionContract,
        approval: AcquisitionActivationApproval,
        source: ValidatedSourceBinding,
    ) -> None:
        approval_matches = (
            contract.lifecycle_state == "activated"
            and approval.tenant_id == contract.tenant_id
            and approval.process_package_ref == contract.process_package_ref
            and approval.product_intent_ref == contract.product_intent_ref
            and approval.destination_product_ref == contract.destination_product_ref
            and approval.approved_by == contract.activated_by
            and approval.approved_at <= contract.activated_at
        )
        source_matches = (
            source.tenant_id == contract.tenant_id
            and source.source_binding_ref == contract.source_binding_ref
            and source.source_binding_revision == contract.source_binding_revision
            and source.credential_revision == contract.credential_revision
            and source.capability_profile_digest == contract.capability_profile_digest
            and source.source_observation_ref == contract.source_observation_ref
            and source.source_observation_digest == contract.source_observation_digest
            and source.validated_at <= contract.activated_at
        )
        if not approval_matches or not source_matches:
            raise AcquisitionContractActivationDeniedError()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def activate(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        activated_at: datetime,
    ) -> AcquisitionContractLifecycleState:
        candidate = AcquisitionContractLifecycleState(
            tenant_id=tenant_id,
            contract_digest=contract_digest,
            revision=1,
            lifecycle_state="activated",
            activated_at=activated_at,
            retired_at=None,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._load_optional(tenant_id, contract_digest)
                if existing is not None:
                    if existing.lifecycle_state != "activated":
                        raise StaleAcquisitionContractLifecycleError(
                            "retired acquisition contract cannot reactivate"
                        )
                    self._connection.commit()
                    return existing
                self._connection.execute(
                    "INSERT INTO acquisition_contract_lifecycles "
                    "(tenant_id, contract_digest, revision, lifecycle_state, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.tenant_id,
                        candidate.contract_digest,
                        candidate.revision,
                        candidate.lifecycle_state,
                        candidate.model_dump_json().encode(),
                    ),
                )
                self._connection.commit()
                return candidate
            except BaseException:
                self._connection.rollback()
                raise

    def get(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState:
        with self._lock:
            state = self._load_optional(tenant_id, contract_digest)
        if state is None:
            raise AcquisitionContractLifecycleNotFoundError((tenant_id, contract_digest))
        return state

    def retire(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        expected_revision: int,
        retired_at: datetime,
    ) -> AcquisitionContractLifecycleState:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._load_optional(tenant_id, contract_digest)
                if current is None:
                    raise AcquisitionContractLifecycleNotFoundError((tenant_id, contract_digest))
                if current.lifecycle_state == "retired":
                    if current.revision == expected_revision + 1:
                        self._connection.commit()
                        return current
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                if current.revision != expected_revision:
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                retired = current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "lifecycle_state": "retired",
                        "retired_at": retired_at,
                    }
                )
                retired = AcquisitionContractLifecycleState.model_validate(
                    retired.model_dump(),
                    strict=True,
                )
                result = self._connection.execute(
                    "UPDATE acquisition_contract_lifecycles "
                    "SET revision = ?, lifecycle_state = ?, payload = ? "
                    "WHERE tenant_id = ? AND contract_digest = ? AND revision = ? "
                    "AND lifecycle_state = 'activated'",
                    (
                        retired.revision,
                        retired.lifecycle_state,
                        retired.model_dump_json().encode(),
                        tenant_id,
                        contract_digest,
                        expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                self._connection.commit()
                return retired
            except BaseException:
                self._connection.rollback()
                raise

    def list_activated(
        self,
        tenant_id: str,
    ) -> tuple[AcquisitionContractLifecycleState, ...]:
        """Every contract currently activated for one tenant.

        This is the tenant half of deriving a run's tenant: a run carries a contract
        digest, and a contract digest is tenant-qualified only here. The filter is
        `activated` rather than "every row", because a retired contract is no longer
        a live contract of this tenant.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision, lifecycle_state, payload "
                "FROM acquisition_contract_lifecycles "
                "WHERE tenant_id = ? AND lifecycle_state = 'activated' "
                "ORDER BY contract_digest",
                (tenant_id,),
            ).fetchall()
        return tuple(self._state_from_row(row) for row in rows)

    def _state_from_row(self, row: tuple[Any, ...]) -> AcquisitionContractLifecycleState:
        state = AcquisitionContractLifecycleState.model_validate_json(row[2], strict=True)
        if state.revision != int(row[0]) or state.lifecycle_state != str(row[1]):
            raise RuntimeError("stored acquisition contract lifecycle authority mismatch")
        return state

    def _load_optional(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState | None:
        row = self._connection.execute(
            "SELECT revision, lifecycle_state, payload "
            "FROM acquisition_contract_lifecycles "
            "WHERE tenant_id = ? AND contract_digest = ?",
            (tenant_id, contract_digest),
        ).fetchone()
        if row is None:
            return None
        return self._state_from_row(row)
