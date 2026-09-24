from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from .contracts import (
    AccessPreviewProposalView,
    AcquisitionReceiptsView,
    AcquisitionReceiptView,
    ActorRole,
    AuthorityRequirementView,
    AuthorityStatusView,
    CapabilityView,
    CatalogAssetView,
    ClarifiedOutcomeView,
    ConstraintView,
    ConversationMessageView,
    ConversationView,
    DashboardView,
    DataProductView,
    DatasetEvidenceView,
    Decision,
    DisplayReferenceView,
    EvidenceContextView,
    FreshnessState,
    InboxItemView,
    InboxView,
    LifecycleEventView,
    RequestDetailView,
    RequesterRequestView,
    RequestKind,
    RequestProposalView,
    RequestState,
    ReviewItemView,
    ReviewKind,
    ReviewSectionView,
    ReviewView,
    RunsView,
    RunView,
    SetupStage,
    SetupStageView,
    SetupView,
    StakeholderAnswerProposalView,
    WarehouseOptionView,
    WorkspaceView,
    setup_snapshot_digest,
)

FIXTURE_TENANT_ID = "tenant-primary"
FIXTURE_WORKSPACE_ID = "workspace-private-revenue"
INITIAL_RESET_TOKEN = "reset_token_fixture_sequence-0000"
BLOCKED_REQUEST_DIGEST = "b" * 64
STALE_REQUEST_DIGEST = "c" * 64
ANSWER_REQUEST_DIGEST = "d" * 64
ACCESS_REQUEST_DIGEST = "e" * 64
FIXED_TIME = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixtureSeed:
    workspace: WorkspaceView
    setup: SetupView
    reviews: Mapping[str, ReviewView]
    inbox: InboxView
    request_details: Mapping[str, RequestDetailView]
    requester_requests: tuple[RequesterRequestView, ...]
    clarified_outcomes: Mapping[str, ClarifiedOutcomeView]
    data_products: Mapping[str, DataProductView]
    runs: RunsView
    acquisition_receipts: AcquisitionReceiptsView
    catalog_assets: Mapping[str, CatalogAssetView]
    dashboards: Mapping[str, DashboardView]


def fixture_clock() -> datetime:
    return FIXED_TIME


def conversation_digest(
    request_id: str, revision: int, messages: Iterable[ConversationMessageView] = ()
) -> str:
    """Digest the exact thread a reply is written against.

    `ConversationMessageCommand` requires this value, so it must be derivable from
    what the read returns. It covers the identity, the revision, and every message
    id in order, which is what changes when someone else posts first.
    """
    identifiers = tuple(message.message_id for message in messages)
    payload = "\x00".join((request_id, str(revision), *identifiers))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _conversation(request_id: str, *, awaiting_role: str | None = None) -> ConversationView:
    messages = (
        ConversationMessageView(
            message_id=f"message-{request_id.removeprefix('request-')}",
            author_label="Riley Requester",
            author_role="requester",
            body="Please use the governed revenue definition.",
            created_at=FIXED_TIME,
        ),
    )
    return ConversationView.model_validate(
        {
            "request_id": request_id,
            "revision": 2,
            "conversation_digest": conversation_digest(request_id, 2, messages),
            "messages": messages,
            "awaiting_role": awaiting_role,
        }
    )


def _evidence_context(
    *, quality_summary: str, freshness: FreshnessState = "current"
) -> EvidenceContextView:
    return EvidenceContextView.model_validate(
        {
            "datasets": (
                DatasetEvidenceView(dataset_ref="dataset-orders", display_name="Synthetic orders"),
            ),
            "metric_versions": ("net-revenue-v1",),
            "as_of": FIXED_TIME,
            "freshness": freshness,
            "quality_summary": quality_summary,
            "lineage_summary": "Synthetic orders to the revenue data product.",
            "authorization_summary": "Fixture projection only; no governed evidence exists.",
            "evidence_refs": (),
        }
    )


