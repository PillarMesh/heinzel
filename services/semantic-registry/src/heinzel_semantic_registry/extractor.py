from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from heinzel_contract_model import digest
from heinzel_contract_service import BusinessProcessManifest, ProcessPackageReceipt

from .models import CandidateKind, CandidateProvenance, SemanticCandidateSet
from .repository import SQLiteSemanticRepository, _CandidateDraft

_DEFINITION = re.compile(
    r"^- (Entity|Event|State|Identity Rule|Integrity Constraint|Classification) "
    r"`([^`]+)`: (.+)$"
)
_RELATIONSHIP = re.compile(r"^- `([^`]+)` --([A-Za-z][A-Za-z0-9_-]*)--> `([^`]+)`$")
_METRIC = re.compile(r"^- `([^`]+)`: (.+)$")
_FENCE_OPEN = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_DEFINITION_KIND = {
    "Entity": CandidateKind.ENTITY,
    "Event": CandidateKind.EVENT,
    "State": CandidateKind.STATE,
    "Identity Rule": CandidateKind.IDENTITY_RULE,
    "Integrity Constraint": CandidateKind.INTEGRITY_CONSTRAINT,
    "Classification": CandidateKind.CLASSIFICATION,
}
_DEFINITION_LABEL = {kind: label for label, kind in _DEFINITION_KIND.items()}


@dataclass(frozen=True, slots=True)
class _ParsedNarrative:
    definitions: tuple[_CandidateDraft, ...]
    relationships: tuple[_CandidateDraft, ...]
    metrics: tuple[_CandidateDraft, ...]
    unresolved_questions: tuple[str, ...]


