"""Find internal material in the public tree.

Heinzel publishes no internal documents, account names, milestone names or personal addresses.
This module lists the tracked text files and reports every forbidden term, after removing the few
places where the company name is allowed to appear.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The company may appear only as the copyright holder, the GitHub organization, the container
# registry path and its web domain. These exact spellings are removed before scanning.
ALLOWED_COMPANY_REFERENCES = (
    "Copyright 2026 PillarMesh",
    "github.com/PillarMesh/",
    "ghcr.io/pillarmesh/",
    "pillarmesh.com",
    "a product of PillarMesh",
)

# The release checks define the forbidden terms, so they are the one place those terms may appear.
EXCLUDED_PREFIXES = ("tests/release/",)

FORBIDDEN_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("company name outside allowed references", re.compile(r"pillar\s*mesh", re.IGNORECASE)),
    ("internal planning tool", re.compile(r"superpowers", re.IGNORECASE)),
    ("personal account", re.compile(r"ks2002119|celeredge|yeskay", re.IGNORECASE)),
    ("internal milestone", re.compile(r"(?i)(?<![a-z0-9])plan[\s_-]?[234][ab]?(?![0-9])")),
    ("internal milestone", re.compile(r"\bGate [A-Z]\b")),
    ("internal milestone", re.compile(r"(?i)(?<![a-z0-9])m[0-8](?![0-9a-z])")),
    ("internal process", re.compile(r"(?i)acceptance ledger|advisory pre-?review")),
    (
        "personal email",
        re.compile(
            r"[A-Za-z0-9._%+-]+@(?!pillarmesh\.com|example\.(?:com|org|net)\b)"
            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str
    text: str


def tracked_text_files(root: Path = ROOT) -> tuple[Path, ...]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.split(b"\0")
    files: list[Path] = []
    for raw in listed:
        if not raw:
            continue
        path = root / raw.decode()
        if not path.is_file():
            continue
        head = path.read_bytes()[:8192]
        if b"\0" in head:
            continue
        files.append(path)
    return tuple(files)


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        cleaned = line
        for allowed in ALLOWED_COMPANY_REFERENCES:
            cleaned = cleaned.replace(allowed, "")
        for reason, pattern in FORBIDDEN_TERMS:
            if pattern.search(cleaned):
                findings.append(Finding(path, number, reason, line.strip()[:160]))
    return findings


def scan_tree(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_text_files(root):
        relative = str(path.relative_to(root))
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(relative, text))
    return findings


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
