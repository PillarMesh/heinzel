from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self

from pillarmesh_contract_model import ArtifactModel, ArtifactReference, canonical_bytes, digest
from pydantic import Field, field_validator, model_validator

from .answer_models import AnswerInterpreterKind

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class FilterDomain(ArtifactModel):
    dimension_ref: str = Field(min_length=1)
    values: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def values_are_unique(self) -> Self:
        if len(self.values) != len(set(self.values)):
            raise ValueError("filter domain values must not contain duplicates")
        return self


class AnswerScopePolicyTerms(ArtifactModel):
    schema_version: Literal["1"] = "1"
    policy_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    prior_policy_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    principal_scope: tuple[str, ...] = Field(min_length=1)
    purposes: tuple[str, ...] = Field(min_length=1)
    semantic_version_ref: ArtifactReference
    data_product_version_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    metric_version_refs: tuple[ArtifactReference, ...]
    dimension_refs: tuple[ArtifactReference, ...]
    filter_domains: tuple[FilterDomain, ...]
    max_time_window: int = Field(gt=0)
    max_staleness: int = Field(ge=0)
    quality_disposition: Literal["block", "label"]
    disclosure_classifications: tuple[str, ...]
    disclosure_entity: str = Field(min_length=1)
    minimum_group_size: int = Field(gt=0)
    restatement_confirmation: Literal["always", "model_interpreted", "never"] = "model_interpreted"
    row_ceiling: int = Field(gt=0)
    byte_ceiling: int = Field(gt=0)
    scan_ceiling: int = Field(gt=0)
    period_scan_budget: int = Field(gt=0)
    statement_timeout: int = Field(gt=0)
    result_retention: int = Field(gt=0)
    agent_access: Literal["allowed", "denied"]
    model_disclosure: Literal["none", "metadata", "results"]
    valid_from: datetime
    valid_until: datetime

    @field_validator("valid_from", "valid_until")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def revision_and_scope_are_consistent(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.revision == 1 and self.prior_policy_digest is not None:
            raise ValueError("first policy revision cannot have a prior digest")
        if self.revision > 1 and self.prior_policy_digest is None:
            raise ValueError("superseding policy revision requires a prior digest")
        for field_name in (
            "principal_scope",
            "purposes",
            "data_product_version_refs",
            "metric_version_refs",
            "dimension_refs",
            "filter_domains",
            "disclosure_classifications",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        return self

    def requires_restatement_confirmation(self, *, interpreter: AnswerInterpreterKind) -> bool:
        return self.restatement_confirmation == "always" or (
            self.restatement_confirmation == "model_interpreted" and interpreter == "model"
        )


class AnswerScopePolicyDraft(AnswerScopePolicyTerms):
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class AnswerScopePolicy(AnswerScopePolicyTerms):
    approval_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("approval_ids")
    @classmethod
    def approval_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("approval_ids must not contain duplicates")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    def canonical_digest(self) -> str:
        return digest(self)


class AnswerScopePolicyApproval(ArtifactModel):
    approval_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(gt=0)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    authority_ref: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class MissingAnswerPolicyApproval(ValueError):
    def __init__(self, missing_authority_refs: tuple[str, ...]) -> None:
        self.missing_authority_refs = missing_authority_refs
        super().__init__(f"missing answer policy approvals: {', '.join(missing_authority_refs)}")


class ProductOwnerAuthority(ArtifactModel):
    data_product_version_ref: ArtifactReference
    authority_ref: str = Field(min_length=1)


class AnswerScopePolicyRepository(Protocol):
    def list_revisions(self, tenant_id: str, policy_id: str) -> tuple[AnswerScopePolicy, ...]: ...

    def save(
        self,
        policy: AnswerScopePolicy,
        *,
        approvals: tuple[AnswerScopePolicyApproval, ...],
    ) -> None: ...


class SQLiteAnswerScopePolicyRepository:
    def __init__(self, connection: sqlite3.Connection, *, owns_connection: bool = False) -> None:
        self._connection = connection
        self._owns_connection = owns_connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS answer_scope_policies ("
            "tenant_id TEXT NOT NULL, policy_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "policy_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, policy_id, revision), "
            "UNIQUE (tenant_id, policy_id, policy_digest));"
            "CREATE TABLE IF NOT EXISTS answer_scope_policy_approvals ("
            "tenant_id TEXT NOT NULL, approval_id TEXT NOT NULL, policy_id TEXT NOT NULL, "
            "policy_revision INTEGER NOT NULL, approval_position INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, approval_id), "
            "UNIQUE (tenant_id, policy_id, policy_revision, approval_position), "
            "FOREIGN KEY (tenant_id, policy_id, policy_revision) REFERENCES "
            "answer_scope_policies (tenant_id, policy_id, revision));"
        )

    @classmethod
    def open(cls, path: Path | str) -> Self:
        return cls(sqlite3.connect(path), owns_connection=True)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def list_revisions(self, tenant_id: str, policy_id: str) -> tuple[AnswerScopePolicy, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM answer_scope_policies "
            "WHERE tenant_id = ? AND policy_id = ? ORDER BY revision",
            (tenant_id, policy_id),
        ).fetchall()
        return tuple(
            AnswerScopePolicy.model_validate_json(bytes(row[0]), strict=True) for row in rows
        )

    def load_latest(self, tenant_id: str, policy_id: str) -> AnswerScopePolicy | None:
        row = self._connection.execute(
            "SELECT payload FROM answer_scope_policies "
            "WHERE tenant_id = ? AND policy_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, policy_id),
        ).fetchone()
        if row is None:
            return None
        return AnswerScopePolicy.model_validate_json(bytes(row[0]), strict=True)

    def list_approvals(
        self, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[AnswerScopePolicyApproval, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM answer_scope_policy_approvals "
            "WHERE tenant_id = ? AND policy_id = ? AND policy_revision = ? "
            "ORDER BY approval_position",
            (tenant_id, policy_id, policy_revision),
        ).fetchall()
        return tuple(
            AnswerScopePolicyApproval.model_validate_json(bytes(row[0]), strict=True)
            for row in rows
        )

    def save(
        self,
        policy: AnswerScopePolicy,
        *,
        approvals: tuple[AnswerScopePolicyApproval, ...],
    ) -> None:
        draft = AnswerScopePolicyDraft.model_validate(
            policy.model_dump(mode="python", exclude={"approval_ids"}), strict=True
        )
        draft_digest = digest(draft)
        if tuple(approval.approval_id for approval in approvals) != policy.approval_ids:
            raise ValueError("policy approvals do not match the activated approval identifiers")
        if any(
            approval.tenant_id != policy.tenant_id
            or approval.policy_id != policy.policy_id
            or approval.policy_revision != policy.revision
            or approval.policy_digest != draft_digest
            or approval.decision != "approve"
            for approval in approvals
        ):
            raise ValueError("policy approval does not bind the activated policy")

        with self._connection:
            row = self._connection.execute(
                "SELECT payload FROM answer_scope_policies "
                "WHERE tenant_id = ? AND policy_id = ? AND revision = ?",
                (policy.tenant_id, policy.policy_id, policy.revision),
            ).fetchone()
            if row is not None:
                recorded = AnswerScopePolicy.model_validate_json(bytes(row[0]), strict=True)
                recorded_approvals = self.list_approvals(
                    policy.tenant_id, policy.policy_id, policy.revision
                )
                if recorded != policy or recorded_approvals != approvals:
                    raise ValueError("policy activation replay conflicts with recorded authority")
                return
            self._connection.execute(
                "INSERT INTO answer_scope_policies "
                "(tenant_id, policy_id, revision, policy_digest, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    policy.tenant_id,
                    policy.policy_id,
                    policy.revision,
                    policy.canonical_digest(),
                    canonical_bytes(policy),
                ),
            )
            self._connection.executemany(
                "INSERT INTO answer_scope_policy_approvals "
                "(tenant_id, approval_id, policy_id, policy_revision, approval_position, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                tuple(
                    (
                        approval.tenant_id,
                        approval.approval_id,
                        approval.policy_id,
                        approval.policy_revision,
                        position,
                        canonical_bytes(approval),
                    )
                    for position, approval in enumerate(approvals)
                ),
            )


