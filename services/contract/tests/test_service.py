from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_compiler import AdmittedPlan, NoValidPlan, evaluate_legality
from heinzel_contract_model import (
    FIXED_PROJECTION,
    IntegrationContract,
    canonical_bytes,
    digest,
)
from heinzel_contract_service import (
    AcquisitionContractLifecycleNotFoundError,
    ActivationSummary,
    ContractAuthorityBoundaryError,
    ContractService,
    SQLiteAcquisitionContractLifecycleRepository,
    StaleAcquisitionContractLifecycleError,
)
from heinzel_evidence import SQLiteStore
from heinzel_execution_graph import GraphSigner
from heinzel_iir import lower_contract
from heinzel_provider_sdk import ColumnObservation, ProviderObservation

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def contract(version: int = 1) -> IntegrationContract:
    return IntegrationContract.model_validate(
        {
            "contract_id": "contract-001",
            "version": version,
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


def observations(observed_at: datetime = NOW) -> tuple[ProviderObservation, ProviderObservation]:
    return (
        ProviderObservation(
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
            observed_at=observed_at,
            snapshot_semantics="snapshot",
            commit_ledger_object_kind=None,
            commit_ledger_columns=None,
            commit_ledger_key_name=None,
            commit_ledger_key_constraint=None,
            evidence_safe=True,
        ),
        ProviderObservation(
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
            observed_at=observed_at,
            snapshot_semantics="unknown",
            commit_ledger_object_kind="base_table",
            commit_ledger_columns=ledger_columns(),
            commit_ledger_key_name="batch_id",
            commit_ledger_key_constraint="primary_key",
            evidence_safe=True,
        ),
    )


class Provider:
    def __init__(self, observation: ProviderObservation) -> None:
        self.observation = observation
        self.observe_calls = 0

    def observe(self) -> ProviderObservation:
        self.observe_calls += 1
        return self.observation.model_copy(update={"observed_at": NOW})


class AuthorityInvalidator:
    def __init__(self) -> None:
        self.fail_at: str | None = None
        self.activation_calls: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def activate_contract_authority(self, tenant_id: str, contract_digest: str) -> int:
        if self.fail_at == "activate":
            raise RuntimeError("private-authority-detail")
        self.activation_calls.append((tenant_id, contract_digest))
        return 0

    def invalidate_contract_authority(self, tenant_id: str, contract_digest: str) -> None:
        if self.fail_at == "retire":
            raise RuntimeError("private-authority-detail")
        self.calls.append((tenant_id, contract_digest))


def service(
    tmp_path: Path,
    *,
    authority_invalidator: AuthorityInvalidator | None = None,
) -> tuple[ContractService, Provider, Provider, SQLiteStore]:
    source_observation, destination_observation = observations()
    source = Provider(source_observation)
    destination = Provider(destination_observation)
    store = SQLiteStore.open(tmp_path / "state.db")
    contract_service = ContractService(
        store=store,
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        clock=lambda: NOW,
        acquisition_authority_invalidator=authority_invalidator,
    )
    return contract_service, source, destination, store


def test_contract_activation_and_retirement_are_tenant_qualified_and_revisioned() -> None:
    source_observation, destination_observation = observations()
    invalidator = AuthorityInvalidator()
    lifecycle = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract_service = ContractService(
        store=SQLiteStore.open(Path(":memory:")),
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: Provider(source_observation),
        destination_resolver=lambda _handle: Provider(destination_observation),
        clock=lambda: NOW,
        acquisition_authority_invalidator=invalidator,
        acquisition_lifecycle_repository=lifecycle,
    )

    activated = contract_service.activate_acquisition_contract("tenant-a", "4" * 64)
    retired = contract_service.retire_acquisition_contract(
        "tenant-a",
        "4" * 64,
        expected_revision=activated.revision,
    )

    assert activated.lifecycle_state == "activated"
    assert retired.lifecycle_state == "retired"
    assert retired.revision == activated.revision + 1
    assert invalidator.activation_calls == [("tenant-a", "4" * 64)]
    assert invalidator.calls == [("tenant-a", "4" * 64)]


def test_contract_retirement_fails_closed_when_authority_cannot_be_invalidated() -> None:
    source_observation, destination_observation = observations()
    invalidator = AuthorityInvalidator()
    lifecycle = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract_service = ContractService(
        store=SQLiteStore.open(Path(":memory:")),
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: Provider(source_observation),
        destination_resolver=lambda _handle: Provider(destination_observation),
        clock=lambda: NOW,
        acquisition_authority_invalidator=invalidator,
        acquisition_lifecycle_repository=lifecycle,
    )
    activated = contract_service.activate_acquisition_contract("tenant-a", "4" * 64)
    invalidator.fail_at = "retire"

    with pytest.raises(
        ContractAuthorityBoundaryError, match="retire acquisition contract"
    ) as error:
        contract_service.retire_acquisition_contract(
            "tenant-a",
            "4" * 64,
            expected_revision=activated.revision,
        )

    assert "private-authority-detail" not in str(error.value)
    assert lifecycle.get("tenant-a", "4" * 64) == activated


def test_unknown_cross_tenant_inactive_and_stale_retirement_have_no_authority_effect() -> None:
    source_observation, destination_observation = observations()
    invalidator = AuthorityInvalidator()
    lifecycle = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract_service = ContractService(
        store=SQLiteStore.open(Path(":memory:")),
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: Provider(source_observation),
        destination_resolver=lambda _handle: Provider(destination_observation),
        clock=lambda: NOW,
        acquisition_authority_invalidator=invalidator,
        acquisition_lifecycle_repository=lifecycle,
    )
    activated = contract_service.activate_acquisition_contract("tenant-a", "4" * 64)

    with pytest.raises(AcquisitionContractLifecycleNotFoundError):
        contract_service.retire_acquisition_contract(
            "tenant-b",
            "4" * 64,
            expected_revision=activated.revision,
        )
    with pytest.raises(StaleAcquisitionContractLifecycleError):
        contract_service.retire_acquisition_contract(
            "tenant-a",
            "4" * 64,
            expected_revision=activated.revision + 1,
        )
    retired = contract_service.retire_acquisition_contract(
        "tenant-a",
        "4" * 64,
        expected_revision=activated.revision,
    )
    with pytest.raises(StaleAcquisitionContractLifecycleError):
        contract_service.retire_acquisition_contract(
            "tenant-a",
            "4" * 64,
            expected_revision=retired.revision,
        )

    assert invalidator.calls == [("tenant-a", "4" * 64)]


def test_draft_versions_are_immutable_and_latest_supersedes_activation(tmp_path: Path) -> None:
    contract_service, _source, _destination, _store = service(tmp_path)
    first = contract_service.create_draft(contract())

    assert contract_service.get_draft("contract-001", 1) == first
    changed = contract().model_copy(update={"freshness_seconds": 600})
    with pytest.raises(ValueError, match="immutable"):
        contract_service.create_draft(changed)

    summary = contract_service.verify("contract-001", 1)
    assert isinstance(summary, ActivationSummary)
    contract_service.create_draft(contract(version=2))
    with pytest.raises(ValueError, match="superseded"):
        contract_service.activate(digest(first), digest(summary), 7)


def test_verify_returns_no_valid_plan_without_creating_run(tmp_path: Path) -> None:
    contract_service, source, _destination, store = service(tmp_path)
    source.observation = source.observation.model_copy(update={"key_type": "TEXT"})
    contract_service.create_draft(contract())

    decision = contract_service.verify("contract-001", 1)

    assert isinstance(decision, NoValidPlan)
    assert store.list_artifact_refs("runs") == ()


def test_verify_captures_evaluation_time_after_observations(tmp_path: Path) -> None:
    ticks = iter(NOW + timedelta(microseconds=offset) for offset in range(3))

    def clock() -> datetime:
        return next(ticks)

    source_observation, destination_observation = observations()

    class ClockedProvider:
        def __init__(self, observation: ProviderObservation) -> None:
            self._observation = observation

        def observe(self) -> ProviderObservation:
            return self._observation.model_copy(update={"observed_at": clock()})

    source = ClockedProvider(source_observation)
    destination = ClockedProvider(destination_observation)
    contract_service = ContractService(
        store=SQLiteStore.open(tmp_path / "state.db"),
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        clock=clock,
    )
    contract_service.create_draft(contract())

    summary = contract_service.verify("contract-001", 1)

    assert isinstance(summary, ActivationSummary)
    assert summary.verified_at == NOW + timedelta(microseconds=2)


def test_verify_publishes_exact_canonical_parent_artifacts_before_summary(
    tmp_path: Path,
) -> None:
    contract_service, source, destination, store = service(tmp_path)
    draft = contract_service.create_draft(contract())

    summary = contract_service.verify("contract-001", 1)

    assert isinstance(summary, ActivationSummary)
    decision = evaluate_legality(draft, source.observation, destination.observation, NOW)
    assert isinstance(decision, AdmittedPlan)
    expected_artifacts = (
        ("integration_contract", summary.contract_digest, canonical_bytes(draft)),
        (
            "provider_observation",
            summary.source_observation_digest,
            canonical_bytes(source.observation),
        ),
        (
            "provider_observation",
            summary.destination_observation_digest,
            canonical_bytes(destination.observation),
        ),
        ("intent_ir", summary.iir_digest, canonical_bytes(lower_contract(draft))),
        ("physical_plan", summary.physical_plan_digest, canonical_bytes(decision.physical_plan)),
        ("legality_decision", summary.legality_decision_digest, canonical_bytes(decision)),
        (
            "signed_execution_graph",
            summary.signed_graph_artifact_digest,
            canonical_bytes(summary.signed_graph),
        ),
    )
    for kind, artifact_digest, expected_payload in expected_artifacts:
        assert store.load_artifact(kind, artifact_digest) == expected_payload
    assert summary.graph_digest == digest(summary.signed_graph.graph)
    assert summary.signed_graph_artifact_digest == digest(summary.signed_graph)
    assert store.load_artifact("activation_summary", digest(summary)) == canonical_bytes(summary)
    assert store.resolve_artifact_ref("activation_summaries", digest(summary)) == digest(summary)


@pytest.mark.parametrize("parent_state", ["missing", "valid_corruption"])
def test_verify_never_publishes_summary_without_exact_contract_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_state: str
) -> None:
    contract_service, _source, _destination, store = service(tmp_path)
    draft = contract_service.create_draft(contract())
    draft_digest = digest(draft)
    original_publish = store.publish_verification

    def disturb_parent_then_publish(
        artifacts: tuple[tuple[str, str, bytes], ...],
        summary_digest: str,
        summary_payload: bytes,
    ) -> None:
        if parent_state == "missing":
            store._connection.execute(
                "DELETE FROM artifacts WHERE kind = 'integration_contract' AND digest = ?",
                (draft_digest,),
            )
        else:
            corrupted = draft.model_copy(update={"freshness_seconds": 600})
            store._connection.execute(
                "UPDATE artifacts SET payload = ? "
                "WHERE kind = 'integration_contract' AND digest = ?",
                (canonical_bytes(corrupted), draft_digest),
            )
        original_publish(artifacts, summary_digest, summary_payload)

    monkeypatch.setattr(store, "publish_verification", disturb_parent_then_publish)

    if parent_state == "valid_corruption":
        with pytest.raises(ValueError, match="different payload"):
            contract_service.verify("contract-001", 1)
        assert store.list_artifact_refs("activation_summaries") == ()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE kind = 'activation_summary'"
            ).fetchone()[0]
            == 0
        )
        return

    summary = contract_service.verify("contract-001", 1)
    assert isinstance(summary, ActivationSummary)
    assert store.load_artifact("integration_contract", draft_digest) == canonical_bytes(draft)
    assert store.resolve_artifact_ref("activation_summaries", digest(summary)) == digest(summary)


