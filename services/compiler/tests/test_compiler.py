import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import heinzel_compiler.legality as legality_module
import pytest
from heinzel_compiler import (
    CompilerDefect,
    NoValidPlan,
    compile_contract,
    evaluate_legality,
)
from heinzel_contract_model import FIXED_PROJECTION, IntegrationContract, canonical_bytes, digest
from heinzel_execution_graph import GraphSigner, GraphVerifier, InvalidGraph
from heinzel_provider_sdk import ColumnObservation, ProviderObservation
from pydantic import ValidationError

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
FIXTURE_DIRECTORY = Path(__file__).parents[1] / "legality" / "fixtures"


def contract() -> IntegrationContract:
    return IntegrationContract.model_validate(
        {
            "contract_id": "contract-001",
            "version": 1,
            "source": {
                "connection_handle": "pg-snapshot",
                "schema": "snapshot_source",
                "table": "orders",
                "primary_key": "order_id",
            },
            "destination": {
                "connection_handle": "sf-snapshot",
                "database": "HEINZEL_SNAPSHOT",
                "schema": "PUBLIC",
                "table": "ORDERS",
                "key": "order_id",
            },
            "projection": [item.model_dump() for item in FIXED_PROJECTION],
            "freshness_seconds": 300,
        }
    )


def columns(destination: bool = False) -> tuple[ColumnObservation, ...]:
    return (
        ColumnObservation(
            name="order_id", type_name="NUMBER(19,0)" if destination else "BIGINT", nullable=False
        ),
        ColumnObservation(
            name="customer_ref",
            type_name="VARCHAR(65535)",
            nullable=False,
        ),
        ColumnObservation(
            name="amount",
            type_name="NUMBER(18,2)" if destination else "NUMERIC(18,2)",
            nullable=False,
        ),
        ColumnObservation(name="currency", type_name="VARCHAR(3)", nullable=False),
        ColumnObservation(
            name="order_status" if destination else "status",
            type_name="VARCHAR(65535)",
            nullable=False,
        ),
        ColumnObservation(
            name="updated_at",
            type_name="TIMESTAMP_TZ(6)" if destination else "TIMESTAMPTZ",
            nullable=False,
        ),
    )


def ledger_columns() -> tuple[ColumnObservation, ...]:
    return (
        ColumnObservation(name="batch_id", type_name="VARCHAR(16777216)", nullable=False),
        ColumnObservation(name="manifest_digest", type_name="VARCHAR(64)", nullable=False),
        ColumnObservation(name="committed_at", type_name="TIMESTAMP_TZ(9)", nullable=False),
    )


def observations() -> tuple[ProviderObservation, ProviderObservation]:
    source = ProviderObservation(
        provider="postgresql",
        connection_handle="pg-snapshot",
        object_identity="pg:fixture:orders:42",
        object_kind="base_table",
        schema_digest="1" * 64,
        columns=columns(),
        key_name="order_id",
        key_type="BIGINT",
        key_nullable=False,
        key_constraint="primary_key",
        stable_key_order=True,
        read_only=True,
        capabilities=("snapshot_read", "stable_primary_key_order", "drift_probe"),
        observed_at=NOW - timedelta(minutes=2),
        snapshot_semantics="snapshot",
        commit_ledger_object_kind=None,
        commit_ledger_columns=None,
        commit_ledger_key_name=None,
        commit_ledger_key_constraint=None,
        evidence_safe=True,
    )
    destination = ProviderObservation(
        provider="snowflake",
        connection_handle="sf-snapshot",
        object_identity="sf:HEINZEL_SNAPSHOT.PUBLIC.ORDERS",
        object_kind="base_table",
        schema_digest="2" * 64,
        columns=columns(destination=True),
        key_name="order_id",
        key_type="NUMBER(19,0)",
        key_nullable=False,
        key_constraint="primary_key",
        stable_key_order=None,
        read_only=None,
        capabilities=("stage_write", "idempotent_merge", "commit_ledger", "visibility_query"),
        observed_at=NOW - timedelta(minutes=2),
        snapshot_semantics="unknown",
        commit_ledger_object_kind="base_table",
        commit_ledger_columns=ledger_columns(),
        commit_ledger_key_name="batch_id",
        commit_ledger_key_constraint="primary_key",
        evidence_safe=True,
    )
    return source, destination


