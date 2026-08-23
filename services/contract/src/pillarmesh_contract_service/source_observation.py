from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from pillarmesh_contract_model import ArtifactModel, ArtifactReference, canonical_bytes, digest
from pydantic import Field, field_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class SourceObservation(ArtifactModel):
    observation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    source_ref: str = Field(min_length=1)
    schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime
    valid_until: datetime

    @field_validator("observed_at", "valid_until")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source observation timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)


class SQLiteSourceObservationRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS source_observations ("
            "tenant_id TEXT NOT NULL, observation_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, observation_id, version))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, observation: SourceObservation) -> SourceObservation:
        payload = canonical_bytes(observation)
        try:
            self._connection.execute(
                "INSERT INTO source_observations "
                "(tenant_id, observation_id, version, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, observation_id, version) DO NOTHING",
                (
                    observation.tenant_id,
                    observation.observation_id,
                    observation.version,
                    payload,
                ),
            )
            row = self._connection.execute(
                "SELECT payload FROM source_observations "
                "WHERE tenant_id = ? AND observation_id = ? AND version = ?",
                (observation.tenant_id, observation.observation_id, observation.version),
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("source observation identity is immutable")
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()
        return observation

    def load(self, tenant_id: str, observation_id: str, version: int) -> SourceObservation:
        row = self._connection.execute(
            "SELECT payload FROM source_observations "
            "WHERE tenant_id = ? AND observation_id = ? AND version = ?",
            (tenant_id, observation_id, version),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, observation_id, version))
        return SourceObservation.model_validate_json(row[0])

    def has_current(self, tenant_id: str, reference: ArtifactReference, *, now: datetime) -> bool:
        try:
            observation = self.load(tenant_id, reference.artifact_id, reference.version)
        except KeyError:
            return False
        expected = ArtifactReference(
            artifact_id=observation.observation_id,
            version=observation.version,
            digest=digest(observation),
        )
        return (
            reference == expected
            and observation.tenant_id == tenant_id
            and observation.observed_at <= now < observation.valid_until
        )
