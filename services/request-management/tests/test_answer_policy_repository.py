from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AnswerScopePolicy,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    AnswerScopePolicyLifecycle,
    ProductOwnerAuthority,
    SQLiteAnswerScopePolicyRepository,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _reference(identifier: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=1, digest="a" * 64)


def _draft(*, tenant_id: str = "tenant-1", **changes: object) -> AnswerScopePolicyDraft:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "tenant_id": tenant_id,
        "revision": 1,
        "prior_policy_digest": None,
        "principal_scope": (f"principal:{tenant_id}",),
        "purposes": ("operations",),
        "semantic_version_ref": _reference("semantic"),
        "data_product_version_refs": (_reference("orders-product"),),
        "metric_version_refs": (_reference("net-revenue"),),
        "dimension_refs": (),
        "filter_domains": (),
        "max_time_window": 3600,
        "max_staleness": 300,
        "quality_disposition": "block",
        "disclosure_classifications": (),
        "disclosure_entity": "customer",
        "minimum_group_size": 1,
        "row_ceiling": 100,
        "byte_ceiling": 100_000,
        "scan_ceiling": 1_000,
        "period_scan_budget": 10_000,
        "statement_timeout": 30,
        "result_retention": 3600,
        "agent_access": "allowed",
        "model_disclosure": "metadata",
        "valid_from": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(days=1),
        "created_at": NOW,
    }
    values.update(changes)
    return AnswerScopePolicyDraft.model_validate(values)


def _approvals(draft: AnswerScopePolicyDraft) -> tuple[AnswerScopePolicyApproval, ...]:
    return tuple(
        AnswerScopePolicyApproval(
            approval_id=f"{draft.tenant_id}-{draft.revision}-{identifier}",
            tenant_id=draft.tenant_id,
            policy_id=draft.policy_id,
            policy_revision=draft.revision,
            policy_digest=digest(draft),
            authority_ref=authority_ref,
            actor_id=f"actor:{identifier}",
            decision="approve",
            created_at=NOW,
        )
        for identifier, authority_ref in (
            ("architect", "role:data_engineering_architect"),
            ("owner", "owner:orders-product"),
        )
    )


def _activate(
    lifecycle: AnswerScopePolicyLifecycle, draft: AnswerScopePolicyDraft
) -> AnswerScopePolicy:
    return lifecycle.activate(
        draft=draft,
        approvals=_approvals(draft),
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=_reference("orders-product"),
                authority_ref="owner:orders-product",
            ),
        ),
        period_scan_budget_threshold=20_000,
    )


def test_sqlite_policy_repository_persists_exact_policy_and_approval_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answer-policy.sqlite3"
    repository = SQLiteAnswerScopePolicyRepository.open(path)
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    draft = _draft()

    first = _activate(lifecycle, draft)
    second_draft = _draft(
        revision=2,
        prior_policy_digest=first.canonical_digest(),
        created_at=NOW + timedelta(minutes=1),
    )
    second = _activate(lifecycle, second_draft)
    repository.close()

    reopened = SQLiteAnswerScopePolicyRepository.open(path)
    assert reopened.load_latest("tenant-1", "policy-1") == second
    assert reopened.list_revisions("tenant-1", "policy-1") == (first, second)
    assert reopened.list_approvals("tenant-1", "policy-1", 1) == _approvals(draft)
    assert reopened.list_approvals("tenant-1", "policy-1", 2) == _approvals(second_draft)
    reopened.close()


def test_policy_activation_replay_returns_the_record_without_duplicate_approvals(
    tmp_path: Path,
) -> None:
    repository = SQLiteAnswerScopePolicyRepository.open(tmp_path / "answer-policy.sqlite3")
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    draft = _draft()

    first = _activate(lifecycle, draft)
    replay = _activate(lifecycle, draft)

    assert replay == first
    assert repository.list_revisions("tenant-1", "policy-1") == (first,)
    assert repository.list_approvals("tenant-1", "policy-1", 1) == _approvals(draft)
    repository.close()


def test_policy_activation_replay_rejects_changed_approval_evidence(tmp_path: Path) -> None:
    repository = SQLiteAnswerScopePolicyRepository.open(tmp_path / "answer-policy.sqlite3")
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    draft = _draft()
    _activate(lifecycle, draft)
    changed = tuple(
        approval.model_copy(update={"actor_id": f"changed:{approval.actor_id}"})
        for approval in _approvals(draft)
    )

    with pytest.raises(
        ValueError, match="policy activation replay conflicts with recorded authority"
    ):
        lifecycle.activate(
            draft=draft,
            approvals=changed,
            product_owner_bindings=(
                ProductOwnerAuthority(
                    data_product_version_ref=_reference("orders-product"),
                    authority_ref="owner:orders-product",
                ),
            ),
            period_scan_budget_threshold=20_000,
        )
    assert repository.list_approvals("tenant-1", "policy-1", 1) == _approvals(draft)
    repository.close()


def test_policy_lookup_is_tenant_scoped_and_expired_latest_revision_is_not_current(
    tmp_path: Path,
) -> None:
    repository = SQLiteAnswerScopePolicyRepository.open(tmp_path / "answer-policy.sqlite3")
    lifecycle = AnswerScopePolicyLifecycle(repository, clock=lambda: NOW)
    first = _activate(lifecycle, _draft(tenant_id="tenant-1"))
    expired = _activate(
        lifecycle,
        _draft(
            tenant_id="tenant-2",
            valid_from=NOW - timedelta(days=2),
            valid_until=NOW - timedelta(days=1),
        ),
    )

    assert repository.list_revisions("tenant-1", "policy-1") == (first,)
    assert repository.list_revisions("tenant-2", "policy-1") == (expired,)
    assert repository.list_revisions("tenant-3", "policy-1") == ()
    assert lifecycle.resolve_current(tenant_id="tenant-1", policy_id="policy-1") == first
    assert lifecycle.resolve_current(tenant_id="tenant-2", policy_id="policy-1") is None
    at_expiry = AnswerScopePolicyLifecycle(repository, clock=lambda: first.valid_until)
    assert at_expiry.resolve_current(tenant_id="tenant-1", policy_id="policy-1") is None
    repository.close()