class AnswerScopePolicyLifecycle:
    def __init__(
        self,
        repository: AnswerScopePolicyRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._clock = clock

    def activate(
        self,
        *,
        draft: AnswerScopePolicyDraft,
        approvals: tuple[AnswerScopePolicyApproval, ...],
        product_owner_bindings: tuple[ProductOwnerAuthority, ...],
        period_scan_budget_threshold: int,
    ) -> AnswerScopePolicy:
        revisions = self._repository.list_revisions(draft.tenant_id, draft.policy_id)
        latest = max(revisions, key=lambda item: item.revision, default=None)
        is_replay = latest is not None and draft.revision == latest.revision
        expected_revision = 1 if latest is None else latest.revision + 1
        if draft.revision != expected_revision and not is_replay:
            raise ValueError("policy revision does not immediately supersede the latest revision")
        expected_prior_digest = None if latest is None else latest.canonical_digest()
        if not is_replay and draft.prior_policy_digest != expected_prior_digest:
            raise ValueError("prior policy digest does not match the latest revision")

        if period_scan_budget_threshold < 0:
            raise ValueError("period scan budget threshold must not be negative")
        owner_products = tuple(
            binding.data_product_version_ref for binding in product_owner_bindings
        )
        if len(owner_products) != len(set(owner_products)) or set(owner_products) != set(
            draft.data_product_version_refs
        ):
            raise ValueError("owner authority must resolve exactly once for every data product")

        required = [
            "role:data_engineering_architect",
            *(binding.authority_ref for binding in product_owner_bindings),
        ]
        if (
            draft.disclosure_classifications
            or draft.filter_domains
            or draft.minimum_group_size > 1
            or draft.model_disclosure == "results"
        ):
            required.append("role:policy_authority")
        if draft.period_scan_budget > period_scan_budget_threshold:
            required.append("role:budget_authority")

        approval_authorities = {
            approval.authority_ref
            for approval in approvals
            if approval.decision == "approve"
            and approval.tenant_id == draft.tenant_id
            and approval.policy_id == draft.policy_id
            and approval.policy_revision == draft.revision
            and approval.policy_digest == digest(draft)
        }
        missing = tuple(
            dict.fromkeys(item for item in required if item not in approval_authorities)
        )
        if missing:
            raise MissingAnswerPolicyApproval(missing)

        admitted_approvals = tuple(
            approval
            for approval in approvals
            if approval.decision == "approve"
            and approval.tenant_id == draft.tenant_id
            and approval.policy_id == draft.policy_id
            and approval.policy_revision == draft.revision
            and approval.policy_digest == digest(draft)
        )
        policy = AnswerScopePolicy(
            **draft.model_dump(),
            approval_ids=tuple(approval.approval_id for approval in admitted_approvals),
        )
        if is_replay and policy != latest:
            raise ValueError("policy activation replay conflicts with recorded authority")
        self._repository.save(policy, approvals=admitted_approvals)
        return policy

    def resolve_current(self, *, tenant_id: str, policy_id: str) -> AnswerScopePolicy | None:
        revisions = self._repository.list_revisions(tenant_id, policy_id)
        if not revisions:
            return None
        latest = max(revisions, key=lambda item: item.revision)
        now = _utc(self._clock(), "clock")
        if not latest.valid_from <= now < latest.valid_until:
            return None
        return latest
