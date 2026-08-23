from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_contract_service import SourceObservation, SQLiteSourceObservationRepository

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def observation(*, tenant_id: str = "tenant-a") -> SourceObservation:
    return SourceObservation(
        observation_id="source-observation-1",
        tenant_id=tenant_id,
        version=1,
        source_ref="finance-system",
        schema_digest="a" * 64,
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def reference(value: SourceObservation) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=value.observation_id,
        version=value.version,
        digest=digest(value),
    )


def test_source_observation_requires_exact_persisted_current_reference() -> None:
    repository = SQLiteSourceObservationRepository(":memory:")
    stored = repository.store(observation())

    assert repository.has_current("tenant-a", reference(stored), now=NOW)
    assert not repository.has_current(
        "tenant-a",
        reference(stored).model_copy(update={"digest": "b" * 64}),
        now=NOW,
    )
    assert not repository.has_current("tenant-b", reference(stored), now=NOW)


def test_stale_source_observation_is_not_current() -> None:
    repository = SQLiteSourceObservationRepository(":memory:")
    stored = repository.store(observation())

    assert not repository.has_current("tenant-a", reference(stored), now=stored.valid_until)
