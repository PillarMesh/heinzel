from __future__ import annotations

import hashlib
import io
import os
import stat
from pathlib import Path
from typing import BinaryIO

import pillarmesh_state.artifacts as artifacts_module
import pytest
from pillarmesh_state.artifacts import (
    AcquisitionArtifactDigestMismatchError,
    AcquisitionArtifactIntegrityError,
    AcquisitionArtifactNotFoundError,
    AcquisitionArtifactStoreError,
    InvalidAcquisitionArtifactIdentifierError,
    LocalAcquisitionArtifactStore,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact_path(root: Path, artifact_digest: str) -> Path:
    matches = tuple(root.rglob(artifact_digest))
    assert len(matches) == 1
    return matches[0]


class _ChunkedReader(io.BytesIO):
    def __init__(self, payload: bytes, *, maximum_chunk_size: int) -> None:
        super().__init__(payload)
        self._maximum_chunk_size = maximum_chunk_size

    def read(self, size: int | None = -1) -> bytes:
        requested_size = -1 if size is None else size
        return super().read(min(requested_size, self._maximum_chunk_size))


class _FailingReader(io.BytesIO):
    def __init__(self, first_chunk: bytes) -> None:
        super().__init__(first_chunk)
        self._read_count = 0

    def read(self, size: int | None = -1) -> bytes:
        self._read_count += 1
        if self._read_count == 1:
            return super().read(size)
        raise OSError("private-reader-detail")


class _RuntimeFailingReader(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        if self.tell() == 0:
            return super().read(1)
        raise RuntimeError("private-runtime-reader-detail")


class _BoundedReader(io.BytesIO):
    def __init__(self, payload: bytes, *, expected_read_size: int) -> None:
        super().__init__(payload)
        self._expected_read_size = expected_read_size

    def read(self, size: int | None = -1) -> bytes:
        if size != self._expected_read_size:
            raise OSError("reader was not bounded")
        return super().read(size)


def _read_all(reader: BinaryIO) -> bytes:
    with reader:
        return reader.read()


def test_store_streams_chunked_content_and_opens_a_verified_reader(tmp_path: Path) -> None:
    payload = b"first\nsecond\nthird\n"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts", chunk_size=7)

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=_ChunkedReader(payload, maximum_chunk_size=3),
    )

    assert store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)
    assert (
        _read_all(store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest))
        == payload
    )
    assert _artifact_path(tmp_path / "artifacts", artifact_digest).stat().st_mode & 0o777 == 0o600


def test_store_requests_only_bounded_chunks_from_the_reader(tmp_path: Path) -> None:
    payload = b"bounded-reader"
    artifact_digest = _digest(payload)
    chunk_size = 4
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts", chunk_size=chunk_size)

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=_BoundedReader(payload, expected_read_size=chunk_size),
    )

    assert store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)


def test_chunk_size_must_be_positive_and_one_byte_chunks_are_supported(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        LocalAcquisitionArtifactStore(tmp_path / "zero", chunk_size=0)
    with pytest.raises(ValueError, match="positive"):
        LocalAcquisitionArtifactStore(tmp_path / "negative", chunk_size=-1)

    payload = b"one-byte-chunks"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "one", chunk_size=1)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )

    assert store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)


def test_partial_reader_failure_never_publishes_an_artifact(tmp_path: Path) -> None:
    payload = b"complete-payload"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    with pytest.raises(AcquisitionArtifactStoreError, match="stream artifact") as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=_FailingReader(payload[:4]),
        )

    assert captured.value.__cause__ is None
    assert "private-reader-detail" not in str(captured.value)
    assert not store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)
    assert not tuple(root.rglob("*.tmp"))


def test_arbitrary_reader_failure_is_sanitized_and_never_published(tmp_path: Path) -> None:
    payload = b"complete-payload"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    with pytest.raises(AcquisitionArtifactStoreError, match="stream artifact") as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=_RuntimeFailingReader(payload),
        )

    assert captured.value.__cause__ is None
    assert "private-runtime-reader-detail" not in str(captured.value)
    assert not tuple(root.rglob(artifact_digest))


