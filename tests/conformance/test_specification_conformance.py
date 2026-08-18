"""Pin the managed-platform addendum to the code that implements it.

The addendum is design authority: sections 6.2, 6.4.1, and 13.3.1 state a field
shape and two transition tables that services must honour. Nothing else stops a
table being edited on one side only, and a specification that quietly disagrees
with the code is worse than one that says nothing, because it is still trusted.
"""

from __future__ import annotations

import re
from pathlib import Path

from pillarmesh_request_management.service import _TRANSITIONS as REQUEST_TRANSITIONS
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
