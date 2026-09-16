from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_contract_service import (
    SourceFreshnessObservation,
    SourceObservation,
    SQLiteSourceFreshnessObservationRepository,
    SQLiteSourceObservationRepository,
)

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


def freshness_observation(
    *,
    tenant_id: str = "tenant-a",
    input_generation_digest: str = "b" * 64,
    watermark_at: datetime = NOW - timedelta(minutes=5),
) -> SourceFreshnessObservation:
    return SourceFreshnessObservation(
        observation_id="source-freshness-1",
        tenant_id=tenant_id,
        version=1,
        source_ref="finance-system",
        input_generation_digest=input_generation_digest,
        data_observation_ref=ArtifactReference(
            artifact_id="acquisition-generation-1",
            version=1,
            digest="c" * 64,
        ),
        watermark_at=watermark_at,
        observed_at=NOW,
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


def test_source_freshness_observation_is_replayed_exactly_for_its_input_generation() -> None:
    repository = SQLiteSourceFreshnessObservationRepository(":memory:")
    expected = freshness_observation()

    first = repository.store(expected)
    replay = repository.store(expected)

    assert replay.model_dump_json() == first.model_dump_json()
    assert (
        repository.read_for_generation(tenant_id="tenant-a", input_generation_digest="b" * 64)
        == expected
    )
    assert (
        repository.read_for_generation(tenant_id="tenant-b", input_generation_digest="b" * 64)
        is None
    )


def test_source_freshness_observation_rejects_conflicting_generation_evidence() -> None:
    repository = SQLiteSourceFreshnessObservationRepository(":memory:")
    expected = freshness_observation()
    repository.store(expected)

    conflicting = expected.model_copy(
        update={
            "observation_id": "source-freshness-2",
            "watermark_at": NOW - timedelta(minutes=3),
        }
    )

    with pytest.raises(ValueError, match=r"freshness observation.*immutable"):
        repository.store(conflicting)


def test_source_freshness_observation_rejects_a_future_watermark() -> None:
    with pytest.raises(ValueError, match="watermark cannot follow"):
        freshness_observation(watermark_at=NOW + timedelta(seconds=1))


def test_source_freshness_store_revalidates_model_copy_before_writing() -> None:
    repository = SQLiteSourceFreshnessObservationRepository(":memory:")
    invalid = freshness_observation().model_copy(
        update={"watermark_at": NOW + timedelta(seconds=1)}
    )

    with pytest.raises(ValueError, match="watermark cannot follow"):
        repository.store(invalid)

    assert (
        repository.read_for_generation(tenant_id="tenant-a", input_generation_digest="b" * 64)
        is None
    )


def test_source_freshness_store_rolls_back_an_interrupted_commit(tmp_path: Path) -> None:
    database_path = tmp_path / "freshness.sqlite3"

    def fail_before_commit(checkpoint: str) -> None:
        assert checkpoint == "before_commit"
        raise RuntimeError("commit interrupted")

    repository = SQLiteSourceFreshnessObservationRepository(
        str(database_path), fault_hook=fail_before_commit
    )
    with pytest.raises(RuntimeError, match="commit interrupted"):
        repository.store(freshness_observation())
    repository.close()

    assert (
        SQLiteSourceFreshnessObservationRepository(str(database_path)).read_for_generation(
            tenant_id="tenant-a", input_generation_digest="b" * 64
        )
        is None
    )


def test_source_freshness_identifier_cannot_name_another_generation() -> None:
    repository = SQLiteSourceFreshnessObservationRepository(":memory:")
    repository.store(freshness_observation())
    conflicting = freshness_observation(input_generation_digest="d" * 64)

    with pytest.raises(ValueError, match="identifier is bound to another generation"):
        repository.store(conflicting)


def test_source_freshness_reader_rejects_tampered_index_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "freshness.sqlite3"
    repository = SQLiteSourceFreshnessObservationRepository(str(database_path))
    repository.store(freshness_observation())
    repository.close()
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_freshness_observations SET input_generation_digest = ?",
        ("d" * 64,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="index does not match"):
        SQLiteSourceFreshnessObservationRepository(str(database_path)).read_for_generation(
            tenant_id="tenant-a", input_generation_digest="d" * 64
        )