def test_digest_mismatch_removes_the_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    with pytest.raises(AcquisitionArtifactDigestMismatchError):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=_digest(b"expected"),
            reader=io.BytesIO(b"different"),
        )

    assert not tuple(root.rglob("*.tmp"))
    assert not tuple(root.rglob(_digest(b"expected")))


def test_existing_identical_content_is_verified_and_reused(tmp_path: Path) -> None:
    payload = b"immutable"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(tmp_path / "artifacts", artifact_digest)
    original_identity = artifact.stat().st_ino

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(b"reader-is-not-consumed-on-replay"),
    )

    assert artifact.stat().st_ino == original_identity
    assert artifact.read_bytes() == payload


def test_existing_contradictory_content_is_an_integrity_failure(tmp_path: Path) -> None:
    payload = b"immutable"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(tmp_path / "artifacts", artifact_digest)
    artifact.write_bytes(b"contradiction")
    artifact.chmod(0o600)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="digest"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert artifact.read_bytes() == b"contradiction"


def test_crash_before_atomic_publication_cleans_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"durable"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    def crash_before_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("publication-private-detail")

    monkeypatch.setattr(artifacts_module, "_link", crash_before_publication)

    with pytest.raises(AcquisitionArtifactStoreError, match="publish artifact") as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert captured.value.__cause__ is None
    assert "publication-private-detail" not in str(captured.value)
    assert not tuple(root.rglob("*.tmp"))
    assert not tuple(root.rglob(artifact_digest))


@pytest.mark.parametrize(
    "artifact_digest",
    (
        "../" + "0" * 61,
        "0" * 63,
        "0" * 64 + "/suffix",
        "A" * 64,
    ),
)
def test_store_rejects_noncanonical_or_traversing_digests(
    tmp_path: Path, artifact_digest: str
) -> None:
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")

    with pytest.raises(InvalidAcquisitionArtifactIdentifierError):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(b"payload"),
        )


def test_store_rejects_an_empty_tenant_identifier(tmp_path: Path) -> None:
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")

    with pytest.raises(InvalidAcquisitionArtifactIdentifierError):
        store.exists_verified(tenant_id="", artifact_digest=_digest(b"payload"))


def test_symlink_substitution_is_rejected_without_touching_its_target(tmp_path: Path) -> None:
    payload = b"safe"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(root, artifact_digest)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    artifact.unlink()
    artifact.symlink_to(outside)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)
    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert outside.read_bytes() == payload


def test_same_digest_is_isolated_between_tenants(tmp_path: Path) -> None:
    payload = b"shared-content"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    for tenant_id in (TENANT_A, TENANT_B):
        store.put_if_absent(
            tenant_id=tenant_id,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    matches = tuple(root.rglob(artifact_digest))
    assert len(matches) == 2
    assert matches[0].parent != matches[1].parent
    assert store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)
    assert store.exists_verified(tenant_id=TENANT_B, artifact_digest=artifact_digest)


def test_missing_artifact_has_distinct_exists_and_open_results(tmp_path: Path) -> None:
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    artifact_digest = _digest(b"missing")

    assert not store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)
    with pytest.raises(AcquisitionArtifactNotFoundError):
        store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)


@pytest.mark.parametrize("root_kind", ("wrong_mode", "regular_file"))
def test_store_rejects_an_unsafe_root_directory(tmp_path: Path, root_kind: str) -> None:
    root = tmp_path / "artifacts"
    if root_kind == "wrong_mode":
        root.mkdir(mode=0o700)
        root.chmod(0o755)
    else:
        root.write_bytes(b"not-a-directory")
        root.chmod(0o700)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        LocalAcquisitionArtifactStore(root)


def test_store_rejects_an_unsafe_tenant_directory_mode(tmp_path: Path) -> None:
    payload = b"payload"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    tenant_directory = next(root.iterdir())
    tenant_directory.chmod(0o755)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)


