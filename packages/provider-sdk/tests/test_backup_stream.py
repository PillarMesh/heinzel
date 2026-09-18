from __future__ import annotations

import io
import struct
from typing import cast

import pytest
from heinzel_provider_sdk import (
    BackupStreamIntegrityError,
    decrypt_backup_stream,
    encrypt_backup_stream,
)

_HEADER = struct.Struct(">8sI4s")
_RECORD = struct.Struct(">QBI")


class _ShortWriteDestination:
    def __init__(self, *, maximum_write_size: int) -> None:
        self._buffer = io.BytesIO()
        self._maximum_write_size = maximum_write_size

    def write(self, value: bytes) -> int:
        return self._buffer.write(value[: self._maximum_write_size])

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


class _InvalidWriteDestination:
    def __init__(self, result: object) -> None:
        self._result = result

    def write(self, value: bytes) -> int:
        if self._result == "oversized":
            return len(value) + 1
        return cast(int, self._result)


_INVALID_WRITE_RESULTS = (
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param("oversized", id="oversized"),
    pytest.param(None, id="none"),
    pytest.param(1.5, id="non-integer"),
    pytest.param(True, id="boolean"),
)


def test_backup_stream_round_trip_stays_within_two_chunk_buffers() -> None:
    chunk_size = 4 * 1024
    plaintext = b"first-generation\x00" * 1_024
    encrypted = io.BytesIO()

    encryption = encrypt_backup_stream(
        io.BytesIO(plaintext),
        encrypted,
        key=b"k" * 32,
        chunk_size=chunk_size,
        nonce_prefix=b"abcd",
    )
    restored = io.BytesIO()
    decryption = decrypt_backup_stream(
        io.BytesIO(encrypted.getvalue()),
        restored,
        key=b"k" * 32,
        maximum_chunk_size=chunk_size,
    )

    assert restored.getvalue() == plaintext
    assert encryption.plaintext_bytes == len(plaintext)
    assert decryption.chunk_count == encryption.chunk_count
    assert encryption.maximum_buffer_bytes <= (chunk_size * 2) + 32
    assert decryption.maximum_buffer_bytes <= (chunk_size * 2) + 32


def test_backup_stream_encryption_completes_short_destination_writes() -> None:
    plaintext = b"short-encryption-write" * 1_024
    encrypted = _ShortWriteDestination(maximum_write_size=3)

    encrypt_backup_stream(
        io.BytesIO(plaintext),
        encrypted,
        key=b"e" * 32,
        chunk_size=4 * 1024,
        nonce_prefix=b"encr",
    )
    restored = io.BytesIO()
    decrypt_backup_stream(
        io.BytesIO(encrypted.getvalue()),
        restored,
        key=b"e" * 32,
        maximum_chunk_size=4 * 1024,
    )

    assert restored.getvalue() == plaintext


def test_backup_stream_decryption_completes_short_destination_writes() -> None:
    plaintext = b"short-decryption-write" * 1_024
    encrypted = io.BytesIO()
    encrypt_backup_stream(
        io.BytesIO(plaintext),
        encrypted,
        key=b"d" * 32,
        chunk_size=4 * 1024,
        nonce_prefix=b"decr",
    )
    restored = _ShortWriteDestination(maximum_write_size=5)

    decrypt_backup_stream(
        io.BytesIO(encrypted.getvalue()),
        restored,
        key=b"d" * 32,
        maximum_chunk_size=4 * 1024,
    )

    assert restored.getvalue() == plaintext


@pytest.mark.parametrize(
    "write_result",
    _INVALID_WRITE_RESULTS,
)
def test_backup_stream_rejects_invalid_destination_write_progress(write_result: object) -> None:
    with pytest.raises(BackupStreamIntegrityError):
        encrypt_backup_stream(
            io.BytesIO(b"customer-backup"),
            _InvalidWriteDestination(write_result),
            key=b"w" * 32,
            chunk_size=4 * 1024,
            nonce_prefix=b"wrte",
        )


@pytest.mark.parametrize("write_result", _INVALID_WRITE_RESULTS)
def test_backup_stream_decryption_rejects_invalid_destination_write_progress(
    write_result: object,
) -> None:
    encrypted = io.BytesIO()
    encrypt_backup_stream(
        io.BytesIO(b"customer-backup"),
        encrypted,
        key=b"w" * 32,
        chunk_size=4 * 1024,
        nonce_prefix=b"wrte",
    )

    with pytest.raises(BackupStreamIntegrityError):
        decrypt_backup_stream(
            io.BytesIO(encrypted.getvalue()),
            _InvalidWriteDestination(write_result),
            key=b"w" * 32,
            maximum_chunk_size=4 * 1024,
        )


def test_backup_stream_rejects_truncated_final_authenticator() -> None:
    encrypted = io.BytesIO()
    encrypt_backup_stream(
        io.BytesIO(b"customer-backup" * 1_024),
        encrypted,
        key=b"t" * 32,
        chunk_size=4 * 1024,
        nonce_prefix=b"trnc",
    )

    with pytest.raises(BackupStreamIntegrityError):
        decrypt_backup_stream(
            io.BytesIO(encrypted.getvalue()[:-1]),
            io.BytesIO(),
            key=b"t" * 32,
            maximum_chunk_size=4 * 1024,
        )


def test_backup_stream_rejects_reordered_authenticated_frames() -> None:
    encrypted = io.BytesIO()
    encrypt_backup_stream(
        io.BytesIO(b"a" * 4_096 + b"b" * 4_096 + b"c"),
        encrypted,
        key=b"r" * 32,
        chunk_size=4 * 1024,
        nonce_prefix=b"rord",
    )
    payload = encrypted.getvalue()
    header = payload[: _HEADER.size]
    offset = _HEADER.size
    frames: list[bytes] = []
    while offset < len(payload):
        record = payload[offset : offset + _RECORD.size]
        _index, _final, ciphertext_size = _RECORD.unpack(record)
        frame_end = offset + _RECORD.size + ciphertext_size
        frames.append(payload[offset:frame_end])
        offset = frame_end

    with pytest.raises(BackupStreamIntegrityError):
        decrypt_backup_stream(
            io.BytesIO(header + frames[1] + frames[0] + b"".join(frames[2:])),
            io.BytesIO(),
            key=b"r" * 32,
            maximum_chunk_size=4 * 1024,
        )


def test_backup_stream_rejects_a_source_that_breaks_the_bounded_read_contract() -> None:
    class OversizedSource:
        def read(self, size: int = -1) -> bytes:
            assert size == 4 * 1024
            return b"x" * (size + 1)

    with pytest.raises(ValueError, match="bounded read"):
        encrypt_backup_stream(
            OversizedSource(),  # type: ignore[arg-type]
            io.BytesIO(),
            key=b"k" * 32,
            chunk_size=4 * 1024,
            nonce_prefix=b"over",
        )
