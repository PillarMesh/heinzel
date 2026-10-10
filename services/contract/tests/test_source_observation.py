from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_contract_service import (
    SourceFreshnessObservation,
    SourceObservation,
    SQLiteAcquisitionSourceObservationRepository,
    SQLiteSourceFreshnessObservationRepository,
    SQLiteSourceObservationRepository,
)
from heinzel_provider_sdk import (
    AcquisitionObjectObservation,
    AcquisitionSourceObservation,
    ColumnObservation,
    ProviderObservation,
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


def acquisition_observation(
    *, tenant_id: str = "tenant-a", observed_at: datetime = NOW
) -> AcquisitionSourceObservation:
    """One provider reading of one source object, as `observe_source` returns it."""
    return AcquisitionSourceObservation(
        tenant_id=tenant_id,
        source_binding_ref="src-0123456789abcdef01234567",
        provider_kind="postgresql",
        object_observations=(
            AcquisitionObjectObservation(
                logical_object_ref="orders",
                provider_observation=ProviderObservation(
                    provider="postgresql",
                    connection_handle="connection-a",
                    object_identity="public.orders",
                    object_kind="base_table",
                    schema_digest="a" * 64,
                    columns=(
                        ColumnObservation(name="order_id", type_name="BIGINT", nullable=False),
                    ),
                    key_name="order_id",
                    key_type="BIGINT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=("snapshot",),
                    observed_at=observed_at,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            ),
        ),
    )


def test_an_acquisition_observation_is_read_back_by_the_reference_it_was_stored_under(
    tmp_path: Path,
) -> None:
    """The whole point: a later process resolves what an earlier one observed.

    Read back through a second repository over the same file rather than the one that wrote
    it, because resolving it in the process that made it is what the runtime could already do.
    """
    stored = acquisition_observation()
    repository = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))
    repository.store(observation_ref="source-observation:a", observation=stored)
    repository.close()

    reopened = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))
    read = reopened.read(tenant_id="tenant-a", observation_ref="source-observation:a")

    assert read == stored
    # And byte-identical, because an activated contract pins `digest(observation)`: a round
    # trip that changed one field would satisfy equality here and fail the activation there.
    assert read is not None
    assert canonical_bytes(read) == canonical_bytes(stored)
    reopened.close()


def test_storing_the_same_observation_again_is_a_no_op(tmp_path: Path) -> None:
    """A retried write must not be an error: the caller cannot tell whether the first landed."""
    repository = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))
    stored = acquisition_observation()

    repository.store(observation_ref="source-observation:a", observation=stored)
    again = repository.store(observation_ref="source-observation:a", observation=stored)

    assert again == stored
    repository.close()


def test_a_different_observation_under_a_taken_reference_is_refused(tmp_path: Path) -> None:
    """Two readings of an unchanged source differ, and the contract pinned one of them.

    The provider stamps a wall clock into every object observation, so this is the ordinary
    case rather than a contrived one: observing the same table a second later produces exactly
    this. Overwriting would change the digest a contract was activated against.
    """
    repository = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))
    repository.store(observation_ref="source-observation:a", observation=acquisition_observation())

    with pytest.raises(ValueError, match="immutable"):
        repository.store(
            observation_ref="source-observation:a",
            observation=acquisition_observation(observed_at=NOW + timedelta(seconds=1)),
        )

    # And the first one is still what the reference resolves to.
    read = repository.read(tenant_id="tenant-a", observation_ref="source-observation:a")
    assert read == acquisition_observation()
    repository.close()


def test_an_unknown_reference_reads_as_absent_and_another_tenant_cannot_reach_one(
    tmp_path: Path,
) -> None:
    """A miss is `None`; a tenant asking for another tenant's reference gets the same answer."""
    repository = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))
    repository.store(observation_ref="source-observation:a", observation=acquisition_observation())

    assert repository.read(tenant_id="tenant-a", observation_ref="source-observation:b") is None
    assert repository.read(tenant_id="tenant-b", observation_ref="source-observation:a") is None
    repository.close()


def test_an_empty_reference_is_refused_rather_than_stored(tmp_path: Path) -> None:
    """A reference nothing can name is a row nothing can resolve."""
    repository = SQLiteAcquisitionSourceObservationRepository(str(tmp_path / "obs.sqlite3"))

    with pytest.raises(ValueError, match="must not be empty"):
        repository.store(observation_ref="", observation=acquisition_observation())

    repository.close()
