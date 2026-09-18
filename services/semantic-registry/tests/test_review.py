from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from heinzel_contract_model import InformationKind, digest
from heinzel_request_management import (
    DecisionKind,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SchemaSemanticChangeRequest,
    SQLiteRequestRepository,
)
from heinzel_semantic_registry import (
    AuthorityResolution,
    AuthorityResolutionStatus,
    AuthoritySourceKind,
    CandidateKind,
    CandidateProvenance,
    OntologyReviewBundle,
    ResolutionReasonCode,
    ReviewItemDecision,
    SemanticReviewService,
    SemanticRevision,
    SQLiteSemanticRepository,
)
from heinzel_semantic_registry.repository import _CandidateDraft
from pydantic import ValidationError

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def resolution(
    *, tenant_id: str = "tenant-a", candidate_id: str = "candidate-refund"
) -> AuthorityResolution:
    return AuthorityResolution(
        resolution_id=f"resolution-{candidate_id}",
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        status=AuthorityResolutionStatus.UNRESOLVED,
        selected_observation_digest=None,
        considered_observation_digests=(),
        required_authority_ref="role:business_owner",
        reason_code=ResolutionReasonCode.NO_ADMITTED_AUTHORITY,
    )


def review_service(
    *,
    semantic_repository: SQLiteSemanticRepository | None = None,
    request_service: RequestManagementService | None = None,
) -> tuple[SemanticReviewService, SQLiteSemanticRepository]:
    repository = semantic_repository or SQLiteSemanticRepository(":memory:")
    repository.grant_authority_role(
        tenant_id="tenant-a", actor_id="business-owner", authority_ref="role:business_owner"
    )
    return SemanticReviewService(
        semantic_repository=repository,
        request_service=request_service
        or RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW),
        clock=lambda: NOW,
    ), repository


def create_bundle(
    service: SemanticReviewService, repository: SQLiteSemanticRepository
) -> OntologyReviewBundle:
    candidate_set = repository._materialize(
        tenant_id="tenant-a",
        package_id="package-a",
        package_version=1,
        original_digest="a" * 64,
        manifest_digest="b" * 64,
        extractor_id="test",
        extractor_version="1",
        candidates=(
            _CandidateDraft(
                kind=CandidateKind.ENTITY,
                name="Refund",
                proposed_definition="A refund",
                related_refs=(),
                provenance=CandidateProvenance(
                    source_kind="manifest", source_digest="a" * 64, source_path="manifest"
                ),
                confidence=Decimal("1"),
            ),
        ),
        unresolved_questions=(),
        created_at=NOW,
    )
    return service.create_bundle(
        tenant_id="tenant-a",
        candidate_set_id=candidate_set.set_id,
        candidate_set_revision=candidate_set.revision,
        resolutions=(resolution(candidate_id=candidate_set.candidates[0].candidate_id),),
    )


def database_dump(
    repository: SQLiteSemanticRepository | SQLiteRequestRepository,
) -> tuple[str, ...]:
    return tuple(repository._connection.iterdump())


def test_submission_failure_after_each_repository_effect_restores_exact_state_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for failure_point in ("bundle", "request", "investigating", "proposed", "awaiting"):
        request_repository = SQLiteRequestRepository.open(":memory:")
        request_service = RequestManagementService(request_repository, clock=lambda: NOW)
        service, semantic_repository = review_service(request_service=request_service)
        bundle = create_bundle(service, semantic_repository)
        semantic_before = database_dump(semantic_repository)
        request_before = database_dump(request_repository)

        if failure_point == "bundle":
            original_store = semantic_repository.store_review_bundle

            def fail_after_bundle(
                bundle_to_store: OntologyReviewBundle,
                store: Callable[[OntologyReviewBundle], object] = original_store,
            ) -> None:
                store(bundle_to_store)
                raise RuntimeError("after bundle effect")

            monkeypatch.setattr(semantic_repository, "store_review_bundle", fail_after_bundle)
        elif failure_point == "request":
            original_submit = request_service.submit_schema_semantic_change

            def fail_after_request(
                submit: Callable[..., object] = original_submit, **kwargs: object
            ) -> None:
                submit(**kwargs)
                raise RuntimeError("after request effect")

            monkeypatch.setattr(
                request_service, "submit_schema_semantic_change", fail_after_request
            )
        else:
            original_transition = request_service.transition
            transition_count = 0

            selected_failure_point = failure_point

            def fail_after_transition(
                *args: object,
                transition: Callable[..., InboxRequest] = original_transition,
                selected_failure: str = selected_failure_point,
                **kwargs: object,
            ) -> InboxRequest:
                nonlocal transition_count
                transitioned = transition(*args, **kwargs)
                transition_count += 1
                expected_count = {"investigating": 1, "proposed": 2, "awaiting": 3}[
                    selected_failure
                ]
                if transition_count == expected_count:
                    raise RuntimeError(f"after {selected_failure} effect")
                return transitioned

            monkeypatch.setattr(request_service, "transition", fail_after_transition)

        with pytest.raises(RuntimeError, match=f"after {failure_point} effect"):
            service.submit_bundle(
                tenant_id="tenant-a",
                bundle_id=bundle.bundle_id,
                expected_revision=bundle.revision,
                requester_id="architect-a",
            )

        assert database_dump(semantic_repository) == semantic_before
        assert database_dump(request_repository) == request_before
        monkeypatch.undo()

        replay = service.submit_bundle(
            tenant_id="tenant-a",
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision,
            requester_id="architect-a",
        )
        assert replay.request_id.startswith("req-00000000000000000001-")


