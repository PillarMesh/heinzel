from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from heinzel_contract_model import InformationKind, digest

from .models import (
    AuthorityObservation,
    AuthorityResolution,
    AuthorityResolutionStatus,
    AuthoritySourceKind,
    CandidateKind,
    ResolutionReasonCode,
    SemanticCandidate,
)

_PRECEDENCE: dict[InformationKind, tuple[AuthoritySourceKind, ...]] = {
    InformationKind.BUSINESS_MEANING: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.IMPORTED_CLASSIFICATION: (
        AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        AuthoritySourceKind.OWNER_DECISION,
    ),
    InformationKind.PROCESS_SEMANTICS: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.IMPORTED_GLOSSARY: (
        AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        AuthoritySourceKind.OWNER_DECISION,
    ),
    InformationKind.IDENTITY: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.RELATIONSHIP: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.METRIC: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.INTEGRITY_CONSTRAINT: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
}

# Kinds that make competing claims about the same aspect of a subject, and can
# therefore contradict one another. Kinds in different groups describe different
# aspects: an imported glossary definition ("money returned to a customer") does not
# disagree with a structural claim ("an entity with its own lifecycle"), so requiring
# them to state the same string would make every candidate in a tenant that already has
# a glossary escalate to its owner.
_CONTRADICTION_GROUPS: tuple[frozenset[InformationKind], ...] = (
    frozenset(
        {
            InformationKind.BUSINESS_MEANING,
            InformationKind.PROCESS_SEMANTICS,
            InformationKind.IMPORTED_CLASSIFICATION,
        }
    ),
    frozenset({InformationKind.IMPORTED_GLOSSARY}),
    frozenset({InformationKind.IDENTITY}),
    frozenset({InformationKind.RELATIONSHIP}),
    frozenset({InformationKind.METRIC}),
    frozenset({InformationKind.INTEGRITY_CONSTRAINT}),
)

# The information kind a candidate is itself a claim about. Used only to choose which
# admitted winner is bound as the authority when several aspects are observed.
_CANDIDATE_GOVERNING_KIND: dict[CandidateKind, InformationKind] = {
    CandidateKind.ENTITY: InformationKind.BUSINESS_MEANING,
    CandidateKind.EVENT: InformationKind.BUSINESS_MEANING,
    CandidateKind.STATE: InformationKind.PROCESS_SEMANTICS,
    CandidateKind.RELATIONSHIP: InformationKind.RELATIONSHIP,
    CandidateKind.IDENTITY_RULE: InformationKind.IDENTITY,
    CandidateKind.INTEGRITY_CONSTRAINT: InformationKind.INTEGRITY_CONSTRAINT,
    CandidateKind.METRIC: InformationKind.METRIC,
    CandidateKind.CLASSIFICATION: InformationKind.IMPORTED_CLASSIFICATION,
}

_BUSINESS_OWNER = "role:business_owner"


def _contradiction_group(information_kind: InformationKind) -> frozenset[InformationKind]:
    return next(group for group in _CONTRADICTION_GROUPS if information_kind in group)


