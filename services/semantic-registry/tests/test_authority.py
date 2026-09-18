from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import pytest
from heinzel_contract_model import InformationKind, SemanticRuleKind, canonical_bytes, digest
from heinzel_semantic_registry import (
    AuthorityObservation,
    AuthorityResolution,
    AuthorityResolutionStatus,
    AuthorityResolver,
    AuthoritySourceKind,
    CandidateKind,
    CandidateProvenance,
    ResolutionReasonCode,
    SemanticCandidate,
    SemanticPersistenceError,
    SQLiteSemanticRepository,
)
from heinzel_semantic_registry.authority import _PRECEDENCE, _utc
from heinzel_semantic_registry.repository import _CandidateDraft
from pydantic import ValidationError

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
resolver = AuthorityResolver(
    tenant_id="tenant-a",
    clock=lambda: NOW,
    candidate_ownership_verifier=lambda tenant_id, candidate: (
        tenant_id == "tenant-a" and candidate.candidate_id.startswith("semcand:tenant-a:")
    ),
)

EXPECTED_PRECEDENCE = {
    InformationKind.BUSINESS_MEANING: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
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
    InformationKind.IMPORTED_CLASSIFICATION: (
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


def entity_candidate(name: str, proposed_definition: str) -> SemanticCandidate:
    return SemanticCandidate(
        candidate_id=f"semcand:tenant-a:{name.lower()}",
        kind=CandidateKind.ENTITY,
        name=name,
        proposed_definition=proposed_definition,
        related_refs=(),
        provenance=CandidateProvenance(
            source_kind="narrative_marker",
            source_digest="a" * 64,
            source_path="narrative.md",
            narrative_line_start=1,
            narrative_line_end=1,
        ),
        confidence=Decimal("1"),
    )


def observation(
    *,
    information_kind: InformationKind,
    source_kind: AuthoritySourceKind,
    subject_ref: str,
    assertion: str,
    tenant_id: str = "tenant-a",
    authority_ref: str | None = None,
    observed_at: datetime = NOW - timedelta(hours=1),
    valid_until: datetime = NOW + timedelta(hours=1),
) -> AuthorityObservation:
    resolved_authority_ref = authority_ref or f"authority:{source_kind.value}"
    observed_digest = digest(
        {
            "information_kind": information_kind,
            "source_kind": source_kind,
            "subject_ref": subject_ref,
            "assertion": assertion,
            "authority_ref": resolved_authority_ref,
        }
    )
    return AuthorityObservation(
        observation_id=f"authobs:{observed_digest[:24]}",
        tenant_id=tenant_id,
        information_kind=information_kind,
        source_kind=source_kind,
        subject_ref=subject_ref,
        assertion=assertion,
        authority_ref=resolved_authority_ref,
        observed_digest=observed_digest,
        observed_at=observed_at,
        valid_until=valid_until,
    )


def record_observation(
    repository: SQLiteSemanticRepository,
    *,
    assertion: str = "entity with its own lifecycle",
    tenant_id: str = "tenant-a",
) -> AuthorityObservation:
    return repository.record_observation(
        tenant_id=tenant_id,
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion=assertion,
        authority_ref="package:bpp-revenue-to-cash:1",
        observed_digest=digest({"tenant_id": tenant_id, "assertion": assertion}),
        observed_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
    )


def persist_refund_candidate(
    repository: SQLiteSemanticRepository,
    *,
    tenant_id: str,
) -> SemanticCandidate:
    return repository._materialize(
        tenant_id=tenant_id,
        package_id="bpp-revenue-to-cash",
        package_version=1,
        original_digest=digest({"tenant_id": tenant_id, "artifact": "original"}),
        manifest_digest=digest({"tenant_id": tenant_id, "artifact": "manifest"}),
        extractor_id="heinzel-bounded-markdown",
        extractor_version="1.0.0",
        candidates=(
            _CandidateDraft(
                kind=CandidateKind.ENTITY,
                name="Refund",
                proposed_definition="A repayment with its own lifecycle.",
                related_refs=(),
                provenance=CandidateProvenance(
                    source_kind="manifest",
                    source_digest=digest({"tenant_id": tenant_id, "source": "manifest"}),
                    source_path="manifest.entities[0]",
                ),
                confidence=Decimal("1"),
            ),
        ),
        unresolved_questions=(),
        created_at=NOW,
    ).candidates[0]


class FaultingObservationConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO authority_observations"):
            raise sqlite3.OperationalError("authority observation write failed")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def test_cross_kind_refund_conflict_escalates_to_business_owner() -> None:
    candidate = entity_candidate("Refund", "A business entity with its own lifecycle")
    # The package observation is essential. Without it there is no admitted
    # business-meaning authority at all, and the assertion below would pass because
    # nothing was resolvable rather than because two kinds disagreed.
    package = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="entity with its own lifecycle",
    )
    catalog = observation(
        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="attribute of Invoice",
    )

    resolution = resolver.resolve(candidate=candidate, observations=(package, catalog))

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.required_authority_ref == "role:business_owner"
    assert resolution.reason_code == "cross_kind_conflict"
    assert set(resolution.considered_observation_digests) == {
        digest(package),
        digest(catalog),
    }


def test_absent_authority_is_not_reported_as_a_cross_kind_conflict() -> None:
    # Distinguishes the two ways resolution can fail, so the test above cannot pass
    # for the wrong reason.
    candidate = entity_candidate("Refund", "A business entity with its own lifecycle")

    resolution = resolver.resolve(candidate=candidate, observations=())

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.reason_code == "no_admitted_authority"
    assert resolution.required_authority_ref == "role:business_owner"


def test_resolver_requires_a_nonempty_tenant() -> None:
    with pytest.raises(ValueError, match=r"^tenant_id must not be empty$"):
        AuthorityResolver(
            tenant_id="",
            clock=lambda: NOW,
            candidate_ownership_verifier=lambda tenant_id, candidate: True,
        )


def test_authority_vocabularies_and_every_precedence_entry_are_explicit() -> None:
    assert {member.value for member in InformationKind} == {
        "business_meaning",
        "process_semantics",
        "imported_glossary",
        "imported_classification",
        "identity",
        "relationship",
        "metric",
        "integrity_constraint",
    }
    assert {member.value for member in SemanticRuleKind} == {
        "identity",
        "relationship",
        "transition",
        "integrity_constraint",
        "metric",
    }
    assert _PRECEDENCE == EXPECTED_PRECEDENCE
    assert set(_PRECEDENCE) == set(InformationKind)


def test_every_resolution_reason_code_is_explicit() -> None:
    assert {member.value for member in ResolutionReasonCode} == {
        "resolved_by_precedence",
        "cross_kind_conflict",
        "same_rank_disagreement",
        "no_admitted_authority",
        "expired_observation",
        "inadmissible_source",
    }


@pytest.mark.parametrize(("information_kind", "ranking"), EXPECTED_PRECEDENCE.items())
def test_every_information_kind_resolves_at_its_exact_highest_precedence(
    information_kind: InformationKind,
    ranking: tuple[AuthoritySourceKind, ...],
) -> None:
    evidence = tuple(
        observation(
            information_kind=information_kind,
            source_kind=source_kind,
            subject_ref="Refund",
            assertion=f"assertion from {source_kind.value}",
        )
        for source_kind in reversed(ranking)
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=evidence,
    )

    expected = next(item for item in evidence if item.source_kind is ranking[0])
    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.reason_code is ResolutionReasonCode.RESOLVED_BY_PRECEDENCE
    assert resolution.selected_observation_digest == digest(expected)
    assert set(resolution.considered_observation_digests) == {digest(item) for item in evidence}


def test_same_rank_agreement_resolves_deterministically() -> None:
    first = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="governed repayment",
        authority_ref="owner:finance",
    )
    second = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="governed repayment",
        authority_ref="owner:revenue",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(second, first),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.selected_observation_digest == min(digest(first), digest(second))
    assert resolution.considered_observation_digests == tuple(
        sorted((digest(first), digest(second)))
    )


