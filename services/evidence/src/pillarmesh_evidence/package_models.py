from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pillarmesh_contract_model import ArtifactModel
from pydantic import Field, field_validator

type ArtifactKind = Literal[
    "integration_contract",
    "provider_observation",
    "intent_ir",
    "physical_plan",
    "legality_decision",
    "signed_execution_graph",
    "activation_summary",
    "source_boundary",
    "segment_manifest",
    "commit_receipt",
    "visibility_proof",
]


class ResourceDisposition(ArtifactModel):
    resource_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # "indeterminate" records an attempted creation whose outcome is unknown -- the
    # honest disposition for any resource still in flight when the package is written
    # (the package directory itself always is). Dropping it would force a false
    # created/not_created claim into the auditor-facing operations metadata.
    creation_state: Literal["created", "preexisting", "not_created", "indeterminate"]
    retention_deadline: datetime
    cleanup_status: Literal["scheduled", "completed", "not_required", "quarantined"]
    cleanup_operation: Literal[
        "delete_synthetic_rows",
        "delete_staged_segments",
        "delete_local_state",
        "none",
    ]

    @field_validator("retention_deadline")
    @classmethod
    def aware_retention_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retention deadline must be timezone-aware")
        return value


class PackageIdentityMetadata(ArtifactModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    uv_lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str = Field(pattern=r"^3\.13\.\d+$")
    mcp_protocol_version: str = Field(min_length=1, max_length=64)
    operator_pseudonym: str = Field(min_length=1, max_length=128)
    host_pseudonym: str = Field(min_length=1, max_length=128)
    transport_decision: Literal["cli-fallback", "desktop-mcp"]


class PackageMetadata(PackageIdentityMetadata):
    resources: tuple[ResourceDisposition, ...] = ()
    limitations: tuple[str, ...] = ()


class ScanInput(ArtifactModel):
    credential_canaries: tuple[str, ...] = ()
    row_value_canaries: tuple[str, ...] = ()
    acceptance_keys: tuple[int, ...] = ()
    local_path_prefixes: tuple[str, ...] = ()

    @field_validator("credential_canaries", "row_value_canaries", "local_path_prefixes")
    @classmethod
    def non_empty_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("scan values must not be empty")
        return values


class ScanFinding(ArtifactModel):
    rule_id: str
    byte_offset: int = Field(ge=0)


class ArtifactEntry(ArtifactModel):
    kind: ArtifactKind
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactEdge(ArtifactModel):
    parent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relationship: str = Field(min_length=1, max_length=64)


class PackageIndex(PackageIdentityMetadata):
    format_version: Literal[1] = 1
    run_id: str
    artifacts: tuple[ArtifactEntry, ...]
    edges: tuple[ArtifactEdge, ...]
    trace_head_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: dict[str, str]


class CheckDisposition(ArtifactModel):
    name: Literal[
        "package_structure",
        "payload_hashes",
        "artifact_digests",
        "artifact_graph",
        "graph_signature",
        "event_chain",
        "terminal_visibility",
        "sensitive_value_scan",
    ]
    outcome: Literal["passed"] = "passed"


class VerificationResult(ArtifactModel):
    verifier_version: Literal["pillarmesh-m0-package-v1"] = "pillarmesh-m0-package-v1"
    checks: tuple[CheckDisposition, ...]
    package_index_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PackageResult(ArtifactModel):
    path: Path
    package_index_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[CheckDisposition, ...]
