from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args, get_type_hints

import heinzel_semantic_registry as semantic_registry
import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_semantic_registry import (
    CandidateKind,
    CandidateProvenance,
    SemanticCandidate,
    SemanticCandidateSet,
    SemanticRepository,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def candidate_set_payload() -> dict[str, object]:
    return {
        "set_id": "semset:tenant-a:1",
        "tenant_id": "tenant-a",
        "revision": 1,
        "package_id": "bpp-revenue-to-cash",
        "package_version": 1,
        "original_digest": "a" * 64,
        "manifest_digest": "b" * 64,
        "extractor_id": "heinzel-bounded-markdown",
        "extractor_version": "1.0.0",
        "candidates": (
            SemanticCandidate(
                candidate_id="semcand:tenant-a:1:entity:1",
                kind=CandidateKind.ENTITY,
                name="Refund",
                proposed_definition="A repayment with its own lifecycle.",
                related_refs=(),
                provenance=CandidateProvenance(
                    source_kind="narrative_marker",
                    source_digest="a" * 64,
                    source_path="narrative.md",
                    narrative_line_start=14,
                    narrative_line_end=14,
                ),
                confidence=Decimal("1"),
            ),
        ),
        "unresolved_questions": (),
        "created_at": NOW,
    }


def test_candidate_set_fields_match_the_addendum_order() -> None:
    assert tuple(SemanticCandidateSet.model_fields) == (
        "schema_version",
        "set_id",
        "tenant_id",
        "revision",
        "package_id",
        "package_version",
        "original_digest",
        "manifest_digest",
        "extractor_id",
        "extractor_version",
        "candidates",
        "unresolved_questions",
        "created_at",
    )


def test_candidate_contracts_are_strict_and_require_utc_record_time() -> None:
    with pytest.raises(ValidationError):
        CandidateProvenance.model_validate(
            {
                "source_kind": "manifest",
                "source_digest": "a" * 64,
                "source_path": "manifest.entities[0]",
                "provider_id": "must-not-enter-semantic-registry",
            }
        )
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        SemanticCandidateSet.model_validate(
            candidate_set_payload() | {"created_at": datetime(2026, 8, 19, 12)}
        )

    candidate_set = SemanticCandidateSet.model_validate(candidate_set_payload())
    field_name = "revision"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(candidate_set, field_name, 2)


def test_narrative_provenance_requires_a_complete_ordered_line_range() -> None:
    provenance = {
        "source_kind": "narrative_marker",
        "source_digest": "a" * 64,
        "source_path": "narrative.md",
    }

    with pytest.raises(ValidationError, match="line range"):
        CandidateProvenance.model_validate(provenance)
    with pytest.raises(ValidationError, match="line range"):
        CandidateProvenance.model_validate(
            provenance | {"narrative_line_start": 14, "narrative_line_end": 13}
        )


def test_candidate_set_uses_canonical_digest_compatible_serialization() -> None:
    candidate_set = SemanticCandidateSet.model_validate(candidate_set_payload())
    payload = canonical_bytes(candidate_set)

    assert SemanticCandidateSet.model_validate_json(payload) == candidate_set
    assert hashlib.sha256(payload).hexdigest() == digest(candidate_set)


def test_public_repository_protocol_has_no_private_draft_materialization_surface() -> None:
    assert not hasattr(SemanticRepository, "materialize")


def semantic_annotation_private_types(annotation: object) -> tuple[type[object], ...]:
    private_types: list[type[object]] = []
    if (
        isinstance(annotation, type)
        and annotation.__module__.startswith("heinzel_semantic_registry")
        and annotation.__name__.startswith("_")
    ):
        private_types.append(annotation)
    for argument in get_args(annotation):
        private_types.extend(semantic_annotation_private_types(argument))
    return tuple(private_types)


def test_exported_signatures_never_annotate_package_private_types() -> None:
    leaks: list[str] = []
    for export_name in semantic_registry.__all__:
        exported = getattr(semantic_registry, export_name)
        targets: list[tuple[str, object]] = []
        if inspect.isclass(exported):
            targets.append((f"{export_name}.__init__", exported.__init__))
            targets.extend(
                (f"{export_name}.{method_name}", method)
                for method_name, method in exported.__dict__.items()
                if not method_name.startswith("_") and inspect.isfunction(method)
            )
        elif inspect.isfunction(exported):
            targets.append((export_name, exported))
        for target_name, target in targets:
            if not hasattr(target, "__annotations__"):
                continue
            for parameter, annotation in get_type_hints(target).items():
                for private_type in semantic_annotation_private_types(annotation):
                    leaks.append(f"{target_name}.{parameter}: {private_type.__name__}")

    assert leaks == []


def test_exported_repository_annotations_resolve_at_runtime() -> None:
    """Public protocol annotations are part of the runtime-facing API contract."""
    for method_name, method in inspect.getmembers(SemanticRepository, inspect.isfunction):
        if method_name.startswith("_"):
            continue
        assert get_type_hints(method), method_name
