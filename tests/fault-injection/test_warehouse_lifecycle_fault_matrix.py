from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pillarmesh_warehouse_control as warehouse_control
import pytest
from pillarmesh_contract_model import digest
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseFailureClassification,
    WarehouseLifecycleCheckpoint,
    WarehousePersistenceError,
)

import tests.acceptance.plan3a_fault_matrix as fault_matrix_module
from tests.acceptance.plan3a_fault_matrix import (
    WarehouseFaultScenarioOutcome,
    canonical_fault_matrix_outcomes,
    required_fault_scenario_ids,
    run_plan3a_fault_matrix,
)

EXPECTED_CHECKPOINTS = {
    "after_provisioning_transition",
    "after_operation_claim",
    "after_resource_plan",
    "after_provider_create",
    "after_validating_transition",
    "after_backup_recorded",
    "after_backup_created",
    "after_restore_plan",
    "after_restore_created",
    "after_restore_verified",
    "after_restore_cleanup",
    "before_validation_admission",
    "after_suspend_effect",
    "after_resume_effect",
    "before_resume_admission",
    "after_retirement_disposition",
    "before_retirement_admission",
}
PROVIDER_OPERATIONS = (
    "provision",
    "reconcile",
    "validate",
    "suspend",
    "resume",
    "retire",
)
PROOF_COUNT_FIELDS = (
    "repository_reopen_count",
    "repository_session_count",
    "provider_session_count",
    "distinct_session_generation_count",
    "provision_effect_count",
    "unique_resource_count",
    "residual_resource_count",
    "suspend_effect_count",
    "resume_effect_count",
    "retire_effect_count",
    "terminal_operation_count",
    "terminal_resource_count",
)
EXPECTED_CLASSIFICATION_PROOF_COUNTS = {
    ("provision", "transient_unavailable"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 2, 1),
    ("provision", "permanent_configuration"): (2, 3, 3, 3, 0, 0, 0, 0, 0, 0, 2, 0),
    ("reconcile", "transient_unavailable"): (3, 4, 4, 4, 1, 1, 0, 0, 0, 1, 2, 1),
    ("reconcile", "permanent_configuration"): (3, 4, 4, 4, 0, 0, 0, 0, 0, 0, 2, 0),
    ("validate", "transient_unavailable"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 2, 1),
    ("validate", "permanent_configuration"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 2, 1),
    ("suspend", "transient_unavailable"): (2, 3, 3, 3, 1, 1, 0, 1, 0, 1, 3, 1),
    ("suspend", "permanent_configuration"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 3, 1),
    ("resume", "transient_unavailable"): (2, 3, 3, 3, 1, 1, 0, 1, 1, 1, 4, 1),
    ("resume", "permanent_configuration"): (2, 3, 3, 3, 1, 1, 0, 1, 0, 1, 4, 1),
    ("retire", "transient_unavailable"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 2, 1),
    ("retire", "permanent_configuration"): (2, 3, 3, 3, 1, 1, 0, 0, 0, 1, 3, 1),
}


@pytest.fixture(scope="module")
def fault_outcomes(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[WarehouseFaultScenarioOutcome, ...]:
    return run_plan3a_fault_matrix(tmp_path_factory.mktemp("warehouse-fault-matrix"))


def _engine_outcomes(
    outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
    engine_kind: EngineKind,
) -> Iterator[WarehouseFaultScenarioOutcome]:
    return (outcome for outcome in outcomes if outcome.engine_kind == engine_kind.value)


def test_lifecycle_fault_checkpoints_are_a_closed_public_vocabulary() -> None:
    checkpoint_type = getattr(warehouse_control, "WarehouseLifecycleCheckpoint", None)

    assert checkpoint_type is not None
    assert {checkpoint.value for checkpoint in checkpoint_type} == EXPECTED_CHECKPOINTS
    with pytest.raises(ValueError):
        checkpoint_type("after_provider_creat")


def test_fault_matrix_is_the_exact_canonical_64_scenario_set(
    fault_outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
) -> None:
    observed = {(outcome.engine_kind, outcome.scenario_id) for outcome in fault_outcomes}

    assert len(fault_outcomes) == 64
    assert observed == required_fault_scenario_ids()
    assert fault_outcomes == canonical_fault_matrix_outcomes(fault_outcomes)
    assert all(outcome.disposition == "passed" for outcome in fault_outcomes)


def test_fault_matrix_outcomes_are_deterministic_across_private_roots(
    tmp_path: Path,
) -> None:
    first = run_plan3a_fault_matrix(tmp_path / "first-fault-matrix")
    second = run_plan3a_fault_matrix(tmp_path / "second-fault-matrix")

    assert first == second


def test_first_operation_claim_restart_cannot_adopt_without_a_provision_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "first-operation-claim"
    scenario_id = "checkpoint:after_operation_claim:1"

    run_plan3a_fault_matrix(root)
    state_path = root / "postgresql" / digest(scenario_id)[:16] / "provider-state.json"
    provider_state = json.loads(state_path.read_text())

    assert len(provider_state["provision_operation_ids"]) == 1


def test_noop_reopen_cannot_satisfy_observed_session_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fault_matrix_module,
        "_reopen",
        lambda session, scenario_directory, engine_kind, fault_hook: session,
    )

    with pytest.raises((RuntimeError, ValueError)):
        run_plan3a_fault_matrix(tmp_path / "noop-reopen")