def test_finalization_transitions_the_exact_linked_request_revision() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    awaiting = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    unresolved = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=2,
        request_id=awaiting.request_id,
        request_revision=awaiting.revision,
        decision=ReviewItemDecision.UNRESOLVED,
    )
    request = service._request_service.get("tenant-a", awaiting.request_id)

    assert unresolved.status == "no_valid_plan"
    assert request.state.value == "investigating"
    terminal = service._request_service.transition(
        "tenant-a",
        request.request_id,
        RequestState.NO_VALID_PLAN,
        actor_id="business-owner",
        expected_revision=request.revision,
    )
    assert terminal.state is RequestState.NO_VALID_PLAN


def test_rejection_finalization_transitions_the_linked_request_to_rejected() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    decided = service.decide_item(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        item_id=bundle.items[0].item_id,
        decision=ReviewItemDecision.REJECT,
        actor_id="business-owner",
        expected_revision=2,
    )
    rebound = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision,
        requester_id="architect-a",
    )
    service._request_service.record_decision(
        tenant_id="tenant-a",
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        actor_id="business-owner",
        kind=DecisionKind.REJECT,
        subject_digest=decided.items[0].semantic_revision_digest,
    )

    rejected = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision + 1,
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        decision=DecisionKind.REJECT,
    )

    assert rejected.status == "rejected"
    assert (
        service._request_service.get("tenant-a", rebound.request_id).state is RequestState.REJECTED
    )


@pytest.mark.parametrize(
    ("decision", "item_decision", "target_state"),
    (
        (DecisionKind.APPROVE, ReviewItemDecision.ACCEPT, RequestState.EXECUTING),
        (DecisionKind.REJECT, ReviewItemDecision.REJECT, RequestState.REJECTED),
    ),
)
def test_request_transition_failure_after_bundle_finalization_restores_exact_state_and_replays(
    monkeypatch: pytest.MonkeyPatch,
    decision: DecisionKind,
    item_decision: ReviewItemDecision,
    target_state: RequestState,
) -> None:
    request_repository = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(request_repository, clock=lambda: NOW)
    service, semantic_repository = review_service(request_service=request_service)
    bundle = create_bundle(service, semantic_repository)
    service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    decided = service.decide_item(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        item_id=bundle.items[0].item_id,
        decision=item_decision,
        actor_id="business-owner",
        expected_revision=2,
    )
    rebound = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision,
        requester_id="architect-a",
    )
    request_service.record_decision(
        tenant_id="tenant-a",
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        actor_id="business-owner",
        kind=decision,
        subject_digest=decided.items[0].semantic_revision_digest,
    )
    semantic_before = database_dump(semantic_repository)
    request_before = database_dump(request_repository)
    original_transition = request_service.transition

    def fail_after_transition(
        tenant_id: str,
        request_id: str,
        state: RequestState,
        *,
        actor_id: str,
        expected_revision: int,
    ) -> None:
        original_transition(
            tenant_id, request_id, state, actor_id=actor_id, expected_revision=expected_revision
        )
        raise RuntimeError("after final request transition")

    monkeypatch.setattr(request_service, "transition", fail_after_transition)
    with pytest.raises(RuntimeError, match="after final request transition"):
        service.finalize_bundle(
            tenant_id="tenant-a",
            bundle_id=bundle.bundle_id,
            expected_revision=decided.revision + 1,
            request_id=rebound.request_id,
            request_revision=rebound.revision,
            decision=decision,
        )

    assert database_dump(semantic_repository) == semantic_before
    assert database_dump(request_repository) == request_before
    monkeypatch.undo()

    replayed = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision + 1,
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        decision=decision,
    )
    assert replayed.status == ("approved" if decision is DecisionKind.APPROVE else "rejected")
    assert request_service.get("tenant-a", rebound.request_id).state is target_state


