from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import (
    AcquisitionActivationApproval,
    AcquisitionContractActivationConflictError,
    AcquisitionContractActivationDeniedError,
    AcquisitionContractLifecycleNotFoundError,
    ActivatedAcquisitionContract,
    ProductIntentBoundActivationService,
    SQLiteAcquisitionContractLifecycleRepository,
    StaleAcquisitionContractLifecycleError,
    ValidatedSourceBinding,
)
from heinzel_provider_sdk import AcquisitionField, AcquisitionObjectSchema

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _reference(artifact_id: str, version: int, marker: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest=marker * 64)


def _object_schema() -> AcquisitionObjectSchema:
    fields = (AcquisitionField(name="invoice_id", value_type="string", nullable=False),)
    return AcquisitionObjectSchema(
        logical_object_ref="billing.invoice",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("invoice_id",),
        source_updated_at_field=None,
    )


def _activation_inputs(
    *, tenant_id: str = "tenant-a"
) -> tuple[ActivatedAcquisitionContract, AcquisitionActivationApproval, ValidatedSourceBinding]:
    process_reference = _reference("process-package-a", 3, "a")
    product_intent_reference = _reference("product-intent-a", 2, "b")
    destination_product_ref = "warehouse-product-a"
    return (
        ActivatedAcquisitionContract.model_validate(
            {
                "tenant_id": tenant_id,
                "contract_ref": "acquisition-contract-a",
                "contract_digest": "c" * 64,
                "process_package_ref": process_reference,
                "product_intent_ref": product_intent_reference,
                "destination_product_ref": destination_product_ref,
                "source_binding_ref": "source-binding-a",
                "source_binding_revision": 4,
                "credential_revision": 2,
                "acknowledgement_consumer_ref": "runtime-a",
                "capability_profile_digest": "d" * 64,
                "source_observation_ref": "source-observation-a",
                "source_observation_digest": "e" * 64,
                "lifecycle_state": "activated",
                "acquisition_modes": ("snapshot",),
                "object_schemas": (_object_schema(),),
                "record_ceiling": 1_000,
                "encoded_byte_ceiling": 1_000_000,
                "activated_by": "architect-a",
                "activated_at": NOW,
            }
        ),
        AcquisitionActivationApproval(
            tenant_id=tenant_id,
            process_package_ref=process_reference,
            product_intent_ref=product_intent_reference,
            destination_product_ref=destination_product_ref,
            approved_by="architect-a",
            approved_at=NOW,
        ),
        ValidatedSourceBinding(
            tenant_id=tenant_id,
            source_binding_ref="source-binding-a",
            source_binding_revision=4,
            credential_revision=2,
            capability_profile_digest="d" * 64,
            source_observation_ref="source-observation-a",
            source_observation_digest="e" * 64,
            validated_at=NOW,
        ),
    )


def test_contract_activation_is_transactional_and_replay_safe() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract, approval, source_validation = _activation_inputs()

    first = repository.activate_contract(
        idempotency_key="activate-request-a",
        contract=contract,
        approval=approval,
        source_validation=source_validation,
    )
    replay = repository.activate_contract(
        idempotency_key="activate-request-a",
        contract=contract,
        approval=approval,
        source_validation=source_validation,
    )

    assert first.revision == 1
    assert first.contract == contract
    assert first.contract_artifact_ref.artifact_id == contract.contract_ref
    assert first.contract_artifact_ref.version == 1
    assert first.contract_artifact_ref.digest == digest(contract)
    assert first.contract.schema_version == "2"
    assert replay.model_dump_json() == first.model_dump_json()
    assert repository.list_contracts("tenant-a") == (first,)
    assert repository.get_contract("tenant-a", contract.contract_ref, 1) == first
    assert repository.list_contracts("tenant-b") == ()
    with pytest.raises(AcquisitionContractLifecycleNotFoundError):
        repository.get_contract("tenant-b", contract.contract_ref, 1)


