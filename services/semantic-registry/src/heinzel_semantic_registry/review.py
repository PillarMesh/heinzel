from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from heinzel_contract_model import ArtifactModel, digest
from heinzel_request_management import (
    DecisionKind,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SchemaSemanticChangeRequest,
)
from pydantic import Field, field_validator

from .models import AuthorityResolution, SemanticCandidateSet


class ReviewSemanticRepository(Protocol):
    def load_revision(self, tenant_id: str, set_id: str, revision: int) -> object: ...

    def verify_review_inputs(
        self,
        *,
        tenant_id: str,
        candidate_set_id: str,
        candidate_set_revision: int,
        candidate_ids: tuple[str, ...],
        observation_digests: tuple[str, ...],
    ) -> SemanticCandidateSet: ...

    def has_authority_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool: ...

    def verify_bundle_candidates(
        self, *, tenant_id: str, candidate_set_digest: str, candidate_ids: tuple[str, ...]
    ) -> None: ...

    def store_review_bundle(self, bundle: OntologyReviewBundle) -> OntologyReviewBundle: ...

    def compensate_review_bundle_submission(
        self, prior: OntologyReviewBundle, attempted: OntologyReviewBundle
    ) -> None: ...

    def store_review_bundle_with_baselines(
        self, bundle: OntologyReviewBundle, revisions: tuple[SemanticRevision, ...]
    ) -> OntologyReviewBundle: ...

    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle: ...

    def next_semantic_revision(self, tenant_id: str, bundle_id: str, item_id: str) -> int: ...

    def store_review_decision(
        self, bundle: OntologyReviewBundle, revision: SemanticRevision | None
    ) -> OntologyReviewBundle: ...


class ReviewItemDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    REVISE = "revise"
    MERGE = "merge"
    UNRESOLVED = "unresolved"


class OntologyReviewItem(ArtifactModel):
    item_id: str = Field(min_length=1)
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    authority_resolution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_authority_ref: str = Field(min_length=1)
    semantic_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "accepted", "rejected", "revised", "merged", "unresolved"]