def test_bundle_failure_after_unresolved_request_transition_restores_exact_state_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_repository = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(request_repository, clock=lambda: NOW)
    service, semantic_repository = review_service(request_service=request_service)
    bundle = create_bundle(service, semantic_repository)
    awaiting = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    semantic_before = database_dump(semantic_repository)
    request_before = database_dump(request_repository)
    original_store = semantic_repository.store_review_decision

    def fail_after_bundle(
        bundle_to_store: OntologyReviewBundle, revision: SemanticRevision | None
    ) -> None:
        original_store(bundle_to_store, revision)
        raise RuntimeError("after unresolved bundle effect")

    monkeypatch.setattr(semantic_repository, "store_review_decision", fail_after_bundle)
    with pytest.raises(RuntimeError, match="after unresolved bundle effect"):
        service.finalize_bundle(
            tenant_id="tenant-a",
            bundle_id=bundle.bundle_id,
            expected_revision=2,
            request_id=awaiting.request_id,
            request_revision=awaiting.revision,
            decision=ReviewItemDecision.UNRESOLVED,
        )

    assert database_dump(semantic_repository) == semantic_before
    assert database_dump(request_repository) == request_before
    monkeypatch.undo()

    replayed = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=2,
        request_id=awaiting.request_id,
        request_revision=awaiting.revision,
        decision=ReviewItemDecision.UNRESOLVED,
    )
    assert replayed.status == "no_valid_plan"
    assert request_service.get("tenant-a", awaiting.request_id).state is RequestState.INVESTIGATING


def test_review_validates_every_considered_observation_digest_tenant() -> None:
    service, repository = review_service()
    candidate_set = repository._materialize(
        tenant_id="tenant-a",
        package_id="package-a",
        package_version=1,
        original_digest="a" * 64,
        manifest_digest="b" * 64,
        extractor_id="test",
        extractor_version="1",
        candidates=(
            _CandidateDraft(
                kind=CandidateKind.ENTITY,
                name="Refund",
                proposed_definition="A refund",
                related_refs=(),
                provenance=CandidateProvenance(
                    source_kind="manifest", source_digest="a" * 64, source_path="manifest"
                ),
                confidence=Decimal("1"),
            ),
        ),
        unresolved_questions=(),
        created_at=NOW,
    )
    foreign = repository.record_observation(
        tenant_id="tenant-b",
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="refund",
        assertion="foreign",
        authority_ref="role:business_owner",
        observed_digest="c" * 64,
        observed_at=NOW,
        valid_until=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    considered = resolution(candidate_id=candidate_set.candidates[0].candidate_id).model_copy(
        update={"considered_observation_digests": (digest(foreign),)}
    )

    with pytest.raises(ValueError, match="observations must belong to the tenant"):
        service.create_bundle(
            tenant_id="tenant-a",
            candidate_set_id=candidate_set.set_id,
            candidate_set_revision=1,
            resolutions=(considered,),
        )


@pytest.mark.parametrize("invalid_field", ("tenant_id", "bundle_id", "item_id", "revision"))
def test_repository_rejects_semantic_revision_identity_mismatch(invalid_field: str) -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    item = bundle.items[0]
    revision = SemanticRevision(
        revision_id="semrev-test",
        tenant_id=bundle.tenant_id,
        bundle_id=bundle.bundle_id,
        item_id=item.item_id,
        revision=2,
        content="revised",
        parent_candidate_ids=item.candidate_ids,
        actor_id="business-owner",
        created_at=NOW,
    )
    invalid_values = {
        "tenant_id": "tenant-b",
        "bundle_id": "other-bundle",
        "item_id": "other-item",
        "revision": 4,
    }
    invalid = revision.model_copy(update={invalid_field: invalid_values[invalid_field]})
    updated_item = item.model_copy(update={"semantic_revision_digest": digest(invalid)})
    updated = bundle.model_copy(update={"revision": 2, "items": (updated_item,), "updated_at": NOW})

    with pytest.raises(ValueError, match="semantic revision"):
        repository.store_review_decision(updated, invalid)


def test_review_bundle_is_immutable_and_digest_addressable() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)

    assert bundle.tenant_id == "tenant-a"
    assert bundle.revision == 1
    assert digest(bundle)
    with pytest.raises(ValidationError):
        type(bundle).model_validate(bundle.model_dump() | {"unexpected": True})
    with pytest.raises(ValidationError):
        type(bundle).model_validate(bundle.model_dump() | {"revision": 0})


