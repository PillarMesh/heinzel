from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from heinzel_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import ActivatedAcquisitionContractRecord
from heinzel_provider_sdk import (
    AcquisitionField,
    AcquisitionNoValidPlan,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionProvider,
    AcquisitionProviderError,
    AcquisitionSourceObservation,
    ColumnObservation,
    ProviderObservation,
)
from heinzel_runtime import (
    AcquisitionArtifactStore,
    AcquisitionContractError,
    AcquisitionEvidenceWriter,
    AcquisitionPreparationResult,
    AcquisitionThrottledError,
    AcquisitionTransientError,
    ActivatedAcquisitionContract,
    ActivatedAcquisitionRunContracts,
    ComposedAcquisitionStateStore,
    LandingContractAuthority,
    activated_contract_resolver,
    compose_acquisition_application,
    landing_contract_resolver,
)
from heinzel_state import AcquisitionStateNotFoundError, RunService, SQLiteRunRepository
from heinzel_trigger import (
    ActivatedRunContract,
    ContractNotActivatedError,
    RunNowPolicy,
    TriggerRunService,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TENANT = "tenant-a"
CONTRACT_REF = "contract:orders"
CONTRACT_DIGEST = "c" * 64
BINDING_REF = "source-binding:orders"


class _ContractReader:
    def __init__(self, records: tuple[ActivatedAcquisitionContractRecord, ...]) -> None:
        self.records = records

    def list_contracts(self, tenant_id: str) -> tuple[ActivatedAcquisitionContractRecord, ...]:
        assert tenant_id == TENANT
        return self.records


class _EarlyState:
    def __init__(self) -> None:
        self.outcomes: list[object] = []

    def load_checkpoint(self, tenant_id: str, contract_digest: str, binding_ref: str) -> object:
        raise AcquisitionStateNotFoundError

    def record_governed_outcome(self, **values: object) -> object:
        self.outcomes.append(values)
        return values


class _Evidence:
    def __init__(self) -> None:
        self.receipts: list[object] = []

    def append(self, receipt: object) -> None:
        self.receipts.append(receipt)


def test_contract_resolver_uses_latest_contract_service_revision_for_fresh_and_replay() -> None:
    older = _record(revision=1)
    current = _record(revision=2)
    resolve = activated_contract_resolver(_ContractReader((older, current)))

    assert resolve(TENANT, CONTRACT_REF) == current
    assert resolve(TENANT, CONTRACT_REF) == current


@pytest.mark.parametrize("lifecycle_state", (None, "inactive"))
def test_contract_resolver_denies_absent_or_inactive_authority(
    lifecycle_state: str | None,
) -> None:
    records = () if lifecycle_state is None else (_record(lifecycle_state=lifecycle_state),)
    resolve = activated_contract_resolver(_ContractReader(records))

    with pytest.raises(AcquisitionContractError, match="not_activated"):
        resolve(TENANT, CONTRACT_REF)


def test_composed_application_returns_no_valid_plan_for_stale_binding() -> None:
    state = cast(ComposedAcquisitionStateStore, _EarlyState())
    evidence = _Evidence()
    application = compose_acquisition_application(
        contract_repository=_ContractReader((_record(),)),
        binding_repository=_BindingReader(_binding(revision=8)),
        observation_resolver=_never_observe,
        provider_resolver=_never_provide,
        state_store=state,
        artifact_store=cast(AcquisitionArtifactStore, object()),
        evidence_writer=cast(AcquisitionEvidenceWriter, evidence),
        reference_factory=lambda kind: f"{kind}:1",
        clock=lambda: NOW,
    )

    result = application.run_now(
        tenant_id=TENANT,
        contract_ref=CONTRACT_REF,
        trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
        acquisition_mode="snapshot",
    )

    assert isinstance(result, AcquisitionPreparationResult)
    assert isinstance(result.governed_outcome, AcquisitionNoValidPlan)
    assert result.governed_outcome.reason_codes == ("source_binding_authority_stale",)
    assert len(evidence.receipts) == 1


def test_composed_application_preserves_provider_failure_classification() -> None:
    state = _ProviderFailureState()
    evidence = _Evidence()
    application = compose_acquisition_application(
        contract_repository=_ContractReader((_record(),)),
        binding_repository=_BindingReader(_binding()),
        observation_resolver=lambda tenant_id, observation_ref: _observation(),
        provider_resolver=_throttled_provider,
        state_store=cast(ComposedAcquisitionStateStore, state),
        artifact_store=cast(AcquisitionArtifactStore, object()),
        evidence_writer=cast(AcquisitionEvidenceWriter, evidence),
        reference_factory=lambda kind: f"{kind}:1",
        clock=lambda: NOW,
    )

    with pytest.raises(AcquisitionThrottledError, match="rate_limited"):
        application.run_now(
            tenant_id=TENANT,
            contract_ref=CONTRACT_REF,
            trigger_window="2026-09-11T12:00:00Z/2026-09-11T13:00:00Z",
            acquisition_mode="snapshot",
        )

    assert len(evidence.receipts) == 1


class _BindingReader:
    def __init__(self, binding: SourceConnectionBinding) -> None:
        self.binding = binding

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        assert (tenant_id, binding_id) == (TENANT, BINDING_REF)
        return self.binding


class _ProviderFailureState(_EarlyState):
    def admit_authority(self, **values: object) -> tuple[int, int]:
        return (0, 0)

    def load_checkpoint_with_cursor(self, *values: object) -> object:
        raise AcquisitionStateNotFoundError

    def load_preparation_for_replay(self, *values: object) -> object:
        raise AcquisitionStateNotFoundError


def _schema() -> AcquisitionObjectSchema:
    fields = (AcquisitionField(name="order_id", value_type="integer", nullable=False),)
    return AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("order_id",),
        source_updated_at_field=None,
    )