def test_reopen_closes_old_repository_and_replaces_session_objects(tmp_path: Path) -> None:
    scenario_directory = tmp_path / "session-identities"
    scenario_directory.mkdir(mode=0o700)
    session = fault_matrix_module._open_session(
        scenario_directory,
        EngineKind.POSTGRESQL,
        lambda checkpoint: None,
    )
    old_repository = session.repository
    old_provider = session.provider
    old_orchestrator = session.orchestrator
    assert session.generation == 1

    reopened = fault_matrix_module._reopen(
        session,
        scenario_directory,
        EngineKind.POSTGRESQL,
        lambda checkpoint: None,
    )
    try:
        with pytest.raises(WarehousePersistenceError):
            old_repository.load("closed-tenant", "closed-binding")
        assert reopened is not session
        assert reopened.repository is not old_repository
        assert reopened.provider is not old_provider
        assert reopened.orchestrator is not old_orchestrator
        assert reopened.generation == 2
        assert reopened.session_generations() == (1, 2)
    finally:
        reopened.close()


@pytest.mark.parametrize("engine_kind", tuple(EngineKind))
def test_every_actual_checkpoint_occurrence_restarts_and_replays_to_cleanup(
    fault_outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
    engine_kind: EngineKind,
) -> None:
    checkpoint_outcomes = tuple(
        outcome
        for outcome in _engine_outcomes(fault_outcomes, engine_kind)
        if outcome.checkpoint is not None
    )
    operation_claim_occurrences = {
        outcome.checkpoint_occurrence
        for outcome in checkpoint_outcomes
        if outcome.checkpoint is WarehouseLifecycleCheckpoint.AFTER_OPERATION_CLAIM
    }

    assert len(checkpoint_outcomes) == 20
    assert operation_claim_occurrences == {1, 2, 3, 4}
    assert {outcome.checkpoint for outcome in checkpoint_outcomes} == set(
        WarehouseLifecycleCheckpoint
    )
    assert all(
        outcome.expected_fault_state == "process_stopped"
        and outcome.observed_fault_state == "process_stopped"
        and outcome.observed_terminal_state == "retired"
        and outcome.replay_disposition == "durable_restart_replayed"
        for outcome in checkpoint_outcomes
    )
    assert all(
        outcome.cleanup_proof.model_dump(exclude={"schema_version"})
        == {
            "repository_reopen_count": 2,
            "repository_session_count": 3,
            "provider_session_count": 3,
            "distinct_session_generation_count": 3,
            "session_generation_digest": digest(
                {
                    "domain": "pillarmesh-plan3a-session-generation-journal-v1",
                    "generations": (1, 2, 3),
                }
            ),
            "provision_effect_count": 1,
            "unique_resource_count": 1,
            "residual_resource_count": 0,
            "suspend_effect_count": 1,
            "resume_effect_count": 1,
            "retire_effect_count": 1,
            "terminal_operation_count": 4,
            "terminal_resource_count": 1,
        }
        for outcome in checkpoint_outcomes
    )


@pytest.mark.parametrize("engine_kind", tuple(EngineKind))
@pytest.mark.parametrize("provider_operation", PROVIDER_OPERATIONS)
@pytest.mark.parametrize(
    ("classification", "expected_state", "expected_replay"),
    (
        (
            WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
            "operation_live",
            "remained_resumable_after_restart",
        ),
        (
            WarehouseFailureClassification.PERMANENT_CONFIGURATION,
            "operation_non_live",
            "terminal_failure_remained_non_live",
        ),
    ),
)
def test_each_provider_operation_preserves_authoritative_classification_branch(
    fault_outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
    engine_kind: EngineKind,
    provider_operation: str,
    classification: WarehouseFailureClassification,
    expected_state: str,
    expected_replay: str,
) -> None:
    scenario_id = f"classification:{provider_operation}:{classification.value}"
    outcome = next(
        outcome
        for outcome in _engine_outcomes(fault_outcomes, engine_kind)
        if outcome.scenario_id == scenario_id
    )

    assert outcome.provider_operation == provider_operation
    assert outcome.failure_classification is classification
    assert outcome.expected_fault_state == expected_state
    assert outcome.observed_fault_state == expected_state
    assert outcome.replay_disposition == expected_replay
    assert outcome.observed_terminal_state == "retired"
    assert len(outcome.cleanup_proof_digest) == 64
    proof_counts = tuple(getattr(outcome.cleanup_proof, field) for field in PROOF_COUNT_FIELDS)
    assert (
        proof_counts
        == EXPECTED_CLASSIFICATION_PROOF_COUNTS[(provider_operation, classification.value)]
    )