def test_resolution_binds_canonical_persisted_observation_artifacts() -> None:
    current = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(current,),
    )

    assert resolution.selected_observation_digest == digest(current)
    assert resolution.considered_observation_digests == (digest(current),)


def test_same_rank_disagreement_is_unresolved() -> None:
    first = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="entity",
        authority_ref="owner:finance",
    )
    second = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="invoice attribute",
        authority_ref="owner:revenue",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(first, second),
    )

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.reason_code is ResolutionReasonCode.SAME_RANK_DISAGREEMENT
    assert resolution.required_authority_ref == "role:business_owner"
    assert set(resolution.considered_observation_digests) == {
        digest(first),
        digest(second),
    }


def test_cross_kind_agreement_resolves_without_creating_global_precedence() -> None:
    meaning = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    classification = observation(
        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="governed repayment",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(classification, meaning),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    # Both agree, so only attribution is at stake: an entity candidate is a
    # business-meaning claim, and the authority for business meaning is the approved
    # business owner, never the declared catalog authority.
    assert resolution.selected_observation_digest == digest(meaning)
    assert set(resolution.considered_observation_digests) == {
        digest(meaning),
        digest(classification),
    }


def test_higher_precedence_same_kind_observation_wins_exactly() -> None:
    package = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="invoice attribute",
    )
    approved = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        subject_ref="Refund",
        assertion="governed repayment",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(package, approved),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.selected_observation_digest == digest(approved)
    assert resolution.considered_observation_digests == tuple(
        sorted((digest(package), digest(approved)))
    )