def test_activation_is_digest_bound_reobserves_and_is_idempotent(tmp_path: Path) -> None:
    contract_service, source, destination, store = service(tmp_path)
    draft = contract_service.create_draft(contract())
    summary = contract_service.verify("contract-001", 1)
    assert isinstance(summary, ActivationSummary)

    run = contract_service.activate(digest(draft), digest(summary), 7)
    repeated = contract_service.activate(digest(draft), digest(summary), 7)

    assert repeated.run_id == run.run_id
    assert store.get_private_state(run.run_id).acceptance_key == 7
    assert "acceptance_key" not in run.model_dump_json()
    assert source.observe_calls == 2
    assert destination.observe_calls == 2
    assert [event.event_type for event in store.trace(run.run_id)[:5]] == [
        "draft_created",
        "verification_completed",
        "legality_admitted",
        "graph_signed",
        "activation",
    ]
    verification = store.trace(run.run_id)[1]
    assert verification.attributes == {
        "destination_observation_digest": summary.destination_observation_digest,
        "iir_digest": summary.iir_digest,
        "legality_decision_digest": summary.legality_decision_digest,
        "physical_plan_digest": summary.physical_plan_digest,
        "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
        "source_observation_digest": summary.source_observation_digest,
        "summary_digest": digest(summary),
    }
    graph_signed = store.trace(run.run_id)[3]
    assert graph_signed.attributes == {
        "graph_digest": summary.graph_digest,
        "key_id": summary.signing_key_id,
        "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
    }

    with pytest.raises(ValueError, match="summary digest"):
        contract_service.activate(digest(draft), "f" * 64, 7)