@pytest.mark.parametrize("changed_input", ("contract", "approval", "source_validation"))
def test_contract_activation_rejects_conflicting_idempotency_replay(changed_input: str) -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract, approval, source_validation = _activation_inputs()
    repository.activate_contract(
        idempotency_key="activate-request-a",
        contract=contract,
        approval=approval,
        source_validation=source_validation,
    )
    if changed_input == "contract":
        contract = contract.model_copy(update={"record_ceiling": contract.record_ceiling + 1})
    elif changed_input == "approval":
        approval = approval.model_copy(update={"approved_by": "architect-b"})
    else:
        source_validation = source_validation.model_copy(update={"credential_revision": 3})

    with pytest.raises(
        AcquisitionContractActivationConflictError,
        match="idempotency key has conflicting activation inputs",
    ) as conflict:
        repository.activate_contract(
            idempotency_key="activate-request-a",
            contract=contract,
            approval=approval,
            source_validation=source_validation,
        )

    assert type(conflict.value) is AcquisitionContractActivationConflictError
    assert repository.list_contracts("tenant-a")[0].contract.record_ceiling == 1_000


@pytest.mark.parametrize("invalid_authority", ("process", "source", "tenant"))
def test_contract_activation_denies_unvalidated_authority_without_enumeration(
    invalid_authority: str,
) -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract, approval, source_validation = _activation_inputs()
    if invalid_authority == "process":
        approval = approval.model_copy(
            update={"process_package_ref": _reference("process-package-a", 2, "f")}
        )
    elif invalid_authority == "source":
        source_validation = source_validation.model_copy(update={"source_binding_revision": 3})
    else:
        source_validation = source_validation.model_copy(update={"tenant_id": "tenant-b"})

    with pytest.raises(
        AcquisitionContractActivationDeniedError,
        match="activation authority is not validated",
    ) as denied:
        repository.activate_contract(
            idempotency_key="activate-request-a",
            contract=contract,
            approval=approval,
            source_validation=source_validation,
        )

    assert type(denied.value) is AcquisitionContractActivationDeniedError
    assert repository.list_contracts("tenant-a") == ()


def test_contract_activation_rolls_back_contract_and_authorities_together() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")

    class FailsSourceValidationInsert:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if "INSERT INTO acquisition_source_validations" in sql:
                raise sqlite3.OperationalError("database is full")
            return self._wrapped.execute(sql, parameters)

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    healthy = repository._connection
    repository._connection = FailsSourceValidationInsert(healthy)  # type: ignore[assignment]
    contract, approval, source_validation = _activation_inputs()
    with pytest.raises(sqlite3.OperationalError, match="database is full"):
        repository.activate_contract(
            idempotency_key="activate-request-a",
            contract=contract,
            approval=approval,
            source_validation=source_validation,
        )
    repository._connection = healthy  # type: ignore[assignment]

    assert repository.list_contracts("tenant-a") == ()
    assert healthy.execute("SELECT COUNT(*) FROM acquisition_activation_approvals").fetchone() == (
        0,
    )
    assert healthy.execute("SELECT COUNT(*) FROM acquisition_source_validations").fetchone() == (0,)


def test_activation_is_idempotent_and_tenant_qualified() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")

    first = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW,
    )
    replay = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW + timedelta(seconds=1),
    )

    assert replay == first
    with pytest.raises(AcquisitionContractLifecycleNotFoundError):
        repository.get("tenant-b", "4" * 64)


def test_retirement_is_exact_cas_and_cannot_reactivate() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    activated = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW,
    )

    with pytest.raises(StaleAcquisitionContractLifecycleError):
        repository.retire(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            expected_revision=activated.revision + 1,
            retired_at=NOW + timedelta(seconds=1),
        )
    retired = repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=1),
    )
    replay = repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=2),
    )

    assert replay == retired
    assert retired.revision == 2
    assert retired.lifecycle_state == "retired"
    with pytest.raises(StaleAcquisitionContractLifecycleError, match="cannot reactivate"):
        repository.activate(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            activated_at=NOW + timedelta(seconds=3),
        )