def _observation() -> AcquisitionSourceObservation:
    return AcquisitionSourceObservation(
        tenant_id=TENANT,
        source_binding_ref=BINDING_REF,
        provider_kind="postgresql",
        object_observations=(
            AcquisitionObjectObservation(
                logical_object_ref="orders",
                provider_observation=ProviderObservation(
                    provider="postgresql",
                    connection_handle="connection-a",
                    object_identity="object-a",
                    object_kind="base_table",
                    schema_digest=_schema().schema_digest,
                    columns=(
                        ColumnObservation(name="order_id", type_name="BIGINT", nullable=False),
                    ),
                    key_name="order_id",
                    key_type="BIGINT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=("snapshot",),
                    observed_at=NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            ),
        ),
    )


def _contract(*, lifecycle_state: str = "activated") -> ActivatedAcquisitionContract:
    observation = _observation()
    return ActivatedAcquisitionContract.model_validate(
        {
            "tenant_id": TENANT,
            "contract_ref": CONTRACT_REF,
            "contract_digest": CONTRACT_DIGEST,
            "process_package_ref": ArtifactReference(
                artifact_id="process:orders", version=1, digest="1" * 64
            ),
            "product_intent_ref": ArtifactReference(
                artifact_id="intent:orders", version=1, digest="2" * 64
            ),
            "destination_product_ref": "product:orders",
            "source_binding_ref": BINDING_REF,
            "source_binding_revision": 7,
            "credential_revision": 2,
            "acknowledgement_consumer_ref": "land:orders",
            "capability_profile_digest": "a" * 64,
            "source_observation_ref": "observation:orders",
            "source_observation_digest": digest(observation),
            "lifecycle_state": lifecycle_state,
            "acquisition_modes": ("snapshot",),
            "object_schemas": (_schema(),),
            "record_ceiling": 100,
            "encoded_byte_ceiling": 10_000,
            "activated_by": "architect-a",
            "activated_at": NOW,
        }
    )


def _record(
    *, revision: int = 2, lifecycle_state: str = "activated"
) -> ActivatedAcquisitionContractRecord:
    contract = _contract(lifecycle_state=lifecycle_state)
    return ActivatedAcquisitionContractRecord(
        tenant_id=TENANT,
        contract_ref=CONTRACT_REF,
        revision=revision,
        contract_digest=CONTRACT_DIGEST,
        contract_artifact_ref=ArtifactReference(
            artifact_id=CONTRACT_REF, version=revision, digest=digest(contract)
        ),
        activated_by="architect-a",
        activated_at=NOW,
        contract=contract,
    )


def _binding(*, revision: int = 7) -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id=BINDING_REF,
        tenant_id=TENANT,
        provider_kind="postgresql",
        connection_handle="connection-a",
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.READY,
        approved_object_refs=("orders",),
        capability_profile_digest="a" * 64,
        source_observation_ref="observation:orders",
        credential_revision=2,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
    )