def test_activation_rejects_expiry_and_stable_provider_drift(tmp_path: Path) -> None:
    current = NOW
    source_observation, destination_observation = observations()
    source = Provider(source_observation)
    destination = Provider(destination_observation)
    store = SQLiteStore.open(tmp_path / "state.db")
    contract_service = ContractService(
        store=store,
        signer=GraphSigner.generate("snapshot-key"),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        clock=lambda: current,
    )
    draft = contract_service.create_draft(contract())
    summary = contract_service.verify("contract-001", 1)
    assert isinstance(summary, ActivationSummary)

    source.observation = source.observation.model_copy(update={"schema_digest": "9" * 64})
    with pytest.raises(ValueError, match="drift"):
        contract_service.activate(digest(draft), digest(summary), 7)

    source.observation = source_observation
    current = NOW + timedelta(minutes=31)
    with pytest.raises(ValueError, match="expired"):
        contract_service.activate(digest(draft), digest(summary), 7)


def test_activation_never_leaves_a_run_without_its_lifecycle_evidence(tmp_path: Path) -> None:
    contract_service, _source, _destination, store = service(tmp_path)
    draft = contract_service.create_draft(contract())
    summary = contract_service.verify("contract-001", 1)
    assert isinstance(summary, ActivationSummary)
    original = store._append_event
    appended = 0

    def failing(*arguments: object, **keywords: object) -> object:
        nonlocal appended
        appended += 1
        if appended == 3:
            raise RuntimeError("evidence write interrupted")
        return original(*arguments, **keywords)  # type: ignore[arg-type]

    store._append_event = failing  # type: ignore[assignment, method-assign]
    with pytest.raises(RuntimeError, match="interrupted"):
        contract_service.activate(digest(draft), digest(summary), 7)
    store._append_event = original  # type: ignore[method-assign]

    # A run persisted without its evidence would be returned verbatim by the activation
    # replay path and could never be repaired, so nothing may survive the failure.
    assert store.list_artifact_refs("runs") == ()
    run = contract_service.activate(digest(draft), digest(summary), 7)
    assert {event.event_type for event in store.trace(run.run_id)} == {
        "draft_created",
        "verification_completed",
        "legality_admitted",
        "graph_signed",
        "activation",
    }
