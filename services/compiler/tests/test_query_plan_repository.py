from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pillarmesh_compiler import GovernedQueryPlan
from pillarmesh_compiler.query_repository import QueryPlanConflict, SQLiteQueryPlanRepository
from pillarmesh_contract_model import digest


def _plan() -> GovernedQueryPlan:
    reference = {"artifact_id": "product-sales", "version": 1, "digest": "a" * 64}
    values = {
        "schema_version": "1",
        "plan_id": "plan-sales",
        "tenant_id": "tenant-a",
        "validation_digest": "b" * 64,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (reference,),
        "product_generation_refs": ({"product_ref": reference, "generation": 1},),
        "minimum_group_size": 5,
        "statement": "SELECT 1",
        "parameters": (),
        "statement_digest": digest("SELECT 1"),
        "parameter_digest": digest(()),
        "estimated_scan": None,
        "ceilings": {
            "row_limit": 10,
            "scan": {"rows": 100, "bytes": 1000},
            "period_scan": {"rows": 1000, "bytes": 10000},
        },
        "routing": "per_question_review",
    }
    return GovernedQueryPlan.model_validate(
        values | {"plan_digest": digest(values), "signature": "test-signature"}
    )


def test_stored_plan_survives_reopen_and_exact_replay(tmp_path: Path) -> None:
    path = tmp_path / "plans.sqlite3"
    plan = _plan()
    with sqlite3.connect(path) as connection:
        repository = SQLiteQueryPlanRepository(connection)
        assert repository.save(plan) == plan
        assert repository.save(plan) == plan
    with sqlite3.connect(path) as connection:
        assert SQLiteQueryPlanRepository(connection).read("tenant-a", plan.plan_digest) == plan


def test_plan_reads_never_cross_tenant_boundary() -> None:
    repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    plan = _plan()
    repository.save(plan)
    assert repository.read("tenant-b", plan.plan_digest) is None


def test_replay_cannot_replace_recorded_signature() -> None:
    repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    plan = _plan()
    repository.save(plan)
    changed = GovernedQueryPlan.model_validate(plan.model_dump() | {"signature": "different"})
    with pytest.raises(QueryPlanConflict):
        repository.save(changed)
    assert repository.read("tenant-a", plan.plan_digest) == plan


def test_plan_read_rejects_corrupted_index_identity() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteQueryPlanRepository(connection)
    plan = repository.save(_plan())
    connection.execute("UPDATE governed_query_plans SET plan_id = 'wrong-plan'")
    connection.commit()

    with pytest.raises(QueryPlanConflict, match="authority"):
        repository.read(plan.tenant_id, plan.plan_digest)


def test_persisted_plan_keeps_decimal_and_timestamp_parameter_types() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from pillarmesh_compiler import SqlParameter

    parameters = (
        SqlParameter(name="p0", value_type="decimal", value=Decimal("-1.250")),
        SqlParameter(name="p1", value_type="timestamp", value=datetime(2026, 9, 12, tzinfo=UTC)),
    )
    values = _plan().model_dump(exclude={"signature", "plan_digest"}) | {
        "parameters": tuple(parameter.model_dump() for parameter in parameters),
        "parameter_digest": digest(parameters),
    }
    plan = GovernedQueryPlan.model_validate(
        values | {"plan_digest": digest(values), "signature": "test"}
    )
    repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    repository.save(plan)

    restored = repository.read(plan.tenant_id, plan.plan_digest)

    assert restored is not None
    assert restored.parameters == parameters
    assert isinstance(restored.parameters[0].value, Decimal)
    assert isinstance(restored.parameters[1].value, datetime)


def test_invalid_decimal_json_is_a_model_validation_error() -> None:
    from pillarmesh_compiler import SqlParameter
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SqlParameter.model_validate_json(
            '{"name":"p0","value_type":"decimal","value":"not-a-number"}', strict=True
        )
