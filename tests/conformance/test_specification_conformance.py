"""Pin the managed-platform addendum to the code that implements it.

The addendum is design authority: sections 6.2, 6.4.1, 9.1.1, 9.1.2, and 13.3.1
state field shapes and transition tables that services must honour. Nothing else
stops a table being edited on one side only, and a specification that quietly
disagrees with the code is worse than one that says nothing, because it is still
trusted.
"""

from __future__ import annotations

import re
from pathlib import Path

from pillarmesh_catalog_control import CatalogBinding
from pillarmesh_catalog_control.service import _TRANSITIONS as CATALOG_TRANSITIONS
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    InformationKind,
    ManagedIntegrationContract,
)
from pillarmesh_request_management import DecisionKind
from pillarmesh_request_management.service import _TRANSITIONS as REQUEST_TRANSITIONS
from pillarmesh_semantic_registry import (
    AuthorityObservation,
    OntologyReviewBundle,
    OntologyReviewItem,
    ReviewItemDecision,
    SemanticCandidateSet,
)
from pillarmesh_warehouse_control.models import EngineKind, WarehouseBinding
from pillarmesh_warehouse_control.service import _TRANSITIONS as WAREHOUSE_TRANSITIONS

ADDENDUM = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "architecture"
    / "specifications"
    / "managed-data-engineering-platform-addendum-v0.1.md"
)


def _section(heading: str) -> str:
    text = ADDENDUM.read_text(encoding="utf-8")
    marker = f"\n{heading}"
    assert marker in text, f"addendum no longer contains {heading!r}"
    body = text.split(marker, 1)[1]
    following = re.search(r"\n#{2,4} ", body)
    return body[: following.start()] if following else body


def _fenced_block(heading: str) -> str:
    section = _section(heading)
    assert "```text" in section, f"{heading} no longer contains a fenced block"
    return section.split("```text", 1)[1].split("```", 1)[0]


def _fenced_fields(heading: str) -> list[str]:
    return [line.split()[0] for line in _fenced_block(heading).strip().splitlines() if line.strip()]


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


def test_warehouse_binding_fields_match_section_6_2() -> None:
    documented = [
        line.split()[0] for line in _fenced_block("### 6.2").strip().splitlines() if line.strip()
    ]

    # Order is part of the contract: the section is read as the artifact's shape.
    assert documented == ["WarehouseBinding", *WarehouseBinding.model_fields]


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
    from pillarmesh_semantic_registry.authority import _CONTRADICTION_GROUPS

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
    from pillarmesh_semantic_registry.authority import _CANDIDATE_GOVERNING_KIND

    documented = {
        candidate: information for candidate, information in _markdown_table_rows("### 9.2.2")
    }
    implemented = {
        candidate.value: information.value
        for candidate, information in _CANDIDATE_GOVERNING_KIND.items()
    }

    assert documented == implemented
