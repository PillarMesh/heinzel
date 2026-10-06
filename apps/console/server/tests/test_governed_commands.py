"""Governed-local command delegation.

Every test here proves one of four properties: the exact domain identity the owning
service needs reaches it unchanged, the console never invents an owning transaction,
a transient failure never becomes a terminal verdict, and no private operation
identity leaves the handle repository.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never

import pytest
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_connection_broker import (
    PrivateSourceCapability,
    SourceAccountMode,
    SourceBindingBoundaryError,
    SourceBindingService,
    SourceBindingValidationEvidence,
    SourceCapabilityProbe,
    SourceConnectionBinding,
    SourceConnectionBindingState,
    SQLiteSourceBindingRepository,
)
from heinzel_console.auth import InFlightCommandKeys, TrustedActorContext
from heinzel_console.contracts import (
    AcquisitionRunNowCommand,
    ActorRole,
    BusinessProcessManifestCommand,
    ClarifiedOutcomeAcceptanceCommand,
    ConversationMessageCommand,
    CreateRequestCommand,
    DecisionCommand,
    ProcessPackageCommand,
    ProductIntentApprovalCommand,
    ProposalPreparationCommand,
    ResetCommand,
    RetryOperationCommand,
    SourceRegistrationCommand,
    WarehouseBindingCommand,
)
from heinzel_console.errors import (
    ConsoleConflict,
    ConsoleError,
    ConsoleInvalidRequest,
    ConsoleNotFound,
    ConsoleUnavailable,
)
from heinzel_console.governed_adapters import (
    BrokerSourceRegistrationCommands,
    EnrolledSourceConnection,
    GovernedWorkspaceIdentity,
    InMemoryWorkspacePrincipalDirectory,
    WarehouseConfirmation,
    WarehouseOperationIdentity,
)
from heinzel_console.governed_backend import (
    CAPABILITY_NOT_DELIVERED,
    GovernedConsoleBackend,
)
from heinzel_console.operation_handles import (
    CONSOLE_HANDLE_PATTERN,
    InMemoryOperationHandleRepository,
)
from heinzel_console.request_intake import request_intake_content
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import (
    BusinessProcessManifest,
    ProcessPackageService,
    SQLiteProcessPackageRepository,
)
from heinzel_evidence import AcquisitionEvidenceReceipt
from heinzel_provider_sdk import AcquisitionNoValidPlan
from heinzel_provider_sdk.errors import AcquisitionProviderError, AcquisitionProviderKind
from heinzel_request_management import (
    ArchitectRequestView,
    ClarifiedOutcomeStatement,
    DeliveryIntent,
    DimensionIntent,
    FreshnessObjective,
    FulfillmentApprovalBinding,
    FulfillmentProposal,
    Grain,
    InboxRequest,
    MeasureIntent,
    ProductIntent,
    ProductIntentApprovalService,
    ProductIntentAuthorityRefs,
    ProductIntentCandidate,
    ProductIntentConstraints,
    ProductIntentSourceCoverage,
    RequesterRequestView,
    RequestManagementService,
    RequestState,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)
from heinzel_request_management.fulfillment_models import (
    AccessScopePreview,
    ApprovalRequirement,
    DisclosureDenial,
)
from heinzel_request_management.requester_view import OwnDecisionView
from heinzel_runtime import AcquisitionOwnershipError, AcquisitionPreparationResult
from heinzel_semantic_registry import OntologyReviewBundle, OntologyReviewItem
from heinzel_semantic_registry.review import ReviewItemDecision
from heinzel_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehousePersistenceError,
)

_TENANT = "tenant-alpha"
_ARCHITECT = "actor-architect"
_REQUESTER = "actor-requester"
_REQUESTER_PRINCIPAL = "principal:requester:actor-requester"
_ARCHITECT_PRINCIPAL = "role:data_engineering_architect"
_FIXED_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_PRIVATE_OPERATION_ID = "wop-private-operation-identity"

_IDENTITY = GovernedWorkspaceIdentity(
    tenant_ref="tenant-alpha",
    tenant_display_name="Alpha",
    workspace_ref="workspace-alpha",
    workspace_display_name="Alpha workspace",
)


def _clock() -> datetime:
    return _FIXED_TIME


def _architect_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_ARCHITECT,
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-architect",
    )


def _requester_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )


def _principals() -> InMemoryWorkspacePrincipalDirectory:
    directory = InMemoryWorkspacePrincipalDirectory()
    directory.bind_principal(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        role="requester",
        principal_ref=_REQUESTER_PRINCIPAL,
    )
    directory.bind_principal(
        tenant_id=_TENANT,
        actor_id=_ARCHITECT,
        role="data_architect",
        principal_ref=_ARCHITECT_PRINCIPAL,
    )
    return directory


def _binding(
    state: WarehouseBindingState = WarehouseBindingState.READY, revision: int = 3
) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="whb-0123456789abcdef01234567",
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
        capability_profile_digest="a" * 64,
        lifecycle_state=state,
        revision=revision,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _RecordingWarehouseCommands:
    def __init__(self, confirmation: WarehouseConfirmation) -> None:
        self._confirmation = confirmation
        self.calls: list[dict[str, str]] = []

    def confirm_binding(
        self, *, tenant_id: str, engine: str, region: str, capacity: str
    ) -> WarehouseConfirmation:
        self.calls.append(
            {"tenant_id": tenant_id, "engine": engine, "region": region, "capacity": capacity}
        )
        return self._confirmation


class _FailingWarehouseCommands:
    def confirm_binding(self, *, tenant_id: str, engine: str, region: str, capacity: str) -> Never:
        raise WarehousePersistenceError("save warehouse binding")


class _StaticWarehouseBindingReader:
    def __init__(self, binding: WarehouseBinding | None) -> None:
        self._binding = binding

    def current_binding(self, tenant_id: str) -> WarehouseBinding | None:
        return self._binding if tenant_id == _TENANT else None


class _StaticCatalogBindingReader:
    def current_binding(self, tenant_id: str) -> CatalogBinding | None:
        if tenant_id != _TENANT:
            return None
        return CatalogBinding(
            binding_id="catalog-binding-alpha",
            tenant_id=_TENANT,
            capability_profile_digest="f" * 64,
            lifecycle_state=CatalogBindingState.READY,
            revision=3,
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
            provisioned_at=_FIXED_TIME,
        )


class _RecordingFulfillmentCommands:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
        subject_digest: str,
        decision: Literal["approve", "reject", "request_changes"],
        expected_revision: int,
    ) -> FulfillmentApprovalBinding:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "actor_id": actor_id,
                "authority_ref": authority_ref,
                "subject_digest": subject_digest,
                "decision": decision,
                "expected_revision": expected_revision,
            }
        )
        return FulfillmentApprovalBinding(
            approval_id="apr-0000000000000000000001-" + "b" * 24,
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision,
            proposal_id="prp-0000000000000000000001-" + "c" * 24,
            proposal_revision=1,
            proposal_digest="d" * 64,
            subject_digest=subject_digest,
            actor_id=actor_id,
            authority_ref=authority_ref,
            decision=decision,
            created_at=_FIXED_TIME,
        )


class _UnavailableFulfillmentCommands:
    def record_approval(self, **_: object) -> Never:
        raise OSError("fulfillment repository is unreachable")


def _statement(request: InboxRequest) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="cls-0000000000000000000001-" + "a" * 24,
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        restated_request="Report governed net revenue for the last closed quarter.",
        purpose_digest=digest(request.payload.purpose),
        in_scope_summary="Governed revenue datasets only.",
        out_of_scope_summary="No customer-level detail.",
        created_at=_FIXED_TIME,
    )


def _answer_draft() -> StakeholderAnswerDraft:
    return StakeholderAnswerDraft(
        answer_text="Net revenue was 12.4 million for the quarter.",
        governed_dataset_refs=(),
        metric_refs=(),
        as_of=_FIXED_TIME,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(),
        disclosure_classifications=(),
    )


def _proposal(request: InboxRequest, statement: ClarifiedOutcomeStatement) -> FulfillmentProposal:
    subject = _answer_draft()
    return FulfillmentProposal(
        proposal_id="prp-0000000000000000000001-" + "c" * 24,
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        revision=1,
        clarified_outcome_digest=digest(statement),
        grounding_snapshot_digest="e" * 64,
        policy_snapshot_digest="f" * 64,
        subject=subject,
        required_approvals=(
            ApprovalRequirement(
                authority_ref=_ARCHITECT_PRINCIPAL,
                reason_code="data_engineering_architect",
                subject_digest=digest(subject),
            ),
            ApprovalRequirement(
                authority_ref=_REQUESTER_PRINCIPAL,
                reason_code="clarified_outcome_acceptance",
                subject_digest=digest(statement),
            ),
        ),
        created_at=_FIXED_TIME,
    )


class _StaticFulfillmentViews:
    def __init__(self, *, request: InboxRequest, statement: ClarifiedOutcomeStatement) -> None:
        self._request = request
        self._statement = statement
        self.own_decisions: tuple[OwnDecisionView, ...] = ()
        self.proposal = _proposal(request, statement)
        self.approvals: tuple[FulfillmentApprovalBinding, ...] = ()

    def requester_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> RequesterRequestView:
        if request_id != self._request.request_id or actor_id != self._request.requester_id:
            raise KeyError(request_id)
        return RequesterRequestView(
            request_id=self._request.request_id,
            state=self._request.state,
            revision=self._request.revision,
            clarified_outcomes=(self._statement,),
            own_decisions=self.own_decisions,
            fulfillment_status="in_review",
            denial_explanation=None,
            no_valid_plan_explanation=None,
        )

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView:
        if request_id != self._request.request_id:
            raise KeyError(request_id)
        return ArchitectRequestView(
            request=self._request,
            clarified_outcomes=(self._statement,),
            proposals=(self.proposal,),
            approvals=self.approvals,
            admissions=(),
            dependencies=(),
            no_valid_plans=(),
            denials=(),
            evidence=(),
        )


class _RecordingSemanticReviewCommands:
    def __init__(self, bundle: OntologyReviewBundle) -> None:
        self._bundle = bundle
        self.calls: list[dict[str, object]] = []

    def decide_item(
        self,
        *,
        tenant_id: str,
        bundle_id: str,
        item_id: str,
        decision: ReviewItemDecision,
        actor_id: str,
        expected_revision: int,
        revised_content: str | None = None,
        merge_candidate_ids: tuple[str, ...] = (),
    ) -> OntologyReviewBundle:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "bundle_id": bundle_id,
                "item_id": item_id,
                "decision": decision,
                "actor_id": actor_id,
                "expected_revision": expected_revision,
                "revised_content": revised_content,
                "merge_candidate_ids": merge_candidate_ids,
            }
        )
        return self._bundle.model_copy(
            update={"revision": expected_revision + 1, "updated_at": _FIXED_TIME}
        )


class _StaticSemanticReviewReader:
    def __init__(self, bundle: OntologyReviewBundle) -> None:
        self._bundle = bundle

    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle:
        if bundle_id != self._bundle.bundle_id:
            raise KeyError(bundle_id)
        return self._bundle


def _review_bundle(pending_items: int = 1) -> OntologyReviewBundle:
    return OntologyReviewBundle(
        bundle_id="orb-0000000000000000000001-" + "a" * 24,
        tenant_id=_TENANT,
        revision=4,
        candidate_set_digest="1" * 64,
        authority_observation_digests=("2" * 64,),
        items=tuple(
            OntologyReviewItem(
                item_id=f"ori-{index}",
                candidate_ids=(f"cand-{index}",),
                authority_resolution_digest="3" * 64,
                required_authority_ref="role:data_owner",
                semantic_revision_digest="4" * 64,
                status="pending",
            )
            for index in range(pending_items)
        ),
        required_authority_refs=("role:data_owner",),
        status="open",
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _Stack:
    def __init__(self, directory: Path) -> None:
        self.repository = SQLiteRequestRepository.open(str(directory / "requests.sqlite3"))
        self.requests = RequestManagementService(self.repository, clock=_clock)
        self.handles = InMemoryOperationHandleRepository()
        self.principals = _principals()

    def close(self) -> None:
        self.repository.close()

    def backend(self, **overrides: object) -> GovernedConsoleBackend:
        arguments: dict[str, object] = {
            "identity": _IDENTITY,
            "operation_handles": self.handles,
            "principals": self.principals,
            "requests": self.requests,
            "request_commands": self.requests,
        }
        arguments.update(overrides)
        return GovernedConsoleBackend(**arguments)  # type: ignore[arg-type]


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[_Stack]:
    governed = _Stack(tmp_path)
    try:
        yield governed
    finally:
        governed.close()


def _question(stack: _Stack) -> InboxRequest:
    return stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Quarterly board reporting",
        question="What was net revenue last quarter?",
    )


def _product_intent(request: InboxRequest) -> ProductIntent:
    return ProductIntent(
        request_id=request.request_id,
        title="Quarterly net revenue",
        business_outcome="Give finance one governed quarterly view.",
        source_refs=("billing-postgresql",),
        grain=Grain(keys=("fiscal_quarter",)),
        measures=(MeasureIntent(metric_ref="net_revenue", aggregation="sum"),),
        dimensions=(DimensionIntent(dimension_ref="fiscal_quarter"),),
        filters=(),
        freshness=FreshnessObjective(maximum_age_seconds=86_400),
        delivery=DeliveryIntent(outputs=("table", "dashboard")),
    )


def _product_constraints() -> ProductIntentConstraints:
    return ProductIntentConstraints(
        approved_source_refs=("billing-postgresql",),
        approved_metric_refs=("net_revenue",),
        approved_dimension_refs=("fiscal_quarter",),
        minimum_source_interval_seconds=86_400,
    )


class _GrantingAuthority:
    """Stands in for governed authority: resolves the references to fixed constraints."""

    def resolve_constraints(
        self,
        *,
        tenant_id: str,
        authority_refs: ProductIntentAuthorityRefs,
        evaluated_at: datetime,
    ) -> ProductIntentConstraints | None:
        del tenant_id, authority_refs, evaluated_at
        return _product_constraints()


def _authority_refs() -> ProductIntentAuthorityRefs:
    return ProductIntentAuthorityRefs(
        semantic_version=ArtifactReference(artifact_id="semantic-1", version=1, digest="a" * 64),
        source_observations=(
            ArtifactReference(artifact_id="observation-1", version=1, digest="b" * 64),
        ),
    )


class _StaticProductIntentReviewReader:
    def __init__(self, candidate: ProductIntentCandidate) -> None:
        self._candidate = candidate

    def current_candidate(self, tenant_id: str, request_id: str) -> ProductIntentCandidate | None:
        candidate = self._candidate
        if candidate.tenant_id != tenant_id or candidate.request_id != request_id:
            return None
        return candidate


def test_typed_product_intent_approval_delegates_the_exact_reviewed_candidate(
    stack: _Stack,
) -> None:
    request = _question(stack)
    intent = _product_intent(request)
    constraints = _product_constraints()
    candidate = ProductIntentCandidate(
        candidate_id="pic-00000000000000000001-" + "a" * 24,
        tenant_id=_TENANT,
        request_id=request.request_id,
        request_revision=request.revision,
        intent=intent,
        constraints=constraints,
        source_coverage=(
            ProductIntentSourceCoverage(
                source_ref="billing-postgresql",
                covered_fields=("fiscal_quarter", "net_revenue"),
                authorized=True,
            ),
        ),
        unresolved_constraints=(),
        proposed_by="external-interpreter",
        proposed_at=_FIXED_TIME,
        authority_refs=_authority_refs(),
    )
    reader = _StaticProductIntentReviewReader(candidate)
    approvals = ProductIntentApprovalService(
        stack.repository, clock=_clock, authority=_GrantingAuthority()
    )
    backend = stack.backend(product_intent_reviews=reader, product_intent_commands=approvals)

    result = backend.approve_product_intent(
        _architect_context(),
        request.request_id,
        ProductIntentApprovalCommand(
            expected_revision=request.revision,
            reviewed_digest=digest(intent),
            active_role="data_architect",
        ),
    )

    assert result.intent_digest == digest(intent)
    assert result.intent_revision == 1
    recorded = approvals.list_for_request(_TENANT, request.request_id)[0]
    assert recorded.intent == intent
    assert recorded.authority_refs == _authority_refs()


def test_typed_product_intent_approval_refuses_unresolved_source_authority(
    stack: _Stack,
) -> None:
    request = _question(stack)
    intent = _product_intent(request)
    candidate = ProductIntentCandidate(
        candidate_id="pic-00000000000000000001-" + "a" * 24,
        tenant_id=_TENANT,
        request_id=request.request_id,
        request_revision=request.revision,
        intent=intent,
        constraints=_product_constraints(),
        source_coverage=(
            ProductIntentSourceCoverage(
                source_ref="billing-postgresql",
                covered_fields=("fiscal_quarter", "net_revenue"),
                authorized=False,
            ),
        ),
        unresolved_constraints=("Source authorization is required.",),
        proposed_by="external-interpreter",
        proposed_at=_FIXED_TIME,
    )
    approvals = ProductIntentApprovalService(
        stack.repository, clock=_clock, authority=_GrantingAuthority()
    )
    backend = stack.backend(
        product_intent_reviews=_StaticProductIntentReviewReader(candidate),
        product_intent_commands=approvals,
    )

    with pytest.raises(ConsoleConflict, match="Resolve every product intent requirement"):
        backend.approve_product_intent(
            _architect_context(),
            request.request_id,
            ProductIntentApprovalCommand(
                expected_revision=request.revision,
                reviewed_digest=digest(intent),
                active_role="data_architect",
            ),
        )

    assert approvals.list_for_request(_TENANT, request.request_id) == ()


def _conversation_digest(request_id: str, revision: int, message_ids: tuple[str, ...]) -> str:
    payload = "\x00".join((request_id, str(revision), *message_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_request_intake_delegates_the_trusted_tenant_and_actor_not_the_browser_payload(
    stack: _Stack,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Quarterly revenue",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            },
        }
    )

    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})
    created = stack.backend().create_request(_requester_context(), command)
    stored = stack.requests.get(_TENANT, created.request_id)

    assert stored.requester_id == _REQUESTER
    assert stored.tenant_id == _TENANT
    assert stored.state is RequestState.SUBMITTED
    assert stored.title == command.title
    assert created.title == command.title
    assert stack.backend().get_inbox(_architect_context()).items[0].title == command.title
    assert created.state == "submitted"


def test_data_access_intake_delegates_every_declared_scope_field(stack: _Stack) -> None:
    expires_at = _FIXED_TIME + timedelta(days=7)
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Revenue export",
            "request": {
                "kind": "data_access",
                "purpose": "Quarterly board reporting",
                "data_product_ref": "product-revenue",
                "requested_fields": ["order_total", "closed_at"],
                "access_mode": "export",
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        }
    )

    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})
    created = stack.backend().create_request(_requester_context(), command)
    stored = stack.requests.get(_TENANT, created.request_id)

    assert stored.title == command.title
    assert created.title == command.title
    assert stack.backend().get_inbox(_architect_context()).items[0].title == command.title
    assert stored.payload.request_type == "data_access"
    assert stored.payload.requested_fields == ("order_total", "closed_at")
    assert stored.payload.access_mode == "export"
    assert stored.payload.expires_at == expires_at


def test_request_intake_refuses_a_command_role_the_trusted_context_does_not_hold(
    stack: _Stack,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Quarterly revenue",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            },
        }
    )

    with pytest.raises(ConsoleNotFound):
        stack.backend().create_request(_architect_context(), command)


def test_conversation_projection_publishes_the_digest_a_reply_must_be_written_against(
    stack: _Stack,
) -> None:
    request = _question(stack)

    conversation = stack.backend().get_conversation(_requester_context(), request.request_id)

    assert conversation.request_id == request.request_id
    assert conversation.revision == request.revision
    assert conversation.messages == ()
    assert conversation.conversation_digest == _conversation_digest(request.request_id, 1, ())


def test_conversation_reply_delegates_the_exact_expected_revision_to_request_management(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    conversation = backend.get_conversation(_requester_context(), request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision,
        conversation_digest=conversation.conversation_digest,
        active_role="requester",
        body="Please use the governed revenue definition.",
    )

    updated = backend.append_conversation_message(_requester_context(), request.request_id, command)
    entries = stack.requests.list_conversation(_TENANT, request.request_id)

    assert len(entries) == 1
    assert entries[0].body == "Please use the governed revenue definition."
    assert updated.revision == request.revision + 1
    assert updated.conversation_digest == _conversation_digest(
        request.request_id, request.revision + 1, (entries[0].entry_id,)
    )


def test_a_stale_conversation_digest_is_a_conflict_and_never_reaches_the_owning_service(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    command = ConversationMessageCommand(
        expected_revision=request.revision,
        conversation_digest="9" * 64,
        active_role="requester",
        body="Reply written against a thread that no longer exists.",
    )

    with pytest.raises(ConsoleConflict) as failure:
        backend.append_conversation_message(_requester_context(), request.request_id, command)

    assert failure.value.recovery_action == "reload"
    assert stack.requests.list_conversation(_TENANT, request.request_id) == ()


def test_a_stale_conversation_revision_is_a_conflict_rather_than_a_denial(stack: _Stack) -> None:
    request = _question(stack)
    backend = stack.backend()
    conversation = backend.get_conversation(_requester_context(), request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision + 5,
        conversation_digest=conversation.conversation_digest,
        active_role="requester",
        body="Reply written against a future revision.",
    )

    with pytest.raises(ConsoleConflict):
        backend.append_conversation_message(_requester_context(), request.request_id, command)


def test_a_sequential_replay_under_one_idempotency_key_still_reaches_the_owning_service(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    keys = InFlightCommandKeys()
    context = _requester_context()

    for _ in range(2):
        conversation = backend.get_conversation(context, request.request_id)
        command = ConversationMessageCommand(
            expected_revision=conversation.revision,
            conversation_digest=conversation.conversation_digest,
            active_role="requester",
            body="Repeated submission under one key.",
        )
        with keys.serialize(context, "idempotency-key-repeated"):
            backend.append_conversation_message(context, request.request_id, command)

    assert len(stack.requests.list_conversation(_TENANT, request.request_id)) == 2


def test_one_idempotency_key_serializes_concurrent_submissions_of_the_same_identity() -> None:
    keys = InFlightCommandKeys()
    context = _requester_context()
    overlaps: list[int] = []
    active = 0
    guard = threading.Lock()
    started = threading.Barrier(2)

    def submit() -> None:
        nonlocal active
        started.wait()
        with keys.serialize(context, "idempotency-key-concurrent"):
            with guard:
                active += 1
                overlaps.append(active)
            with guard:
                active -= 1

    workers = [threading.Thread(target=submit) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert overlaps == [1, 1]


def test_warehouse_confirmation_delegates_the_declared_engine_region_and_capacity(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=3,
            lifecycle_state=WarehouseBindingState.READY,
            operation_identity=None,
            failure_classification=None,
        )
    )
    backend = stack.backend(
        warehouse_commands=commands,
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
    )
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "engine": "postgresql",
            "region": "us-west-2",
            "capacity": "mvp-fixed",
        }
    ]
    assert operation.state == "succeeded"
    assert CONSOLE_HANDLE_PATTERN.fullmatch(operation.operation_id) is not None


def test_a_confirmed_warehouse_operation_never_serializes_its_private_identity(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.VALIDATING,
            operation_identity=WarehouseOperationIdentity(
                binding_id="whb-0123456789abcdef01234567",
                operation_id=_PRIVATE_OPERATION_ID,
            ),
            failure_classification=None,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)
    serialized = operation.model_dump_json()
    record = stack.handles.load(tenant_id=_TENANT, console_handle=operation.operation_id)

    assert _PRIVATE_OPERATION_ID not in serialized
    assert "whb-0123456789abcdef01234567" not in serialized
    assert record is not None
    assert _PRIVATE_OPERATION_ID in record.private_identity


def test_a_transient_warehouse_failure_never_becomes_a_terminal_denial(stack: _Stack) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.PROVISIONING,
            operation_identity=WarehouseOperationIdentity(
                binding_id="whb-0123456789abcdef01234567",
                operation_id=_PRIVATE_OPERATION_ID,
            ),
            failure_classification=WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert operation.state != "failed"
    assert operation.failure is not None
    assert operation.failure.classification == "transient"


def test_an_ambiguous_warehouse_outcome_stays_distinct_from_success_and_failure(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.PROVISIONING,
            operation_identity=None,
            failure_classification=WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert operation.state == "outcome_unknown"
    assert operation.failure is not None
    assert operation.failure.classification == "unknown"


def test_an_unavailable_warehouse_repository_reports_retry_rather_than_a_verdict(
    stack: _Stack,
) -> None:
    backend = stack.backend(warehouse_commands=_FailingWarehouseCommands())
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.confirm_warehouse_binding(_architect_context(), command)

    assert failure.value.code == "downstream_unavailable"
    assert failure.value.recovery_action == "retry"


def test_clarified_outcome_acceptance_delegates_the_requester_principal_and_exact_digest(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest=digest(statement),
        active_role="requester",
        decision="approve",
    )

    backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "request_id": request.request_id,
            "actor_id": _REQUESTER,
            "authority_ref": _REQUESTER_PRINCIPAL,
            "subject_digest": digest(statement),
            "decision": "approve",
            "expected_revision": request.revision,
        }
    ]


def test_a_stale_clarified_outcome_digest_never_reaches_the_fulfillment_service(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest="9" * 64,
        active_role="requester",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert commands.calls == []


def test_an_unavailable_fulfillment_repository_never_becomes_a_non_conformance_verdict(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    backend = stack.backend(
        fulfillment=_StaticFulfillmentViews(request=request, statement=statement),
        fulfillment_commands=_UnavailableFulfillmentCommands(),
    )
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest=digest(statement),
        active_role="requester",
        decision="approve",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert failure.value.recovery_action == "retry"


def test_an_architect_decision_delegates_the_architect_authority_and_reviewed_subject(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    subject_digest = digest(_answer_draft())
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = DecisionCommand(
        expected_revision=request.revision,
        reviewed_digest=subject_digest,
        active_role="data_architect",
        decision="approve",
    )

    detail = backend.decide_request(_architect_context(), request.request_id, command)

    assert commands.calls[0]["authority_ref"] == _ARCHITECT_PRINCIPAL
    assert commands.calls[0]["subject_digest"] == subject_digest
    assert commands.calls[0]["expected_revision"] == request.revision
    assert detail.request_id == request.request_id


def test_an_architect_decision_against_an_unheld_subject_never_reaches_the_service(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = DecisionCommand(
        expected_revision=request.revision,
        reviewed_digest="9" * 64,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.decide_request(_architect_context(), request.request_id, command)

    assert commands.calls == []


def test_a_semantic_review_decision_delegates_the_bundles_exact_revision(stack: _Stack) -> None:
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="approve",
    )

    review = backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "bundle_id": bundle.bundle_id,
            "item_id": "ori-0",
            "decision": ReviewItemDecision.ACCEPT,
            "actor_id": _ARCHITECT,
            "expected_revision": bundle.revision,
            "revised_content": None,
            "merge_candidate_ids": (),
        }
    ]
    assert review.revision == bundle.revision + 1


def test_a_semantic_review_decision_against_a_stale_digest_never_reaches_the_service(
    stack: _Stack,
) -> None:
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest="9" * 64,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_a_multi_item_review_cannot_be_decided_by_a_command_that_names_no_item(
    stack: _Stack,
) -> None:
    bundle = _review_bundle(pending_items=2)
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_a_review_change_request_without_replacement_wording_is_refused(
    stack: _Stack,
) -> None:
    """The owning revise decision requires wording; the command may carry it now.

    This once read "because no revision content can be carried", which stopped being
    true when `DecisionCommand` gained the field. What remains true is that a change
    request arriving without wording has nothing to revise with.
    """
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="request_changes",
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_the_process_package_command_stays_undelivered_with_its_dependency_named(
    stack: _Stack,
) -> None:
    narrative = "# Revenue to cash\n"
    command = ProcessPackageCommand(
        expected_revision=1,
        package_digest=hashlib.sha256(narrative.encode()).hexdigest(),
        active_role="data_architect",
        file_name="revenue-to-cash.md",
        media_type="text/markdown; charset=utf-8",
        narrative_markdown=narrative,
        manifest=BusinessProcessManifestCommand(
            process_name="Revenue to cash",
            owner="Finance operations",
            participants=(),
            outcomes=(),
            entities=(),
            events=(),
            states=(),
            rules=(),
            source_references=(),
            unresolved_questions=(),
        ),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        stack.backend().submit_process_package(_architect_context(), command)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_process_package_delegates_exact_markdown_and_strict_manifest(stack: _Stack) -> None:
    repository = SQLiteProcessPackageRepository(":memory:")
    packages = ProcessPackageService(repository, clock=_clock)
    narrative = "# Revenue to cash\n\nInvoice settlement closes the process.\n"
    command = ProcessPackageCommand(
        expected_revision=1,
        package_digest=hashlib.sha256(narrative.encode()).hexdigest(),
        active_role="data_architect",
        file_name="revenue-to-cash.md",
        media_type="text/markdown; charset=utf-8",
        narrative_markdown=narrative,
        manifest=BusinessProcessManifestCommand(
            process_name="Revenue to cash",
            owner="Finance operations",
            participants=("Billing", "Finance"),
            outcomes=("Settled invoice",),
            entities=("Invoice",),
            events=("Invoice settled",),
            states=("settled",),
            rules=("Only settled invoices close",),
            source_references=("billing-postgresql",),
            unresolved_questions=(),
        ),
    )

    result = stack.backend(
        process_package_commands=packages,
        warehouse_bindings=_StaticWarehouseBindingReader(_binding(revision=1)),
    ).submit_process_package(_architect_context(), command)

    assert result.state == "succeeded"
    latest = packages.latest(_TENANT)
    assert latest is not None
    assert packages.get_original(_TENANT, latest.receipt.package_id, 1) == narrative.encode()
    assert result.summary == "Business process package saved."
    assert latest.receipt.package_id not in result.summary
    repository.close()


def test_governed_setup_exposes_process_stage_and_projects_owning_receipt(
    stack: _Stack,
) -> None:
    repository = SQLiteProcessPackageRepository(":memory:")
    packages = ProcessPackageService(repository, clock=_clock)
    backend = stack.backend(
        process_package_commands=packages,
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_StaticCatalogBindingReader(),
    )

    before = backend.get_setup(_architect_context())

    assert before.active_stage == "business_process"
    assert next(item for item in before.stages if item.stage == "sources").state == "blocked"
    assert (
        next(item for item in before.stages if item.stage == "business_process").state == "current"
    )
    packages.upload(
        _TENANT,
        b"# Revenue to cash\n",
        "text/markdown; charset=utf-8",
        BusinessProcessManifest(
            process_name="Revenue to cash",
            owner="Finance operations",
            participants=(),
            outcomes=(),
            entities=(),
            events=(),
            states=(),
            rules=(),
            source_references=(),
            unresolved_questions=(),
        ),
        _ARCHITECT,
    )

    after = backend.get_setup(_architect_context())

    assert after.process_package is not None
    assert after.process_package.candidate_summary == "Revenue to cash"
    assert after.process_package.version == 1
    assert (
        next(item for item in after.stages if item.stage == "business_process").state == "complete"
    )
    repository.close()


def test_operation_retry_and_demo_reset_stay_undelivered_in_governed_mode(stack: _Stack) -> None:
    backend = stack.backend()
    retry = RetryOperationCommand(
        expected_revision=1,
        operation_digest="0" * 64,
        retry_token="r" * 40,
        active_role="data_architect",
    )
    reset = ResetCommand(
        expected_revision=1,
        setup_digest="0" * 64,
        reset_token="t" * 40,
        active_role="data_architect",
    )

    with pytest.raises(ConsoleUnavailable) as retry_failure:
        backend.retry_operation(_architect_context(), "op_" + "0" * 32, retry)
    with pytest.raises(ConsoleUnavailable) as reset_failure:
        backend.reset(_architect_context(), reset)

    assert retry_failure.value.code == CAPABILITY_NOT_DELIVERED
    assert reset_failure.value.code == CAPABILITY_NOT_DELIVERED


def test_a_command_never_runs_without_the_owning_service_being_wired(stack: _Stack) -> None:
    backend = stack.backend(warehouse_commands=None)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.confirm_warehouse_binding(_architect_context(), command)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_another_tenants_request_is_not_readable_as_a_conversation(stack: _Stack) -> None:
    request = stack.requests.submit_question(
        tenant_id="tenant-other",
        requester_id=_REQUESTER,
        purpose="Other tenant purpose",
        question="Other tenant question?",
    )

    with pytest.raises(ConsoleNotFound):
        stack.backend().get_conversation(_requester_context(), request.request_id)


def test_a_named_item_decides_that_item_of_a_multi_item_review(stack: _Stack) -> None:
    """`decide_item` has always decided one item; the command can now say which.

    A bundle with several undecided items was refused outright, so an architect could
    not decide any of them through the console until every other item happened to be
    resolved elsewhere.
    """
    bundle = _review_bundle(pending_items=3)
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    backend.decide_review(
        _architect_context(),
        bundle.bundle_id,
        DecisionCommand(
            expected_revision=bundle.revision,
            reviewed_digest=bundle.candidate_set_digest,
            active_role="data_architect",
            decision="approve",
            review_item_id="ori-1",
        ),
    )

    assert [call["item_id"] for call in commands.calls] == ["ori-1"]


def test_a_change_request_delegates_the_replacement_wording(stack: _Stack) -> None:
    """`request_changes` reaches the owning revise decision now, carrying its wording.

    The console previously refused it and could not substitute `unresolved`, which
    drives the bundle to `no_valid_plan` -- a far stronger verdict than asking for a
    change.
    """
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    backend.decide_review(
        _architect_context(),
        bundle.bundle_id,
        DecisionCommand(
            expected_revision=bundle.revision,
            reviewed_digest=bundle.candidate_set_digest,
            active_role="data_architect",
            decision="request_changes",
            revised_content="net revenue excludes refunds",
        ),
    )

    assert commands.calls[0]["decision"] is ReviewItemDecision.REVISE
    assert commands.calls[0]["revised_content"] == "net revenue excludes refunds"


@pytest.mark.parametrize("named", ("ori-9", "ori-0"))
def test_an_item_that_is_not_awaiting_a_decision_is_refused(stack: _Stack, named: str) -> None:
    """Naming an absent item, or one already decided, is refused rather than resolved.

    Falling back to "the only pending one" for an unrecognised name would apply the
    decision to an item the architect did not read.
    """
    bundle = _review_bundle(pending_items=2).model_copy(
        update={
            "items": (
                _review_bundle(pending_items=2).items[0].model_copy(update={"status": "accepted"}),
                _review_bundle(pending_items=2).items[1],
            )
        }
    )
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(
            _architect_context(),
            bundle.bundle_id,
            DecisionCommand(
                expected_revision=bundle.revision,
                reviewed_digest=bundle.candidate_set_digest,
                active_role="data_architect",
                decision="approve",
                review_item_id=named,
            ),
        )

    assert commands.calls == []


def test_replacement_wording_on_an_approval_is_refused(stack: _Stack) -> None:
    """Wording only means something for a revision, so accepting it elsewhere invents one."""
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(
            _architect_context(),
            bundle.bundle_id,
            DecisionCommand(
                expected_revision=bundle.revision,
                reviewed_digest=bundle.candidate_set_digest,
                active_role="data_architect",
                decision="approve",
                revised_content="wording that approves nothing",
            ),
        )

    assert commands.calls == []


@pytest.mark.parametrize("kind", ["stakeholder_question", "data_access"])
def test_request_intake_rejects_a_mismatched_digest_without_writing(
    stack: _Stack, kind: str
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Quarterly revenue",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            }
            if kind == "stakeholder_question"
            else {
                "kind": "data_access",
                "purpose": "Reporting",
                "data_product_ref": "product-revenue",
                "requested_fields": ["total"],
                "access_mode": "export",
                "expires_at": (_FIXED_TIME + timedelta(days=7)).isoformat(),
            },
        }
    )

    with pytest.raises(ConsoleInvalidRequest) as failure:
        stack.backend().create_request(_requester_context(), command)

    assert failure.value.code == "request_digest_mismatch"
    assert failure.value.field == "request_digest"
    assert stack.requests.list_inbox(_TENANT) == ()


@pytest.mark.parametrize("role", ["requester", "data_architect", "data_owner", "policy_approver"])
def test_conversation_roles_are_recorded_from_trusted_context_and_projected_verbatim(
    stack: _Stack,
    role: ActorRole,
) -> None:
    request = _question(stack)
    context = TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        roles=("requester", "data_architect", "data_owner", "policy_approver"),
        active_role=role,
        session_id="session-multiple-roles",
    )
    backend = stack.backend()
    conversation = backend.get_conversation(context, request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision,
        conversation_digest=conversation.conversation_digest,
        active_role=context.active_role,
        body="A recorded contribution.",
    )

    updated = backend.append_conversation_message(context, request.request_id, command)
    entry = stack.requests.list_conversation(_TENANT, request.request_id)[0]

    assert entry.author_role == context.active_role
    assert updated.messages[0].author_role == entry.author_role
    reread = backend.get_conversation(_requester_context(), request.request_id)
    assert reread.messages[0].author_role == role


def test_legacy_conversation_roles_are_not_inferred_from_actor_identity(stack: _Stack) -> None:
    request = _question(stack)
    stack.requests.append_conversation(
        _TENANT, request.request_id, _REQUESTER, "Legacy reply.", expected_revision=1
    )

    conversation = stack.backend().get_conversation(_requester_context(), request.request_id)

    assert conversation.messages[0].author_role is None


def test_a_claimed_conversation_role_cannot_override_the_active_trusted_role(stack: _Stack) -> None:
    request = _question(stack)
    context = TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        roles=("requester", "data_architect"),
        active_role="requester",
        session_id="session-multiple-roles",
    )
    backend = stack.backend()
    conversation = backend.get_conversation(context, request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision,
        conversation_digest=conversation.conversation_digest,
        active_role="data_architect",
        body="Claiming an inactive role.",
    )

    with pytest.raises(ConsoleNotFound):
        backend.append_conversation_message(context, request.request_id, command)

    assert stack.requests.list_conversation(_TENANT, request.request_id) == ()
    assert stack.requests.get(_TENANT, request.request_id).revision == request.revision


def test_governed_answer_is_reviewable_with_exact_artifact_identity(stack: _Stack) -> None:
    request = _question(stack)
    views = _StaticFulfillmentViews(request=request, statement=_statement(request))
    backend = stack.backend(fulfillment=views)

    detail = backend.get_request_detail(_architect_context(), request.request_id)

    assert detail.proposal is not None
    assert detail.proposal.kind == "stakeholder_answer"
    assert detail.proposal.candidate == _answer_draft().answer_text


def test_artifact_ids_are_preserved_without_becoming_catalog_lookup_keys(stack: _Stack) -> None:
    request = _question(stack)
    views = _StaticFulfillmentViews(request=request, statement=_statement(request))
    reference = ArtifactReference(
        artifact_id="urn:catalog/Revenue <Q1>?version=two", version=2, digest="a" * 64
    )
    answer = StakeholderAnswerDraft.model_validate(
        {
            **_answer_draft().model_dump(),
            "governed_dataset_refs": [reference],
            "metric_refs": [reference],
            "material_quality_limitations": [reference],
            "lineage_refs": [reference],
        }
    )
    views.proposal = FulfillmentProposal.model_validate(
        {
            **views.proposal.model_dump(),
            "subject": answer,
            "required_approvals": [
                ApprovalRequirement(
                    authority_ref=_ARCHITECT_PRINCIPAL,
                    reason_code="data_engineering_architect",
                    subject_digest=digest(answer),
                ),
                views.proposal.required_approvals[1],
            ],
        }
    )

    detail = stack.backend(fulfillment=views).get_request_detail(
        _architect_context(), request.request_id
    )

    assert detail.proposal is not None and detail.proposal.kind == "stakeholder_answer"
    dataset = detail.evidence.datasets[0]
    assert dataset.artifact_reference is not None
    assert dataset.artifact_reference.model_dump() == reference.model_dump()
    assert dataset.dataset_ref == f"artifact-{digest(reference)}"
    assert detail.proposal.datasets == detail.evidence.datasets
    assert detail.proposal.metric_references[0].model_dump() == reference.model_dump()
    assert detail.proposal.quality_references[0].model_dump() == reference.model_dump()
    assert detail.proposal.lineage_references[0].model_dump() == reference.model_dump()


@pytest.mark.parametrize("kind", ["access_preview", "disclosure_denial"])
def test_non_answer_proposals_keep_the_owning_scope_or_denial(stack: _Stack, kind: str) -> None:
    request = _question(stack)
    views = _StaticFulfillmentViews(request=request, statement=_statement(request))
    reference = ArtifactReference(artifact_id="urn:product/Revenue", version=3, digest="b" * 64)
    subject = (
        AccessScopePreview(
            requester_principal_ref=_REQUESTER_PRINCIPAL,
            data_product_ref=reference,
            access_mode="dashboard",
            requested_fields=("total", "email"),
            effective_object_refs=(reference,),
            effective_fields=("total",),
            excluded_scopes=("email",),
            classifications=(),
            expires_at=_FIXED_TIME,
        )
        if kind == "access_preview"
        else DisclosureDenial(
            reason_code="purpose_not_allowed",
            requester_safe_explanation="This purpose is outside approved scope.",
            denied_scope_digest="c" * 64,
        )
    )
    views.proposal = FulfillmentProposal.model_validate(
        {
            **views.proposal.model_dump(),
            "subject": subject,
            "required_approvals": [
                ApprovalRequirement(
                    authority_ref=_ARCHITECT_PRINCIPAL,
                    reason_code="data_engineering_architect",
                    subject_digest=digest(subject),
                )
            ],
        }
    )

    detail = stack.backend(fulfillment=views).get_request_detail(
        _architect_context(), request.request_id
    )

    assert detail.proposal is not None
    assert detail.proposal.kind == kind
    if detail.proposal.kind == "access_preview":
        assert detail.proposal.data_product_reference is not None
        assert detail.proposal.data_product_reference.model_dump() == reference.model_dump()
        assert detail.proposal.effective_scope == ("total",)
        assert detail.proposal.exclusions == ("email",)
        assert detail.proposal.intended_checks == ()
    else:
        assert detail.proposal.kind == "disclosure_denial"
        assert detail.proposal.explanation == "This purpose is outside approved scope."


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other-tenant"),
        ("request_id", "other-request"),
        ("request_revision", 2),
        ("proposal_id", "other-proposal"),
        ("proposal_revision", 2),
        ("proposal_digest", "0" * 64),
        ("subject_digest", "0" * 64),
        ("authority_ref", "other-authority"),
        ("decision", "reject"),
        ("duplicate", True),
        ("valid", True),
    ],
)
def test_only_one_exact_approval_is_shown_as_recorded(
    stack: _Stack, field: str, value: object
) -> None:
    request = _question(stack)
    views = _StaticFulfillmentViews(request=request, statement=_statement(request))
    approval = FulfillmentApprovalBinding(
        approval_id="approval-test",
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        proposal_id=views.proposal.proposal_id,
        proposal_revision=views.proposal.revision,
        proposal_digest=digest(views.proposal),
        subject_digest=digest(views.proposal.subject),
        actor_id=_ARCHITECT,
        authority_ref=_ARCHITECT_PRINCIPAL,
        decision="approve",
        created_at=_FIXED_TIME,
    )
    if field not in ("valid", "duplicate"):
        approval = FulfillmentApprovalBinding.model_validate(
            {**approval.model_dump(), field: value}
        )
    views.approvals = (approval, approval) if field == "duplicate" else (approval,)

    detail = stack.backend(fulfillment=views).get_request_detail(
        _architect_context(), request.request_id
    )

    assert detail.proposal is not None
    assert detail.proposal.required_approvals[0].satisfied is (field == "valid")


def test_preparation_without_an_owning_adapter_is_explicitly_unavailable(stack: _Stack) -> None:
    backend = stack.backend()
    with pytest.raises(ConsoleUnavailable) as failure:
        backend.prepare_request_proposal(
            _architect_context(),
            "request-new",
            ProposalPreparationCommand(expected_revision=1, active_role="data_architect"),
        )

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


class _RecordingAccessPreparation:
    def __init__(self, outcome: FulfillmentProposal) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def propose_access(self, **values: object) -> FulfillmentProposal:
        self.calls.append(values)
        return self.outcome


def test_data_access_preparation_delegates_to_the_access_proposal_boundary(
    stack: _Stack,
) -> None:
    submitted = stack.requests.submit_access_request(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Review governed revenue",
        data_product_id="product:revenue",
        requested_fields=("region", "revenue"),
        access_mode="query",
        expires_at=_FIXED_TIME + timedelta(days=1),
    )
    clarifying = stack.requests.transition(
        _TENANT,
        submitted.request_id,
        RequestState.CLARIFYING,
        actor_id=_ARCHITECT,
        expected_revision=submitted.revision,
    )
    investigating = stack.requests.transition(
        _TENANT,
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id=_ARCHITECT,
        expected_revision=clarifying.revision,
    )
    views = _StaticFulfillmentViews(
        request=investigating,
        statement=_statement(investigating),
    )
    commands = _RecordingAccessPreparation(views.proposal)
    backend = stack.backend(
        fulfillment=views,
        fulfillment_preparation_commands=commands,
    )
    detail = backend.get_request_detail(_architect_context(), investigating.request_id)
    assert detail.preparation_actions == ("prepare_access",)

    backend.prepare_request_proposal(
        _architect_context(),
        investigating.request_id,
        ProposalPreparationCommand(
            expected_revision=investigating.revision,
            active_role="data_architect",
        ),
    )

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "request_id": investigating.request_id,
            "actor_id": _ARCHITECT,
            "expected_revision": investigating.revision,
        }
    ]


class _RecordingAcquisitionCommands:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_now(self, **values: object) -> AcquisitionPreparationResult:
        self.calls.append(values)
        evidence = AcquisitionEvidenceReceipt(
            evidence_id="evidence:acquisition:orders",
            tenant_id=str(values["tenant_id"]),
            run_intent_ref="a" * 64,
            contract_ref=str(values["contract_ref"]),
            source_binding_ref="source-binding:orders",
            acquisition_mode="snapshot",
            logical_object_refs=("orders",),
            prepared_receipt_ref=None,
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=0,
            resulting_checkpoint_revision=None,
            reason_codes=("acquisition_mode_not_admitted",),
            outcome="no_valid_plan",
            created_at=_FIXED_TIME,
        )
        return AcquisitionPreparationResult(
            evidence=evidence,
            prepared_receipt=None,
            batch_manifest=None,
            governed_outcome=AcquisitionNoValidPlan(
                reason_codes=("acquisition_mode_not_admitted",),
                failed_constraints=("contract.acquisition_modes",),
            ),
        )


def test_run_now_delegates_only_the_selected_contract_and_trigger_to_acquisition(
    stack: _Stack,
) -> None:
    commands = _RecordingAcquisitionCommands()
    backend = stack.backend(acquisition_commands=commands)
    command = AcquisitionRunNowCommand(
        active_role="data_architect",
        contract_ref="contract:orders:v4",
        trigger_window="2026-09-01T12:00:00Z/2026-09-01T13:00:00Z",
        acquisition_mode="snapshot",
    )

    first = backend.run_acquisition_now(_architect_context(), command)
    replay = backend.run_acquisition_now(_architect_context(), command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "contract_ref": "contract:orders:v4",
            "trigger_window": "2026-09-01T12:00:00Z/2026-09-01T13:00:00Z",
            "acquisition_mode": "snapshot",
        },
        {
            "tenant_id": _TENANT,
            "contract_ref": "contract:orders:v4",
            "trigger_window": "2026-09-01T12:00:00Z/2026-09-01T13:00:00Z",
            "acquisition_mode": "snapshot",
        },
    ]
    assert first == replay
    assert first.outcome == "no_valid_plan"
    serialized = first.model_dump_json()
    assert "run_intent_ref" not in serialized
    assert "prepared_receipt_ref" not in serialized
    assert "checkpoint" not in serialized


def test_run_now_requires_an_owning_acquisition_application(stack: _Stack) -> None:
    command = AcquisitionRunNowCommand(
        active_role="data_architect",
        contract_ref="contract:orders:v4",
        trigger_window="2026-09-01T12:00:00Z/2026-09-01T13:00:00Z",
        acquisition_mode="snapshot",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        stack.backend().run_acquisition_now(_architect_context(), command)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_composed_acquisition_application_marks_source_acquisition_ready(stack: _Stack) -> None:
    capabilities = (
        stack.backend(acquisition_commands=_RecordingAcquisitionCommands())
        .get_workspace(_architect_context())
        .capabilities
    )

    source_acquisition = next(
        capability
        for capability in capabilities
        if capability.capability_id == "source-acquisition"
    )
    assert source_acquisition.state == "ready"
    assert source_acquisition.dependency is None


class _ForeignAcquisitionCommands:
    def run_now(self, **values: object) -> Never:
        raise AcquisitionOwnershipError("contract_authority_mismatch")


def test_run_now_does_not_reveal_a_contract_owned_by_another_tenant(stack: _Stack) -> None:
    command = AcquisitionRunNowCommand(
        active_role="data_architect",
        contract_ref="contract:foreign:v1",
        trigger_window="2026-09-01T12:00:00Z/2026-09-01T13:00:00Z",
        acquisition_mode="snapshot",
    )

    with pytest.raises(ConsoleNotFound):
        stack.backend(acquisition_commands=_ForeignAcquisitionCommands()).run_acquisition_now(
            _architect_context(), command
        )


# A DSN shaped like one a deployment's secret custody holds. It is given to a resolver that
# leaks it in its own exception message, so every assertion below that the console's answer does
# not contain it is a real check of the path a connection detail could travel.
_SOURCE_DSN_CANARY = (
    "host=source.invalid port=5432 user=acquisition password=canary-password dbname=orders"
)
_SOURCE_HANDLE = "enrolled-orders"
_SOURCE_OBJECT = "customer_orders"


class _SourceSecretResolver:
    """Mints a reference pair per binding and credential revision, holding no detail in it.

    Stands in for the deployment's custodian in exactly the shape `SourceBindingService` takes.
    The references are derived from the identity asked for rather than drawn randomly, so a test
    can assert what was persisted, and they carry nothing of the DSN -- which is the property the
    real store keeps by drawing them from the operating system's generator.
    """

    def __init__(self, *, enrolled: frozenset[str] = frozenset({_SOURCE_HANDLE})) -> None:
        self._enrolled = enrolled

    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        provider_kind: AcquisitionProviderKind,
        connection_handle: str,
        account_mode: SourceAccountMode,
        credential_revision: int,
    ) -> PrivateSourceCapability:
        if connection_handle not in self._enrolled:
            # The real store names the operation and the handle and never the detail. This one
            # puts the DSN in the message deliberately: it is the worst a custodian could do, and
            # the console's answer must still carry none of it.
            raise RuntimeError(
                f"no connection enrolled for {connection_handle}: {_SOURCE_DSN_CANARY}"
            )
        return PrivateSourceCapability(
            tenant_id=tenant_id,
            binding_id=binding_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            credential_revision=credential_revision,
            endpoint_reference=f"endpoint-ref:{credential_revision:064x}",
            credential_reference=f"credential-ref:{credential_revision:064x}",
        )


class _SourceCapabilityProbe:
    """Returns the evidence a passing two-probe validation would, for one declared object.

    A double that is never more permissive than the real probe: it refuses a declaration that is
    not the binding's approved objects, which is what `PostgreSQLSourceCapabilityProbe` refuses
    `permanent_configuration` for, and the evidence it returns is the real artifact so the
    service's own checks on it still run.
    """

    def __init__(self, *, declared: tuple[str, ...] = (_SOURCE_OBJECT,)) -> None:
        self._declared = declared
        self.calls: list[tuple[str, int]] = []

    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> SourceBindingValidationEvidence:
        self.calls.append((binding.connection_handle, binding.revision))
        if binding.approved_object_refs != self._declared:
            raise AssertionError("the probe was asked to validate an undeclared declaration")
        return SourceBindingValidationEvidence(
            evidence_id=f"source-validation:{binding.binding_id}:{binding.revision}",
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            credential_revision=capability.credential_revision,
            provider_kind=binding.provider_kind,
            positive_probe_succeeded=True,
            positive_probe_digest="1" * 64,
            denial_probe_succeeded=True,
            denial_probe_digest="2" * 64,
            source_observation_ref="source-observation:postgresql:" + "1" * 64,
            capability_profile_digest="3" * 64,
            observed_at=observed_at,
        )


class _RefusingSourceCapabilityProbe:
    """A source whose connecting role reaches outside its declaration.

    The error is the provider SDK's own, which composes its message from the provider and the
    reason code and admits no free text: a probe cannot put a connection detail into it even by
    mistake. The leak path this suite exercises with a canary is therefore the secret resolver's,
    which raises whatever the deployment's custodian raises.
    """

    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> Never:
        raise AcquisitionProviderError(
            provider_kind="postgresql",
            classification="authorization_denied",
            reason_code="authorization_denied",
        )


class _EnrolledSourceConnections:
    """The deployment's offering of enrolled connections, for one tenant and no other."""

    def __init__(self, *connections: EnrolledSourceConnection) -> None:
        self._connections = connections
        self.asked: list[str] = []

    def list_enrolled_source_connections(
        self, tenant_id: str
    ) -> tuple[EnrolledSourceConnection, ...]:
        self.asked.append(tenant_id)
        return self._connections if tenant_id == _TENANT else ()