@pytest.mark.parametrize(
    ("mutation", "precondition"),
    [
        (lambda s, d: (s.model_copy(update={"object_kind": "view"}), d), 1),
        (lambda s, d: (s.model_copy(update={"connection_handle": "other-source"}), d), 1),
        (lambda s, d: (s.model_copy(update={"key_type": "TEXT"}), d), 2),
        (lambda s, d: (s.model_copy(update={"columns": s.columns[:-1]}), d), 3),
        (lambda s, d: (s, d.model_copy(update={"columns": (*d.columns[:-1], d.columns[-2])})), 4),
        (lambda s, d: (s, d.model_copy(update={"commit_ledger_columns": ()})), 5),
        (lambda s, d: (s, d.model_copy(update={"connection_handle": "other-destination"})), 5),
        (lambda s, d: (s, d.model_copy(update={"key_type": "VARCHAR"})), 6),
        (lambda s, d: (s.model_copy(update={"snapshot_semantics": "unknown"}), d), 7),
        (lambda s, d: (s.model_copy(update={"observed_at": NOW - timedelta(minutes=11)}), d), 8),
        (lambda s, d: (s.model_copy(update={"capabilities": ("snapshot_read",)}), d), 9),
        (lambda s, d: (s, d.model_copy(update={"evidence_safe": False})), 10),
    ],
)
def test_each_legality_precondition_fails_closed(mutation: object, precondition: int) -> None:
    source, destination = observations()
    changed_source, changed_destination = mutation(source, destination)  # type: ignore[operator]

    decision = evaluate_legality(contract(), changed_source, changed_destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.execution_occurred is False
    assert any(
        item.number == precondition and item.status != "satisfied"
        for item in decision.preconditions
    )


@pytest.mark.parametrize(
    ("mutation", "precondition"),
    [
        (lambda c, s, d: (c, s.model_copy(update={"provider": "snowflake"}), d), 1),
        (lambda c, s, d: (c, s.model_copy(update={"key_nullable": True}), d), 2),
        (lambda c, s, d: (c, s.model_copy(update={"key_constraint": "unique"}), d), 2),
        (lambda c, s, d: (c, s.model_copy(update={"stable_key_order": False}), d), 2),
        (lambda c, s, d: (c, s.model_copy(update={"read_only": False}), d), 1),
        (lambda c, s, d: (c, s, d.model_copy(update={"provider": "postgresql"})), 5),
        (lambda c, s, d: (c, s, d.model_copy(update={"object_kind": "view"})), 5),
        (
            lambda c, s, d: (
                c,
                s,
                d.model_copy(update={"commit_ledger_object_kind": "view"}),
            ),
            5,
        ),
        (lambda c, s, d: (c, s, d.model_copy(update={"commit_ledger_key_name": "other"})), 5),
        (
            lambda c, s, d: (
                c,
                s,
                d.model_copy(update={"commit_ledger_key_constraint": "unique"}),
            ),
            5,
        ),
        (lambda c, s, d: (c, s, d.model_copy(update={"key_name": "other"})), 6),
        (lambda c, s, d: (c, s, d.model_copy(update={"key_nullable": True})), 6),
        (lambda c, s, d: (c, s, d.model_copy(update={"key_constraint": "unique"})), 6),
        (lambda c, s, d: (c.model_copy(update={"materialization_mode": "cdc"}), s, d), 7),
        (lambda c, s, d: (c.model_copy(update={"commit_behavior": "append"}), s, d), 7),
        (lambda c, s, d: (c.model_copy(update={"deletion_behavior": "propagate"}), s, d), 7),
        (lambda c, s, d: (c, s, d.model_copy(update={"capabilities": ()})), 9),
        (lambda c, s, d: (c, s.model_copy(update={"evidence_safe": False}), d), 10),
    ],
)
def test_each_atomic_legality_fact_is_required(mutation: object, precondition: int) -> None:
    source, destination = observations()
    changed_contract, changed_source, changed_destination = mutation(  # type: ignore[operator]
        contract(), source, destination
    )

    decision = evaluate_legality(changed_contract, changed_source, changed_destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[precondition - 1].status == "unsatisfied"


def test_unsafe_required_evidence_redaction_is_not_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = observations()
    unsafe = (
        *legality_module.REQUIRED_EVIDENCE[:-1],
        legality_module.REQUIRED_EVIDENCE[-1].model_copy(update={"redaction_class": "secret"}),
    )
    monkeypatch.setattr(legality_module, "REQUIRED_EVIDENCE", unsafe)

    decision = evaluate_legality(contract(), source, destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[9].status == "unsatisfied"


@pytest.mark.parametrize(
    ("mutation", "precondition", "reason", "evidence_ids"),
    [
        pytest.param(
            lambda c, s, d: (c, s.model_copy(update={"read_only": False}), d),
            1,
            "source must be a PostgreSQL base table",
            ("pg:fixture:orders:42",),
            id="precondition-1-mutmut-39",
        ),
        pytest.param(
            lambda c, s, d: (c, s.model_copy(update={"stable_key_order": False}), d),
            2,
            "source key must be a non-null BIGINT total order",
            ("1" * 64,),
            id="precondition-2-mutmut-71",
        ),
        pytest.param(
            lambda c, s, d: (c, s.model_copy(update={"columns": s.columns[:-1]}), d),
            3,
            "source and destination columns must match the fixed snapshot mapping",
            ("1" * 64, "2" * 64),
            id="precondition-3-mutmut-107",
        ),
        pytest.param(
            lambda c, s, d: (c.model_copy(update={"projection": c.projection[::-1]}), s, d),
            4,
            "projection must be fixed and destination names unique",
            (),
            id="precondition-4-mutmut-120",
        ),
        pytest.param(
            lambda c, s, d: (
                c,
                s,
                d.model_copy(update={"commit_ledger_object_kind": "view"}),
            ),
            5,
            "Snowflake target and commit ledger schemas must exactly match the snapshot contract",
            ("sf:HEINZEL_SNAPSHOT.PUBLIC.ORDERS",),
            id="precondition-5-mutmut-144",
        ),
        pytest.param(
            lambda c, s, d: (c, s, d.model_copy(update={"key_nullable": True})),
            6,
            "destination key must preserve the source key",
            ("2" * 64,),
            id="precondition-6-mutmut-198",
        ),
        pytest.param(
            lambda c, s, d: (c.model_copy(update={"materialization_mode": "cdc"}), s, d),
            7,
            "snapshot and commit semantics must exactly match the snapshot contract",
            (),
            id="precondition-7-mutmut-235",
        ),
        pytest.param(
            lambda c, s, d: (
                c,
                s.model_copy(update={"observed_at": NOW - timedelta(minutes=11)}),
                d,
            ),
            8,
            "provider observations must be no older than ten minutes",
            (),
            id="precondition-8-mutmut-260",
        ),
        pytest.param(
            lambda c, s, d: (c, s, d.model_copy(update={"capabilities": ()})),
            9,
            "providers must declare every required snapshot capability",
            (),
            id="precondition-9-mutmut-287",
        ),
        pytest.param(
            lambda c, s, d: (c, s.model_copy(update={"evidence_safe": False}), d),
            10,
            "required evidence must be satisfiable without secrets or row values",
            (),
            id="precondition-10-mutmut-309",
        ),
    ],
)
def test_requirement_to_mutant_diagnostic_mapping_is_exact(
    mutation: object,
    precondition: int,
    reason: str,
    evidence_ids: tuple[str, ...],
) -> None:
    source, destination = observations()
    changed_contract, changed_source, changed_destination = mutation(  # type: ignore[operator]
        contract(), source, destination
    )

    decision = evaluate_legality(changed_contract, changed_source, changed_destination, NOW)

    assert isinstance(decision, NoValidPlan)
    failed = tuple(item for item in decision.preconditions if item.status != "satisfied")
    assert tuple((item.number, item.reason, item.evidence_ids) for item in failed) == (
        (precondition, reason, evidence_ids),
    )
    assert decision.smallest_changes == (reason,)


def test_admitted_plan_operator_sequence_is_exact() -> None:
    source, destination = observations()

    decision = evaluate_legality(contract(), source, destination, NOW)

    assert not isinstance(decision, NoValidPlan)
    assert decision.physical_plan.operators == (
        "read_snapshot",
        "project",
        "encode_segment",
        "commit",
        "verify",
    )


def test_compiler_is_reproducible_and_graph_is_signed() -> None:
    source, destination = observations()
    signer = GraphSigner.generate("snapshot-key")

    first = compile_contract(contract(), source, destination, signer, NOW)
    second = compile_contract(contract(), source, destination, signer, NOW)

    assert canonical_bytes(first.iir) == canonical_bytes(second.iir)
    assert canonical_bytes(first.physical_plan) == canonical_bytes(second.physical_plan)
    assert canonical_bytes(first.legality_decision) == canonical_bytes(second.legality_decision)
    assert canonical_bytes(first.signed_graph.graph) == canonical_bytes(second.signed_graph.graph)
    assert canonical_bytes(first.signed_graph) == canonical_bytes(second.signed_graph)
    assert digest(first.iir) == first.iir_digest
    assert digest(first.physical_plan) == first.signed_graph.graph.physical_plan_digest
    assert digest(first.legality_decision) == first.signed_graph.graph.legality_decision_digest
    assert digest(first.signed_graph) == first.signed_graph_artifact_digest
    verified = GraphVerifier({"snapshot-key": signer.public_key}).verify(first.signed_graph, NOW)
    assert verified.contract_digest == first.contract_digest


def test_modified_contract_and_observation_artifacts_break_graph_parent_binding() -> None:
    source, destination = observations()
    original_contract = contract()
    bundle = compile_contract(
        original_contract, source, destination, GraphSigner.generate("snapshot-key"), NOW
    )
    graph = bundle.signed_graph.graph
    modified_contract = original_contract.model_copy(update={"contract_id": "contract-tampered"})
    modified_source = source.model_copy(update={"object_identity": "pg:fixture:tampered"})
    modified_destination = destination.model_copy(
        update={"object_identity": "sf:tampered-destination"}
    )

    assert graph.contract_digest == digest(original_contract)
    assert graph.source_observation_digest == digest(source)
    assert graph.destination_observation_digest == digest(destination)
    assert graph.contract_digest != digest(modified_contract)
    assert graph.source_observation_digest != digest(modified_source)
    assert graph.destination_observation_digest != digest(modified_destination)


def test_compilation_bundle_rejects_a_mismatched_graph_parent() -> None:
    source, destination = observations()
    bundle = compile_contract(
        contract(), source, destination, GraphSigner.generate("snapshot-key"), NOW
    )
    mismatched_graph = bundle.signed_graph.graph.model_copy(
        update={"physical_plan_digest": "f" * 64}
    )
    mismatched_signed_graph = bundle.signed_graph.model_copy(
        update={"graph": mismatched_graph, "graph_digest": digest(mismatched_graph)}
    )

    with pytest.raises(ValidationError, match="physical plan digest"):
        type(bundle).model_validate(
            {
                **bundle.model_dump(exclude={"signed_graph"}),
                "signed_graph": mismatched_signed_graph.model_dump(),
            }
        )


def test_admitted_legality_decision_rejects_a_mismatched_physical_plan_parent() -> None:
    source, destination = observations()
    bundle = compile_contract(
        contract(), source, destination, GraphSigner.generate("snapshot-key"), NOW
    )

    with pytest.raises(ValidationError, match="physical plan digest"):
        type(bundle.legality_decision).model_validate(
            {
                **bundle.legality_decision.model_dump(),
                "physical_plan_digest": "f" * 64,
            }
        )


def test_observation_from_a_different_handle_is_not_admitted() -> None:
    source, destination = observations()
    changed = source.model_copy(update={"connection_handle": "another-source"})

    decision = evaluate_legality(contract(), changed, destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[0].status == "unsatisfied"


@pytest.mark.parametrize("provider", ["source", "destination"])
@pytest.mark.parametrize("age", [timedelta(0), timedelta(minutes=10)])
def test_observation_freshness_includes_exact_boundaries(provider: str, age: timedelta) -> None:
    source, destination = observations()
    if provider == "source":
        source = source.model_copy(update={"observed_at": NOW - age})
    else:
        destination = destination.model_copy(update={"observed_at": NOW - age})

    decision = evaluate_legality(contract(), source, destination, NOW)

    assert not isinstance(decision, NoValidPlan)


@pytest.mark.parametrize("provider", ["source", "destination"])
@pytest.mark.parametrize(
    "observed_at",
    [NOW + timedelta(microseconds=1), NOW - timedelta(minutes=10, microseconds=1)],
    ids=["future", "stale"],
)
def test_source_and_destination_freshness_fail_independently(
    provider: str, observed_at: datetime
) -> None:
    source, destination = observations()
    if provider == "source":
        source = source.model_copy(update={"observed_at": observed_at})
    else:
        destination = destination.model_copy(update={"observed_at": observed_at})

    decision = evaluate_legality(contract(), source, destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[7].status == "unsatisfied"


@pytest.mark.parametrize(
    ("mutation", "precondition"),
    [
        (lambda s, d: (s.model_copy(update={"key_name": None}), d), 2),
        (lambda s, d: (s.model_copy(update={"read_only": None}), d), 1),
        (lambda s, d: (s, d.model_copy(update={"commit_ledger_columns": None})), 5),
        (lambda s, d: (s, d.model_copy(update={"object_kind": "unknown"})), 5),
        (
            lambda s, d: (s, d.model_copy(update={"commit_ledger_object_kind": "unknown"})),
            5,
        ),
        (lambda s, d: (s.model_copy(update={"snapshot_semantics": "unknown"}), d), 7),
        (lambda s, d: (s.model_copy(update={"observed_at": None}), d), 8),
        (lambda s, d: (s.model_copy(update={"capabilities": None}), d), 9),
        (lambda s, d: (s, d.model_copy(update={"evidence_safe": None})), 10),
    ],
)
def test_unknown_required_provider_fact_produces_no_valid_plan(
    mutation: object, precondition: int
) -> None:
    source, destination = observations()
    changed_source, changed_destination = mutation(source, destination)  # type: ignore[operator]

    decision = evaluate_legality(contract(), changed_source, changed_destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[precondition - 1].status == "unknown"


@pytest.mark.parametrize(
    ("mutation", "precondition"),
    [
        (
            lambda d: d.model_copy(
                update={
                    "columns": (
                        d.columns[0],
                        d.columns[1].model_copy(update={"type_name": "VARCHAR(65534)"}),
                        *d.columns[2:],
                    )
                }
            ),
            3,
        ),
        (
            lambda d: d.model_copy(
                update={
                    "columns": (
                        *d.columns[:-1],
                        d.columns[-1].model_copy(update={"type_name": "TIMESTAMP_TZ(5)"}),
                    )
                }
            ),
            3,
        ),
        (
            lambda d: d.model_copy(
                update={
                    "commit_ledger_columns": (
                        ledger_columns()[0],
                        ledger_columns()[1].model_copy(update={"type_name": "VARCHAR(63)"}),
                        ledger_columns()[2],
                    )
                }
            ),
            5,
        ),
    ],
)
def test_destination_narrowing_is_not_admitted(mutation: object, precondition: int) -> None:
    source, destination = observations()

    decision = evaluate_legality(contract(), source, mutation(destination), NOW)  # type: ignore[operator]

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[precondition - 1].status == "unsatisfied"


def test_unconstrained_source_text_is_not_admitted() -> None:
    source, destination = observations()
    source = source.model_copy(
        update={
            "columns": (
                source.columns[0],
                source.columns[1].model_copy(update={"type_name": "TEXT"}),
                *source.columns[2:],
            )
        }
    )

    decision = evaluate_legality(contract(), source, destination, NOW)

    assert isinstance(decision, NoValidPlan)
    assert decision.preconditions[2].status == "unsatisfied"


def _fixture_cases(fixture_path: Path) -> list[tuple[dict[str, object], dict[str, object]]]:
    payload = json.loads(fixture_path.read_bytes())
    result: list[tuple[dict[str, object], dict[str, object]]] = []
    for case in payload["cases"]:
        inputs = copy.deepcopy(payload["inputs"])
        for pointer, value in case["replacements"].items():
            current = inputs
            parts = pointer.removeprefix("/").split("/")
            for part in parts[:-1]:
                current = current[int(part)] if isinstance(current, list) else current[part]
            final = parts[-1]
            if isinstance(current, list):
                current[int(final)] = value
            else:
                current[final] = value
        result.append((inputs, case))
    return result


@pytest.mark.parametrize("fixture_path", sorted(FIXTURE_DIRECTORY.glob("*.json")))
def test_legality_json_fixture_is_executable_and_self_contained(fixture_path: Path) -> None:
    for inputs, case in _fixture_cases(fixture_path):
        fixture_contract = IntegrationContract.model_validate(inputs["contract"])
        source = ProviderObservation.model_validate(inputs["source_observation"])
        destination = ProviderObservation.model_validate(inputs["destination_observation"])
        fixture_now = datetime.fromisoformat(inputs["evaluated_at"])  # type: ignore[arg-type]

        decision = evaluate_legality(fixture_contract, source, destination, fixture_now)

        expected = case["expected"]
        assert isinstance(expected, dict)
        assert ("no_valid_plan" if isinstance(decision, NoValidPlan) else "admitted") == expected[
            "decision"
        ], case["name"]
        assert [
            item.number for item in decision.preconditions if item.status != "satisfied"
        ] == expected["failed_preconditions"], case["name"]


def test_every_admitted_provider_pair_has_an_executable_regression_fixture() -> None:
    fixtures = []
    for path in sorted(FIXTURE_DIRECTORY.glob("*.json")):
        payload = json.loads(path.read_bytes())
        for inputs, case in _fixture_cases(path):
            expected = case["expected"]
            assert isinstance(expected, dict)
            if expected["decision"] == "admitted":
                fixtures.append((payload["provider_pair"], inputs))

    assert {tuple(pair) for pair, _inputs in fixtures} == {("postgresql", "snowflake")}
    for _pair, inputs in fixtures:
        decision = evaluate_legality(
            IntegrationContract.model_validate(inputs["contract"]),
            ProviderObservation.model_validate(inputs["source_observation"]),
            ProviderObservation.model_validate(inputs["destination_observation"]),
            datetime.fromisoformat(inputs["evaluated_at"]),  # type: ignore[arg-type]
        )
        assert not isinstance(decision, NoValidPlan)


def test_graph_verifier_rejects_tampering() -> None:
    source, destination = observations()
    signer = GraphSigner.generate("snapshot-key")
    result = compile_contract(contract(), source, destination, signer, NOW)
    tampered_graph = result.signed_graph.graph.model_copy(update={"max_rows": 10_001})
    tampered = result.signed_graph.model_copy(update={"graph": tampered_graph})

    with pytest.raises(InvalidGraph, match="digest"):
        GraphVerifier({"snapshot-key": signer.public_key}).verify(tampered, NOW)


def test_graph_evidence_mismatch_is_compiler_defect() -> None:
    source, destination = observations()

    with pytest.raises(CompilerDefect, match="evidence set"):
        compile_contract(
            contract(),
            source,
            destination,
            GraphSigner.generate("snapshot-key"),
            NOW,
            graph_evidence_override=("terminal_success",),
        )