def test_review_rejects_cross_tenant_bundle_access() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)

    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.submit_bundle(
            tenant_id="tenant-b",
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision,
            requester_id="attacker",
        )


def test_review_rejects_stale_bundle_revision() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=bundle.revision,
        requester_id="architect-a",
    )

    with pytest.raises(ValueError, match="revision is stale"):
        service.decide_item(
            tenant_id="tenant-a",
            bundle_id=bundle.bundle_id,
            item_id=bundle.items[0].item_id,
            decision=ReviewItemDecision.UNRESOLVED,
            actor_id="business-owner",
            expected_revision=bundle.revision,
        )


def test_submission_binds_the_persisted_awaiting_approval_bundle_and_request_path() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)

    submitted = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    persisted = repository.load_review_bundle("tenant-a", bundle.bundle_id)

    assert submitted.state.value == "awaiting_approval"
    assert isinstance(submitted.payload, SchemaSemanticChangeRequest)
    assert submitted.payload.review_bundle_digest == digest(persisted)
    assert persisted.status == "awaiting_approval"


def test_terminal_approval_requires_exact_authority_binding_for_every_item() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=1,
        requester_id="architect-a",
    )
    decided = service.decide_item(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        item_id=bundle.items[0].item_id,
        decision=ReviewItemDecision.ACCEPT,
        actor_id="business-owner",
        expected_revision=2,
    )
    rebound = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision,
        requester_id="architect-a",
    )

    with pytest.raises(ValueError, match="missing exact authority decision binding"):
        service.finalize_bundle(
            tenant_id="tenant-a",
            bundle_id=bundle.bundle_id,
            expected_revision=decided.revision + 1,
            request_id=rebound.request_id,
            request_revision=rebound.revision,
            decision=DecisionKind.APPROVE,
        )

    service._request_service.record_decision(
        tenant_id="tenant-a",
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        actor_id="business-owner",
        kind=DecisionKind.APPROVE,
        subject_digest=decided.items[0].semantic_revision_digest,
    )
    approved = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision + 1,
        request_id=rebound.request_id,
        request_revision=rebound.revision,
        decision=DecisionKind.APPROVE,
    )

    assert approved.status == "approved"
    assert (
        service._request_service.get("tenant-a", rebound.request_id).state is RequestState.EXECUTING
    )


def test_approved_bundle_submission_replay_returns_the_existing_terminal_request() -> None:
    service, repository = review_service()
    bundle = create_bundle(service, repository)
    service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=bundle.revision,
        requester_id="architect-a",
    )
    decided = service.decide_item(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        item_id=bundle.items[0].item_id,
        decision=ReviewItemDecision.ACCEPT,
        actor_id="business-owner",
        expected_revision=bundle.revision + 1,
    )
    awaiting = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision,
        requester_id="architect-a",
    )
    service._request_service.record_decision(
        tenant_id="tenant-a",
        request_id=awaiting.request_id,
        request_revision=awaiting.revision,
        actor_id="business-owner",
        kind=DecisionKind.APPROVE,
        subject_digest=decided.items[0].semantic_revision_digest,
    )
    approved = service.finalize_bundle(
        tenant_id="tenant-a",
        bundle_id=bundle.bundle_id,
        expected_revision=decided.revision + 1,
        request_id=awaiting.request_id,
        request_revision=awaiting.revision,
        decision=DecisionKind.APPROVE,
    )
    inbox_size = len(service._request_service.list_inbox("tenant-a"))

    replay = service.submit_bundle(
        tenant_id="tenant-a",
        bundle_id=approved.bundle_id,
        expected_revision=approved.revision,
        requester_id="architect-a",
    )

    assert replay.request_id == awaiting.request_id
    assert replay.state is RequestState.EXECUTING
    assert len(service._request_service.list_inbox("tenant-a")) == inbox_size