def _enrolled_connection(
    handle: str = _SOURCE_HANDLE, *, objects: tuple[str, ...] = (_SOURCE_OBJECT,)
) -> EnrolledSourceConnection:
    return EnrolledSourceConnection(
        connection_handle=handle,
        provider_kind="postgresql",
        account_mode="not_applicable",
        declared_object_refs=objects,
    )


@dataclass(frozen=True, slots=True)
class _SourceRegistry:
    repository: SQLiteSourceBindingRepository
    service: SourceBindingService


def _source_registry(
    tmp_path: Path,
    *,
    probe: SourceCapabilityProbe | None = None,
    resolver: _SourceSecretResolver | None = None,
) -> _SourceRegistry:
    """A real broker over a real repository, with the two collaborators it cannot run without.

    `probe` is annotated as the broker's own protocol rather than as either double, so the type
    checker answers for each double satisfying what `SourceBindingService` takes.
    """
    repository = SQLiteSourceBindingRepository(str(tmp_path / "source-bindings.sqlite3"))
    return _SourceRegistry(
        repository=repository,
        service=SourceBindingService(
            repository,
            secret_resolver=resolver or _SourceSecretResolver(),
            capability_probes={"postgresql": probe or _SourceCapabilityProbe()},
            clock=_clock,
        ),
    )


