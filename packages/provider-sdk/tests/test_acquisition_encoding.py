from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import cast

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import AcquisitionFieldValue, AcquisitionRecord
from heinzel_provider_sdk.acquisition_encoding import (
    AcquisitionCeilingExceeded,
    CanonicalJsonlSegmentEncoder,
    encode_canonical_jsonl,
    iter_artifact_chunks,
)
from heinzel_provider_sdk.acquisition_protocols import AcquisitionArtifactReader

_UPDATED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_RECORD_BYTES = (
    b'{"fields":[{"name":"amount","value":"10.50"},{"name":"updated_at",'
    b'"value":"2026-09-01T12:00:00.000000Z"}],"logical_object_ref":"order",'
    b'"operation":"upsert","record_key":"order:7","schema_version":"1",'
    b'"source_created_at":null,"source_updated_at":"2026-09-01T12:00:00.000000Z"}'
)


def _record(*, record_key: str = "order:7") -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="order",
        record_key=record_key,
        source_created_at=None,
        source_updated_at=_UPDATED_AT,
        fields=(
            AcquisitionFieldValue(name="amount", value=Decimal("10.50")),
            AcquisitionFieldValue(name="updated_at", value=_UPDATED_AT),
        ),
    )


def _record_set_digest(*records: AcquisitionRecord) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"heinzel-acquisition-record-set-v1\0")
    for record in records:
        model_digest = bytes.fromhex(digest(record))
        hasher.update(len(model_digest).to_bytes(8, "big"))
        hasher.update(model_digest)
    return hasher.hexdigest()


def test_encoder_writes_exact_canonical_jsonl_and_both_digests() -> None:
    writer = BytesIO()

    result = encode_canonical_jsonl(
        (_record(),),
        writer,
        record_ceiling=1,
        encoded_byte_ceiling=len(_RECORD_BYTES) + 1,
    )

    expected_content = _RECORD_BYTES + b"\n"
    assert writer.getvalue() == expected_content
    assert result.record_count == 1
    assert result.encoded_bytes == len(expected_content)
    assert result.content_digest == hashlib.sha256(expected_content).hexdigest()
    assert result.record_set_digest == _record_set_digest(_record())


class _ShortWriter:
    def __init__(self, maximum_write: int) -> None:
        self._maximum_write = maximum_write
        self._buffer = bytearray()

    def write(self, value: bytes) -> int:
        written = min(self._maximum_write, len(value))
        self._buffer.extend(value[:written])
        return written

    def getvalue(self) -> bytes:
        return bytes(self._buffer)


class _InvalidProgressWriter:
    def __init__(self, invalid_progress: int) -> None:
        self._invalid_progress = invalid_progress
        self._write_count = 0

    def write(self, value: bytes) -> int:
        self._write_count += 1
        if self._write_count == 1:
            return self._invalid_progress
        return len(value)


class _LateOverreportingWriter:
    def __init__(self) -> None:
        self._write_count = 0

    def write(self, value: bytes) -> int:
        self._write_count += 1
        if self._write_count == 1:
            return 1
        return len(value) + 1


def test_encoder_finishes_short_writes_without_changing_digests() -> None:
    writer = _ShortWriter(maximum_write=1)

    result = encode_canonical_jsonl(
        (_record(),),
        writer,
        record_ceiling=1,
        encoded_byte_ceiling=len(_RECORD_BYTES) + 1,
    )

    assert writer.getvalue() == _RECORD_BYTES + b"\n"
    assert result.content_digest == hashlib.sha256(_RECORD_BYTES + b"\n").hexdigest()


@pytest.mark.parametrize("invalid_progress", [0, len(_RECORD_BYTES) + 2])
def test_encoder_rejects_a_writer_that_reports_invalid_progress(invalid_progress: int) -> None:
    writer = _InvalidProgressWriter(invalid_progress)

    with pytest.raises(OSError) as captured:
        encode_canonical_jsonl(
            (_record(),),
            writer,
            record_ceiling=1,
            encoded_byte_ceiling=len(_RECORD_BYTES) + 1,
        )

    assert str(captured.value) == "artifact writer made invalid progress"


def test_encoder_rejects_overreported_progress_after_a_short_write() -> None:
    writer = _LateOverreportingWriter()

    with pytest.raises(OSError, match="artifact writer made invalid progress"):
        encode_canonical_jsonl(
            (_record(),),
            writer,
            record_ceiling=1,
            encoded_byte_ceiling=len(_RECORD_BYTES) + 1,
        )


