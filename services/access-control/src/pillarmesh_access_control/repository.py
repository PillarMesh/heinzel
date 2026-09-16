from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from pillarmesh_contract_model import canonical_bytes, digest

from .models import (
    CurrentEntitlementSnapshot,
    EnterpriseEntitlementAssertion,
    EnterpriseEntitlementObservation,
)


class EntitlementObservationConflict(ValueError):
    pass


class EntitlementObservationRollback(ValueError):
    pass


class EntitlementSnapshotIntegrityError(ValueError):
    pass


class SQLiteEntitlementRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    @classmethod
    def open(cls, path: str | Path) -> SQLiteEntitlementRepository:
        return cls(sqlite3.connect(str(path)))

    def close(self) -> None:
        self._connection.close()

    def record_observation(
        self,
        assertion: EnterpriseEntitlementAssertion,
        *,
        recorded_at: datetime,
    ) -> EnterpriseEntitlementObservation:
        provenance = assertion.provenance
        key = (
            assertion.tenant_id,
            provenance.connected_authority_ref,
            assertion.principal_ref,
            assertion.purpose_digest,
        )
        assertion_digest = assertion.assertion_digest()
        observation = EnterpriseEntitlementObservation(
            **assertion.model_dump(mode="python"),
            observation_id=f"entitlement-observation:{assertion_digest}",
            recorded_at=recorded_at,
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            latest = self._connection.execute(
                """
                SELECT source_revision, assertion_digest, payload
                FROM entitlement_observations
                WHERE tenant_id = ? AND connected_authority_ref = ?
                  AND principal_ref = ? AND purpose_digest = ?
                ORDER BY source_revision DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
            if latest is not None:
                latest_revision = int(latest[0])
                if provenance.source_revision < latest_revision:
                    raise EntitlementObservationRollback("source revision rollback")
                if provenance.source_revision == latest_revision:
                    if str(latest[1]) != assertion_digest:
                        raise EntitlementObservationConflict("source revision equivocation")
                    existing = EnterpriseEntitlementObservation.model_validate_json(
                        bytes(latest[2]), strict=True
                    )
                    self._connection.commit()
                    return existing
            self._connection.execute(
                """
                INSERT INTO entitlement_observations (
                    observation_id, tenant_id, connected_authority_ref, principal_ref,
                    purpose_digest, source_revision, assertion_digest, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    *key,
                    provenance.source_revision,
                    assertion_digest,
                    canonical_bytes(observation),
                ),
            )
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return observation

    def record_snapshot(
        self,
        observation: EnterpriseEntitlementObservation,
        *,
        resolved_at: datetime,
    ) -> CurrentEntitlementSnapshot:
        values: dict[str, object] = {
            "tenant_id": observation.tenant_id,
            "principal_ref": observation.principal_ref,
            "purpose_digest": observation.purpose_digest,
            "connected_authority_ref": observation.provenance.connected_authority_ref,
            "source_revision": observation.provenance.source_revision,
            "source_payload_digest": observation.provenance.source_payload_digest,
            "observation_id": observation.observation_id,
            "product_version_refs": observation.product_version_refs,
            "semantic_refs": observation.semantic_refs,
            "filter_domains": observation.filter_domains,
            "permissions": observation.permissions,
            "effective_at": observation.effective_at,
            "valid_until": observation.valid_until,
        }
        semantic_values = dict(values)
        semantic_values.pop("observation_id")
        snapshot_digest = digest({"schema_version": "1", **semantic_values})
        snapshot = CurrentEntitlementSnapshot.model_validate(
            {
                **values,
                "snapshot_id": f"entitlement-snapshot:{snapshot_digest}",
                "snapshot_digest": snapshot_digest,
                "resolved_at": resolved_at,
            }
        )
        existing = self._connection.execute(
            """
            SELECT snapshot_id, snapshot_digest, tenant_id, principal_ref, purpose_digest, payload
            FROM entitlement_snapshots
            WHERE snapshot_digest = ?
            """,
            (snapshot_digest,),
        ).fetchone()
        if existing is not None:
            try:
                stored = CurrentEntitlementSnapshot.model_validate_json(
                    bytes(existing[5]), strict=True
                )
            except ValueError as error:
                raise EntitlementSnapshotIntegrityError("snapshot payload is invalid") from error
            indexed_scope = tuple(str(value) for value in existing[:5])
            payload_scope = (
                stored.snapshot_id,
                stored.snapshot_digest,
                stored.tenant_id,
                stored.principal_ref,
                stored.purpose_digest,
            )
            if indexed_scope != payload_scope or stored.snapshot_digest != snapshot.snapshot_digest:
                raise EntitlementSnapshotIntegrityError("snapshot index does not match payload")
            return stored
        self._connection.execute(
            """
            INSERT INTO entitlement_snapshots (
                snapshot_id, snapshot_digest, tenant_id, principal_ref, purpose_digest, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.snapshot_digest,
                snapshot.tenant_id,
                snapshot.principal_ref,
                snapshot.purpose_digest,
                canonical_bytes(snapshot),
            ),
        )
        self._connection.commit()
        return snapshot

    def load_latest_observation(
        self,
        *,
        tenant_id: str,
        connected_authority_ref: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> EnterpriseEntitlementObservation:
        row = self._connection.execute(
            """
            SELECT payload
            FROM entitlement_observations
            WHERE tenant_id = ? AND connected_authority_ref = ?
              AND principal_ref = ? AND purpose_digest = ?
            ORDER BY source_revision DESC
            LIMIT 1
            """,
            (tenant_id, connected_authority_ref, principal_ref, purpose_digest),
        ).fetchone()
        if row is None:
            raise KeyError("entitlement observation not found")
        return EnterpriseEntitlementObservation.model_validate_json(bytes(row[0]), strict=True)

    def list_observations(
        self,
        *,
        tenant_id: str,
        connected_authority_ref: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> tuple[EnterpriseEntitlementObservation, ...]:
        rows = self._connection.execute(
            """
            SELECT payload
            FROM entitlement_observations
            WHERE tenant_id = ? AND connected_authority_ref = ?
              AND principal_ref = ? AND purpose_digest = ?
            ORDER BY source_revision
            """,
            (tenant_id, connected_authority_ref, principal_ref, purpose_digest),
        ).fetchall()
        return tuple(
            EnterpriseEntitlementObservation.model_validate_json(bytes(row[0]), strict=True)
            for row in rows
        )

    def count_observations(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM entitlement_observations").fetchone()
        assert row is not None
        return int(row[0])

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entitlement_observations (
                observation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connected_authority_ref TEXT NOT NULL,
                principal_ref TEXT NOT NULL,
                purpose_digest TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                assertion_digest TEXT NOT NULL,
                payload BLOB NOT NULL,
                UNIQUE (
                    tenant_id, connected_authority_ref, principal_ref,
                    purpose_digest, source_revision
                )
            );
            CREATE TABLE IF NOT EXISTS entitlement_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                snapshot_digest TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                principal_ref TEXT NOT NULL,
                purpose_digest TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            """
        )
        self._connection.commit()
