from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from .config import REPOSITORY_ROOT, HarnessError, _has_symlink_component, _inside
from .private_files import atomic_private_replace, read_private_file, require_private_directory

THIRTY_DAYS = 30 * 24 * 60 * 60
ONE_DAY = 24 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60

CleanupOperation = Literal[
    "delete_synthetic_rows", "delete_staged_segments", "delete_local_state", "none"
]
CreationState = Literal["created", "preexisting", "not_created", "indeterminate"]
CleanupStatus = Literal["scheduled", "completed", "not_required", "quarantined"]


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _PrivateResource:
    kind: str
    exact_identifier: str
    resource_digest: str
    creation_state: CreationState
    retention_deadline: datetime
    cleanup_status: CleanupStatus
    cleanup_operation: CleanupOperation


class PrivateResourceLedger:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime],
        writer: Callable[[bytes], None] | None = None,
    ) -> None:
        self.path = path
        self._clock = clock
        self._writer = writer
        self._resources: dict[str, _PrivateResource] = {}
        self._context: dict[str, object] = {}

    @staticmethod
    def _digest(kind: str, exact_identifier: str) -> str:
        payload = f"heinzel-resource-v1\0{kind}\0{exact_identifier}".encode()
        return hashlib.sha256(payload).hexdigest()

    def register(
        self,
        *,
        kind: str,
        exact_identifier: str,
        retention_seconds: int,
        cleanup_operation: CleanupOperation,
    ) -> str:
        resource_digest = self._digest(kind, exact_identifier)
        existing = self._resources.get(resource_digest)
        if existing is not None:
            return existing.resource_digest
        self._resources[resource_digest] = _PrivateResource(
            kind=kind,
            exact_identifier=exact_identifier,
            resource_digest=resource_digest,
            creation_state="not_created",
            retention_deadline=self._clock() + timedelta(seconds=retention_seconds),
            cleanup_status="scheduled",
            cleanup_operation=cleanup_operation,
        )
        return resource_digest

    def mark_attempted(self, resource_digest: str) -> None:
        self._resources[resource_digest].creation_state = "indeterminate"

    def mark_created(self, resource_digest: str) -> None:
        self._resources[resource_digest].creation_state = "created"

    def mark_not_created(self, resource_digest: str) -> None:
        self._resources[resource_digest].creation_state = "not_created"

    def mark_quarantined(self, resource_digest: str) -> None:
        resource = self._resources[resource_digest]
        resource.cleanup_status = "quarantined"
        resource.retention_deadline = self._clock() + timedelta(seconds=SEVEN_DAYS)

    def mark_scheduled(self, resource_digest: str, *, retention_seconds: int) -> None:
        resource = self._resources[resource_digest]
        resource.cleanup_status = "scheduled"
        resource.retention_deadline = self._clock() + timedelta(seconds=retention_seconds)

    def mark_completed(self, resource_digest: str) -> None:
        self._resources[resource_digest].cleanup_status = "completed"

    def set_context(self, **values: object) -> None:
        self._context.update(values)

    def sanitized_dispositions(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "resource_digest": resource.resource_digest,
                "creation_state": resource.creation_state,
                "retention_deadline": utc_text(resource.retention_deadline),
                "cleanup_status": resource.cleanup_status,
                "cleanup_operation": resource.cleanup_operation,
            }
            for resource in sorted(self._resources.values(), key=lambda item: item.resource_digest)
        )

    def resources_by_kind(self) -> dict[str, str]:
        return {resource.kind: resource.resource_digest for resource in self._resources.values()}

    def persist(self, *, run_state: str) -> None:
        payload = {
            "format_version": 1,
            "owner_authorization_reference": self._context.get("owner_authorization_reference"),
            "run_state": run_state,
            "context": self._context,
            "resources": [
                {
                    "kind": resource.kind,
                    "exact_identifier": resource.exact_identifier,
                    "resource_digest": resource.resource_digest,
                    "creation_state": resource.creation_state,
                    "retention_deadline": utc_text(resource.retention_deadline),
                    "cleanup_status": resource.cleanup_status,
                    "cleanup_operation": resource.cleanup_operation,
                }
                for resource in sorted(
                    self._resources.values(), key=lambda item: item.resource_digest
                )
            ],
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if self._writer is None:
            atomic_private_replace(self.path, encoded)
        else:
            self._writer(encoded)


def cleanup_status(
    ledger_path: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[dict[str, str], ...]:
    if (
        not ledger_path.is_absolute()
        or _inside(ledger_path, repository_root)
        or _has_symlink_component(ledger_path)
    ):
        raise HarnessError("private cleanup ledger is unsafe")
    try:
        require_private_directory(ledger_path.parent)
        payload = read_private_file(ledger_path)
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        raise HarnessError("private cleanup ledger is invalid") from None
    resources = raw.get("resources")
    if not isinstance(resources, list):
        raise HarnessError("private cleanup ledger is invalid")
    status: list[dict[str, str]] = []
    for item in resources:
        if not isinstance(item, dict):
            raise HarnessError("private cleanup ledger is invalid")
        resource_digest = item.get("resource_digest")
        kind = item.get("kind")
        creation_state = item.get("creation_state")
        cleanup_value = item.get("cleanup_status")
        cleanup_operation = item.get("cleanup_operation")
        retention_deadline = item.get("retention_deadline")
        valid_deadline = False
        if isinstance(retention_deadline, str):
            try:
                parsed_deadline = datetime.fromisoformat(retention_deadline.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                valid_deadline = utc_text(parsed_deadline) == retention_deadline
        if (
            not isinstance(resource_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", resource_digest) is None
            or kind
            not in {
                "source_row",
                "target_row",
                "commit_ledger_entry",
                "staged_segment",
                "local_segment",
                "local_state",
                "local_output",
                "evidence_package",
            }
            or creation_state not in {"created", "preexisting", "not_created", "indeterminate"}
            or cleanup_value not in {"scheduled", "completed", "not_required", "quarantined"}
            or cleanup_operation
            not in {
                "delete_synthetic_rows",
                "delete_staged_segments",
                "delete_local_state",
                "none",
            }
            or not valid_deadline
        ):
            raise HarnessError("private cleanup ledger is invalid")
        status.append(
            {
                "resource_digest": resource_digest,
                "kind": str(kind),
                "creation_state": str(creation_state),
                "cleanup_status": str(cleanup_value),
                "cleanup_operation": str(cleanup_operation),
                "retention_deadline": cast(str, retention_deadline),
            }
        )
    return tuple(sorted(status, key=lambda item: item["resource_digest"]))


def live_diagnostic_ledger_path(
    environment: dict[str, str] | os._Environ[str],
    label: str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    raw = environment.get("HEINZEL_LIVE_DIAGNOSTIC_LEDGER_DIR")
    if not raw:
        raise HarnessError("missing required variables: HEINZEL_LIVE_DIAGNOSTIC_LEDGER_DIR")
    directory = Path(raw).expanduser()
    if not directory.is_absolute() or _inside(directory, repository_root):
        raise HarnessError("live diagnostic ledger directory is unsafe")
    require_private_directory(directory)
    if re.fullmatch(r"[a-z0-9-]+", label) is None:
        raise HarnessError("live diagnostic ledger label is invalid")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{label}-{stamp}-{secrets.token_hex(8)}.json"