@pytest.mark.parametrize(
    "valid_until",
    (NOW - timedelta(microseconds=1), NOW),
)
def test_expired_observation_is_invalid_at_and_before_the_clock(
    valid_until: datetime,
) -> None:
    expired = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
        observed_at=NOW - timedelta(hours=1),
        valid_until=valid_until,
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(expired,),
    )

    assert resolution.status is AuthorityResolutionStatus.INVALID
    assert resolution.reason_code is ResolutionReasonCode.EXPIRED_OBSERVATION
    assert resolution.selected_observation_digest is None


def test_observation_with_future_expiry_remains_admitted() -> None:
    current = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
        valid_until=NOW + timedelta(microseconds=1),
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(current,),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.selected_observation_digest == digest(current)


def test_inadmissible_source_is_invalid_and_retains_all_evidence() -> None:
    inadmissible = observation(
        information_kind=InformationKind.IMPORTED_GLOSSARY,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    admitted = observation(
        information_kind=InformationKind.IMPORTED_GLOSSARY,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="governed repayment",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(admitted, inadmissible),
    )

    assert resolution.status is AuthorityResolutionStatus.INVALID
    assert resolution.reason_code is ResolutionReasonCode.INADMISSIBLE_SOURCE
    assert set(resolution.considered_observation_digests) == {
        digest(inadmissible),
        digest(admitted),
    }


def test_candidate_proposal_is_never_used_as_authority() -> None:
    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "governed repayment"),
        observations=(),
    )

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.selected_observation_digest is None
    assert resolution.considered_observation_digests == ()


def test_resolver_rejects_observations_from_another_tenant() -> None:
    foreign = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
        tenant_id="tenant-b",
    )

    with pytest.raises(
        ValueError,
        match=r"^observation tenant does not match resolver tenant$",
    ):
        resolver.resolve(
            candidate=entity_candidate("Refund", "candidate proposal"),
            observations=(foreign,),
        )


