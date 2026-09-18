from __future__ import annotations

import csv
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import OrderRow, SegmentManifest, SourceBoundary

_HEADER = ("order_id", "customer_ref", "amount", "currency", "order_status", "updated_at")
_SCHEMA_DIGEST = digest(
    {
        "columns": (
            ("order_id", "NUMBER(19,0)"),
            ("customer_ref", "VARCHAR(65535)"),
            ("amount", "NUMBER(18,2)"),
            ("currency", "VARCHAR(3)"),
            ("order_status", "VARCHAR(65535)"),
            ("updated_at", "TIMESTAMP_TZ(6)"),
        ),
        "format": "rfc4180-utf8-lf",
    }
)


@dataclass(frozen=True, slots=True)
class EncodingLimits:
    max_rows: int = 10_000
    max_bytes: int = 64 * 1024 * 1024


class ResourceLimitExceeded(ValueError):
    pass


def _row_values(row: OrderRow) -> tuple[object, ...]:
    return (
        row.order_id,
        row.customer_ref,
        format(row.amount, ".2f"),
        row.currency,
        row.status,
        row.updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


def _extend_row_set(hasher: object, row: OrderRow) -> None:
    payload = canonical_bytes({"domain": "heinzel-row-v1", "row": row})
    hasher.update(len(payload).to_bytes(8, "big"))  # type: ignore[attr-defined]
    hasher.update(payload)  # type: ignore[attr-defined]


def encode_segment(
    rows: Iterable[OrderRow],
    boundary: SourceBoundary | Callable[[], SourceBoundary],
    acceptance_key: int,
    batch_id: str,
    output: Path,
    *,
    limits: EncodingLimits | None = None,
) -> SegmentManifest:
    effective_limits = limits or EncodingLimits()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    row_set_hasher = hashlib.sha256()
    accepted_row: OrderRow | None = None
    row_count = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(_HEADER)
            for row in rows:
                row_count += 1
                if row_count > effective_limits.max_rows:
                    raise ResourceLimitExceeded(
                        f"row ceiling exceeded: {row_count} > {effective_limits.max_rows}"
                    )
                writer.writerow(_row_values(row))
                _extend_row_set(row_set_hasher, row)
                if row.order_id == acceptance_key:
                    accepted_row = row
                stream.flush()
                if stream.tell() > effective_limits.max_bytes:
                    raise ResourceLimitExceeded(
                        f"byte ceiling exceeded: {stream.tell()} > {effective_limits.max_bytes}"
                    )

        if accepted_row is None:
            raise ValueError(f"acceptance key {acceptance_key} is absent from the snapshot")
        encoded_bytes = temporary_path.stat().st_size
        segment_digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        os.replace(temporary_path, output)
        temporary_path = None
        final_boundary = boundary() if callable(boundary) else boundary
        return SegmentManifest(
            batch_id=batch_id,
            segment_name="segment.csv",
            segment_digest=segment_digest,
            row_set_digest=row_set_hasher.hexdigest(),
            row_count=row_count,
            encoded_bytes=encoded_bytes,
            schema_digest=_SCHEMA_DIGEST,
            source_boundary_digest=digest(final_boundary),
            acceptance_value_digest=digest(accepted_row),
        )
    except BaseException:
        output.unlink(missing_ok=True)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
