import csv
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from heinzel_authoring_mcp import AuthoringApplication
from heinzel_contract_model import FIXED_PROJECTION, IntegrationContract, digest
from heinzel_contract_service import ContractService
from heinzel_evidence import SQLiteStore
from heinzel_execution_graph import GraphSigner, GraphVerifier
from heinzel_provider_sdk import (
    ColumnObservation,
    CommitReceipt,
    DriftProbe,
    OrderRow,
    ProviderObservation,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from heinzel_provider_snowflake import encode_segment
from heinzel_runtime import Runtime


def _payload(value: object) -> Mapping[str, object]:
    """Narrow an AuthoringApplication result, declared `object`, to the mapping it is."""
    assert isinstance(value, Mapping), value
    return value


def _events(value: object) -> Sequence[Mapping[str, object]]:
    """Narrow a trace, declared `object`, to the sequence of events it is."""
    assert isinstance(value, list), value
    return [_payload(event) for event in value]


def _text(value: object) -> str:
    """Narrow a payload member, declared `object`, to the identifier string it is."""
    assert isinstance(value, str), value
    return value


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


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


class Source:
    def __init__(self) -> None:
        self.rows: list[OrderRow] = []
        self.reads = 0

    def observe(self) -> ProviderObservation:
        return ProviderObservation(
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
            observed_at=NOW,
            snapshot_semantics="snapshot",
            commit_ledger_object_kind=None,
            commit_ledger_columns=None,
            commit_ledger_key_name=None,
            commit_ledger_key_constraint=None,
            evidence_safe=True,
        )

    def drift_probe(self) -> DriftProbe:
        observation = self.observe()
        return DriftProbe(
            object_identity=observation.object_identity,
            schema_digest=observation.schema_digest,
        )

    def read_snapshot(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        ordered = tuple(sorted(self.rows, key=lambda row: row.order_id))
        key_min = ordered[0].order_id if ordered else None
        key_max = ordered[-1].order_id if ordered else None
        return (
            SourceBoundary(
                object_identity="pg:fixture:orders:42",
                schema_digest="1" * 64,
                snapshot_identity=f"fixture:{self.reads}",
                key_range_digest=digest({"key_min": key_min, "key_max": key_max}),
                row_count=len(ordered),
                query_shape_digest="3" * 64,
                opened_at=NOW,
                closed_at=NOW,
            ),
            ordered,
        )


class Destination:
    def __init__(self) -> None:
        self.staged: dict[str, OrderRow] = {}
        self.rows: dict[int, OrderRow] = {}
        self.ledger: dict[str, str] = {}
        self.mutations = 0

    def observe(self) -> ProviderObservation:
        return ProviderObservation(
            provider="snowflake",
            connection_handle="sf-snapshot",
            object_identity="sf:fixture:orders",
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
            observed_at=NOW,
            snapshot_semantics="unknown",
            commit_ledger_object_kind="base_table",
            commit_ledger_columns=ledger_columns(),
            commit_ledger_key_name="batch_id",
            commit_ledger_key_constraint="primary_key",
            evidence_safe=True,
        )

    def stage(self, segment: Path, manifest: SegmentManifest) -> None:
        with segment.open(encoding="utf-8", newline="") as stream:
            parsed = tuple(csv.DictReader(stream))
        self.staged = {
            manifest.batch_id: OrderRow(
                order_id=int(parsed[-1]["order_id"]),
                customer_ref=parsed[-1]["customer_ref"],
                amount=Decimal(parsed[-1]["amount"]),
                currency=parsed[-1]["currency"],
                status=parsed[-1]["order_status"],
                updated_at=datetime.fromisoformat(parsed[-1]["updated_at"].replace("Z", "+00:00")),
            )
        }

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        from heinzel_contract_model import digest

        manifest_digest = digest(manifest)
        existing = self.ledger.get(manifest.batch_id)
        if existing is None:
            row = self.staged[manifest.batch_id]
            self.rows[row.order_id] = row
            self.ledger[manifest.batch_id] = manifest_digest
            self.mutations += 1
        elif existing != manifest_digest:
            raise AssertionError("conflicting fake manifest")
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=manifest_digest,
            query_ids=("fake-merge", "fake-ledger"),
            affected_rows=0 if existing is not None else 1,
            ledger_identity=digest({"provider": "snowflake", "ledger": "sf:fixture:commit-ledger"}),
            committed_at=NOW,
            replayed=existing is not None,
        )

    def verify_visibility(self, manifest: SegmentManifest, acceptance_key: int) -> VisibilityProof:
        from heinzel_contract_model import digest

        value_digest = digest(self.rows[acceptance_key])
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=value_digest,
            query_id="fake-fresh-visibility",
            verified_at=NOW,
        )


def contract_payload() -> dict[str, object]:
    return {
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


def test_fresh_row_reaches_terminal_visibility_and_reconstructable_trace(tmp_path: Path) -> None:
    source = Source()
    destination = Destination()
    signer = GraphSigner.generate("snapshot-key")
    store = SQLiteStore.open(tmp_path / "state.db")
    contracts = ContractService(
        store=store,
        signer=signer,
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        clock=lambda: NOW,
    )
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"snapshot-key": signer.public_key}),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        segment_encoder=encode_segment,
        output_dir=tmp_path / "output",
        clock=lambda: NOW,
    )
    application = AuthoringApplication(contracts, runtime)
    created = _payload(application.create_draft(contract_payload()))
    verified = _payload(application.verify("contract-001", 1))
    acceptance_key = 7
    assert acceptance_key not in destination.rows
    source.rows.append(
        OrderRow(
            order_id=acceptance_key,
            customer_ref="fresh-customer-7",
            amount=Decimal("10.50"),
            currency="USD",
            status="paid",
            updated_at=NOW,
        )
    )

    run = _payload(
        application.activate(
            _text(created["contract_digest"]),
            _text(verified["summary_digest"]),
            acceptance_key,
        )
    )
    repeated = _payload(
        application.activate(
            _text(created["contract_digest"]),
            _text(verified["summary_digest"]),
            acceptance_key,
        )
    )

    assert run["state"] == "succeeded"
    assert repeated["run_id"] == run["run_id"]
    assert destination.rows[acceptance_key].customer_ref == "fresh-customer-7"
    assert destination.mutations == 1
    trace = _events(application.get_trace(_text(run["run_id"])))
    assert trace[-1]["event_type"] == "terminal_success"
    assert store.verify_chain(_text(run["run_id"]))


def test_no_valid_plan_reads_no_rows_and_mutates_nothing(tmp_path: Path) -> None:
    source = Source()
    destination = Destination()
    signer = GraphSigner.generate("snapshot-key")
    store = SQLiteStore.open(tmp_path / "state.db")
    contracts = ContractService(
        store=store,
        signer=signer,
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        clock=lambda: NOW,
    )
    source.observe = lambda: Source.observe(source).model_copy(  # type: ignore[method-assign]
        update={"key_type": "TEXT"}
    )
    contracts.create_draft(IntegrationContract.model_validate(contract_payload()))

    result = contracts.verify("contract-001", 1)

    assert result.result == "no_valid_plan"  # type: ignore[union-attr]
    assert source.reads == 0
    assert destination.mutations == 0
    assert store.list_artifact_refs("runs") == ()