class AuthorityResolver:
    def __init__(
        self,
        *,
        tenant_id: str,
        clock: Callable[[], datetime],
        candidate_ownership_verifier: Callable[[str, SemanticCandidate], bool],
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        self._tenant_id = tenant_id
        self._clock = clock
        self._candidate_ownership_verifier = candidate_ownership_verifier

    def resolve(
        self,
        *,
        candidate: SemanticCandidate,
        observations: tuple[AuthorityObservation, ...],
    ) -> AuthorityResolution:
        if not self._candidate_ownership_verifier(self._tenant_id, candidate):
            raise ValueError("candidate is not owned by resolver tenant")
        considered_digests = tuple(sorted(digest(item) for item in observations))
        if any(item.tenant_id != self._tenant_id for item in observations):
            raise ValueError("observation tenant does not match resolver tenant")
        if any(item.subject_ref != candidate.name for item in observations):
            raise ValueError("observation subject does not match candidate name")
        if not observations:
            return self._resolution(
                candidate=candidate,
                status=AuthorityResolutionStatus.UNRESOLVED,
                selected_digest=None,
                considered_digests=considered_digests,
                required_authority_ref=_BUSINESS_OWNER,
                reason_code=ResolutionReasonCode.NO_ADMITTED_AUTHORITY,
            )

        now = _utc(self._clock())
        if any(item.observed_at > now for item in observations):
            raise ValueError("observation observed_at is in the future")
        if any(item.valid_until <= now for item in observations):
            return self._resolution(
                candidate=candidate,
                status=AuthorityResolutionStatus.INVALID,
                selected_digest=None,
                considered_digests=considered_digests,
                required_authority_ref=None,
                reason_code=ResolutionReasonCode.EXPIRED_OBSERVATION,
            )
        if any(item.source_kind not in _PRECEDENCE[item.information_kind] for item in observations):
            return self._resolution(
                candidate=candidate,
                status=AuthorityResolutionStatus.INVALID,
                selected_digest=None,
                considered_digests=considered_digests,
                required_authority_ref=None,
                reason_code=ResolutionReasonCode.INADMISSIBLE_SOURCE,
            )

        winners: list[AuthorityObservation] = []
        for information_kind in InformationKind:
            same_kind = tuple(
                item for item in observations if item.information_kind is information_kind
            )
            if not same_kind:
                continue
            ranking = _PRECEDENCE[information_kind]
            highest_rank = min(ranking.index(item.source_kind) for item in same_kind)
            highest = tuple(
                item for item in same_kind if ranking.index(item.source_kind) == highest_rank
            )
            if len({item.assertion for item in highest}) != 1:
                return self._resolution(
                    candidate=candidate,
                    status=AuthorityResolutionStatus.UNRESOLVED,
                    selected_digest=None,
                    considered_digests=considered_digests,
                    required_authority_ref=_BUSINESS_OWNER,
                    reason_code=ResolutionReasonCode.SAME_RANK_DISAGREEMENT,
                )
            winners.append(min(highest, key=digest))

        for group in _CONTRADICTION_GROUPS:
            competing = tuple(item for item in winners if item.information_kind in group)
            if len({item.assertion for item in competing}) > 1:
                return self._resolution(
                    candidate=candidate,
                    status=AuthorityResolutionStatus.UNRESOLVED,
                    selected_digest=None,
                    considered_digests=considered_digests,
                    required_authority_ref=_BUSINESS_OWNER,
                    reason_code=ResolutionReasonCode.CROSS_KIND_CONFLICT,
                )

        # Bind the winner from the aspect the candidate itself asserts. Selecting merely
        # from its contradiction group is not enough: imported_classification shares a
        # group with business_meaning, so an entity candidate could record the catalog's
        # classification as the authority for a claim section 9.2 assigns to the approved
        # business owner. The assertions agree by this point, so only the attribution
        # would be wrong -- which is the part this subsystem exists to get right.
        governing_kind = _CANDIDATE_GOVERNING_KIND[candidate.kind]
        governing_group = _contradiction_group(governing_kind)
        preferred = (
            tuple(item for item in winners if item.information_kind is governing_kind)
            or tuple(item for item in winners if item.information_kind in governing_group)
            or winners
        )
        selected = min(preferred, key=digest)
        return self._resolution(
            candidate=candidate,
            status=AuthorityResolutionStatus.RESOLVED,
            selected_digest=digest(selected),
            considered_digests=considered_digests,
            required_authority_ref=None,
            reason_code=ResolutionReasonCode.RESOLVED_BY_PRECEDENCE,
        )

    def _resolution(
        self,
        *,
        candidate: SemanticCandidate,
        status: AuthorityResolutionStatus,
        selected_digest: str | None,
        considered_digests: tuple[str, ...],
        required_authority_ref: str | None,
        reason_code: ResolutionReasonCode,
    ) -> AuthorityResolution:
        identity_material = {
            "domain": "heinzel-authority-resolution-v1",
            "tenant_id": self._tenant_id,
            "candidate_id": candidate.candidate_id,
            "status": status,
            "selected_observation_digest": selected_digest,
            "considered_observation_digests": considered_digests,
            "required_authority_ref": required_authority_ref,
            "reason_code": reason_code,
        }
        return AuthorityResolution(
            resolution_id=f"authres-{digest(identity_material)[:24]}",
            tenant_id=self._tenant_id,
            candidate_id=candidate.candidate_id,
            status=status,
            selected_observation_digest=selected_digest,
            considered_observation_digests=considered_digests,
            required_authority_ref=required_authority_ref,
            reason_code=reason_code,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("clock must return a timezone-aware UTC timestamp")
    return value.astimezone(UTC)
