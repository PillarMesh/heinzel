from datetime import datetime, timedelta
from typing import Literal

from heinzel_contract_model import FIXED_PROJECTION, IntegrationContract, digest
from heinzel_execution_graph import EvidenceRequirement
from heinzel_provider_sdk import ColumnObservation, ProviderObservation

from .models import AdmittedPlan, NoValidPlan, PhysicalPlan, PreconditionResult

RULE_ID = "SNAPSHOT-POSTGRESQL-SNOWFLAKE-001"
MAX_OBSERVATION_AGE = timedelta(minutes=10)

REQUIRED_EVIDENCE_SPEC: tuple[tuple[str, Literal["metadata", "digest"]], ...] = (
    ("activation", "digest"),
    ("graph_verified", "digest"),
    ("drift_revalidated", "digest"),
    ("snapshot_opened", "metadata"),
    ("extraction_completed", "metadata"),
    ("manifest_created", "digest"),
    ("commit_attempted", "digest"),
    ("commit_resolved", "digest"),
    ("visibility_verified", "digest"),
    ("terminal_success", "digest"),
)
REQUIRED_EVIDENCE: tuple[EvidenceRequirement, ...] = tuple(
    EvidenceRequirement(event_type=event_type, redaction_class=redaction_class)
    for event_type, redaction_class in REQUIRED_EVIDENCE_SPEC
)

SOURCE_COLUMNS = (
    ColumnObservation(name="order_id", type_name="BIGINT", nullable=False),
    ColumnObservation(name="customer_ref", type_name="VARCHAR(65535)", nullable=False),
    ColumnObservation(name="amount", type_name="NUMERIC(18,2)", nullable=False),
    ColumnObservation(name="currency", type_name="VARCHAR(3)", nullable=False),
    ColumnObservation(name="status", type_name="VARCHAR(65535)", nullable=False),
    ColumnObservation(name="updated_at", type_name="TIMESTAMPTZ", nullable=False),
)
DESTINATION_COLUMNS = (
    ColumnObservation(name="order_id", type_name="NUMBER(19,0)", nullable=False),
    ColumnObservation(name="customer_ref", type_name="VARCHAR(65535)", nullable=False),
    ColumnObservation(name="amount", type_name="NUMBER(18,2)", nullable=False),
    ColumnObservation(name="currency", type_name="VARCHAR(3)", nullable=False),
    ColumnObservation(name="order_status", type_name="VARCHAR(65535)", nullable=False),
    ColumnObservation(name="updated_at", type_name="TIMESTAMP_TZ(6)", nullable=False),
)
COMMIT_LEDGER_COLUMNS = (
    ColumnObservation(name="batch_id", type_name="VARCHAR(16777216)", nullable=False),
    ColumnObservation(name="manifest_digest", type_name="VARCHAR(64)", nullable=False),
    ColumnObservation(name="committed_at", type_name="TIMESTAMP_TZ(9)", nullable=False),
)
SOURCE_CAPABILITIES = {"snapshot_read", "stable_primary_key_order", "drift_probe"}
DESTINATION_CAPABILITIES = {
    "stage_write",
    "idempotent_merge",
    "commit_ledger",
    "visibility_query",
}


def _result(number: int, passed: bool | None, reason: str, *evidence: str) -> PreconditionResult:
    status: Literal["satisfied", "unsatisfied", "unknown"] = (
        "unknown" if passed is None else "satisfied" if passed else "unsatisfied"
    )
    return PreconditionResult(
        number=number,
        status=status,
        reason=reason,
        evidence_ids=tuple(evidence),
    )


def _all_known(*facts: bool | None) -> bool | None:
    if any(fact is False for fact in facts):
        return False
    if any(fact is None for fact in facts):
        return None
    return True


def _known_equal(actual: object, expected: object) -> bool | None:
    if actual is None or actual == "unknown":
        return None
    return actual == expected


def _fresh(observed_at: datetime | None, now: datetime) -> bool | None:
    if observed_at is None:
        return None
    age = now - observed_at
    return timedelta(0) <= age <= MAX_OBSERVATION_AGE