def _source_backend(
    stack: _Stack,
    registry: _SourceRegistry | None,
    *,
    enrolled: _EnrolledSourceConnections | None = None,
    warehouse_revision: int = 3,
) -> GovernedConsoleBackend:
    return stack.backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding(revision=warehouse_revision)),
        catalog_bindings=_StaticCatalogBindingReader(),
        source_bindings=None if registry is None else registry.service,
        enrolled_source_connections=enrolled,
        source_registration_commands=(
            None if registry is None else BrokerSourceRegistrationCommands(registry.service)
        ),
    )


def _registration(handle: str = _SOURCE_HANDLE, *, revision: int = 3) -> SourceRegistrationCommand:
    return SourceRegistrationCommand(
        expected_revision=revision,
        active_role="data_architect",
        connection_handle=handle,
    )


def test_registering_a_source_drives_the_broker_to_a_probed_ready_binding(
    stack: _Stack, tmp_path: Path
) -> None:
    """One console command, three broker transactions, and evidence at the end of them.

    The binding is read back out of the repository rather than trusted from the returned view:
    `ready` is only real if `record_validation` committed it with the evidence beside it.
    """
    probe = _SourceCapabilityProbe()
    registry = _source_registry(tmp_path, probe=probe)
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    operation = backend.register_source(_architect_context(), _registration())

    assert operation.state == "succeeded"
    assert operation.phase == "source_binding_validated"
    (stored,) = registry.repository.list_for_tenant(_TENANT)
    assert stored.lifecycle_state is SourceConnectionBindingState.READY
    assert stored.revision == 3
    assert stored.connection_handle == _SOURCE_HANDLE
    assert stored.approved_object_refs == (_SOURCE_OBJECT,)
    assert stored.capability_profile_digest == "3" * 64
    evidence = registry.repository.load_validation(_TENANT, stored.binding_id, 2)
    assert evidence.positive_probe_succeeded is True
    assert evidence.denial_probe_succeeded is True
    assert probe.calls == [(_SOURCE_HANDLE, 2)]
    registry.repository.close()