def _review(
    review_id: str,
    kind: ReviewKind,
    title: str,
    digest_character: str,
    authority_role: ActorRole,
) -> ReviewView:
    constraints: tuple[ConstraintView, ...] = ()
    if kind == "data_product":
        constraints = (
            ConstraintView(
                code="no-valid-plan",
                summary="Activation cannot proceed without the required source authority.",
                responsible_role="data_owner",
                permitted_next_action="Obtain source authority and request a new plan.",
            ),
        )
    return ReviewView.model_validate(
        {
            "review_id": review_id,
            "kind": kind,
            "title": title,
            "summary": "Synthetic approval gate for the revenue-to-cash fixture.",
            "revision": 1,
            "reviewed_digest": digest_character * 64,
            "sections": (
                ReviewSectionView(
                    section_id=f"section-{kind.replace('_', '-')}",
                    title="Review summary",
                    items=(ReviewItemView(label="Scope", value="Revenue to cash"),),
                ),
            ),
            "required_authorities": (
                AuthorityRequirementView(
                    role=authority_role,
                    reason="Confirm the exact reviewed boundary for this approval gate.",
                    subject_digest=digest_character * 64,
                    satisfied=False,
                ),
            ),
            "constraints": constraints,
            "evidence_refs": (),
            "can_decide": True,
        }
    )


def _request_details() -> dict[str, RequestDetailView]:
    answer_proposal = StakeholderAnswerProposalView(
        kind="stakeholder_answer",
        purpose="Explain the weekly net revenue movement.",
        candidate="Synthetic net revenue increased after delayed invoices were recognized.",
        metric_version="net-revenue-v1",
        as_of=FIXED_TIME,
        freshness="current",
        quality_limitations=("Fixture values are synthetic and cannot support a real decision.",),
        datasets=(
            DatasetEvidenceView(dataset_ref="dataset-orders", display_name="Synthetic orders"),
        ),
        lineage_summary="Synthetic orders to net revenue.",
        authorization_summary="Architect approval remains required.",
        required_authorities=(
            # The demonstration issues one architect identity, so an authority no
            # identity here can hold would leave the required-roles list reading
            # `Not recorded` forever and admission honestly unreachable. The access
            # preview keeps a policy authority the architect cannot satisfy, which is
            # where the demonstration shows an approval it cannot record itself.
            AuthorityStatusView(
                role="data_architect",
                reason="Approve the stakeholder answer scope.",
                satisfied=False,
            ),
        ),
    )
    access_proposal = AccessPreviewProposalView(
        kind="access_preview",
        purpose="Investigate delayed invoice recognition.",
        data_product_ref="product-revenue",
        access_mode="query",
        requested_fields=("invoice_id", "recognized_at"),
        effective_scope=("invoice_id",),
        exclusions=("recognized_at",),
        expires_at=FIXED_TIME + timedelta(days=7),
        intended_checks=("Can query synthetic invoice identifiers.",),
        denied_checks=("Cannot query excluded recognition timestamps.",),
        authority_summary="Policy approval remains required.",
        required_authorities=(
            AuthorityStatusView(
                role="policy_approver",
                reason="Approve the effective access scope.",
                satisfied=False,
            ),
        ),
    )

    def detail(
        request_id: str,
        *,
        kind: RequestKind,
        state: RequestState,
        revision: int,
        digest: str | None,
        proposal: RequestProposalView | None,
        quality: str,
        actions: tuple[Decision, ...],
        freshness: FreshnessState = "current",
    ) -> RequestDetailView:
        return RequestDetailView.model_validate(
            {
                "request_id": request_id,
                "kind": kind,
                "state": state,
                "title": request_id.replace("request-", "").replace("-", " ").title(),
                "purpose": "Exercise a deterministic console lifecycle boundary.",
                "revision": revision,
                "proposal_digest": digest,
                "proposal": proposal,
                "conversation": _conversation(request_id, awaiting_role="requester"),
                "lifecycle": (
                    LifecycleEventView(
                        event_id=f"event-{request_id.removeprefix('request-')}",
                        state=state,
                        summary="Fixture lifecycle state established.",
                        occurred_at=FIXED_TIME,
                    ),
                ),
                "evidence": _evidence_context(
                    quality_summary=quality,
                    freshness=freshness,
                ),
                "available_actions": actions,
            }
        )

    return {
        "request-answer": detail(
            "request-answer",
            kind="stakeholder_question",
            state="proposed",
            revision=2,
            digest=ANSWER_REQUEST_DIGEST,
            proposal=answer_proposal,
            quality="Synthetic evidence context only.",
            actions=("approve", "reject", "request_changes"),
        ),
        "request-access": detail(
            "request-access",
            kind="data_access",
            state="awaiting_approval",
            revision=2,
            digest=ACCESS_REQUEST_DIGEST,
            proposal=access_proposal,
            quality="Synthetic access preview only.",
            actions=("approve", "reject", "request_changes"),
        ),
        "request-blocked-acceptance": detail(
            "request-blocked-acceptance",
            kind="stakeholder_question",
            state="clarifying",
            revision=2,
            digest=BLOCKED_REQUEST_DIGEST,
            proposal=answer_proposal,
            quality="Requester acceptance is required before architect action.",
            actions=(),
        ),
        "request-stale": detail(
            "request-stale",
            kind="data_access",
            state="awaiting_approval",
            revision=3,
            digest=STALE_REQUEST_DIGEST,
            proposal=access_proposal,
            quality="A newer synthetic proposal revision exists.",
            actions=("approve", "reject", "request_changes"),
            freshness="stale",
        ),
        "request-no-valid-plan": detail(
            "request-no-valid-plan",
            kind="data_access",
            state="investigating",
            revision=2,
            digest=None,
            proposal=None,
            quality="No Valid Plan",
            actions=(),
        ),
    }