@pytest.mark.parametrize("unsafe_kind", ("wrong_mode", "hard_link", "fifo"))
def test_store_rejects_unsafe_artifact_file_metadata(tmp_path: Path, unsafe_kind: str) -> None:
    payload = b"payload"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(root, artifact_digest)
    if unsafe_kind == "wrong_mode":
        artifact.chmod(0o644)
    elif unsafe_kind == "hard_link":
        os.link(artifact, tmp_path / "outside-link")
    else:
        artifact.unlink()
        os.mkfifo(artifact, mode=0o600)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)


def test_partial_os_writes_preserve_every_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"partial-write-payload"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    real_write = os.write

    def short_write(descriptor: int, chunk: bytes) -> int:
        return real_write(descriptor, chunk[:2])

    monkeypatch.setattr(os, "write", short_write)

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )

    assert (
        _read_all(store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest))
        == payload
    )


def test_zero_progress_write_fails_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"zero-progress"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    def zero_write(_descriptor: int, _chunk: bytes) -> int:
        return 0

    monkeypatch.setattr(os, "write", zero_write)

    with pytest.raises(AcquisitionArtifactStoreError, match="stream artifact"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert not tuple(root.rglob(artifact_digest))


def test_concurrent_identical_publication_is_verified_reused_and_resynchronized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"concurrent-identical"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_fsync = artifacts_module._fsync
    directory_syncs = 0

    def publish_during_file_fsync(descriptor: int) -> None:
        nonlocal directory_syncs
        descriptor_mode = os.fstat(descriptor).st_mode
        real_fsync(descriptor)
        if stat.S_ISREG(descriptor_mode):
            concurrent = next(root.iterdir()) / artifact_digest
            concurrent.write_bytes(payload)
            concurrent.chmod(0o600)
        elif stat.S_ISDIR(descriptor_mode):
            directory_syncs += 1

    def unexpected_publication(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verified concurrent content must be reused")

    monkeypatch.setattr(artifacts_module, "_fsync", publish_during_file_fsync)
    monkeypatch.setattr(artifacts_module, "_link", unexpected_publication)

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )

    assert directory_syncs >= 2
    assert not tuple(root.rglob("*.tmp"))


def test_concurrent_contradictory_publication_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"intended"
    contradiction = b"concurrent-contradiction"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_fsync = artifacts_module._fsync

    def publish_during_file_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            concurrent = next(root.iterdir()) / artifact_digest
            concurrent.write_bytes(contradiction)
            concurrent.chmod(0o600)

    monkeypatch.setattr(artifacts_module, "_fsync", publish_during_file_fsync)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="digest"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert _artifact_path(root, artifact_digest).read_bytes() == contradiction
    assert not tuple(root.rglob("*.tmp"))


@pytest.mark.parametrize("winning_payload_kind", ("identical", "contradictory"))
def test_destination_created_at_atomic_publication_is_verified_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winning_payload_kind: str,
) -> None:
    payload = b"intended"
    winning_payload = payload if winning_payload_kind == "identical" else b"contradiction"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_link = artifacts_module._link
    injected = False

    def lose_publication_race(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            tenant_directory = next(root.iterdir())
            winner = tenant_directory / artifact_digest
            winner_temporary = tenant_directory / f".{artifact_digest}.{'a' * 24}.tmp"
            winner_temporary.write_bytes(winning_payload)
            winner_temporary.chmod(0o600)
            os.link(winner_temporary, winner)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(artifacts_module, "_link", lose_publication_race)

    if winning_payload_kind == "contradictory":
        with pytest.raises(AcquisitionArtifactIntegrityError, match="digest"):
            store.put_if_absent(
                tenant_id=TENANT_A,
                artifact_digest=artifact_digest,
                reader=io.BytesIO(payload),
            )
    else:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert _artifact_path(root, artifact_digest).read_bytes() == winning_payload
    assert not tuple(root.rglob("*.tmp"))


def test_replay_repairs_a_crash_after_no_clobber_link_before_temporary_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"linked-before-crash"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_unlink = artifacts_module._unlink

    def crash_cleanup(_path: str, *, dir_fd: int) -> None:
        del dir_fd
        raise OSError("cleanup-private-detail")

    monkeypatch.setattr(artifacts_module, "_unlink", crash_cleanup)

    with pytest.raises(AcquisitionArtifactStoreError, match="clean up artifact"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    artifact = _artifact_path(root, artifact_digest)
    assert artifact.stat().st_nlink == 2
    assert tuple(root.rglob("*.tmp"))

    monkeypatch.setattr(artifacts_module, "_unlink", real_unlink)
    assert (
        _read_all(store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest))
        == payload
    )
    assert artifact.stat().st_nlink == 1
    assert not tuple(root.rglob("*.tmp"))


def test_recovery_sync_failure_preserves_the_temporary_link_for_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"linked-before-crash"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(root, artifact_digest)
    temporary_link = artifact.parent / f".{artifact_digest}.{'a' * 24}.tmp"
    os.link(artifact, temporary_link)
    real_fsync = artifacts_module._fsync

    def fail_recovery_sync(_descriptor: int) -> None:
        raise OSError("recovery-fsync-private-detail")

    monkeypatch.setattr(artifacts_module, "_fsync", fail_recovery_sync)

    with pytest.raises(AcquisitionArtifactStoreError, match="synchronize artifact directory"):
        store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)

    assert temporary_link.exists()
    assert artifact.stat().st_nlink == 2

    monkeypatch.setattr(artifacts_module, "_fsync", real_fsync)
    assert (
        _read_all(store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest))
        == payload
    )
    assert not temporary_link.exists()
    assert artifact.stat().st_nlink == 1


