from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from heinzel_contract_model import (
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactApprovalRequirement,
    ImpactAuthoritySnapshot,
    ImpactSubject,
    digest,
)
from heinzel_request_management import (
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingError,
    FulfillmentIntegrityError,
    FulfillmentProposal,
    ImpactAdmissionResolutionError,
    RequestState,
)

_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_answer_fulfillment.py")))
service = _SUPPORT["service"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]


def _source_record(version: int) -> ArtifactReference:
    return ArtifactReference(
        artifact_id="metric:net-revenue",
        version=version,
        digest=("a" if version == 1 else "b") * 64,
    )


class MutableImpactResolver:
    def __init__(self) -> None:
        self.version = 1
        self.graph_version = 1

    def bind(self, *, tenant_id: str, proposal: FulfillmentProposal) -> ImpactAdmissionBinding:
        return ImpactAdmissionBinding.create(
            graph_snapshot_digest=("c" if self.graph_version == 1 else "d") * 64,
            authority_snapshot=self._authority_snapshot(tenant_id, proposal),
        )

    def rederive(
        self,
        *,
        tenant_id: str,
        subject: ImpactSubject,
        source_record_refs: tuple[ArtifactReference, ...],
    ) -> ImpactAuthoritySnapshot:
        current = self._authority_snapshot_for_subject(tenant_id, subject)
        assert source_record_refs != ()
        return current

    def _authority_snapshot(
        self, tenant_id: str, proposal: FulfillmentProposal
    ) -> ImpactAuthoritySnapshot:
        subject = ImpactSubject(
            subject_kind="metric_version_change",
            subject_ref="metric:net-revenue",
            change_subject_digest=digest(proposal.subject),
        )
        return self._authority_snapshot_for_subject(tenant_id, subject)

    def _authority_snapshot_for_subject(
        self, tenant_id: str, subject: ImpactSubject
    ) -> ImpactAuthoritySnapshot:
        return ImpactAuthoritySnapshot(
            tenant_id=tenant_id,
            subject=subject,
            source_record_refs=(_source_record(self.version),),
            derived_approval_requirements=(
                ImpactApprovalRequirement(
                    authority_ref="role:finance_data_owner",
                    reason_code="validated_context_graph_dependency",
                    affected_subject_ref="dashboard:monthly-close",
                ),
            ),
        )


class ImpactRoleResolver:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == "tenant-a" and (actor_id, authority_ref) in {
            ("requester-a", "principal:requester-a"),
            ("architect-a", "role:data_engineering_architect"),
            ("owner-a", "role:finance_data_owner"),
        }


class DefectiveImpactResolver(MutableImpactResolver):
    def __init__(self, *, failure: Exception, fail_during: str) -> None:
        super().__init__()
        self.failure = failure
        self.fail_during = fail_during

    def bind(self, *, tenant_id: str, proposal: FulfillmentProposal) -> ImpactAdmissionBinding:
        if self.fail_during == "bind":
            raise self.failure
        return super().bind(tenant_id=tenant_id, proposal=proposal)

    def rederive(
        self,
        *,
        tenant_id: str,
        subject: ImpactSubject,
        source_record_refs: tuple[ArtifactReference, ...],
    ) -> ImpactAuthoritySnapshot:
        if self.fail_during == "rederive":
            raise self.failure
        return super().rederive(
            tenant_id=tenant_id,
            subject=subject,
            source_record_refs=source_record_refs,
        )


def test_validated_impact_requirements_only_add_to_owning_requirements() -> None:
    impact = MutableImpactResolver()
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = impact
    investigating, _ = submit_and_clarify(fulfillment, requests)

    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert proposal.impact_admission_binding is not None
    assert proposal.impact_admission_binding.graph_snapshot_digest == "c" * 64
    assert {item.authority_ref for item in proposal.required_approvals} == {
        "principal:requester-a",
        "role:data_engineering_architect",
        "role:finance_data_owner",
    }


def test_changed_cited_metric_version_supersedes_bound_proposal_before_admission() -> None:
    impact = MutableImpactResolver()
    fulfillment, requests, repository = service()
    fulfillment._impact_admission_resolver = impact
    fulfillment._authority_role_resolver = ImpactRoleResolver()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "principal:requester-a": "requester-a",
        "role:data_engineering_architect": "architect-a",
        "role:finance_data_owner": "owner-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    impact.version = 2
    impact.graph_version = 2

    revised = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    assert isinstance(revised, FulfillmentProposal)
    assert revised.revision == 2
    assert revised.prior_proposal_digest == digest(proposal)
    assert revised.impact_admission_binding is not None
    assert revised.impact_admission_binding.graph_snapshot_digest == "d" * 64
    assert revised.impact_admission_binding.authority_snapshot.source_record_refs == (
        _source_record(2),
    )
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.PROPOSED
    assert repository.list_admissions("tenant-a", proposal.request_id) == ()


def test_equivalent_authority_survives_a_graph_projection_rebuild() -> None:
    impact = MutableImpactResolver()
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = impact
    fulfillment._authority_role_resolver = ImpactRoleResolver()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "principal:requester-a": "requester-a",
        "role:data_engineering_architect": "architect-a",
        "role:finance_data_owner": "owner-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    impact.graph_version = 2

    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    assert isinstance(admission, FulfillmentAdmissionReceipt)
    assert admission.proposal_digest == digest(proposal)


def test_bound_impact_proposal_fails_closed_when_authority_resolver_is_unavailable() -> None:
    impact = MutableImpactResolver()
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = impact
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    fulfillment._impact_admission_resolver = None

    with pytest.raises(ValueError, match="impact authority is not configured"):
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )


def test_declared_impact_resolution_failure_is_sanitized() -> None:
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = DefectiveImpactResolver(
        failure=ImpactAdmissionResolutionError("private provider detail"),
        fail_during="bind",
    )
    investigating, _ = submit_and_clarify(fulfillment, requests)

    with pytest.raises(FulfillmentGroundingError) as captured:
        fulfillment.propose_answer(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
            actor_id="architect-a",
            expected_revision=investigating.revision,
        )

    assert str(captured.value) == "impact authority could not be resolved"
    assert captured.value.__cause__ is None


def test_unexpected_impact_binding_defect_is_sanitized_at_public_boundary() -> None:
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = DefectiveImpactResolver(
        failure=AssertionError("adapter invariant failed"),
        fail_during="bind",
    )
    investigating, _ = submit_and_clarify(fulfillment, requests)

    with pytest.raises(FulfillmentIntegrityError) as captured:
        fulfillment.propose_answer(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
            actor_id="architect-a",
            expected_revision=investigating.revision,
        )

    assert str(captured.value) == "fulfillment operation failed integrity validation"
    assert captured.value.__cause__ is None


def test_unexpected_impact_rederivation_defect_is_sanitized_at_public_boundary() -> None:
    impact = MutableImpactResolver()
    fulfillment, requests, _repository = service()
    fulfillment._impact_admission_resolver = impact
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    fulfillment._impact_admission_resolver = DefectiveImpactResolver(
        failure=RuntimeError("adapter implementation defect"),
        fail_during="rederive",
    )

    with pytest.raises(FulfillmentIntegrityError) as captured:
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )

    assert str(captured.value) == "fulfillment operation failed integrity validation"
    assert captured.value.__cause__ is None
