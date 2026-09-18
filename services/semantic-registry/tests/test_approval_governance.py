from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heinzel_contract_model import ContractFormationStatus
from heinzel_semantic_registry import ApprovedSemanticCompiler

from .test_approval import NOW, _StrictSemanticVersionRepository, valid_input


def test_compiler_requires_exact_current_authority_and_decision_binding_for_each_item() -> None:
    compilation_input = valid_input()
    stale = compilation_input.authority_observations[0].model_copy(
        update={
            "observed_at": NOW - timedelta(days=2),
            "valid_until": NOW - timedelta(days=1),
        }
    )

    result = ApprovedSemanticCompiler(_StrictSemanticVersionRepository()).compile(
        compilation_input.model_copy(update={"authority_observations": (stale,)}), now=NOW
    )

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert "authority:identity:customer:stale" in result.no_valid_plan.constraints


def test_compiler_returns_governed_no_valid_plan_for_unknown_or_conflicting_contract_values() -> (
    None
):
    compilation_input = valid_input()
    conflicting = compilation_input.authority_observations[0].model_copy(
        update={"observation_id": "observation-0002", "observed_digest": "f" * 64}
    )

    result = ApprovedSemanticCompiler(_StrictSemanticVersionRepository()).compile(
        compilation_input.model_copy(
            update={
                "authority_observations": (compilation_input.authority_observations[0], conflicting)
            }
        ),
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert "authority:identity:customer:conflicting" in result.no_valid_plan.constraints


def test_compiler_canonicalizes_unordered_inputs_before_identity_and_digest() -> None:
    compilation_input = valid_input()
    reversed_input = compilation_input.model_copy(
        update={
            "approval_ids": tuple(reversed(compilation_input.approval_ids)),
            "authority_observations": tuple(reversed(compilation_input.authority_observations)),
        }
    )

    first = ApprovedSemanticCompiler(_StrictSemanticVersionRepository()).compile(
        compilation_input, now=NOW
    )
    second = ApprovedSemanticCompiler(_StrictSemanticVersionRepository()).compile(
        reversed_input, now=NOW
    )

    assert first.semantic_version_id == second.semantic_version_id
    assert first.version == second.version
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})
