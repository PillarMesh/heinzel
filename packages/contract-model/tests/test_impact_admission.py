from __future__ import annotations

import pytest
from pillarmesh_contract_model import (
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactApprovalRequirement,
    ImpactAuthoritySnapshot,
    ImpactSubject,
    digest,
)
from pydantic import ValidationError


def _reference(artifact_id: str, version: int, character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest=character * 64)


def _subject() -> ImpactSubject:
    return ImpactSubject(
        subject_kind="metric_version_change",
        subject_ref="metric:revenue:v2",
        change_subject_digest="a" * 64,
    )


def _snapshot() -> ImpactAuthoritySnapshot:
    return ImpactAuthoritySnapshot(
        tenant_id="tenant-a",
        subject=_subject(),
        source_record_refs=(
            _reference("record:z", 1, "c"),
            _reference("record:a", 2, "b"),
        ),
        derived_approval_requirements=(
            ImpactApprovalRequirement(
                authority_ref="principal:policy",
                reason_code="validated_context_graph_dependency",
                affected_subject_ref="policy:finance",
            ),
            ImpactApprovalRequirement(
                authority_ref="principal:analytics",
                reason_code="validated_context_graph_dependency",
                affected_subject_ref="dashboard:revenue",
            ),
        ),
    )


def test_snapshot_requires_canonical_source_records_and_requirements() -> None:
    snapshot = _snapshot()

    assert tuple(reference.artifact_id for reference in snapshot.source_record_refs) == (
        "record:a",
        "record:z",
    )
    assert tuple(
        requirement.authority_ref for requirement in snapshot.derived_approval_requirements
    ) == ("principal:analytics", "principal:policy")


@pytest.mark.parametrize(
    "source_record_refs",
    (
        (_reference("record:a", 1, "a"), _reference("record:a", 1, "a")),
        (_reference("record:a", 1, "a"), _reference("record:a", 1, "b")),
    ),
)
def test_snapshot_rejects_duplicate_or_conflicting_source_versions(
    source_record_refs: tuple[ArtifactReference, ...],
) -> None:
    with pytest.raises(ValidationError, match="source record"):
        ImpactAuthoritySnapshot(
            tenant_id="tenant-a",
            subject=_subject(),
            source_record_refs=source_record_refs,
            derived_approval_requirements=(),
        )


@pytest.mark.parametrize("second_reason", ("validated_context_graph_dependency", "other_reason"))
def test_snapshot_rejects_duplicate_or_conflicting_requirements(second_reason: str) -> None:
    requirement = ImpactApprovalRequirement(
        authority_ref="principal:analytics",
        reason_code="validated_context_graph_dependency",
        affected_subject_ref="dashboard:revenue",
    )
    conflicting = requirement.model_copy(update={"reason_code": second_reason})

    with pytest.raises(ValidationError, match="approval requirement"):
        ImpactAuthoritySnapshot(
            tenant_id="tenant-a",
            subject=_subject(),
            source_record_refs=(_reference("record:a", 1, "a"),),
            derived_approval_requirements=(requirement, conflicting),
        )


def test_binding_requires_exact_tenant_subject_and_authority_digest() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError, match="tenant"):
        ImpactAdmissionBinding(
            tenant_id="tenant-b",
            subject=snapshot.subject,
            graph_snapshot_digest="d" * 64,
            authority_snapshot=snapshot,
            authority_snapshot_digest=snapshot.authority_digest,
        )
    with pytest.raises(ValidationError, match="subject"):
        ImpactAdmissionBinding(
            tenant_id=snapshot.tenant_id,
            subject=snapshot.subject.model_copy(update={"subject_ref": "metric:other:v1"}),
            graph_snapshot_digest="d" * 64,
            authority_snapshot=snapshot,
            authority_snapshot_digest=snapshot.authority_digest,
        )
    with pytest.raises(ValidationError, match="authority snapshot digest"):
        ImpactAdmissionBinding(
            tenant_id=snapshot.tenant_id,
            subject=snapshot.subject,
            graph_snapshot_digest="d" * 64,
            authority_snapshot=snapshot,
            authority_snapshot_digest="e" * 64,
        )


def test_graph_snapshot_digest_requires_lowercase_sha256_shape() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError, match="graph_snapshot_digest"):
        ImpactAdmissionBinding(
            tenant_id=snapshot.tenant_id,
            subject=snapshot.subject,
            graph_snapshot_digest="D" * 64,
            authority_snapshot=snapshot,
            authority_snapshot_digest=snapshot.authority_digest,
        )


def test_authority_digest_is_deterministic_and_domain_separated() -> None:
    first = _snapshot()
    second = _snapshot()

    assert first.authority_digest == second.authority_digest
    assert (
        first.authority_digest == "fce8daf48edb4a03d7e1289c9ce29442b8e936bf4736677c3bb3b2bfd672163f"
    )
    assert first.authority_digest != digest(first)
    binding = ImpactAdmissionBinding.create(
        graph_snapshot_digest="d" * 64,
        authority_snapshot=first,
    )
    assert binding.authority_snapshot_digest == first.authority_digest


def test_models_are_strict_frozen_and_support_only_declared_subject_kinds() -> None:
    subject = _subject()

    with pytest.raises(ValidationError, match="frozen"):
        subject.subject_ref = "metric:other:v1"
    with pytest.raises(ValidationError):
        ImpactSubject.model_validate(
            {
                "subject_kind": "unknown_change",
                "subject_ref": "metric:revenue:v2",
                "change_subject_digest": "a" * 64,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        ImpactAuthoritySnapshot.model_validate(
            {
                "tenant_id": "tenant-a",
                "subject": _subject(),
                "source_record_refs": [_reference("record:a", 1, "a")],
                "derived_approval_requirements": (),
            },
            strict=True,
        )


@pytest.mark.parametrize(
    "subject_kind",
    (
        "source_drift",
        "metric_version_change",
        "contract_supersession",
        "generation_failure",
        "policy_change",
        "grant_change",
        "retirement",
    ),
)
def test_every_declared_impact_subject_kind_is_supported(subject_kind: str) -> None:
    subject = ImpactSubject.model_validate(
        {
            "subject_kind": subject_kind,
            "subject_ref": "subject:one",
            "change_subject_digest": "a" * 64,
        },
        strict=True,
    )

    assert subject.subject_kind == subject_kind