def test_resolver_rejects_a_candidate_not_owned_by_the_resolver_tenant() -> None:
    foreign_candidate = entity_candidate("Refund", "candidate proposal").model_copy(
        update={"candidate_id": "semcand:tenant-b:refund"}
    )

    with pytest.raises(
        ValueError,
        match=r"^candidate is not owned by resolver tenant$",
    ):
        resolver.resolve(candidate=foreign_candidate, observations=())


def test_repository_backed_resolver_accepts_exact_persisted_candidate_payload() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    candidate = persist_refund_candidate(repository, tenant_id="tenant-a")
    repository_resolver = AuthorityResolver(
        tenant_id="tenant-a",
        clock=lambda: NOW,
        candidate_ownership_verifier=repository.owns_candidate,
    )

    resolution = repository_resolver.resolve(candidate=candidate, observations=())

    assert resolution.candidate_id == candidate.candidate_id
    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED


def test_repository_backed_resolver_denies_candidate_from_foreign_tenant() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    candidate = persist_refund_candidate(repository, tenant_id="tenant-b")
    repository_resolver = AuthorityResolver(
        tenant_id="tenant-a",
        clock=lambda: NOW,
        candidate_ownership_verifier=repository.owns_candidate,
    )

    with pytest.raises(ValueError, match=r"^candidate is not owned by resolver tenant$"):
        repository_resolver.resolve(candidate=candidate, observations=())


def test_repository_backed_resolver_denies_same_id_with_altered_payload() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    candidate = persist_refund_candidate(repository, tenant_id="tenant-a")
    altered = candidate.model_copy(update={"name": "Invoice"})
    repository_resolver = AuthorityResolver(
        tenant_id="tenant-a",
        clock=lambda: NOW,
        candidate_ownership_verifier=repository.owns_candidate,
    )

    with pytest.raises(ValueError, match=r"^candidate is not owned by resolver tenant$"):
        repository_resolver.resolve(candidate=altered, observations=())


def test_resolver_rejects_observation_for_a_different_candidate_subject() -> None:
    mismatched = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Invoice",
        assertion="governed repayment",
    )

    with pytest.raises(
        ValueError,
        match=r"^observation subject does not match candidate name$",
    ):
        resolver.resolve(
            candidate=entity_candidate("Refund", "candidate proposal"),
            observations=(mismatched,),
        )


def test_resolver_rejects_an_observation_from_the_future() -> None:
    future = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
        observed_at=NOW + timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match=r"^observation observed_at is in the future$"):
        resolver.resolve(
            candidate=entity_candidate("Refund", "candidate proposal"),
            observations=(future,),
        )


def test_resolver_admits_an_observation_recorded_at_the_current_time() -> None:
    current = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
        observed_at=NOW,
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(current,),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.selected_observation_digest == digest(current)