@pytest.mark.parametrize(
    ("record_ceiling", "byte_ceiling", "limit_kind", "expected_message"),
    (
        (0, len(_RECORD_BYTES) + 1, "records", "record ceiling exceeded for order"),
        (1, len(_RECORD_BYTES), "encoded_bytes", "encoded byte ceiling exceeded for order"),
    ),
)
def test_encoder_refuses_a_batch_instead_of_truncating_at_a_ceiling(
    record_ceiling: int,
    byte_ceiling: int,
    limit_kind: str,
    expected_message: str,
) -> None:
    writer = BytesIO()

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        encode_canonical_jsonl(
            (_record(),),
            writer,
            record_ceiling=record_ceiling,
            encoded_byte_ceiling=byte_ceiling,
        )

    assert captured.value.limit_kind == limit_kind
    assert captured.value.logical_object_ref == "order"
    assert captured.value.ceiling == (record_ceiling if limit_kind == "records" else byte_ceiling)
    assert str(captured.value) == expected_message
    assert writer.getvalue() == b""


def test_encoder_refuses_a_later_record_without_writing_part_of_that_record() -> None:
    writer = BytesIO()
    first_line = _RECORD_BYTES + b"\n"

    with pytest.raises(AcquisitionCeilingExceeded, match="record ceiling exceeded"):
        encode_canonical_jsonl(
            (_record(), _record(record_key="order:8")),
            writer,
            record_ceiling=1,
            encoded_byte_ceiling=10_000,
        )

    assert writer.getvalue() == first_line


def test_encoder_counts_every_record_and_encoded_byte() -> None:
    writer = BytesIO()
    records = (_record(), _record(record_key="order:8"))

    result = encode_canonical_jsonl(
        records,
        writer,
        record_ceiling=2,
        encoded_byte_ceiling=10_000,
    )

    assert result.record_count == 2
    assert result.encoded_bytes == len(writer.getvalue())
    assert result.encoded_bytes > len(_RECORD_BYTES) + 1


def test_incremental_encoder_matches_the_iterable_encoder_without_retaining_records() -> None:
    records = (_record(), _record(record_key="order:8"))
    expected_writer = BytesIO()
    expected = encode_canonical_jsonl(
        records,
        expected_writer,
        record_ceiling=2,
        encoded_byte_ceiling=10_000,
    )
    writer = BytesIO()
    encoder = CanonicalJsonlSegmentEncoder(
        writer,
        record_ceiling=2,
        encoded_byte_ceiling=10_000,
    )

    for record in records:
        encoder.append(record)
    observed = encoder.finish()

    assert observed == expected
    assert writer.getvalue() == expected_writer.getvalue()


def test_incremental_encoder_cannot_append_after_finish() -> None:
    encoder = CanonicalJsonlSegmentEncoder(
        BytesIO(),
        record_ceiling=1,
        encoded_byte_ceiling=10_000,
    )
    encoder.finish()

    with pytest.raises(RuntimeError, match="finished"):
        encoder.append(_record())


def test_artifact_reader_yields_bounded_chunks_until_eof() -> None:
    reader = BytesIO(b"abcdefghij")

    chunks = tuple(iter_artifact_chunks(reader, chunk_size=4))

    assert chunks == (b"abcd", b"efgh", b"ij")


def test_artifact_reader_accepts_one_byte_chunks() -> None:
    reader = BytesIO(b"ab")

    chunks = tuple(iter_artifact_chunks(reader, chunk_size=1))

    assert chunks == (b"a", b"b")


class _OversizedReader:
    def read(self, size: int = -1) -> bytes:
        return b"x" * (size + 1)


class _NonBytesReader:
    def read(self, size: int = -1) -> object:
        return "not-bytes"


def test_artifact_reader_rejects_a_chunk_larger_than_the_requested_bound() -> None:
    with pytest.raises(OSError) as captured:
        tuple(iter_artifact_chunks(_OversizedReader(), chunk_size=4))

    assert str(captured.value) == "artifact reader exceeded requested chunk size"


def test_artifact_reader_rejects_a_non_bytes_chunk() -> None:
    with pytest.raises(TypeError) as captured:
        reader = cast(AcquisitionArtifactReader, _NonBytesReader())
        tuple(iter_artifact_chunks(reader, chunk_size=4))

    assert str(captured.value) == "artifact reader returned non-bytes chunk"


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_artifact_reader_rejects_non_positive_chunk_size(chunk_size: int) -> None:
    with pytest.raises(ValueError) as captured:
        tuple(iter_artifact_chunks(BytesIO(b"value"), chunk_size=chunk_size))

    assert str(captured.value) == "chunk_size must be positive"