def _never_observe(tenant_id: str, observation_ref: str) -> AcquisitionSourceObservation:
    raise AssertionError("stale binding must stop before observation")


def _never_provide(binding: SourceConnectionBinding) -> AcquisitionProvider:
    raise AssertionError("stale binding must stop before provider resolution")


def _throttled_provider(binding: SourceConnectionBinding) -> AcquisitionProvider:
    raise AcquisitionProviderError(
        provider_kind="postgresql",
        classification="throttled",
        reason_code="rate_limited",
    )


def _run_contracts(
    records: tuple[ActivatedAcquisitionContractRecord, ...],
) -> ActivatedAcquisitionRunContracts:
    return ActivatedAcquisitionRunContracts(_ContractReader(records))


def test_trigger_reads_the_current_activated_contract_revision_as_its_run_authority() -> None:
    contract = _run_contracts((_record(revision=1), _record(revision=2))).load_activated(
        TENANT, CONTRACT_REF, 2
    )

    assert contract == ActivatedRunContract(
        tenant_id=TENANT, contract_ref=CONTRACT_REF, revision=2, plan_digest=CONTRACT_DIGEST
    )


def test_trigger_materializes_no_run_for_a_superseded_or_unknown_contract() -> None:
    contracts = _run_contracts((_record(revision=1), _record(revision=2)))
    runs = RunService(SQLiteRunRepository(":memory:"), clock=lambda: NOW)
    triggers = TriggerRunService(contracts, runs)

    assert contracts.load_activated(TENANT, CONTRACT_REF, 1) is None
    assert contracts.load_activated(TENANT, "contract:unknown", 2) is None
    with pytest.raises(ContractNotActivatedError):
        triggers.materialize_run_now(
            tenant_id=TENANT,
            contract_ref=CONTRACT_REF,
            contract_revision=1,
            policy=RunNowPolicy(policy_version="run-now-v1"),
            requested_at=NOW,
        )
    assert runs.list_runs(TENANT) == ()


def test_trigger_materializes_no_run_for_a_contract_that_is_no_longer_activated() -> None:
    contracts = _run_contracts((_record(revision=2, lifecycle_state="inactive"),))

    assert contracts.load_activated(TENANT, CONTRACT_REF, 2) is None


def test_an_unavailable_contract_store_is_not_read_as_no_contract() -> None:
    class _Unavailable:
        def list_contracts(self, tenant_id: str) -> tuple[ActivatedAcquisitionContractRecord, ...]:
            raise OSError("contract store unavailable")

    with pytest.raises(AcquisitionTransientError):
        ActivatedAcquisitionRunContracts(_Unavailable()).load_activated(TENANT, CONTRACT_REF, 2)


def test_landing_authority_is_read_from_the_current_activated_contract() -> None:
    resolve = landing_contract_resolver(_ContractReader((_record(revision=1), _record(revision=2))))

    authority = resolve(TENANT, CONTRACT_REF)

    assert authority == LandingContractAuthority(
        tenant_id=TENANT,
        contract_ref=CONTRACT_REF,
        contract_digest=CONTRACT_DIGEST,
        revision=2,
        acknowledgement_consumer_ref="land:orders",
    )


def test_landing_authority_refuses_a_contract_that_is_not_activated() -> None:
    resolve = landing_contract_resolver(_ContractReader((_record(lifecycle_state="inactive"),)))

    with pytest.raises(AcquisitionContractError):
        resolve(TENANT, CONTRACT_REF)
