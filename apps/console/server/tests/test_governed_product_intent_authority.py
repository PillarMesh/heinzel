"""Product intent constraints come from governed records, not from whoever proposed the intent.

`GovernedProductIntentAuthority` is the only source of constraints for typed product intent
approval. These tests pin that every constraint is derived from a record loaded by exact identity,
and that a reference which does not resolve to a current record owned by the tenant, with the
digest the reference claims, yields no constraints at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_console.governed_adapters import (
    _CADENCE_SECONDS,
    _SUPPORTED_CADENCES,
    GovernedProductIntentAuthority,
)
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    SemanticObject,
    digest,
)
from pillarmesh_contract_service import SourceObservation, SQLiteSourceObservationRepository
from pillarmesh_request_management import ProductIntentAuthorityRefs, ProductIntentConstraints
from pillarmesh_semantic_registry import SQLiteSemanticVersionRepository

_TENANT = "tenant-a"
_NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def _semantic_version(*, tenant_id: str = _TENANT) -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-revenue", version=1, digest="1" * 64
        ),
        candidate_set_digest="2" * 64,
        review_bundle_digest="3" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="The sales region assigned to revenue.",
                source_refs=("process-revenue",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(
            SemanticObject(
                object_id="total-revenue",
                name="Total revenue",
                definition="The sum of approved revenue values.",
                source_refs=("process-revenue",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-1",),
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
    )


def _observation(
    *,
    tenant_id: str = _TENANT,
    observed_at: datetime = _NOW - timedelta(hours=1),
    valid_until: datetime = _NOW + timedelta(hours=1),
) -> SourceObservation:
    return SourceObservation(
        observation_id="source-observation-sales",
        tenant_id=tenant_id,
        version=1,
        source_ref="source-live-a",
        schema_digest="4" * 64,
        observed_at=observed_at,
        valid_until=valid_until,
    )


def _reference(record: ApprovedSemanticVersion | SourceObservation) -> ArtifactReference:
    if isinstance(record, ApprovedSemanticVersion):
        return ArtifactReference(
            artifact_id=record.semantic_version_id, version=record.version, digest=digest(record)
        )
    return ArtifactReference(
        artifact_id=record.observation_id, version=record.version, digest=digest(record)
    )


class _SemanticVersions:
    """Keyed by identity only, so a record owned by another tenant can be returned."""

    def __init__(self, *records: ApprovedSemanticVersion) -> None:
        self._records = {(r.semantic_version_id, r.version): r for r in records}

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion:
        del tenant_id
        return self._records[(semantic_version_id, version)]


class _SourceObservations:
    def __init__(self, *records: SourceObservation) -> None:
        self._records = {(r.observation_id, r.version): r for r in records}

    def load(self, tenant_id: str, observation_id: str, version: int) -> SourceObservation:
        del tenant_id
        return self._records[(observation_id, version)]


def _resolve(
    semantic_versions: _SemanticVersions,
    source_observations: _SourceObservations,
    refs: ProductIntentAuthorityRefs,
    *,
    evaluated_at: datetime = _NOW,
) -> ProductIntentConstraints | None:
    return GovernedProductIntentAuthority(
        semantic_versions=semantic_versions, source_observations=source_observations
    ).resolve_constraints(tenant_id=_TENANT, authority_refs=refs, evaluated_at=evaluated_at)


def _refs(
    semantic_version: ArtifactReference, observation: ArtifactReference
) -> ProductIntentAuthorityRefs:
    return ProductIntentAuthorityRefs(
        semantic_version=semantic_version, source_observations=(observation,)
    )


def test_constraints_are_derived_from_the_referenced_records() -> None:
    semantic_version, observation = _semantic_version(), _observation()

    constraints = _resolve(
        _SemanticVersions(semantic_version),
        _SourceObservations(observation),
        _refs(_reference(semantic_version), _reference(observation)),
    )

    assert constraints == ProductIntentConstraints(
        approved_source_refs=("source-live-a",),
        approved_metric_refs=("total-revenue",),
        approved_dimension_refs=("region",),
        minimum_source_interval_seconds=86_400,
    )


@pytest.mark.parametrize(
    "case",
    (
        "unknown-semantic-version",
        "foreign-semantic-version",
        "altered-semantic-version",
        "unknown-observation",
        "foreign-observation",
        "altered-observation",
        "expired-observation",
        "future-observation",
    ),
)
def test_an_unusable_reference_yields_no_constraints(case: str) -> None:
    semantic_version, observation = _semantic_version(), _observation()
    semantic_ref, observation_ref = _reference(semantic_version), _reference(observation)
    semantic_versions = _SemanticVersions(semantic_version)
    observations = _SourceObservations(observation)
    evaluated_at = _NOW

    if case == "unknown-semantic-version":
        semantic_ref = semantic_ref.model_copy(update={"version": 2})
    elif case == "foreign-semantic-version":
        foreign = _semantic_version(tenant_id="tenant-b")
        semantic_versions, semantic_ref = _SemanticVersions(foreign), _reference(foreign)
    elif case == "altered-semantic-version":
        semantic_ref = semantic_ref.model_copy(update={"digest": "f" * 64})
    elif case == "unknown-observation":
        observation_ref = observation_ref.model_copy(update={"artifact_id": "other"})
    elif case == "foreign-observation":
        foreign_observation = _observation(tenant_id="tenant-b")
        observations = _SourceObservations(foreign_observation)
        observation_ref = _reference(foreign_observation)
    elif case == "altered-observation":
        observation_ref = observation_ref.model_copy(update={"digest": "f" * 64})
    elif case == "expired-observation":
        evaluated_at = observation.valid_until
    elif case == "future-observation":
        evaluated_at = observation.observed_at - timedelta(seconds=1)

    assert (
        _resolve(
            semantic_versions,
            observations,
            _refs(semantic_ref, observation_ref),
            evaluated_at=evaluated_at,
        )
        is None
    ), case


def test_every_supported_trigger_cadence_has_a_minimum_source_interval() -> None:
    assert frozenset({"daily"}) == _SUPPORTED_CADENCES
    assert _CADENCE_SECONDS.keys() >= _SUPPORTED_CADENCES


def test_the_owning_sqlite_stores_serve_the_authority(tmp_path: Path) -> None:
    """Resolve through the real repositories, including their tenant-scoped loads."""
    semantic_version, observation = _semantic_version(), _observation()
    semantic_path = str(tmp_path / "semantic-versions.sqlite3")
    observation_path = str(tmp_path / "source-observations.sqlite3")
    semantic_store = SQLiteSemanticVersionRepository(semantic_path)
    observation_store = SQLiteSourceObservationRepository(observation_path)
    try:
        stored_semantic_version = semantic_store.store(semantic_version)
        stored_observation = observation_store.store(observation)
        authority = GovernedProductIntentAuthority(
            semantic_versions=semantic_store, source_observations=observation_store
        )
        refs = _refs(_reference(stored_semantic_version), _reference(stored_observation))

        resolved = authority.resolve_constraints(
            tenant_id=_TENANT, authority_refs=refs, evaluated_at=_NOW
        )
        cross_tenant = authority.resolve_constraints(
            tenant_id="tenant-b", authority_refs=refs, evaluated_at=_NOW
        )
    finally:
        semantic_store.close()
        observation_store.close()

    assert resolved is not None
    assert resolved.approved_metric_refs == ("total-revenue",)
    assert cross_tenant is None


def test_activation_is_bound_to_a_recorded_approved_intent_and_its_sources() -> None:
    """An approved intent from request management gates contract-service activation."""
    import sqlite3

    from pillarmesh_console.governed_adapters import GovernedApprovedProductIntentSources
    from pillarmesh_contract_service import (
        AcquisitionActivationApproval,
        AcquisitionContractActivationDeniedError,
        ActivatedAcquisitionContract,
        ProductIntentBoundActivationService,
        SQLiteAcquisitionContractLifecycleRepository,
        ValidatedSourceBinding,
    )
    from pillarmesh_provider_sdk import AcquisitionField, AcquisitionObjectSchema
    from pillarmesh_request_management import (
        ApprovedProductIntent,
        DeliveryIntent,
        DimensionIntent,
        FreshnessObjective,
        Grain,
        MeasureIntent,
        ProductIntent,
        ProductIntentApprovalService,
        RequestManagementService,
        SQLiteRequestRepository,
    )

    class _Granting:
        def resolve_constraints(self, **_: object) -> ProductIntentConstraints:
            return ProductIntentConstraints(
                approved_source_refs=("source-live-a",),
                approved_metric_refs=("total-revenue",),
                approved_dimension_refs=("region",),
                minimum_source_interval_seconds=86_400,
            )

    request_repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    requests = RequestManagementService(request_repository, clock=lambda: _NOW)
    approvals = ProductIntentApprovalService(
        request_repository, clock=lambda: _NOW, authority=_Granting()
    )
    request = requests.submit_question(
        tenant_id=_TENANT,
        requester_id="requester-a",
        purpose="Revenue by region",
        question="What is revenue by region?",
    )
    approved = approvals.approve(
        tenant_id=_TENANT,
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=ProductIntent(
            request_id=request.request_id,
            title="Revenue by region",
            business_outcome="Regional revenue.",
            source_refs=("source-live-a",),
            grain=Grain(keys=("region",)),
            measures=(MeasureIntent(metric_ref="total-revenue", aggregation="sum"),),
            dimensions=(DimensionIntent(dimension_ref="region"),),
            filters=(),
            freshness=FreshnessObjective(maximum_age_seconds=86_400),
            delivery=DeliveryIntent(outputs=("dataset",)),
        ),
        authority_refs=ProductIntentAuthorityRefs(
            semantic_version=ArtifactReference(artifact_id="s", version=1, digest="1" * 64),
            source_observations=(ArtifactReference(artifact_id="o", version=1, digest="2" * 64),),
        ),
    )
    assert isinstance(approved, ApprovedProductIntent)

    fields = (AcquisitionField(name="region", value_type="string", nullable=False),)

    def activation(
        *, product_intent_ref: ArtifactReference, source_binding_ref: str = "source-live-a"
    ) -> dict[str, object]:
        process_ref = ArtifactReference(artifact_id="process-revenue", version=1, digest="1" * 64)
        return {
            "contract": ActivatedAcquisitionContract.model_validate(
                {
                    "tenant_id": _TENANT,
                    "contract_ref": "acquisition-contract-revenue",
                    "contract_digest": "c" * 64,
                    "process_package_ref": process_ref,
                    "product_intent_ref": product_intent_ref,
                    "destination_product_ref": "product-revenue",
                    "source_binding_ref": source_binding_ref,
                    "source_binding_revision": 1,
                    "credential_revision": 1,
                    "acknowledgement_consumer_ref": "runtime-a",
                    "capability_profile_digest": "d" * 64,
                    "source_observation_ref": "source-observation-live-a",
                    "source_observation_digest": "e" * 64,
                    "lifecycle_state": "activated",
                    "acquisition_modes": ("snapshot",),
                    "object_schemas": (
                        AcquisitionObjectSchema(
                            logical_object_ref="sales",
                            schema_digest=digest(fields),
                            fields=fields,
                            record_key_fields=("region",),
                            source_updated_at_field=None,
                        ),
                    ),
                    "record_ceiling": 100,
                    "encoded_byte_ceiling": 100_000,
                    "activated_by": "architect-a",
                    "activated_at": _NOW,
                }
            ),
            "approval": AcquisitionActivationApproval(
                tenant_id=_TENANT,
                process_package_ref=process_ref,
                product_intent_ref=product_intent_ref,
                destination_product_ref="product-revenue",
                approved_by="architect-a",
                approved_at=_NOW,
            ),
            "source_validation": ValidatedSourceBinding(
                tenant_id=_TENANT,
                source_binding_ref=source_binding_ref,
                source_binding_revision=1,
                credential_revision=1,
                capability_profile_digest="d" * 64,
                source_observation_ref="source-observation-live-a",
                source_observation_digest="e" * 64,
                validated_at=_NOW,
            ),
        }

    contracts = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    service = ProductIntentBoundActivationService(
        contracts, product_intents=GovernedApprovedProductIntentSources(approvals)
    )
    unapproved = approved.artifact_reference.model_copy(update={"digest": "f" * 64})

    for denied in (
        activation(product_intent_ref=unapproved),
        activation(
            product_intent_ref=approved.artifact_reference, source_binding_ref="source-other"
        ),
    ):
        with pytest.raises(AcquisitionContractActivationDeniedError):
            service.activate(idempotency_key="denied", **denied)  # type: ignore[arg-type]
    activated = service.activate(
        idempotency_key="activate-revenue",
        **activation(product_intent_ref=approved.artifact_reference),  # type: ignore[arg-type]
    )

    assert activated.contract.product_intent_ref == approved.artifact_reference
    assert contracts.list_contracts(_TENANT) == (activated,)