class OntologyReviewBundle(ArtifactModel):
    schema_version: Literal["1"] = "1"
    bundle_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_observation_digests: tuple[str, ...]
    items: tuple[OntologyReviewItem, ...]
    required_authority_refs: tuple[str, ...]
    status: Literal["open", "awaiting_approval", "approved", "rejected", "no_valid_plan"]
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class SemanticRevision(ArtifactModel):
    revision_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    content: str = Field(min_length=1)
    parent_candidate_ids: tuple[str, ...]
    actor_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class SemanticReviewService:
    def __init__(
        self,
        *,
        semantic_repository: ReviewSemanticRepository,
        request_service: RequestManagementService,
        clock: Callable[[], datetime],
    ) -> None:
        self._semantic_repository = semantic_repository
        self._request_service = request_service
        self._clock = clock

    def create_bundle(
        self,
        *,
        tenant_id: str,
        candidate_set_id: str,
        candidate_set_revision: int,
        resolutions: tuple[AuthorityResolution, ...],
    ) -> OntologyReviewBundle:
        if not resolutions or any(resolution.tenant_id != tenant_id for resolution in resolutions):
            raise ValueError("review resolutions must belong to the tenant")
        candidate_ids = tuple(resolution.candidate_id for resolution in resolutions)
        observation_digests = tuple(
            sorted(
                {
                    observation_digest
                    for resolution in resolutions
                    for observation_digest in resolution.considered_observation_digests
                }
            )
        )
        candidate_set = self._semantic_repository.verify_review_inputs(
            tenant_id=tenant_id,
            candidate_set_id=candidate_set_id,
            candidate_set_revision=candidate_set_revision,
            candidate_ids=candidate_ids,
            observation_digests=observation_digests,
        )
        now = self._now()
        bundle_identity = (tenant_id, candidate_set_id, candidate_set_revision, resolutions)
        bundle_id = f"review-{digest(bundle_identity)[:24]}"
        baseline_revisions = tuple(
            SemanticRevision(
                revision_id=f"semrev-{digest((bundle_id, resolution.candidate_id))[:24]}",
                tenant_id=tenant_id,
                bundle_id=bundle_id,
                item_id=f"item-{resolution.candidate_id}",
                revision=1,
                content=next(
                    candidate.proposed_definition or candidate.name
                    for candidate in candidate_set.candidates
                    if candidate.candidate_id == resolution.candidate_id
                ),
                parent_candidate_ids=(resolution.candidate_id,),
                actor_id="system:baseline",
                created_at=now,
            )
            for resolution in resolutions
        )
        items = tuple(
            OntologyReviewItem(
                item_id=revision.item_id,
                candidate_ids=revision.parent_candidate_ids,
                authority_resolution_digest=digest(resolution),
                required_authority_ref=resolution.required_authority_ref or "role:business_owner",
                semantic_revision_digest=digest(revision),
                status="pending",
            )
            for resolution, revision in zip(resolutions, baseline_revisions, strict=True)
        )
        bundle = OntologyReviewBundle(
            bundle_id=bundle_id,
            tenant_id=tenant_id,
            revision=1,
            candidate_set_digest=digest(candidate_set),
            authority_observation_digests=observation_digests,
            items=items,
            required_authority_refs=tuple(sorted({item.required_authority_ref for item in items})),
            status="open",
            created_at=now,
            updated_at=now,
        )
        return self._semantic_repository.store_review_bundle_with_baselines(
            bundle, baseline_revisions
        )

    def submit_bundle(
        self, *, tenant_id: str, bundle_id: str, expected_revision: int, requester_id: str
    ) -> InboxRequest:
        bundle = self._load_current_bundle(tenant_id, bundle_id, expected_revision)
        if bundle.status == "approved":
            terminal_requests = tuple(
                request
                for request in self._request_service.list_inbox(tenant_id)
                if isinstance(request.payload, SchemaSemanticChangeRequest)
                and request.payload.review_bundle_id == bundle_id
                and request.state
                in {
                    RequestState.EXECUTING,
                    RequestState.VERIFYING,
                    RequestState.DELIVERED,
                    RequestState.MONITORING,
                    RequestState.RETIRED,
                }
            )
            if len(terminal_requests) != 1:
                raise ValueError("approved review bundle does not have one terminal request")
            return terminal_requests[0]
        if bundle.status == "no_valid_plan":
            unresolved_requests = tuple(
                request
                for request in self._request_service.list_inbox(tenant_id)
                if isinstance(request.payload, SchemaSemanticChangeRequest)
                and request.payload.review_bundle_id == bundle_id
                and request.state in {RequestState.INVESTIGATING, RequestState.NO_VALID_PLAN}
            )
            if len(unresolved_requests) != 1:
                raise ValueError("No Valid Plan review bundle does not have one terminal request")
            return unresolved_requests[0]
        if bundle.status == "awaiting_approval":
            awaiting = bundle
            existing = next(
                (
                    request
                    for request in self._request_service.list_inbox(tenant_id)
                    if isinstance(request.payload, SchemaSemanticChangeRequest)
                    and request.payload.review_bundle_id == bundle_id
                    and request.payload.review_bundle_digest == digest(bundle)
                ),
                None,
            )
            if existing is not None:
                return existing
        elif bundle.status == "open":
            awaiting = bundle.model_copy(
                update={
                    "revision": bundle.revision + 1,
                    "status": "awaiting_approval",
                    "updated_at": self._now(),
                }
            )
        else:
            raise ValueError("review bundle cannot be submitted from its current state")
        prepared = self._request_service.prepare_schema_semantic_change(
            tenant_id=tenant_id,
            requester_id=requester_id,
            purpose="Review schema semantic change",
            review_bundle_id=bundle.bundle_id,
            review_bundle_digest=digest(awaiting),
            required_authority_refs=bundle.required_authority_refs,
        )
        try:
            self._semantic_repository.store_review_bundle(awaiting)
            submitted = self._request_service.submit_schema_semantic_change(
                tenant_id=tenant_id,
                requester_id=requester_id,
                purpose="Review schema semantic change",
                review_bundle_id=bundle.bundle_id,
                review_bundle_digest=digest(awaiting),
                required_authority_refs=bundle.required_authority_refs,
            )
            if submitted.request_id != prepared.request_id:
                raise RuntimeError("prepared semantic request identity changed")
            investigating = self._request_service.transition(
                tenant_id,
                submitted.request_id,
                RequestState.INVESTIGATING,
                actor_id=requester_id,
                expected_revision=submitted.revision,
            )
            proposed = self._request_service.transition(
                tenant_id,
                investigating.request_id,
                RequestState.PROPOSED,
                actor_id=requester_id,
                expected_revision=investigating.revision,
            )
            return self._request_service.transition(
                tenant_id,
                proposed.request_id,
                RequestState.AWAITING_APPROVAL,
                actor_id=requester_id,
                expected_revision=proposed.revision,
            )
        except Exception:
            if self._request_service.get_optional(tenant_id, prepared.request_id) is not None:
                self._request_service.discard_unapproved_semantic_request(
                    tenant_id=tenant_id,
                    request_id=prepared.request_id,
                    review_bundle_digest=digest(awaiting),
                )
            self._semantic_repository.compensate_review_bundle_submission(bundle, awaiting)
            raise

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
        bundle = self._load_current_bundle(tenant_id, bundle_id, expected_revision)
        item = next((item for item in bundle.items if item.item_id == item_id), None)
        if item is None:
            raise KeyError(f"review item {item_id} does not belong to bundle")
        if not self._semantic_repository.has_authority_role(
            tenant_id=tenant_id, actor_id=actor_id, authority_ref=item.required_authority_ref
        ):
            raise PermissionError("actor lacks required authority role")
        if decision in {ReviewItemDecision.REVISE, ReviewItemDecision.MERGE}:
            if not revised_content:
                raise ValueError("revised_content is required for a material revision")
            parents = (
                merge_candidate_ids if decision is ReviewItemDecision.MERGE else item.candidate_ids
            )
            if decision is ReviewItemDecision.MERGE:
                self._semantic_repository.verify_bundle_candidates(
                    tenant_id=tenant_id,
                    candidate_set_digest=bundle.candidate_set_digest,
                    candidate_ids=parents,
                )
            revision_identity = (
                bundle_id,
                item_id,
                expected_revision,
                revised_content,
                parents,
            )
            revision = SemanticRevision(
                revision_id=f"semrev-{digest(revision_identity)[:24]}",
                tenant_id=tenant_id,
                bundle_id=bundle_id,
                item_id=item_id,
                revision=self._semantic_repository.next_semantic_revision(
                    tenant_id, bundle_id, item_id
                ),
                content=revised_content,
                parent_candidate_ids=parents,
                actor_id=actor_id,
                created_at=self._now(),
            )
            revision_digest = digest(revision)
        else:
            revision = None
            revision_digest = item.semantic_revision_digest
        statuses = {
            ReviewItemDecision.ACCEPT: "accepted",
            ReviewItemDecision.REJECT: "rejected",
            ReviewItemDecision.REVISE: "revised",
            ReviewItemDecision.MERGE: "merged",
            ReviewItemDecision.UNRESOLVED: "unresolved",
        }
        updated_item = item.model_copy(
            update={"status": statuses[decision], "semantic_revision_digest": revision_digest}
        )
        updated_items = tuple(
            updated_item if value.item_id == item_id else value for value in bundle.items
        )
        status = "no_valid_plan" if decision is ReviewItemDecision.UNRESOLVED else "open"
        updated = bundle.model_copy(
            update={
                "revision": bundle.revision + 1,
                "items": updated_items,
                "status": status,
                "updated_at": self._now(),
            }
        )
        return self._semantic_repository.store_review_decision(updated, revision)

    def finalize_bundle(
        self,
        *,
        tenant_id: str,
        bundle_id: str,
        expected_revision: int,
        request_id: str,
        request_revision: int,
        decision: DecisionKind | ReviewItemDecision,
    ) -> OntologyReviewBundle:
        if decision not in {
            DecisionKind.APPROVE,
            DecisionKind.REJECT,
            ReviewItemDecision.UNRESOLVED,
        }:
            raise ValueError("terminal bundle decision must approve or reject")
        bundle = self._load_current_bundle(tenant_id, bundle_id, expected_revision)
        if bundle.status != "awaiting_approval":
            raise ValueError("review bundle is not awaiting approval")
        if decision is not ReviewItemDecision.UNRESOLVED and any(
            item.status not in {"accepted", "rejected"} for item in bundle.items
        ):
            raise ValueError("every review item must have a terminal decision")
        request = self._request_service.get(tenant_id, request_id)
        if request.revision != request_revision:
            raise ValueError("request revision is stale")
        if request.state is not RequestState.AWAITING_APPROVAL or not isinstance(
            request.payload, SchemaSemanticChangeRequest
        ):
            raise ValueError("request is not awaiting semantic approval")
        if (
            request.payload.review_bundle_id != bundle.bundle_id
            or request.payload.review_bundle_digest != digest(bundle)
        ):
            raise ValueError("request does not bind the current review bundle digest")
        if decision is ReviewItemDecision.UNRESOLVED:
            updated_bundle = bundle.model_copy(
                update={
                    "revision": bundle.revision + 1,
                    "status": "no_valid_plan",
                    "updated_at": self._now(),
                }
            )
            try:
                transitioned = self._request_service.record_review_decision(
                    tenant_id=tenant_id,
                    request_id=request_id,
                    request_revision=request_revision,
                    actor_id=request.requester_id,
                    decision="unresolved",
                )
                if transitioned.state is not RequestState.INVESTIGATING:
                    raise RuntimeError("unresolved review did not return to investigating")
                return self._semantic_repository.store_review_decision(
                    updated_bundle,
                    None,
                )
            except Exception:
                self._semantic_repository.compensate_review_bundle_submission(
                    bundle, updated_bundle
                )
                self._request_service.compensate_transition(
                    prior=request, attempted_state=RequestState.INVESTIGATING
                )
                raise
        decisions = self._request_service.list_decisions(tenant_id, request_id)
        for item in bundle.items:
            if not any(
                recorded.kind is decision
                and recorded.request_revision == request_revision
                and recorded.subject_digest == item.semantic_revision_digest
                and self._semantic_repository.has_authority_role(
                    tenant_id=tenant_id,
                    actor_id=recorded.actor_id,
                    authority_ref=item.required_authority_ref,
                )
                for recorded in decisions
            ):
                raise ValueError("missing exact authority decision binding")
        status = "approved" if decision is DecisionKind.APPROVE else "rejected"
        updated_bundle = bundle.model_copy(
            update={
                "revision": bundle.revision + 1,
                "status": status,
                "updated_at": self._now(),
            }
        )
        target_state = (
            RequestState.EXECUTING if decision is DecisionKind.APPROVE else RequestState.REJECTED
        )
        try:
            updated = self._semantic_repository.store_review_decision(updated_bundle, None)
            self._request_service.transition(
                tenant_id,
                request_id,
                target_state,
                actor_id=next(
                    recorded.actor_id
                    for recorded in decisions
                    if recorded.kind is decision and recorded.request_revision == request_revision
                ),
                expected_revision=request_revision,
            )
            return updated
        except Exception:
            self._request_service.compensate_transition(prior=request, attempted_state=target_state)
            self._semantic_repository.compensate_review_bundle_submission(bundle, updated_bundle)
            raise

    def _load_current_bundle(
        self, tenant_id: str, bundle_id: str, expected_revision: int
    ) -> OntologyReviewBundle:
        bundle = self._semantic_repository.load_review_bundle(tenant_id, bundle_id)
        if bundle.revision != expected_revision:
            raise ValueError("review bundle revision is stale")
        return bundle

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return timezone-aware UTC")
        return now.astimezone(UTC)