@pytest.mark.parametrize(
    "malformed_name",
    (
        "wrong_prefix",
        "wrong_suffix",
        "wrong_token_length",
        "wrong_token_alphabet",
    ),
)
def test_replay_never_removes_a_malformed_hard_link(
    tmp_path: Path,
    malformed_name: str,
) -> None:
    payload = b"linked-before-crash"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    artifact = _artifact_path(root, artifact_digest)
    tenant_directory = artifact.parent
    names = {
        "wrong_prefix": f"x{artifact_digest}.{'a' * 24}.tmp",
        "wrong_suffix": f".{artifact_digest}.{'a' * 24}.bad",
        "wrong_token_length": f".{artifact_digest}.{'a' * 23}.tmp",
        "wrong_token_alphabet": f".{artifact_digest}.X{'a' * 23}.tmp",
    }
    malformed_link = tenant_directory / names[malformed_name]
    os.link(artifact, malformed_link)

    with pytest.raises(AcquisitionArtifactIntegrityError, match="unsafe"):
        store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)

    assert malformed_link.exists()
    assert artifact.stat().st_nlink == 2


def test_concurrent_reuse_cleanup_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"concurrent-identical"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_fsync = artifacts_module._fsync

    def publish_during_file_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            concurrent = next(root.iterdir()) / artifact_digest
            concurrent.write_bytes(payload)
            concurrent.chmod(0o600)

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("cleanup-private-detail")

    monkeypatch.setattr(artifacts_module, "_fsync", publish_during_file_fsync)
    monkeypatch.setattr(artifacts_module, "_unlink", fail_cleanup)

    with pytest.raises(AcquisitionArtifactStoreError, match="clean up artifact"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )


def test_public_operations_close_their_directory_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"descriptor-lifecycle"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )
    real_close = artifacts_module._close_safely
    closed_descriptors: list[int] = []

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(artifacts_module, "_close_safely", record_close)
    operations = (
        lambda: store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        ),
        lambda: store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest),
        lambda: store.open_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest).close(),
    )

    for operation in operations:
        closed_descriptors.clear()
        operation()
        assert len(closed_descriptors) >= 2


def test_retry_repairs_uncertain_root_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    real_fsync = artifacts_module._fsync

    def fail_root_parent_fsync(_descriptor: int) -> None:
        raise OSError("root-parent-fsync-private-detail")

    monkeypatch.setattr(artifacts_module, "_fsync", fail_root_parent_fsync)

    with pytest.raises(AcquisitionArtifactStoreError, match="synchronize artifact root"):
        LocalAcquisitionArtifactStore(root)

    assert root.is_dir()
    parent_syncs = 0

    def record_parent_fsync(descriptor: int) -> None:
        nonlocal parent_syncs
        parent_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", record_parent_fsync)
    LocalAcquisitionArtifactStore(root)

    assert parent_syncs == 1


