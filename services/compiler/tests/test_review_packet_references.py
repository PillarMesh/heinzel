"""The independent review packet must only cite files and tests that exist.

A reviewer follows the packet's references to decide precondition 18. A renamed test or a moved
fixture would leave a citation pointing at nothing, and the reviewer would be told evidence exists
that cannot be found. Resolve every repository path and `path::test` reference the packet makes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PACKET = (
    Path(__file__).parents[1]
    / "legality"
    / "product-sql"
    / "reviews"
    / "PRODUCT-SQL-V2-PROJECT-SUM-001-POSTGRESQL-REVIEW-PACKET.md"
)
_REFERENCE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md|json))(?:::([A-Za-z0-9_]+))?`")


def _repository_file(relative: str) -> Path | None:
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / relative
        if candidate.is_file():
            return candidate
    return None


def _defined_functions(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _references() -> tuple[tuple[str, str | None], ...]:
    text = _PACKET.read_text(encoding="utf-8")
    return tuple(
        (match.group(1), match.group(2))
        for match in _REFERENCE.finditer(text)
        if "/" in match.group(1)
    )


def test_the_packet_cites_enough_evidence_to_be_checked() -> None:
    references = _references()

    assert len(references) >= 20
    assert sum(1 for _, test_name in references if test_name) >= 12


def test_every_cited_file_and_test_exists() -> None:
    missing: list[str] = []
    for relative, test_name in _references():
        path = _repository_file(relative)
        if path is None:
            missing.append(relative)
        elif test_name is not None and test_name not in _defined_functions(path):
            missing.append(f"{relative}::{test_name}")

    assert missing == []
