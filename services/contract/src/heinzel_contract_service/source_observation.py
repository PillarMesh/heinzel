from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

from heinzel_contract_model import ArtifactModel, ArtifactReference, canonical_bytes, digest
from heinzel_provider_sdk import AcquisitionSourceObservation
from pydantic import ConfigDict, Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
type SourceFreshnessFaultHook = Callable[[str], None]


def _noop_freshness_fault_hook(checkpoint: str) -> None:
    del checkpoint


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


class SourceFreshnessObservation(ArtifactModel):
    """A measured source watermark bound to one immutable acquisition generation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    observation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    source_ref: str = Field(min_length=1)
    input_generation_digest: str = Field(pattern=_DIGEST_PATTERN)
    data_observation_ref: ArtifactReference
    watermark_at: datetime
    observed_at: datetime

    @field_validator("watermark_at", "observed_at")
    @classmethod
    def _freshness_timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source freshness timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _watermark_precedes_observation(self) -> SourceFreshnessObservation:
        if self.watermark_at > self.observed_at:
            raise ValueError("source watermark cannot follow its observation")
        return self


class ValidatedSourceBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    source_binding_revision: int = Field(ge=1)
    credential_revision: int = Field(ge=1)
    capability_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_observation_ref: str = Field(min_length=1)
    source_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    validated_at: datetime

    @field_validator("validated_at")
    @classmethod
    def _validated_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source validation timestamp must be timezone-aware UTC")
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


class SQLiteSourceFreshnessObservationRepository:
    def __init__(
        self,
        database_path: str,
        *,
        fault_hook: SourceFreshnessFaultHook = _noop_freshness_fault_hook,
        check_same_thread: bool = True,
    ) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
        self._fault_hook = fault_hook
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS source_freshness_observations ("
            "tenant_id TEXT NOT NULL, input_generation_digest TEXT NOT NULL, "
            "observation_id TEXT NOT NULL, version INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, input_generation_digest), "
            "UNIQUE (tenant_id, observation_id, version))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, observation: SourceFreshnessObservation) -> SourceFreshnessObservation:
        observation = SourceFreshnessObservation.model_validate(
            observation.model_dump(mode="python"), strict=True
        )
        payload = canonical_bytes(observation)
        try:
            self._connection.execute(
                "INSERT INTO source_freshness_observations "
                "(tenant_id, input_generation_digest, observation_id, version, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, input_generation_digest) DO NOTHING",
                (
                    observation.tenant_id,
                    observation.input_generation_digest,
                    observation.observation_id,
                    observation.version,
                    payload,
                ),
            )
            row = self._connection.execute(
                "SELECT payload FROM source_freshness_observations "
                "WHERE tenant_id = ? AND input_generation_digest = ?",
                (observation.tenant_id, observation.input_generation_digest),
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("source freshness observation identity is immutable")
            self._fault_hook("before_commit")
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise ValueError(
                "source freshness observation identifier is bound to another generation"
            ) from error
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return observation

    def read_for_generation(
        self, *, tenant_id: str, input_generation_digest: str
    ) -> SourceFreshnessObservation | None:
        row = self._connection.execute(
            "SELECT tenant_id, input_generation_digest, observation_id, version, payload "
            "FROM source_freshness_observations "
            "WHERE tenant_id = ? AND input_generation_digest = ?",
            (tenant_id, input_generation_digest),
        ).fetchone()
        if row is None:
            return None
        observation = SourceFreshnessObservation.model_validate_json(row[4])
        if (
            row[0] != observation.tenant_id
            or row[1] != observation.input_generation_digest
            or row[2] != observation.observation_id
            or row[3] != observation.version
            or observation.tenant_id != tenant_id
            or observation.input_generation_digest != input_generation_digest
        ):
            raise ValueError("source freshness observation index does not match its payload")
        return observation


class SQLiteAcquisitionSourceObservationRepository:
    """Keeps the source observation an acquisition contract was activated over.

    The observation is the provider's reading of the source at the moment of activation, and
    the activated contract pins `digest(observation)`. Nothing durably held it, so the runner
    could only resolve it from the process that made it: a start that activated a contract and
    then failed before landing left a contract no later start could satisfy, and the only
    recovery was discarding the state directory. A second acquisition under the same contract
    was unreachable for the same reason.

    It is immutable per reference. Two readings of an unchanged source are not equal -- the
    provider stamps a wall-clock `observed_at` into each object observation -- so overwriting
    one would silently change the digest a contract was activated against, which is the one
    thing the activation exists to pin. Storing the identical bytes again is a no-op, so a
    retried write is safe; storing different bytes under a reference already taken is refused.
    """

    def __init__(self, database_path: str, *, check_same_thread: bool = True) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_source_observations ("
            "tenant_id TEXT NOT NULL, observation_ref TEXT NOT NULL, "
            "source_binding_ref TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, observation_ref))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(
        self, *, observation_ref: str, observation: AcquisitionSourceObservation
    ) -> AcquisitionSourceObservation:
        """Record this observation under this reference, or confirm the recorded one is it."""
        if not observation_ref:
            raise ValueError("acquisition source observation reference must not be empty")
        payload = canonical_bytes(observation)
        try:
            self._connection.execute(
                "INSERT INTO acquisition_source_observations "
                "(tenant_id, observation_ref, source_binding_ref, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, observation_ref) DO NOTHING",
                (
                    observation.tenant_id,
                    observation_ref,
                    observation.source_binding_ref,
                    payload,
                ),
            )
            row = self._connection.execute(
                "SELECT payload FROM acquisition_source_observations "
                "WHERE tenant_id = ? AND observation_ref = ?",
                (observation.tenant_id, observation_ref),
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("acquisition source observation reference is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return observation

    def read(self, *, tenant_id: str, observation_ref: str) -> AcquisitionSourceObservation | None:
        """The observation recorded under this reference, or `None` where none is."""
        row = self._connection.execute(
            "SELECT tenant_id, source_binding_ref, payload FROM acquisition_source_observations "
            "WHERE tenant_id = ? AND observation_ref = ?",
            (tenant_id, observation_ref),
        ).fetchone()
        if row is None:
            return None
        observation = AcquisitionSourceObservation.model_validate_json(row[2])
        # The index is derived from the payload on the way in, so a disagreement means the row
        # was written by something other than `store` -- which is a corrupted record, not a
        # miss. Reporting it as a miss would have the caller observe the source again and
        # activate a contract the stored one contradicts.
        if (
            row[0] != observation.tenant_id
            or row[1] != observation.source_binding_ref
            or observation.tenant_id != tenant_id
        ):
            raise ValueError("acquisition source observation index does not match its payload")
        return observation
