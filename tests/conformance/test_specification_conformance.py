"""Pin the managed-platform addendum to the code that implements it.

The addendum is design authority: sections 6.2, 6.4.1, 9.1.1, 9.1.2, and 13.3.1
state field shapes and transition tables that services must honour. Nothing else
stops a table being edited on one side only, and a specification that quietly
disagrees with the code is worse than one that says nothing, because it is still
trusted.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest
from heinzel_access_control import (
    ConnectedAuthorityProvenance,
    CurrentEntitlementSnapshot,
    EnterpriseEntitlementAssertion,
    EnterpriseEntitlementObservation,
    EntitlementFilterDomain,
    EntitlementLookupRequest,
    SignedEntitlementBody,
    SignedEntitlementEnvelope,
)
from heinzel_bi_control import DashboardContract
from heinzel_catalog_control import CatalogBinding
from heinzel_catalog_control.service import _TRANSITIONS as CATALOG_TRANSITIONS
from heinzel_compiler import GovernedQueryPlan
from heinzel_connection_broker import (
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
)
from heinzel_connection_broker.service import _TRANSITIONS as SOURCE_BINDING_TRANSITIONS
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    InformationKind,
    ManagedIntegrationContract,
)
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionBoundary,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionNoValidPlan,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    AcquisitionSourceObservation,
    ResynchronizationRequired,
)
from heinzel_request_management import (
    AccessScopePreview,
    AnswerIntentValidation,
    AnswerQuestionIntent,
    AnswerScopePolicy,
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DataProductChangeRequest,
    DecisionKind,
    DenialDispositionReceipt,
    DisclosureDenial,
    FulfillmentAdmissionReceipt,
    FulfillmentApprovalBinding,
    FulfillmentEvidenceReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    PolicyAdmissionReceipt,
    RequestDependency,
    RequestNoValidPlan,
    StakeholderAnswerDraft,
)
from heinzel_request_management.service import _TRANSITIONS as REQUEST_TRANSITIONS
from heinzel_runtime import AnswerExecutionReceipt
from heinzel_semantic_registry import (
    ApprovedProductVersionMetadata,
    AuthorityObservation,
    OntologyReviewBundle,
    OntologyReviewItem,
    ReviewItemDecision,
    SemanticCandidateSet,
)
from heinzel_warehouse_control.evidence import (
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
)
from heinzel_warehouse_control.models import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseBinding,
    WarehouseFailureClassification,
    WarehousePrincipalClass,
    WarehouseValidationProfile,
)
from heinzel_warehouse_control.service import _TRANSITIONS as WAREHOUSE_TRANSITIONS
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

ADDENDUM = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "architecture"
    / "specifications"
    / "managed-data-engineering-platform-addendum-v0.1.md"
)


@pytest.mark.parametrize(
    ("artifact", "heading"),
    [
        (ConnectedAuthorityProvenance, "#### ConnectedAuthorityProvenance"),
        (EntitlementFilterDomain, "#### EntitlementFilterDomain"),
        (EnterpriseEntitlementAssertion, "#### EnterpriseEntitlementAssertion"),
        (EnterpriseEntitlementObservation, "#### EnterpriseEntitlementObservation"),
        (CurrentEntitlementSnapshot, "#### CurrentEntitlementSnapshot"),
        (EntitlementLookupRequest, "#### EntitlementLookupRequest"),
        (SignedEntitlementBody, "#### SignedEntitlementBody"),
        (SignedEntitlementEnvelope, "#### SignedEntitlementEnvelope"),
    ],
)
def test_access_control_artifact_fields_match_section_18_2(
    artifact: type[BaseModel], heading: str
) -> None:
    assert _fenced_fields(heading) == [artifact.__name__, *artifact.model_fields]


def test_dashboard_contract_fields_match_section_16_3() -> None:
    assert _fenced_fields("### 16.3 Dashboard contract") == [
        DashboardContract.__name__,
        *DashboardContract.model_fields,
    ]


def _section(heading: str, *, addendum: Path = ADDENDUM) -> str:
    text = addendum.read_text(encoding="utf-8")
    marker = f"\n{heading}"
    assert marker in text, f"addendum no longer contains {heading!r}"
    body = text.split(marker, 1)[1]
    following = re.search(r"\n#{2,4} ", body)
    return body[: following.start()] if following else body


def _fenced_block(heading: str, *, addendum: Path = ADDENDUM) -> str:
    section = _section(heading, addendum=addendum)
    assert "```text" in section, f"{heading} no longer contains a fenced block"
    return section.split("```text", 1)[1].split("```", 1)[0]


def _fenced_fields(heading: str, *, addendum: Path = ADDENDUM) -> list[str]:
    return [
        line.split()[0]
        for line in _fenced_block(heading, addendum=addendum).strip().splitlines()
        if line.strip()
    ]


def _normalized_fenced_lines(heading: str, *, addendum: Path = ADDENDUM) -> list[str]:
    return [
        " ".join(line.split())
        for line in _fenced_block(heading, addendum=addendum).strip().splitlines()
        if line.strip()
    ]


def _normalized_section(heading: str) -> str:
    return " ".join(_section(heading).split())


def _markdown_table_rows(heading: str) -> list[tuple[str, str]]:
    """Read a two-column markdown table from a section, skipping header and rule."""
    rows: list[tuple[str, str]] = []
    for line in _section(heading).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] in {"Candidate kind"} or set(cells[0]) <= {"-", " "}:
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _markdown_rows(heading: str, column_count: int) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for line in _section(heading).splitlines():
        if not line.startswith("|"):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) != column_count or all(set(cell) <= {"-", " "} for cell in cells):
            continue
        rows.append(cells)
    return rows[1:]


def _documented_transitions(heading: str) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for line in _fenced_block(heading).strip().splitlines():
        source, arrow, targets = line.partition("→")
        assert arrow, f"{heading} row is not a transition: {line!r}"
        parsed = {target.strip() for target in targets.split(",") if target.strip()}
        parsed.discard("(terminal)")
        rows[source.strip()] = parsed
    return rows


def _implemented_transitions(transitions: dict[object, frozenset[object]]) -> dict[str, set[str]]:
    return {
        state.value: {target.value for target in targets}  # type: ignore[attr-defined]
        for state, targets in transitions.items()
    }


def test_warehouse_transition_table_matches_section_6_4_1() -> None:
    documented = _documented_transitions("### 6.4.1")
    implemented = _implemented_transitions(WAREHOUSE_TRANSITIONS)

    assert documented == implemented


def test_source_binding_transition_table_matches_section_11_4() -> None:
    documented = _documented_transitions("#### Source connection binding transitions")
    implemented = _implemented_transitions(SOURCE_BINDING_TRANSITIONS)

    assert documented == implemented


def test_request_transition_table_matches_section_13_3_1() -> None:
    documented = _documented_transitions("### 13.3.1")
    implemented = _implemented_transitions(REQUEST_TRANSITIONS)

    # Section 13.3.1 lists only the states with outgoing transitions and names the
    # terminals in prose, so compare that half here and the terminals below.
    assert documented == {state: targets for state, targets in implemented.items() if targets}


def test_request_terminal_states_match_section_13_3_1_prose() -> None:
    sentence = next(line for line in _section("### 13.3.1").splitlines() if "are terminal" in line)
    documented = set(re.findall(r"`([a-z_]+)`", sentence))
    implemented = {state.value for state, targets in REQUEST_TRANSITIONS.items() if not targets}

    assert documented == implemented


@pytest.mark.parametrize(
    ("artifact", "heading"),
    (
        (FulfillmentGroundingSnapshot, "#### FulfillmentGroundingSnapshot"),
        (FulfillmentPolicySnapshot, "#### FulfillmentPolicySnapshot"),
        (ClarifiedOutcomeStatement, "#### ClarifiedOutcomeStatement"),
        (StakeholderAnswerDraft, "#### StakeholderAnswerDraft"),
        (AccessScopePreview, "#### AccessScopePreview"),
        (DisclosureDenial, "#### DisclosureDenial"),
        (ApprovalRequirement, "#### ApprovalRequirement"),
        (FulfillmentProposal, "#### FulfillmentProposal"),
        (FulfillmentApprovalBinding, "#### FulfillmentApprovalBinding"),
        (FulfillmentAdmissionReceipt, "#### FulfillmentAdmissionReceipt"),
        (DenialDispositionReceipt, "#### DenialDispositionReceipt"),
        (RequestDependency, "#### RequestDependency"),
        (DataProductChangeRequest, "#### DataProductChangeRequest"),
        (RequestNoValidPlan, "#### RequestNoValidPlan"),
        (FulfillmentEvidenceReceipt, "#### FulfillmentEvidenceReceipt"),
    ),
)
def test_request_fulfillment_artifact_fields_match_section_13_7(
    artifact: type[BaseModel], heading: str
) -> None:
    assert _fenced_fields(heading) == [artifact.__name__, *artifact.model_fields]


@pytest.mark.parametrize(
    ("artifact", "heading"),
    (
        (AnswerScopePolicy, "#### AnswerScopePolicy"),
        (AnswerQuestionIntent, "#### AnswerQuestionIntent"),
        (AnswerIntentValidation, "#### AnswerIntentValidation"),
        (GovernedQueryPlan, "#### GovernedQueryPlan"),
        (PolicyAdmissionReceipt, "#### PolicyAdmissionReceipt"),
        (SourceFreshnessObservation, "#### SourceFreshnessObservation"),
        (ApprovedProductVersionMetadata, "#### ApprovedProductVersionMetadata"),
        (AnswerExecutionReceipt, "#### AnswerExecutionReceipt"),
    ),
)
def test_governed_answer_artifact_fields_match_section_13_8(
    artifact: type[BaseModel], heading: str
) -> None:
    assert _fenced_fields(heading) == [artifact.__name__, *artifact.model_fields]


@pytest.mark.parametrize(
    ("artifact", "heading"),
    (
        (SourceConnectionBinding, "#### SourceConnectionBinding"),
        (SourceBindingValidationEvidence, "#### SourceBindingValidationEvidence"),
        (AcquisitionSourceObservation, "#### AcquisitionSourceObservation"),
        (AcquisitionObjectObservation, "#### AcquisitionObjectObservation"),
        (AcquisitionIntent, "#### AcquisitionIntent"),
        (AcquisitionField, "#### AcquisitionField"),
        (AcquisitionObjectSchema, "#### AcquisitionObjectSchema"),
        (AcquisitionFieldValue, "#### AcquisitionFieldValue"),
        (AcquisitionRecord, "#### AcquisitionRecord"),
        (AcquisitionBoundary, "#### AcquisitionBoundary"),
        (AcquisitionSegmentManifest, "#### AcquisitionSegmentManifest"),
        (AcquisitionBatchManifest, "#### AcquisitionBatchManifest"),
        (AcquisitionPreparedReceipt, "#### AcquisitionPreparedReceipt"),
        (AcquisitionAcknowledgement, "#### AcquisitionAcknowledgement"),
        (AcquisitionCheckpointReceipt, "#### AcquisitionCheckpointReceipt"),
        (AcquisitionNoValidPlan, "#### AcquisitionNoValidPlan"),
        (ResynchronizationRequired, "#### ResynchronizationRequired"),
    ),
)
def test_source_acquisition_acquisition_fields_match_section_11_4(
    artifact: type[BaseModel], heading: str
) -> None:
    assert _fenced_fields(heading) == [artifact.__name__, *artifact.model_fields]


def test_request_fulfillment_approval_matrix_matches_section_13_7() -> None:
    assert _markdown_rows("#### Plan 3B approval matrix", 3) == [
        (
            "Stakeholder answer",
            "Requester clarified-outcome acceptance; `role:data_engineering_architect`",
            "`role:policy_authority` for classified disclosure",
        ),
        (
            "Access scope",
            "Requester clarified-outcome acceptance; exact data-product owner",
            "`role:policy_authority` for classified, finance, residency, retention, masking, "
            "or widened-access implications",
        ),
        (
            "Disclosure denial",
            "Requester clarified-outcome acceptance; `role:data_engineering_architect`",
            "Applicable `role:policy_authority`",
        ),
    ]


def test_request_fulfillment_authority_records_remain_disjoint() -> None:
    assert _markdown_rows("#### Plan 3B authority record boundary", 3) == [
        (
            "`DecisionBinding`",
            "Plan 2 semantic review",
            "Never satisfies a Plan 3B requirement",
        ),
        (
            "`FulfillmentApprovalBinding`",
            "Plan 3B fulfillment proposal",
            "Never satisfies a Plan 2 semantic review",
        ),
    ]


def test_request_fulfillment_admission_predicate_is_pinned_term_by_term() -> None:
    assert _normalized_fenced_lines("#### Plan 3B admission predicate") == [
        "request_state awaiting_approval",
        "proposal_revision latest",
        "tenant_match request | proposal | grounding | policy | every_binding",
        "snapshot_digests exact",
        "policy_valid_until future_or_reresolve",
        "approval_match authority_ref | subject_digest | proposal_digest | proposal_revision | "
        "approve",
        "negative_decisions none",
        "current_actor_role required",
        "cancelled false",
    ]


def test_request_fulfillment_control_plane_non_claims_are_exact() -> None:
    assert _fenced_fields("#### Plan 3B execution non-claims") == [
        "query_execution",
        "grant_application",
        "requester_delivery",
        "verification",
        "expiry",
        "revocation",
    ]


def test_request_fulfillment_outcome_matrix_is_pinned() -> None:
    assert _markdown_rows("#### Plan 3B outcome matrix", 3) == [
        ("Unsettled restatement", "Clarification pending", "`clarifying`"),
        ("Approved assets and scope", "Answer or access proposal", "`proposed`"),
        ("Missing semantic meaning", "Semantic-change dependency", "`investigating`"),
        ("Missing governed data capability", "Data-product-change dependency", "`investigating`"),
        ("Insufficient disclosure authorization", "Denial proposal", "`proposed`"),
        ("Conflicting or unverifiable authority", "`No Valid Plan`", "`no_valid_plan`"),
        ("Material candidate edit", "Superseding proposal revision", "`investigating`"),
        ("Complete answer or access approvals", "Execution-ready admission", "`executing`"),
        ("Complete denial approvals", "Denial disposition", "`rejected`"),
        ("Exact requirement rejected", "Rejection", "`rejected`"),
        ("Approver requests changes", "New investigation", "`investigating`"),
        ("Expired equivalent policy", "Admission after re-resolution", "`executing`"),
        ("Expired changed policy", "Superseding proposal revision", "`investigating`"),
        ("Cancellation with open proposal", "Cancellation evidence only", "`cancelled`"),
    ]


def test_request_fulfillment_visibility_matrix_is_pinned() -> None:
    assert _markdown_rows("#### Plan 3B visibility matrix", 6) == [
        (
            "Requester",
            "Own request",
            "Yes",
            "Never before verified delivery",
            "Own labelled Plan 2 and Plan 3B decisions",
            "Status only",
        ),
        ("Data engineering architect", "Yes", "Yes", "Yes", "Yes", "Yes"),
        (
            "Required approver",
            "Relevant conversation",
            "Yes",
            "Exact subject requiring the role",
            "Relevant role",
            "Status",
        ),
        ("Unrelated tenant actor", "No", "No", "No", "No", "No"),
    ]


def test_warehouse_binding_fields_match_section_6_2() -> None:
    documented = [
        line.split()[0] for line in _fenced_block("### 6.2").strip().splitlines() if line.strip()
    ]

    # Order is part of the contract: the section is read as the artifact's shape.
    assert documented == ["WarehouseBinding", *WarehouseBinding.model_fields]


def _assert_documented_artifact_fields(
    artifact: type[BaseModel], heading: str, *, addendum: Path = ADDENDUM
) -> None:
    expected = [artifact.__name__]
    for field_name, field in artifact.model_fields.items():
        documented = field_name
        if field.default is not PydanticUndefined:
            documented = f"{documented} {field.default}"
        if isinstance(field.annotation, type) and issubclass(field.annotation, StrEnum):
            documented = f"{documented} {' | '.join(member.value for member in field.annotation)}"
        if field_name == "engine_version":
            pattern = next(
                metadata.pattern
                for metadata in field.metadata
                if getattr(metadata, "pattern", None) is not None
            )
            max_length = next(
                metadata.max_length
                for metadata in field.metadata
                if getattr(metadata, "max_length", None) is not None
            )
            documented = f"{documented} {pattern} max_length={max_length}"
        expected.append(documented)

    assert _normalized_fenced_lines(heading, addendum=addendum) == expected


@pytest.mark.parametrize(
    ("artifact", "heading"),
    (
        (WarehouseValidationEvidence, "#### WarehouseValidationEvidence"),
        (WarehouseResumeValidationEvidence, "#### WarehouseResumeValidationEvidence"),
        (WarehouseRestoreVerification, "#### WarehouseRestoreVerification"),
        (WarehouseRetirementEvidence, "#### WarehouseRetirementEvidence"),
    ),
)
def test_warehouse_evidence_fields_match_section_6_2(
    artifact: type[BaseModel], heading: str
) -> None:
    _assert_documented_artifact_fields(artifact, heading)


def test_warehouse_principal_classes_match_section_18_1() -> None:
    documented = _fenced_fields("### 18.1")
    implemented = [member.value for member in WarehousePrincipalClass]

    expected = [
        "administration",
        "ingestion_runtime",
        "transformation_runtime",
        "answer_runtime",
        "backup_restore",
        "customer_sql",
        "catalog",
        "bi",
    ]
    assert documented == implemented == expected


def test_warehouse_validation_vocabulary_matches_section_6_2() -> None:
    section = _fenced_block("#### WarehouseValidationEvidence")
    profile_line = next(line for line in section.splitlines() if "validation_profile" in line)
    disposition_line = next(
        line for line in section.splitlines() if "encryption_at_rest_disposition" in line
    )
    documented_profiles = {value.strip() for value in profile_line.split(maxsplit=1)[1].split("|")}
    documented_dispositions = {
        value.strip() for value in disposition_line.split(maxsplit=1)[1].split("|")
    }

    assert "validation_profile" in section
    assert documented_profiles == {member.value for member in WarehouseValidationProfile}
    assert documented_dispositions == {member.value for member in EncryptionAtRestDisposition}


def test_warehouse_failure_classifications_match_section_6_2() -> None:
    documented = _fenced_fields("#### WarehouseFailureClassification")

    assert documented == [member.value for member in WarehouseFailureClassification]


def test_warehouse_provider_operations_match_section_6_4() -> None:
    documented = _fenced_fields("#### Provider operations")

    assert documented == ["provision", "reconcile", "validate", "suspend", "resume", "retire"]


def test_warehouse_lifecycle_gates_match_section_6_4() -> None:
    lifecycle = _normalized_section("### 6.4")

    assert "record_validation" in lifecycle
    assert "record_resume_validation" in lifecycle
    assert "record_retirement" in lifecycle
    assert "Plain `transition` cannot enter `ready` or `retired`" in lifecycle
    assert (
        "fresh TLS, network-isolation, monitoring, positive, denial, engine-version, and "
        "storage-integrity probes" in lifecycle
    )


def test_warehouse_retirement_meaning_matches_section_6_4() -> None:
    lifecycle = _normalized_section("### 6.4")

    assert (
        "each exact ledger resource is complete, retained to its contractual deadline, or has an "
        "attributable cleanup failure" in lifecycle
    )
    assert "does not mean data is physically deleted before its retention deadline" in lifecycle


def test_warehouse_readiness_conditions_match_sections_6_4_and_6_5() -> None:
    lifecycle = _normalized_section("### 6.4")
    components = _normalized_section("### 6.5")

    assert "engine identity, version, storage encryption, network isolation" in lifecycle
    assert "runtime and administration roles, target and ledger capabilities" in lifecycle
    assert "backups, monitoring, fixed-capacity alerts, and positive and denial probes" in lifecycle
    assert "local_acceptance` with `deferred_local_acceptance" in components
    assert "production` with `proven" in components


def test_clickhouse_exclusions_match_section_20_9() -> None:
    exit_criteria = _normalized_section("### 20.9")

    assert "multi-statement transactional guarantees" in exit_criteria
    assert "transactional DDL rollback" in exit_criteria
    assert "deferred foreign-key enforcement" in exit_criteria
    assert "row-by-row update/delete semantics" in exit_criteria
    assert "No Valid Plan" in exit_criteria


def test_delivery_sequence_keeps_plan_3a_before_downstream_capabilities() -> None:
    delivery_sequence = _normalized_section("## 21")

    assert "Plan 3A warehouse contracts" in delivery_sequence
    assert (
        "precede source acquisition, destination data movement, transformations, scheduling, "
        "Superset, and full operations" in delivery_sequence
    )


def test_mutated_warehouse_evidence_field_fails_conformance_comparison(tmp_path: Path) -> None:
    mutated_addendum = tmp_path / "managed-data-engineering-platform-addendum-v0.1.md"
    mutated_addendum.write_text(
        ADDENDUM.read_text(encoding="utf-8").replace("  evidence_id\n", "  evidence_handle\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError):
        _assert_documented_artifact_fields(
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            addendum=mutated_addendum,
        )


@pytest.mark.parametrize(
    ("artifact", "heading", "documented", "mutated"),
    (
        (
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            "schema_version                    1",
            "schema_version                    2",
        ),
        (
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            "engine_kind                       postgresql | clickhouse",
            "engine_kind                       postgresql | mysql",
        ),
        (
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            "validation_profile                local_acceptance | production",
            "validation_profile                local_acceptance | staging",
        ),
        (
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            "encryption_at_rest_disposition    proven | deferred_local_acceptance",
            "encryption_at_rest_disposition    proven | deferred_production",
        ),
        (
            WarehouseValidationEvidence,
            "#### WarehouseValidationEvidence",
            "max_length=64",
            "max_length=65",
        ),
        (
            WarehouseResumeValidationEvidence,
            "#### WarehouseResumeValidationEvidence",
            "engine_kind                     postgresql | clickhouse",
            "engine_kind                     postgresql | mysql",
        ),
    ),
)
def test_mutated_warehouse_evidence_annotation_fails_conformance_comparison(
    artifact: type[BaseModel], heading: str, documented: str, mutated: str, tmp_path: Path
) -> None:
    mutated_addendum = tmp_path / "managed-data-engineering-platform-addendum-v0.1.md"
    addendum = ADDENDUM.read_text(encoding="utf-8")
    artifact_start = addendum.index(f"\n{heading}")
    mutated_addendum.write_text(
        addendum[:artifact_start] + addendum[artifact_start:].replace(documented, mutated, 1),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError):
        _assert_documented_artifact_fields(artifact, heading, addendum=mutated_addendum)


def test_documented_engine_catalog_matches_engine_kind() -> None:
    line = next(line for line in _fenced_block("### 6.2").splitlines() if "engine_kind" in line)
    documented = {value.strip() for value in line.split(maxsplit=1)[1].split("|")}

    assert documented == {member.value for member in EngineKind}


def test_catalog_binding_fields_match_section_9_1_1() -> None:
    documented = _fenced_fields("### 9.1.1")

    assert documented == ["CatalogBinding", *CatalogBinding.model_fields]


def test_semantic_candidate_set_fields_match_section_8_4() -> None:
    documented = _fenced_fields("### 8.4")

    assert documented == ["SemanticCandidateSet", *SemanticCandidateSet.model_fields]


def test_authority_observation_fields_match_section_9_2() -> None:
    documented = _fenced_fields("### 9.2")

    assert documented == ["AuthorityObservation", *AuthorityObservation.model_fields]


def test_ontology_review_bundle_fields_match_section_9_3() -> None:
    section = _section("### 9.3")
    documented = [
        line.split()[0]
        for line in section.split("OntologyReviewBundle", 1)[1]
        .split("ApprovedSemanticVersion", 1)[0]
        .strip("`\\n ")
        .splitlines()
        if line.strip() and not line.startswith("```")
    ]

    assert documented == [*OntologyReviewBundle.model_fields]


def test_approved_semantic_version_fields_match_section_9_3() -> None:
    section = _section("### 9.3")
    documented = [
        line.split()[0]
        for line in section.split("ApprovedSemanticVersion", 1)[1]
        .split("### 9.4", 1)[0]
        .strip("`\\n ")
        .splitlines()
        if line.strip() and not line.startswith("```")
    ]

    assert documented == [*ApprovedSemanticVersion.model_fields]


def test_managed_integration_contract_fields_match_section_10() -> None:
    documented = _fenced_fields("## 10")

    assert documented == ["ManagedIntegrationContract", *ManagedIntegrationContract.model_fields]


def test_ontology_review_outcomes_match_section_9_3() -> None:
    section = _normalized_section("### 9.3")
    documented = set(re.findall(r"`(accept|reject|revise|merge|unresolved)`", section))

    assert documented == {member.value for member in ReviewItemDecision}
    assert set(OntologyReviewItem.model_fields["status"].annotation.__args__) == {
        "pending",
        "accepted",
        "rejected",
        "revised",
        "merged",
        "unresolved",
    }
    assert {member.value for member in DecisionKind} == {"approve", "reject", "request_changes"}


def test_catalog_transition_table_matches_section_9_1_2() -> None:
    documented = _documented_transitions("### 9.1.2")
    implemented = _implemented_transitions(CATALOG_TRANSITIONS)

    assert documented == implemented


def test_required_unresolved_meaning_is_fail_closed_to_no_valid_plan() -> None:
    review = _normalized_section("### 9.3")

    assert "required meaning remains unresolved" in review
    assert "must transition from `investigating` to `no_valid_plan`" in review
    assert "cannot progress to `proposed` or execution" in review


def test_catalog_and_semantic_artifacts_share_a_durable_envelope() -> None:
    envelope = _normalized_section("### 5.1")

    assert "canonical serialization" in envelope
    assert "digest-addressable" in envelope
    assert "rejects unknown input" in envelope
    assert "domain tag, tenant identifier, and repository-assigned sequence" in envelope
    assert "never from a clock" in envelope


def test_catalog_and_semantic_lookup_denies_cross_tenant_access_before_deserialization() -> None:
    envelope = _normalized_section("### 5.1")

    assert "tenant identity in its initial query" in envelope
    assert "before reading or deserializing the artifact payload" in envelope


def test_contradiction_groups_match_section_9_2_1() -> None:
    from heinzel_semantic_registry.authority import _CONTRADICTION_GROUPS

    documented = {
        frozenset(member.strip() for member in line.split(","))
        for line in _fenced_block("### 9.2.1").strip().splitlines()
        if line.strip()
    }
    implemented = {frozenset(kind.value for kind in group) for group in _CONTRADICTION_GROUPS}

    assert documented == implemented


def test_section_9_2_1_partitions_every_information_kind_exactly_once() -> None:
    documented = [
        member.strip()
        for line in _fenced_block("### 9.2.1").strip().splitlines()
        if line.strip()
        for member in line.split(",")
    ]

    # A kind missing from the groups would raise at resolution time; a kind named twice
    # would make the "contradicts" relation ambiguous.
    assert sorted(documented) == sorted(kind.value for kind in InformationKind)
    assert len(documented) == len(set(documented))


def test_governing_information_kind_matches_section_9_2_2() -> None:
    from heinzel_semantic_registry.authority import _CANDIDATE_GOVERNING_KIND

    documented = {
        candidate: information for candidate, information in _markdown_table_rows("### 9.2.2")
    }
    implemented = {
        candidate.value: information.value
        for candidate, information in _CANDIDATE_GOVERNING_KIND.items()
    }

    assert documented == implemented