@pytest.mark.parametrize(
    "clock_value",
    (
        datetime(2026, 8, 19, 12),
        datetime(2026, 8, 19, 13, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_resolver_rejects_a_clock_that_is_not_timezone_aware_utc(
    clock_value: datetime,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"^clock must return a timezone-aware UTC timestamp$",
    ):
        AuthorityResolver(
            tenant_id="tenant-a",
            clock=lambda: clock_value,
            candidate_ownership_verifier=lambda tenant_id, candidate: (
                tenant_id == "tenant-a" and candidate.candidate_id.startswith("semcand:tenant-a:")
            ),
        ).resolve(
            candidate=entity_candidate("Refund", "candidate proposal"),
            observations=(
                observation(
                    information_kind=InformationKind.BUSINESS_MEANING,
                    source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
                    subject_ref="Refund",
                    assertion="governed repayment",
                ),
            ),
        )


def test_utc_normalization_uses_the_canonical_utc_timezone() -> None:
    zero_offset = timezone(timedelta(0), name="source-zero-offset")

    normalized = _utc(datetime(2026, 8, 19, 12, tzinfo=zero_offset))

    assert normalized.tzinfo is UTC


def test_resolution_identity_is_deterministic_across_input_order() -> None:
    package = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    owner = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    candidate = entity_candidate("Refund", "candidate proposal")

    forward = resolver.resolve(candidate=candidate, observations=(package, owner))
    reverse = resolver.resolve(candidate=candidate, observations=(owner, package))

    assert reverse == forward
    expected_identity_material = {
        "domain": "heinzel-authority-resolution-v1",
        "tenant_id": "tenant-a",
        "candidate_id": candidate.candidate_id,
        "status": AuthorityResolutionStatus.RESOLVED,
        "selected_observation_digest": digest(owner),
        "considered_observation_digests": tuple(sorted((digest(package), digest(owner)))),
        "required_authority_ref": None,
        "reason_code": ResolutionReasonCode.RESOLVED_BY_PRECEDENCE,
    }
    assert forward.resolution_id == f"authres-{digest(expected_identity_material)[:24]}"
    assert forward.tenant_id == "tenant-a"


def test_authority_artifacts_are_strict_frozen_and_require_utc_windows() -> None:
    current = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )

    with pytest.raises(ValidationError):
        AuthorityObservation.model_validate(current.model_dump() | {"candidate": "not authority"})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        AuthorityObservation.model_validate(
            current.model_dump() | {"observed_at": datetime(2026, 8, 19, 11)}
        )
    with pytest.raises(ValidationError, match="valid_until must be later than observed_at"):
        AuthorityObservation.model_validate(
            current.model_dump() | {"valid_until": current.observed_at}
        )

    field_name = "assertion"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(current, field_name, "changed")


def test_resolution_artifact_rejects_unknown_fields() -> None:
    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(),
    )

    with pytest.raises(ValidationError):
        AuthorityResolution.model_validate(resolution.model_dump() | {"winner": "candidate"})


def test_resolution_artifact_rejects_duplicate_considered_digests() -> None:
    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(),
    )

    with pytest.raises(ValidationError, match="unique"):
        AuthorityResolution.model_validate(
            resolution.model_dump() | {"considered_observation_digests": ("a" * 64, "a" * 64)}
        )


def test_resolution_artifact_rejects_selected_digest_not_considered() -> None:
    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(),
    )

    with pytest.raises(ValidationError, match="considered"):
        AuthorityResolution.model_validate(
            resolution.model_dump()
            | {
                "selected_observation_digest": "b" * 64,
                "considered_observation_digests": ("a" * 64,),
            }
        )


def test_observation_repository_replays_exact_material_and_appends_changes() -> None:
    repository = SQLiteSemanticRepository(":memory:")

    first = record_observation(repository)
    replay = record_observation(repository)
    changed = record_observation(repository, assertion="approved governed repayment")

    assert replay == first
    assert changed.observation_id != first.observation_id
    assert repository.load_observation("tenant-a", first.observation_id) == first
    assert repository.load_observation("tenant-a", changed.observation_id) == changed


def test_observation_identity_is_deterministic_per_tenant_sequence() -> None:
    first_repository = SQLiteSemanticRepository(":memory:")
    second_repository = SQLiteSemanticRepository(":memory:")

    first = record_observation(first_repository)
    second = record_observation(second_repository)
    other_tenant = record_observation(second_repository, tenant_id="tenant-b")

    assert second.observation_id == first.observation_id
    assert other_tenant.observation_id != first.observation_id


def test_observation_lookup_denies_cross_tenant_access_before_deserialization() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    foreign = record_observation(repository, tenant_id="tenant-b")
    repository._connection.execute(
        "UPDATE authority_observations SET payload = ? WHERE tenant_id = ? AND observation_id = ?",
        (b"not-json", "tenant-b", foreign.observation_id),
    )
    repository._connection.commit()

    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.load_observation("tenant-a", foreign.observation_id)