def test_a_registered_source_is_shown_and_stops_being_offered_for_registration(
    stack: _Stack, tmp_path: Path
) -> None:
    """The setup read is the register: what is registered, and what is still enrollable."""
    registry = _source_registry(tmp_path)
    enrolled = _EnrolledSourceConnections(
        _enrolled_connection(), _enrolled_connection("enrolled-billing")
    )
    backend = _source_backend(stack, registry, enrolled=enrolled)

    before = backend.get_setup(_architect_context())

    assert before.sources == ()
    assert [item.connection_handle for item in before.enrollable_sources] == [
        _SOURCE_HANDLE,
        "enrolled-billing",
    ]
    assert next(item for item in before.stages if item.stage == "sources").state == "current"

    backend.register_source(_architect_context(), _registration())
    after = backend.get_setup(_architect_context())

    (registered,) = after.sources
    assert registered.connection_handle == _SOURCE_HANDLE
    assert registered.source_type == "postgresql"
    assert registered.lifecycle_state == "ready"
    assert registered.state == "ready"
    assert registered.account_mode == "not_applicable"
    assert registered.approved_object_refs == (_SOURCE_OBJECT,)
    assert registered.capability_authority_digest == "3" * 64
    assert [item.connection_handle for item in after.enrollable_sources] == ["enrolled-billing"]
    assert next(item for item in after.stages if item.stage == "sources").state == "complete"
    assert enrolled.asked == [_TENANT, _TENANT, _TENANT]
    registry.repository.close()