def test_retry_repairs_uncertain_tenant_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"tenant-directory-retry"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    real_fsync = artifacts_module._fsync
    failed = False

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed:
            failed = True
            raise OSError("tenant-fsync-private-detail")
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", fail_first_directory_fsync)

    with pytest.raises(AcquisitionArtifactStoreError, match="synchronize tenant directory"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    directory_syncs = 0

    def record_directory_fsync(descriptor: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", record_directory_fsync)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )

    assert directory_syncs >= 2


def test_file_fsync_failure_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"durable"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    real_fsync = artifacts_module._fsync

    def fail_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("fsync-private-detail")
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", fail_fsync)

    with pytest.raises(AcquisitionArtifactStoreError, match="synchronize artifact") as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert captured.value.__cause__ is None
    assert "fsync-private-detail" not in str(captured.value)
    assert not tuple(root.rglob(artifact_digest))
    assert not tuple(root.rglob("*.tmp"))


def test_directory_fsync_failure_reports_uncertain_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)
    seed = b"seed"
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=_digest(seed),
        reader=io.BytesIO(seed),
    )
    payload = b"published-before-directory-fsync"
    artifact_digest = _digest(payload)
    real_fsync = artifacts_module._fsync
    directory_fsyncs = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 2:
                raise OSError("directory-fsync-private-detail")
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", fail_directory_fsync)

    with pytest.raises(
        AcquisitionArtifactStoreError, match="synchronize artifact directory"
    ) as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert captured.value.__cause__ is None
    assert "directory-fsync-private-detail" not in str(captured.value)
    assert store.exists_verified(tenant_id=TENANT_A, artifact_digest=artifact_digest)

    replay_directory_syncs = 0

    def record_replay_fsync(descriptor: int) -> None:
        nonlocal replay_directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            replay_directory_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts_module, "_fsync", record_replay_fsync)
    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(b"replay-does-not-consume-reader"),
    )

    assert replay_directory_syncs == 2


def test_atomic_publication_failure_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"durable"
    artifact_digest = _digest(payload)
    root = tmp_path / "artifacts"
    store = LocalAcquisitionArtifactStore(root)

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("publication-private-detail")

    monkeypatch.setattr(artifacts_module, "_link", fail_publication)

    with pytest.raises(AcquisitionArtifactStoreError, match="publish artifact"):
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert not tuple(root.rglob(artifact_digest))
    assert not tuple(root.rglob("*.tmp"))


def test_cleanup_failure_never_replaces_the_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"durable"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("primary-private-detail")

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("cleanup-private-detail")

    monkeypatch.setattr(artifacts_module, "_link", fail_publication)
    monkeypatch.setattr(artifacts_module, "_unlink", fail_cleanup)

    with pytest.raises(AcquisitionArtifactStoreError, match="publish artifact") as captured:
        store.put_if_absent(
            tenant_id=TENANT_A,
            artifact_digest=artifact_digest,
            reader=io.BytesIO(payload),
        )

    assert captured.value.__cause__ is None
    assert "cleanup-private-detail" not in str(captured.value)


def test_directory_fsync_happens_after_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"ordered"
    artifact_digest = _digest(payload)
    store = LocalAcquisitionArtifactStore(tmp_path / "artifacts")
    events: list[str] = []
    real_fsync = artifacts_module._fsync
    real_link = artifacts_module._link

    def record_fsync(descriptor: int) -> None:
        events.append(
            "file_fsync" if stat.S_ISREG(os.fstat(descriptor).st_mode) else "directory_fsync"
        )
        real_fsync(descriptor)

    def record_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        events.append("publish")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(artifacts_module, "_fsync", record_fsync)
    monkeypatch.setattr(artifacts_module, "_link", record_link)

    store.put_if_absent(
        tenant_id=TENANT_A,
        artifact_digest=artifact_digest,
        reader=io.BytesIO(payload),
    )

    assert events[-3:] == ["file_fsync", "publish", "directory_fsync"]
