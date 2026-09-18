from __future__ import annotations

import os

import pytest

from tests.release.public_tree import scan_text, scan_tree


@pytest.mark.parametrize(
    "line",
    [
        "import pillarmesh_runtime",
        "PILLARMESH_STATE_PATH=/x",
        "see docs/superpowers/plans",
        "run tests.acceptance.run_plan4a",
        "Plan 3B evidence",
        "blocked until Gate A closes",
        "the M0 thin thread",
        "recorded in the acceptance ledger",
        "use account ks2002119",
        "mail someone@gmail.com",
    ],
)
def test_the_scanner_catches_each_kind_of_internal_term(line: str) -> None:
    assert scan_text("sample.md", line), line


@pytest.mark.parametrize(
    "line",
    [
        "Copyright 2026 PillarMesh",
        "https://github.com/PillarMesh/heinzel/issues",
        "ghcr.io/pillarmesh/heinzel:1.0.0",
        "Contact karthik@pillarmesh.com",
        "user@example.com is a placeholder",
        "explain the plan to the team",
        "digest a2b3c4 and m0de are not milestones",
    ],
)
def test_the_scanner_allows_company_references_and_ordinary_words(line: str) -> None:
    assert scan_text("sample.md", line) == [], line


def test_the_public_tree_contains_nothing_internal() -> None:
    if os.environ.get("HEINZEL_ENFORCE_PUBLIC_TREE") != "1":
        pytest.skip("public-tree gate is enforced from Task 20 and in public CI")
    findings = scan_tree()
    report = "\n".join(f"{f.path}:{f.line}: {f.reason}: {f.text}" for f in findings[:200])
    assert findings == [], f"{len(findings)} internal references remain:\n{report}"