def test_the_sources_stage_reports_its_missing_reader_rather_than_an_empty_register(
    stack: _Stack,
) -> None:
    """No broker read wired: blocked, with the dependency named and nothing offered.

    The sentence has to differ from the one an unbuilt stage carries, because a wired reader with
    nothing registered and no reader at all are different situations.
    """
    backend = _source_backend(stack, None)

    setup = backend.get_setup(_architect_context())

    stage = next(item for item in setup.stages if item.stage == "sources")
    assert stage.state == "blocked"
    assert stage.detail == "No connection broker read is wired for this stage."
    assert setup.sources == ()
    assert setup.enrollable_sources == ()
    unbuilt = next(item for item in setup.stages if item.stage == "meaning")
    assert unbuilt.detail == "No governed implementation is wired for this stage."


def test_the_registration_command_stays_undelivered_with_its_dependency_named(
    stack: _Stack,
) -> None:
    backend = stack.backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_StaticCatalogBindingReader(),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.register_source(_architect_context(), _registration())

    assert failure.value.code == CAPABILITY_NOT_DELIVERED
    assert "connection broker" in failure.value.safe_message


def test_registering_a_source_is_not_a_requesters_to_make(stack: _Stack, tmp_path: Path) -> None:
    """A requester is answered exactly as for an unknown resource, and nothing is registered."""
    registry = _source_registry(tmp_path)
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    with pytest.raises(ConsoleNotFound):
        backend.register_source(
            _requester_context(),
            SourceRegistrationCommand(
                expected_revision=3, active_role="data_architect", connection_handle=_SOURCE_HANDLE
            ),
        )

    assert registry.repository.list_for_tenant(_TENANT) == ()
    registry.repository.close()