def test_failed_observation_append_rolls_back_without_burning_identity() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original_connection = repository._connection
    baseline_repository = SQLiteSemanticRepository(":memory:")
    expected = record_observation(baseline_repository)

    repository._connection = cast(
        sqlite3.Connection,
        FaultingObservationConnection(original_connection),
    )
    with pytest.raises(SemanticPersistenceError, match="record authority observation"):
        record_observation(repository)

    repository._connection = original_connection
    recovered = record_observation(repository)

    assert recovered.observation_id == expected.observation_id


def test_observation_payload_round_trips_as_canonical_bytes() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    stored = record_observation(repository)

    row = repository._connection.execute(
        "SELECT payload FROM authority_observations WHERE tenant_id = ? AND observation_id = ?",
        (stored.tenant_id, stored.observation_id),
    ).fetchone()

    assert row is not None
    assert bytes(row[0]) == canonical_bytes(stored)


def test_glossary_definition_does_not_contradict_a_structural_claim() -> None:
    # The common case for a tenant that already runs OpenMetadata: the package states
    # what Refund *is*, the glossary states what the word means. Neither disagrees, so
    # requiring one identical string would escalate every such candidate to its owner.
    meaning = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="an entity with its own lifecycle",
    )
    glossary = observation(
        information_kind=InformationKind.IMPORTED_GLOSSARY,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="money returned to a customer",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(glossary, meaning),
    )

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.reason_code is ResolutionReasonCode.RESOLVED_BY_PRECEDENCE
    # The structural claim governs an entity candidate, never the glossary prose.
    assert resolution.selected_observation_digest == digest(meaning)
    assert set(resolution.considered_observation_digests) == {digest(meaning), digest(glossary)}


def test_classification_still_contradicts_business_meaning() -> None:
    # The narrowing above must not weaken the conflict it was carved out of.
    meaning = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="an entity with its own lifecycle",
    )
    classification = observation(
        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="attribute of Invoice",
    )

    resolution = resolver.resolve(
        candidate=entity_candidate("Refund", "candidate proposal"),
        observations=(meaning, classification),
    )

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.reason_code is ResolutionReasonCode.CROSS_KIND_CONFLICT


def test_contradiction_groups_partition_every_information_kind_exactly_once() -> None:
    from heinzel_semantic_registry.authority import _CONTRADICTION_GROUPS

    covered = [kind for group in _CONTRADICTION_GROUPS for kind in group]

    assert sorted(covered, key=lambda kind: kind.value) == sorted(
        InformationKind, key=lambda kind: kind.value
    )
    assert len(covered) == len(set(covered))


def test_every_candidate_kind_declares_a_governing_information_kind() -> None:
    from heinzel_semantic_registry.authority import _CANDIDATE_GOVERNING_KIND

    assert set(_CANDIDATE_GOVERNING_KIND) == set(CandidateKind)


def test_classification_candidate_binds_the_catalog_observation_as_authority() -> None:
    # The mirror of the entity case: a classification candidate IS an
    # imported_classification claim, so the catalog observation is the right authority
    # even though it shares a contradiction group with business meaning.
    meaning = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    classification = observation(
        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="governed repayment",
    )
    candidate = SemanticCandidate(
        candidate_id="semcand:tenant-a:refund",
        kind=CandidateKind.CLASSIFICATION,
        name="Refund",
        proposed_definition="candidate proposal",
        related_refs=(),
        provenance=CandidateProvenance(
            source_kind="narrative_marker",
            source_digest="a" * 64,
            source_path="narrative.md",
            narrative_line_start=1,
            narrative_line_end=1,
        ),
        confidence=Decimal("1"),
    )

    resolution = resolver.resolve(candidate=candidate, observations=(meaning, classification))

    assert resolution.status is AuthorityResolutionStatus.RESOLVED
    assert resolution.selected_observation_digest == digest(classification)