def _has_capabilities(actual: tuple[str, ...] | None, required: set[str]) -> bool | None:
    if actual is None:
        return None
    return set(actual) >= required


def evaluate_legality(
    contract: IntegrationContract,
    source: ProviderObservation,
    destination: ProviderObservation,
    now: datetime,
) -> AdmittedPlan | NoValidPlan:
    plan = PhysicalPlan(
        rule_id=RULE_ID,
        operators=("read_snapshot", "project", "encode_segment", "commit", "verify"),
        projection=contract.projection,
        required_evidence=REQUIRED_EVIDENCE,
    )
    destination_names = tuple(column.name for column in destination.columns)
    results = (
        _result(
            1,
            _all_known(
                source.provider == "postgresql",
                _known_equal(source.object_kind, "base_table"),
                source.connection_handle == contract.source.connection_handle,
                source.read_only,
            ),
            "source must be a PostgreSQL base table",
            source.object_identity,
        ),
        _result(
            2,
            _all_known(
                _known_equal(source.key_name, contract.source.primary_key),
                _known_equal(source.key_type, "BIGINT"),
                _known_equal(source.key_nullable, False),
                _known_equal(source.key_constraint, "primary_key"),
                source.stable_key_order,
            ),
            "source key must be a non-null BIGINT total order",
            source.schema_digest,
        ),
        _result(
            3,
            source.columns == SOURCE_COLUMNS and destination.columns == DESTINATION_COLUMNS,
            "source and destination columns must match the fixed snapshot mapping",
            source.schema_digest,
            destination.schema_digest,
        ),
        _result(
            4,
            contract.projection == FIXED_PROJECTION
            and len(destination_names) == len(set(destination_names)),
            "projection must be fixed and destination names unique",
        ),
        _result(
            5,
            _all_known(
                destination.provider == "snowflake",
                _known_equal(destination.object_kind, "base_table"),
                destination.connection_handle == contract.destination.connection_handle,
                _known_equal(destination.commit_ledger_object_kind, "base_table"),
                _known_equal(destination.commit_ledger_columns, COMMIT_LEDGER_COLUMNS),
                _known_equal(destination.commit_ledger_key_name, "batch_id"),
                _known_equal(destination.commit_ledger_key_constraint, "primary_key"),
            ),
            "Snowflake target and commit ledger schemas must exactly match the snapshot contract",
            destination.object_identity,
        ),
        _result(
            6,
            _all_known(
                _known_equal(destination.key_name, contract.destination.key),
                _known_equal(destination.key_type, "NUMBER(19,0)"),
                _known_equal(destination.key_nullable, False),
                _known_equal(destination.key_constraint, "primary_key"),
            ),
            "destination key must preserve the source key",
            destination.schema_digest,
        ),
        _result(
            7,
            _all_known(
                _known_equal(source.snapshot_semantics, "snapshot"),
                contract.materialization_mode == "snapshot",
                contract.commit_behavior == "idempotent_key_upsert",
                contract.deletion_behavior == "not_observed",
            ),
            "snapshot and commit semantics must exactly match the snapshot contract",
        ),
        _result(
            8,
            _all_known(_fresh(source.observed_at, now), _fresh(destination.observed_at, now)),
            "provider observations must be no older than ten minutes",
        ),
        _result(
            9,
            _all_known(
                _has_capabilities(source.capabilities, SOURCE_CAPABILITIES),
                _has_capabilities(destination.capabilities, DESTINATION_CAPABILITIES),
            ),
            "providers must declare every required snapshot capability",
        ),
        _result(
            10,
            _all_known(
                source.evidence_safe,
                destination.evidence_safe,
                all(
                    item.redaction_class in {"metadata", "digest"}
                    for item in plan.required_evidence
                ),
            ),
            "required evidence must be satisfiable without secrets or row values",
        ),
    )
    if all(item.status == "satisfied" for item in results):
        return AdmittedPlan(
            rule_id=RULE_ID,
            preconditions=results,
            physical_plan_digest=digest(plan),
            physical_plan=plan,
        )
    changes = tuple(item.reason for item in results if item.status != "satisfied")
    return NoValidPlan(
        rule_id=RULE_ID,
        preconditions=results,
        smallest_changes=changes,
    )