def test_a_stale_setup_revision_never_reaches_the_broker(stack: _Stack, tmp_path: Path) -> None:
    """The guard is the setup revision the browser read, as it is for the process package."""
    registry = _source_registry(tmp_path)
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    with pytest.raises(ConsoleConflict) as refusal:
        backend.register_source(_architect_context(), _registration(revision=2))

    assert refusal.value.code == "stale_revision"
    assert refusal.value.recovery_action == "reload"
    assert registry.repository.list_for_tenant(_TENANT) == ()
    registry.repository.close()


def test_a_handle_that_is_not_enrolled_is_refused_before_the_broker_is_asked(
    stack: _Stack, tmp_path: Path
) -> None:
    """Nothing is drafted for a handle nobody enrolled, and the refusal discloses nothing.

    The same refusal answers a handle that does not exist and one that is already registered, so
    submitting guesses cannot enumerate the custodian's handles.
    """
    registry = _source_registry(tmp_path)
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    with pytest.raises(ConsoleInvalidRequest) as refusal:
        backend.register_source(_architect_context(), _registration("never-enrolled"))

    assert refusal.value.code == "source_handle_not_enrollable"
    assert refusal.value.field == "connection_handle"
    assert registry.repository.list_for_tenant(_TENANT) == ()

    backend.register_source(_architect_context(), _registration())
    with pytest.raises(ConsoleInvalidRequest) as already:
        backend.register_source(_architect_context(), _registration())

    assert already.value.code == refusal.value.code
    assert already.value.safe_message == refusal.value.safe_message
    registry.repository.close()