class DeterministicManifestExtractor:
    def __init__(
        self,
        repository: SQLiteSemanticRepository,
        *,
        extractor_id: str,
        extractor_version: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not extractor_id or not extractor_version:
            raise ValueError("extractor identity and version must not be empty")
        self._repository = repository
        self._extractor_id = extractor_id
        self._extractor_version = extractor_version
        self._clock = clock

    def extract(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        manifest: BusinessProcessManifest,
        original: bytes,
    ) -> SemanticCandidateSet:
        if receipt.tenant_id != tenant_id:
            raise ValueError("receipt tenant does not match extraction tenant")
        if sha256(original).hexdigest() != receipt.original_digest:
            raise ValueError("original digest does not match receipt")
        if digest(manifest) != receipt.manifest_digest:
            raise ValueError("manifest digest does not match receipt")
        try:
            narrative = original.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("original narrative must be valid UTF-8") from error
        parsed = _parse_narrative(narrative, receipt.original_digest)
        manifest_candidates = _manifest_candidates(manifest, receipt.manifest_digest)
        candidates, formation_questions = _form_candidates(manifest_candidates, parsed)
        questions = _deduplicate(
            (
                *manifest.unresolved_questions,
                *formation_questions,
                *parsed.unresolved_questions,
            )
        )
        return self._repository._materialize(
            tenant_id=tenant_id,
            package_id=receipt.package_id,
            package_version=receipt.version,
            original_digest=receipt.original_digest,
            manifest_digest=receipt.manifest_digest,
            extractor_id=self._extractor_id,
            extractor_version=self._extractor_version,
            candidates=candidates,
            unresolved_questions=questions,
            created_at=self._clock(),
        )


def _manifest_candidates(
    manifest: BusinessProcessManifest, manifest_digest: str
) -> tuple[_CandidateDraft, ...]:
    candidates: list[_CandidateDraft] = []
    groups = (
        (CandidateKind.ENTITY, "entities", manifest.entities),
        (CandidateKind.EVENT, "events", manifest.events),
        (CandidateKind.STATE, "states", manifest.states),
        (CandidateKind.INTEGRITY_CONSTRAINT, "rules", manifest.rules),
    )
    for kind, field_name, values in groups:
        for index, name in enumerate(values):
            candidates.append(
                _CandidateDraft(
                    kind=kind,
                    name=name,
                    proposed_definition=None,
                    related_refs=(),
                    provenance=CandidateProvenance(
                        source_kind="manifest",
                        source_digest=manifest_digest,
                        source_path=f"manifest.{field_name}[{index}]",
                    ),
                    confidence=Decimal("1"),
                )
            )
    return _deduplicate_candidates(tuple(candidates))


def _parse_narrative(narrative: str, source_digest: str) -> _ParsedNarrative:
    section: str | None = None
    fence: tuple[str, int] | None = None
    definitions: list[_CandidateDraft] = []
    relationships: list[_CandidateDraft] = []
    metrics: list[_CandidateDraft] = []
    questions: list[str] = []
    for line_number, line in enumerate(narrative.splitlines(), start=1):
        if fence is not None:
            fence_character, minimum_length = fence
            if re.fullmatch(
                rf"[ ]{{0,3}}{re.escape(fence_character)}{{{minimum_length},}}[ \t]*",
                line,
            ):
                fence = None
            continue
        fence = _fence_opener(line)
        if fence is not None:
            continue
        if line.startswith("#"):
            heading = line.removeprefix("## ") if line.startswith("## ") else ""
            section = heading if heading in {"Definitions", "Relationships", "Metrics"} else None
            continue
        if not line.startswith("- ") or section is None:
            continue
        provenance = CandidateProvenance(
            source_kind="narrative_marker",
            source_digest=source_digest,
            source_path="narrative.md",
            narrative_line_start=line_number,
            narrative_line_end=line_number,
        )
        if section == "Definitions":
            match = _DEFINITION.fullmatch(line)
            if match is None:
                if any(line.startswith(f"- {label} ") for label in _DEFINITION_KIND):
                    raise ValueError(f"malformed Definitions marker at line {line_number}")
                questions.append(f"Unrecognized Definitions marker at line {line_number}.")
                continue
            label, name, proposed_definition = match.groups()
            definitions.append(
                _CandidateDraft(
                    kind=_DEFINITION_KIND[label],
                    name=name,
                    proposed_definition=proposed_definition,
                    related_refs=(),
                    provenance=provenance,
                    confidence=Decimal("1"),
                )
            )
        elif section == "Relationships":
            match = _RELATIONSHIP.fullmatch(line)
            if match is None:
                raise ValueError(f"malformed Relationships marker at line {line_number}")
            source, relationship_name, destination = match.groups()
            relationships.append(
                _CandidateDraft(
                    kind=CandidateKind.RELATIONSHIP,
                    name=relationship_name,
                    proposed_definition=None,
                    related_refs=(source, destination),
                    provenance=provenance,
                    confidence=Decimal("1"),
                )
            )
        else:
            match = _METRIC.fullmatch(line)
            if match is None:
                raise ValueError(f"malformed Metrics marker at line {line_number}")
            name, proposed_definition = match.groups()
            metrics.append(
                _CandidateDraft(
                    kind=CandidateKind.METRIC,
                    name=name,
                    proposed_definition=proposed_definition,
                    related_refs=(),
                    provenance=provenance,
                    confidence=Decimal("1"),
                )
            )
    return _ParsedNarrative(
        definitions=_deduplicate_candidates(tuple(definitions)),
        relationships=_deduplicate_candidates(tuple(relationships)),
        metrics=_deduplicate_candidates(tuple(metrics)),
        unresolved_questions=tuple(questions),
    )


def _fence_opener(line: str) -> tuple[str, int] | None:
    content = line.lstrip(" ")
    if len(line) - len(content) > 3:
        return None
    match = _FENCE_OPEN.fullmatch(content)
    if match is None:
        return None
    marker = match.group("fence")
    if marker.startswith("`") and "`" in match.group("info"):
        return None
    return marker[0], len(marker)


def _form_candidates(
    manifest_candidates: tuple[_CandidateDraft, ...], parsed: _ParsedNarrative
) -> tuple[tuple[_CandidateDraft, ...], tuple[str, ...]]:
    definitions: dict[tuple[CandidateKind, str], list[_CandidateDraft]] = {}
    for definition in parsed.definitions:
        definitions.setdefault((definition.kind, definition.name), []).append(definition)
    candidates: list[_CandidateDraft] = []
    questions: list[str] = []
    consumed: set[tuple[CandidateKind, str]] = set()
    for manifest_candidate in manifest_candidates:
        key = manifest_candidate.kind, manifest_candidate.name
        matched = definitions.get(key, [])
        if matched:
            candidates.extend(matched)
            consumed.add(key)
            if len(matched) > 1:
                questions.append(
                    f"Conflicting definitions for {_DEFINITION_LABEL[manifest_candidate.kind]} "
                    f"`{manifest_candidate.name}`."
                )
        else:
            candidates.append(manifest_candidate)
            if manifest_candidate.kind in {
                CandidateKind.ENTITY,
                CandidateKind.EVENT,
                CandidateKind.STATE,
            }:
                questions.append(
                    f"Missing definition for {_DEFINITION_LABEL[manifest_candidate.kind]} "
                    f"`{manifest_candidate.name}`."
                )
    for key, matched in definitions.items():
        if key in consumed:
            continue
        candidates.extend(matched)
        if len(matched) > 1:
            questions.append(f"Conflicting definitions for {_DEFINITION_LABEL[key[0]]} `{key[1]}`.")
    reference_counts: dict[str, int] = {}
    for candidate in candidates:
        reference_counts[candidate.name] = reference_counts.get(candidate.name, 0) + 1
    for relationship in parsed.relationships:
        line_number = relationship.provenance.narrative_line_start
        for reference in relationship.related_refs:
            count = reference_counts.get(reference, 0)
            if count == 0:
                questions.append(
                    f"Unresolved relationship reference `{reference}` at line {line_number}."
                )
            elif count > 1:
                questions.append(
                    f"Ambiguous relationship reference `{reference}` at line {line_number}."
                )
    candidates.extend(parsed.relationships)
    candidates.extend(parsed.metrics)
    return tuple(candidates), tuple(questions)


def _deduplicate_candidates(candidates: tuple[_CandidateDraft, ...]) -> tuple[_CandidateDraft, ...]:
    unique: list[_CandidateDraft] = []
    seen: set[tuple[CandidateKind, str, str | None, tuple[str, ...]]] = set()
    for candidate in candidates:
        key = (
            candidate.kind,
            candidate.name,
            candidate.proposed_definition,
            candidate.related_refs,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique)


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