def test_activated_contracts_list_only_for_their_own_tenant() -> None:
    """A tenant-scoped listing is the first half of deriving a run's tenant.

    The repository could only read one contract at a time, so nothing could answer
    "which contracts are activated for this tenant". Listing is what makes the
    tenant derivable from a run's contract digest without storing a tenant on the
    run itself.
    """
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    repository.activate(tenant_id="tenant-a", contract_digest="4" * 64, activated_at=NOW)
    repository.activate(tenant_id="tenant-a", contract_digest="5" * 64, activated_at=NOW)
    repository.activate(tenant_id="tenant-b", contract_digest="6" * 64, activated_at=NOW)

    listed = repository.list_activated("tenant-a")

    assert {state.contract_digest for state in listed} == {"4" * 64, "5" * 64}
    assert {state.tenant_id for state in listed} == {"tenant-a"}


def test_a_retired_contract_leaves_the_activated_listing() -> None:
    """Listing means *activated*, not *ever activated*.

    A retired contract's runs were still witnessed under this tenant, but the
    listing answers which contracts are live, and retirement is exactly the
    transition that ends that.
    """
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    activated = repository.activate(
        tenant_id="tenant-a", contract_digest="4" * 64, activated_at=NOW
    )
    repository.activate(tenant_id="tenant-a", contract_digest="5" * 64, activated_at=NOW)

    repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=1),
    )

    listed = repository.list_activated("tenant-a")
    assert {state.contract_digest for state in listed} == {"5" * 64}


def test_a_tenant_with_no_activated_contracts_lists_empty() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")

    assert repository.list_activated("tenant-unknown") == ()


class _ProductIntents:
    """Grants one approved intent for one tenant, with the given sources."""

    def __init__(self, reference: ArtifactReference, tenant_id: str, sources: tuple[str, ...]):
        self._reference, self._tenant_id, self._sources = reference, tenant_id, sources

    def approved_source_refs(
        self, *, tenant_id: str, product_intent_ref: ArtifactReference
    ) -> tuple[str, ...] | None:
        if tenant_id != self._tenant_id or product_intent_ref != self._reference:
            return None
        return self._sources


def _service(
    repository: SQLiteAcquisitionContractLifecycleRepository,
    *,
    sources: tuple[str, ...] = ("source-binding-a",),
    reference: ArtifactReference | None = None,
) -> ProductIntentBoundActivationService:
    contract, _, _ = _activation_inputs()
    return ProductIntentBoundActivationService(
        repository,
        product_intents=_ProductIntents(
            reference or contract.product_intent_ref, "tenant-a", sources
        ),
    )


def test_activation_of_an_approved_intent_is_recorded_and_replays() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract, approval, validation = _activation_inputs()
    service = _service(repository)

    first = service.activate(
        idempotency_key="activate-1",
        contract=contract,
        approval=approval,
        source_validation=validation,
    )
    replay = service.activate(
        idempotency_key="activate-1",
        contract=contract,
        approval=approval,
        source_validation=validation,
    )

    assert first.revision == 1
    assert replay == first
    assert repository.list_contracts("tenant-a") == (first,)


@pytest.mark.parametrize("case", ("unapproved-intent", "other-tenant", "source-not-approved"))
def test_activation_is_refused_without_an_approved_intent_for_the_source(case: str) -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    contract, approval, validation = _activation_inputs()
    if case == "unapproved-intent":
        service = _service(
            repository,
            reference=ArtifactReference(artifact_id="never-approved", version=1, digest="9" * 64),
        )
    elif case == "other-tenant":
        service = _service(repository)
        contract, approval, validation = _activation_inputs(tenant_id="tenant-b")
    else:
        service = _service(repository, sources=("some-other-source",))

    with pytest.raises(AcquisitionContractActivationDeniedError):
        service.activate(
            idempotency_key=f"activate-{case}",
            contract=contract,
            approval=approval,
            source_validation=validation,
        )

    assert repository.list_contracts(contract.tenant_id) == ()