def _rendered_failure_chain(error: BaseException) -> tuple[str, ...]:
    """Everything a traceback or a response would print from this failure, chain included.

    The whole chain rather than the outermost exception: a console error carries the owning
    failure as its cause, and a traceback prints every link, so reading only the top one would
    pass while a detail sat one `raise ... from` away.

    The walk follows the links a traceback follows: the cause, and the context only while the
    raising code did not suppress it. `raise ... from None` leaves `__context__` set and tells
    every printer to stop there, which is how the broker drops what a resolver raised -- so a
    suppressed context is not something a log or a response can reach, and treating it as one
    here would assert against Python's own rule rather than against this code.
    """
    rendered: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        rendered.extend((repr(current), str(current), repr(current.args)))
        if isinstance(current, ConsoleError):
            rendered.extend((current.code, current.safe_message, str(current.field)))
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return tuple(rendered)


def test_a_custodian_that_cannot_resolve_a_handle_never_leaks_what_it_was_resolving(
    stack: _Stack, tmp_path: Path
) -> None:
    """The one path a connection detail could travel, held shut.

    The resolver is offered a handle it has no connection for and raises with the DSN in its own
    message. The broker drops that cause, and this asserts the console's answer -- its code, its
    message, and everything reachable from the exception -- carries none of it.
    """
    registry = _source_registry(tmp_path, resolver=_SourceSecretResolver(enrolled=frozenset()))
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.register_source(_architect_context(), _registration())

    # The property the walk above relies on, asserted rather than assumed: the broker raises its
    # boundary error with the resolver's own exception suppressed, so nothing that prints a
    # traceback reaches the message the custodian wrote.
    boundary = failure.value.__cause__
    assert isinstance(boundary, SourceBindingBoundaryError)
    assert boundary.__cause__ is None
    assert boundary.__suppress_context__ is True

    rendered = " ".join(_rendered_failure_chain(failure.value))
    assert _SOURCE_DSN_CANARY not in rendered
    assert "canary-password" not in rendered
    assert "postgresql://" not in rendered
    assert failure.value.recovery_action == "contact_support"
    registry.repository.close()