def build_fixture_seed() -> FixtureSeed:
    request_details = _request_details()
    clarified_outcomes = {
        "request-blocked-acceptance": ClarifiedOutcomeView(
            request_id="request-blocked-acceptance",
            revision=2,
            statement_digest=BLOCKED_REQUEST_DIGEST,
            restated_request="Explain the weekly net revenue movement.",
            purpose="Support a synthetic stakeholder review.",
            in_scope_summary="Synthetic aggregate revenue only.",
            out_of_scope_summary="Customer and payment details.",
            accepted=False,
        )
    }
    inbox_items = tuple(
        InboxItemView(
            request_id=request_id,
            kind=detail.kind,
            state=detail.state,
            title=detail.title,
            purpose=detail.purpose,
            risk="medium" if detail.kind == "data_access" else "low",
            blocked_reason=(
                detail.evidence.quality_summary if detail.available_actions == () else None
            ),
        )
        for request_id, detail in request_details.items()
    )
    requester_requests = tuple(
        RequesterRequestView(
            request_id=request_id,
            kind=detail.kind,
            state=detail.state,
            title=detail.title,
            requested_outcome=detail.purpose,
            revision=detail.revision,
            updated_at=FIXED_TIME,
            clarified_outcome=clarified_outcomes.get(request_id),
            question=(
                "What changed in weekly net revenue?"
                if detail.kind == "stakeholder_question"
                else None
            ),
        )
        for request_id, detail in request_details.items()
    )
    setup_stages: tuple[SetupStage, ...] = (
        "foundation",
        "managed_services",
        "sources",
        "business_process",
        "meaning",
        "data_product",
        "activation",
    )
    setup_payload: dict[str, object] = {
        "workspace_ref": "workspace-revenue",
        "revision": 1,
        "reset_token": INITIAL_RESET_TOKEN,
        "active_stage": "foundation",
        "stages": tuple(
            SetupStageView(
                stage=stage,
                label=stage.replace("_", " ").title(),
                state="current" if stage == "foundation" else "not_started",
            )
            for stage in setup_stages
        ),
        "warehouse_options": (
            WarehouseOptionView(
                engine="postgresql",
                label="PostgreSQL",
                supported_region="us-west-2",
                fixed_capacity="fixed-small",
            ),
            WarehouseOptionView(
                engine="clickhouse",
                label="ClickHouse",
                supported_region="us-west-2",
                fixed_capacity="fixed-small",
            ),
        ),
        "pending_review_refs": (
            "review-meaning",
            "review-data-product",
            "review-activation",
        ),
    }
    unbound_setup = SetupView.model_validate(setup_payload | {"setup_digest": "0" * 64})
    setup = SetupView.model_validate(
        unbound_setup.model_dump(mode="python")
        | {"setup_digest": setup_snapshot_digest(unbound_setup)}
    )
    reviews = {
        "review-meaning": _review(
            "review-meaning", "meaning", "Meaning approval", "1", "data_architect"
        ),
        "review-data-product": _review(
            "review-data-product", "data_product", "Data product approval", "2", "data_owner"
        ),
        "review-activation": _review(
            "review-activation", "activation", "Activation approval", "3", "budget_approver"
        ),
    }
    workspace = WorkspaceView(
        workspace=DisplayReferenceView(ref="workspace-revenue", display_name="Revenue to cash"),
        state="setup",
        capabilities=(
            CapabilityView(
                capability_id="fixture-journey",
                label="Fixture journey",
                state="ready",
                detail="Synthetic lifecycle interactions are available.",
            ),
            CapabilityView(
                capability_id="governed-evidence",
                label="Governed evidence",
                state="not_delivered",
                detail="Fixture mode cannot issue authoritative evidence.",
                dependency="Governed evidence service wiring",
            ),
            CapabilityView(
                capability_id="downstream-activation",
                label="Downstream activation",
                state="not_delivered",
                detail="No real downstream system is connected in fixture mode.",
                dependency="Governed runtime wiring",
            ),
            CapabilityView(
                capability_id="data-access-intake",
                label="Data access requests",
                state="ready",
                detail="Synthetic data access requests can be submitted and reviewed.",
            ),
        ),
    )
    return FixtureSeed(
        workspace=workspace,
        setup=setup,
        reviews=MappingProxyType(reviews),
        inbox=InboxView(items=inbox_items, selected_request_id="request-answer"),
        request_details=MappingProxyType(request_details),
        requester_requests=requester_requests,
        clarified_outcomes=MappingProxyType(clarified_outcomes),
        data_products=MappingProxyType(
            {
                "product-revenue": DataProductView(
                    data_product_id="product-revenue",
                    version=1,
                    publication_status="published",
                    name="Current revenue by region",
                    description="Approved revenue grouped by region for finance reporting.",
                    product_revision=1,
                    generation=1,
                    catalog_revision=1,
                    namespace="analytics",
                    relation_name="revenue_by_region",
                    column_count=2,
                    source_count=1,
                    freshness_observed_at=FIXED_TIME,
                )
            }
        ),
        runs=RunsView(
            runs=(
                RunView(
                    run_id="run-synthetic",
                    contract_digest="b" * 64,
                    state="succeeded",
                    created_at=FIXED_TIME - timedelta(minutes=2),
                    updated_at=FIXED_TIME - timedelta(minutes=1),
                ),
            )
        ),
        acquisition_receipts=AcquisitionReceiptsView(
            receipts=(
                AcquisitionReceiptView(
                    evidence_id="evidence-ref:synthetic-prepared",
                    contract_ref="contract:synthetic-orders:v1",
                    source_binding_ref="source-binding:synthetic-orders",
                    acquisition_mode="snapshot",
                    logical_object_refs=("orders",),
                    outcome="prepared",
                    reason_codes=(),
                    created_at=FIXED_TIME - timedelta(minutes=4),
                ),
                # A refusal is seeded beside a success so the demo shows the shape an
                # operator actually has to act on, not only the happy path.
                AcquisitionReceiptView(
                    evidence_id="evidence-ref:synthetic-refused",
                    contract_ref="contract:synthetic-payments:v1",
                    source_binding_ref="source-binding:synthetic-payments",
                    acquisition_mode="incremental",
                    logical_object_refs=("payments",),
                    outcome="no_valid_plan",
                    reason_codes=("contract_not_activated",),
                    created_at=FIXED_TIME - timedelta(minutes=6),
                ),
            )
        ),
        catalog_assets=MappingProxyType(
            {
                "asset-revenue": CatalogAssetView(
                    asset_ref="asset-revenue",
                    display_name="Synthetic net revenue",
                    definition="Recognized invoice value in the fixture scenario.",
                    owner="Synthetic Finance Data",
                    classifications=("synthetic",),
                    lineage_summary="Synthetic orders to net revenue.",
                    link_ref="link-catalog-revenue",
                )
            }
        ),
        dashboards=MappingProxyType(
            {
                "dashboard-revenue": DashboardView(
                    dashboard_ref="dashboard-revenue",
                    display_name="Synthetic revenue overview",
                    version=1,
                    lifecycle_state="active",
                    as_of=FIXED_TIME,
                    freshness="current",
                    access_state="workspace_role",
                    published_at=FIXED_TIME,
                    state="not_delivered",
                    summary="The downstream dashboard is intentionally unavailable.",
                    preview_ref="preview-dashboard-revenue",
                    link_ref="link-dashboard-revenue",
                )
            }
        ),
    )
