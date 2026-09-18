from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from typing import Literal, Protocol

from heinzel_contract_model import canonical_bytes, digest
from pydantic import Field

from .acquisition_models import AcquisitionRecord
from .acquisition_protocols import AcquisitionArtifactReader
from .models import ProviderModel

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_RECORD_SET_DOMAIN = b"heinzel-acquisition-record-set-v1\0"


class _AcquisitionArtifactWriter(Protocol):
    def write(self, value: bytes) -> int: ...


class EncodedAcquisitionSegment(ProviderModel):
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    record_set_digest: str = Field(pattern=_DIGEST_PATTERN)
    record_count: int = Field(ge=0)
    encoded_bytes: int = Field(ge=0)


class CanonicalJsonlSegmentEncoder:
    def __init__(
        self,
        writer: _AcquisitionArtifactWriter,
        *,
        record_ceiling: int,
        encoded_byte_ceiling: int,
    ) -> None:
        self._writer = writer
        self._record_ceiling = record_ceiling
        self._encoded_byte_ceiling = encoded_byte_ceiling
        self._content_hasher = hashlib.sha256()
        self._record_set_hasher = hashlib.sha256()
        self._record_set_hasher.update(_RECORD_SET_DOMAIN)
        self._record_count = 0
        self._encoded_bytes = 0
        self._finished = False

    def append(self, record: AcquisitionRecord) -> None:
        if self._finished:
            raise RuntimeError("canonical JSONL segment encoder is finished")
        record_payload = canonical_bytes(record)
        line = record_payload + b"\n"
        if self._record_count + 1 > self._record_ceiling:
            raise AcquisitionCeilingExceeded(
                logical_object_ref=record.logical_object_ref,
                limit_kind="records",
                ceiling=self._record_ceiling,
            )
        if self._encoded_bytes + len(line) > self._encoded_byte_ceiling:
            raise AcquisitionCeilingExceeded(
                logical_object_ref=record.logical_object_ref,
                limit_kind="encoded_bytes",
                ceiling=self._encoded_byte_ceiling,
            )

        _write_all(self._writer, line)
        self._content_hasher.update(line)
        record_digest = bytes.fromhex(digest(record))
        self._record_set_hasher.update(len(record_digest).to_bytes(8, "big"))
        self._record_set_hasher.update(record_digest)
        self._record_count += 1
        self._encoded_bytes += len(line)

    def finish(self) -> EncodedAcquisitionSegment:
        self._finished = True
        return EncodedAcquisitionSegment(
            content_digest=self._content_hasher.hexdigest(),
            record_set_digest=self._record_set_hasher.hexdigest(),
            record_count=self._record_count,
            encoded_bytes=self._encoded_bytes,
        )


class AcquisitionCeilingExceeded(RuntimeError):
    def __init__(
        self,
        *,
        logical_object_ref: str,
        limit_kind: Literal["records", "encoded_bytes"],
        ceiling: int,
    ) -> None:
        label = "record" if limit_kind == "records" else "encoded byte"
        super().__init__(f"{label} ceiling exceeded for {logical_object_ref}")
        self.logical_object_ref = logical_object_ref
        self.limit_kind = limit_kind
        self.ceiling = ceiling


def _write_all(writer: _AcquisitionArtifactWriter, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = writer.write(value[offset:])
        if written <= 0 or written > len(value) - offset:
            raise OSError("artifact writer made invalid progress")
        offset += written


def encode_canonical_jsonl(
    records: Iterable[AcquisitionRecord],
    writer: _AcquisitionArtifactWriter,
    *,
    record_ceiling: int,
    encoded_byte_ceiling: int,
) -> EncodedAcquisitionSegment:
    encoder = CanonicalJsonlSegmentEncoder(
        writer,
        record_ceiling=record_ceiling,
        encoded_byte_ceiling=encoded_byte_ceiling,
    )

    for record in records:
        encoder.append(record)
    return encoder.finish()


def iter_artifact_chunks(
    reader: AcquisitionArtifactReader, *, chunk_size: int = 64 * 1024
) -> Iterator[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    while True:
        chunk = reader.read(chunk_size)
        if not isinstance(chunk, bytes):
            raise TypeError("artifact reader returned non-bytes chunk")
        if not chunk:
            return
        if len(chunk) > chunk_size:
            raise OSError("artifact reader exceeded requested chunk size")
        yield chunk