def test_a_source_that_reaches_outside_its_declaration_is_never_shown_as_registered(
    stack: _Stack, tmp_path: Path
) -> None:
    """A refused probe leaves a binding validating, and the console says so rather than ready."""
    registry = _source_registry(tmp_path, probe=_RefusingSourceCapabilityProbe())
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.register_source(_architect_context(), _registration())

    assert _SOURCE_DSN_CANARY not in failure.value.safe_message
    (stored,) = registry.repository.list_for_tenant(_TENANT)
    assert stored.lifecycle_state is SourceConnectionBindingState.VALIDATING
    assert stored.capability_profile_digest is None
    setup = backend.get_setup(_architect_context())
    (projected,) = setup.sources
    assert projected.lifecycle_state == "validating"
    assert projected.state == "blocked"
    assert projected.capability_authority_digest is None
    registry.repository.close()


def test_no_setup_projection_of_a_registered_source_carries_a_connection_detail(
    stack: _Stack, tmp_path: Path
) -> None:
    """The whole serialized view, checked rather than reasoned about.

    A registered binding carries a handle and two references the broker persisted privately. The
    references are not in the projection either: holding one is holding the thing that opens the
    credential, and the console has no business with it.
    """
    registry = _source_registry(tmp_path)
    backend = _source_backend(
        stack, registry, enrolled=_EnrolledSourceConnections(_enrolled_connection())
    )
    backend.register_source(_architect_context(), _registration())

    payload = backend.get_setup(_architect_context()).model_dump_json()

    assert _SOURCE_HANDLE in payload
    assert _SOURCE_DSN_CANARY not in payload
    assert "canary-password" not in payload
    assert "postgresql://" not in payload
    assert "endpoint-ref:" not in payload
    assert "credential-ref:" not in payload
    registry.repository.close()


def test_a_broker_that_hands_back_an_unvalidated_binding_is_not_reported_as_registered(
    stack: _Stack, tmp_path: Path
) -> None:
    """A collaborator that returns a draft has not registered anything.

    `validate` returns `ready` or raises, so this is a collaborator breaking the protocol. The
    console must not turn that into a success view: an architect would be told a source had been
    probed when nothing recorded that it was.
    """

    class _UnvalidatingCommands:
        def register_source(
            self,
            *,
            tenant_id: str,
            connection_handle: str,
            provider_kind: AcquisitionProviderKind,
            account_mode: SourceAccountMode,
            approved_object_refs: tuple[str, ...],
        ) -> SourceConnectionBinding:
            return service.create_draft(
                tenant_id=tenant_id,
                provider_kind=provider_kind,
                connection_handle=connection_handle,
                account_mode=account_mode,
                approved_object_refs=approved_object_refs,
            )

    registry = _source_registry(tmp_path)
    service = registry.service
    backend = stack.backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_StaticCatalogBindingReader(),
        source_bindings=service,
        enrolled_source_connections=_EnrolledSourceConnections(_enrolled_connection()),
        source_registration_commands=_UnvalidatingCommands(),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.register_source(_architect_context(), _registration())

    assert failure.value.code == CAPABILITY_NOT_DELIVERED
    registry.repository.close()
