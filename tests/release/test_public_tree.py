"""Find internal material in the public tree.

Heinzel publishes no internal documents, account names, milestone names or personal addresses.
This module lists the tracked text files and reports every forbidden term, after removing the few
places where the company name is allowed to appear.

Only two kinds of pattern live in this file: the company name (outside its allowed references)
and personal email addresses. Everything else that is specific to how this company operates
internally -- account names, tool names, process names, milestone and gate codes -- is loaded at
run time from a private terms file, named by the HEINZEL_PRIVATE_TERMS_FILE environment variable.
That file lives outside the public repository; the release audit supplies its path when it runs
this scanner with enforcement on. Without the variable set, only the generic patterns apply, and
enforcing without the variable set is treated as a configuration error, not a clean tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_Term = tuple[str, "re.Pattern[str]"]
_Terms = tuple[_Term, ...]

# The company name, outside the places it is allowed to appear. Not anchored to word
# boundaries, so it also catches identifiers such as PILLARMESH_STATE_PATH.
_COMPANY_PATTERN = re.compile(r"(?i)pillar[\s_.-]*mesh")

_GENERIC_TERMS: _Terms = (("company name outside allowed references", _COMPANY_PATTERN),)

# Allowed occurrences of the company name are removed from a line, with anchored regexes
# rather than plain substring replacement, before any forbidden pattern is checked. A plain
# substring removal would also delete the "pillarmesh.com" prefix out of a longer, unrelated
# domain such as "pillarmesh.company" or "pillarmesh.com.evil.io", hiding a real hit. The
# lookahead allows a lone sentence-ending period (".", not followed by another domain label)
# and an optional "www." prefix, but still rejects any other subdomain.
_ALLOWED_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Copyright 2026 PillarMesh"),
    re.compile(r"github\.com/PillarMesh/"),
    re.compile(r"ghcr\.io/pillarmesh/"),
    re.compile(r"(?i)(?<![a-z0-9.-])(?:www\.)?pillarmesh\.com(?![a-z0-9-]|\.[a-z0-9])"),
    re.compile(r"a product of PillarMesh"),
)

# A domain is exempt from the personal-email check when its final label (the TLD) is one of
# these reserved, non-routable names.
_RESERVED_EMAIL_TLDS = frozenset({"example", "test", "invalid", "localhost"})

# A domain is also exempt when it is exactly one of these documentation domains, or a
# subdomain of one -- unlike the TLD rule above, this covers "sub.example.org" too.
_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")

# Vendor-shipped default addresses that appear verbatim in third-party config templates; these
# are not personal addresses. open-metadata's docker-compose ships this exact address.
_VENDOR_EMAIL_ALLOWLIST = frozenset({"admin@open-metadata.org"})

# The only company-domain addresses allowed in the public tree. Any other address at this
# domain (e.g. a departed employee's) is still a personal-email finding.
_COMPANY_EMAIL_ALLOWLIST = frozenset({"contact@pillarmesh.com", "karthik@pillarmesh.com"})

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# The file this scanner lives in intentionally contains lines that its own patterns match, to
# prove the patterns work. It is the only file excluded from the whole-tree scan, and only
# because of that; nothing else in the tree gets a pass.
_EXCLUDED_PATH = "tests/release/test_public_tree.py"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str
    text: str


# Reviewed binary files: git path -> sha256 of the full file content. Empty until the release
# audit reviews a binary and lists it here as safe to publish; a changed digest is a finding.
REVIEWED_BINARIES: Mapping[str, str] = {}


def _is_exempt_email(match: re.Match[str], line: str) -> bool:
    text = match.group(0)
    lower = text.lower()
    if lower in _VENDOR_EMAIL_ALLOWLIST or lower in _COMPANY_EMAIL_ALLOWLIST:
        return True
    if lower == "git@github.com" and line[match.end() : match.end() + 1] == ":":
        return True
    domain = lower.split("@", 1)[1]
    if domain.rsplit(".", 1)[-1] in _RESERVED_EMAIL_TLDS:
        return True
    return domain in _EXAMPLE_DOMAINS or any(
        domain.endswith("." + example) for example in _EXAMPLE_DOMAINS
    )


def _has_forbidden_email(line: str) -> bool:
    return any(not _is_exempt_email(match, line) for match in _EMAIL_PATTERN.finditer(line))


def _strip_allowed_references(line: str) -> str:
    cleaned = line
    for pattern in _ALLOWED_REFERENCE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned


def _load_private_terms(path_str: str) -> _Terms:
    path = Path(path_str)
    if not path.is_file():
        raise ValueError(f"private terms file not found: {path_str}")
    terms: list[_Term] = []
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"{path_str}:{number}: expected 'reason<TAB>regex', got: {line!r}")
        reason, _, pattern_text = line.partition("\t")
        reason = reason.strip()
        pattern_text = pattern_text.strip()
        if not reason or not pattern_text:
            raise ValueError(f"{path_str}:{number}: expected 'reason<TAB>regex', got: {line!r}")
        try:
            compiled = re.compile(pattern_text)
        except re.error as error:
            raise ValueError(
                f"{path_str}:{number}: invalid regex {pattern_text!r}: {error}"
            ) from error
        terms.append((reason, compiled))
    if not terms:
        raise ValueError(f"private terms file has no terms: {path_str}")
    return tuple(terms)


def _active_terms() -> _Terms:
    private_path = os.environ.get("HEINZEL_PRIVATE_TERMS_FILE")
    if not private_path:
        return _GENERIC_TERMS
    return _GENERIC_TERMS + _load_private_terms(private_path)


def tracked_files(root: Path = ROOT) -> tuple[str, ...]:
    """Every tracked file's path, exactly as git records it, relative to root."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.split(b"\0")
    relatives: list[str] = []
    for raw in listed:
        if not raw:
            continue
        relative = os.fsdecode(raw)
        if (root / relative).is_file():
            relatives.append(relative)
    return tuple(relatives)


def _scan_line(path: str, number: int, line: str, terms: _Terms) -> list[Finding]:
    cleaned = _strip_allowed_references(line)
    findings: list[Finding] = []
    for reason, pattern in terms:
        if pattern.search(cleaned):
            findings.append(Finding(path, number, reason, line.strip()[:160]))
    # The email check runs on the original line: allowed-reference stripping is only meant to
    # hide legitimate mentions of the company's own domain from the company-name pattern, and
    # must not also hide who an address belongs to from the email pattern.
    if _has_forbidden_email(line):
        findings.append(Finding(path, number, "personal email", line.strip()[:160]))
    return findings


def scan_text(path: str, text: str, terms: _Terms | None = None) -> tuple[Finding, ...]:
    if terms is None:
        terms = _active_terms()
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(_scan_line(path, number, line, terms))
    return tuple(findings)


def scan_tree(root: Path = ROOT, terms: _Terms | None = None) -> tuple[Finding, ...]:
    if terms is None:
        terms = _active_terms()
    findings: list[Finding] = []
    for relative in tracked_files(root):
        if relative == _EXCLUDED_PATH:
            continue
        findings.extend(_scan_line(relative, 0, relative, terms))
        raw = (root / relative).read_bytes()
        if b"\0" in raw[:8192]:
            digest = hashlib.sha256(raw).hexdigest()
            if REVIEWED_BINARIES.get(relative) != digest:
                findings.append(Finding(relative, 0, "unreviewed binary file", digest))
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.extend(scan_text(relative, text, terms))
    return tuple(findings)


# --- generic patterns: company name and personal email -----------------------------------


@pytest.mark.parametrize(
    ("line", "expected_reason"),
    [
        ("import pillarmesh_runtime", "company name outside allowed references"),
        ("PILLARMESH_STATE_PATH=/x", "company name outside allowed references"),
        ("pillar mesh", "company name outside allowed references"),
        ("Pillar-Mesh", "company name outside allowed references"),
        ("pillar_mesh", "company name outside allowed references"),
        ("x@pillarmesh.company", "company name outside allowed references"),
        ("https://pillarmesh.com.evil.io", "company name outside allowed references"),
        ("internal.pillarmesh.com", "company name outside allowed references"),
        ("github.com/PillarMesh/pillarmesh", "company name outside allowed references"),
        ("mail someone@gmail.com", "personal email"),
        ("reachme@testers.co", "personal email"),
    ],
)
def test_generic_terms_catch_each_kind(line: str, expected_reason: str) -> None:
    findings = scan_text("sample.md", line, terms=_GENERIC_TERMS)
    assert findings, line
    assert findings[0].reason == expected_reason


@pytest.mark.parametrize(
    "line",
    [
        "Copyright 2026 PillarMesh",
        "https://github.com/PillarMesh/heinzel/issues",
        "ghcr.io/pillarmesh/heinzel:1.0.0",
        "a product of PillarMesh, built for the community",
        "See pillarmesh.com for details",
        "See pillarmesh.com.",
        "www.pillarmesh.com is the homepage",
        "Contact karthik@pillarmesh.com",
        "user@example.com is a placeholder",
        "foo@bar.example.org is also fine",
        "bounce@mail.invalid is a stand-in address",
        "admin@server.localhost is a stand-in address",
        "clone via git@github.com:some-org/some-repo.git",
        "admin@open-metadata.org ships as a vendor default",
    ],
)
def test_generic_terms_allow_company_references_and_ordinary_words(line: str) -> None:
    assert scan_text("sample.md", line, terms=_GENERIC_TERMS) == (), line


@pytest.mark.parametrize(
    "line",
    [
        "bob@test.acme.io",
        "ceo@example.co.uk",
        "ops@invalid.corp.com",
        "jane.doe@pillarmesh.com",
    ],
)
def test_email_exemptions_still_catch_non_exempt_domains(line: str) -> None:
    findings = scan_text("sample.md", line, terms=_GENERIC_TERMS)
    assert findings, line
    assert findings[0].reason == "personal email"


@pytest.mark.parametrize(
    "line",
    [
        "private@example.test",
        "admin@localhost.invalid",
        "user@example.com",
        "a@sub.example.org",
        "contact@pillarmesh.com",
    ],
)
def test_email_exemptions_allow_reserved_and_company_domains(line: str) -> None:
    assert scan_text("sample.md", line, terms=_GENERIC_TERMS) == (), line


# --- the private terms loader mechanics (stand-in patterns only, never the real ones) ------


def test_the_loader_parses_reason_tab_regex_lines(tmp_path: Path) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text(
        "# a private terms file\n\nstand-in account\tacct-[0-9]+\nstand-in tool\tsuper-widget\n"
    )
    terms = _load_private_terms(str(terms_file))
    assert [(reason, pattern.pattern) for reason, pattern in terms] == [
        ("stand-in account", "acct-[0-9]+"),
        ("stand-in tool", "super-widget"),
    ]


def test_the_loader_allows_multiple_lines_under_the_same_reason(tmp_path: Path) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in widget\twidget-[0-9]+\nstand-in widget\tgadget-[0-9]+\n")
    terms = _load_private_terms(str(terms_file))
    assert [reason for reason, _ in terms] == ["stand-in widget", "stand-in widget"]
    assert scan_text("sample.md", "see widget-1", terms=terms)[0].reason == "stand-in widget"
    assert scan_text("sample.md", "see gadget-2", terms=terms)[0].reason == "stand-in widget"


def test_scan_text_uses_loaded_private_terms(tmp_path: Path) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in account\tacct-[0-9]+\n")
    terms = _GENERIC_TERMS + _load_private_terms(str(terms_file))
    findings = scan_text("sample.md", "use acct-42 now", terms=terms)
    assert findings and findings[0].reason == "stand-in account"


def test_the_loader_raises_if_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _load_private_terms(str(tmp_path / "missing.txt"))


def test_the_loader_raises_if_the_file_is_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    with pytest.raises(ValueError):
        _load_private_terms(str(empty))


def test_the_loader_raises_if_the_file_has_only_comments(tmp_path: Path) -> None:
    only_comments = tmp_path / "comments.txt"
    only_comments.write_text("# nothing here\n\n")
    with pytest.raises(ValueError):
        _load_private_terms(str(only_comments))


def test_the_loader_raises_on_a_malformed_line(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.txt"
    malformed.write_text("no tab on this line\n")
    with pytest.raises(ValueError):
        _load_private_terms(str(malformed))


def test_the_loader_raises_on_an_invalid_regex(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("bad regex\t[unterminated\n")
    with pytest.raises(ValueError):
        _load_private_terms(str(invalid))


def test_active_terms_is_generic_only_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEINZEL_PRIVATE_TERMS_FILE", raising=False)
    assert _active_terms() == _GENERIC_TERMS


def test_active_terms_raises_if_the_env_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError):
        _active_terms()


def test_active_terms_loads_the_env_file_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    terms = _active_terms()
    assert terms[: len(_GENERIC_TERMS)] == _GENERIC_TERMS
    assert terms[-1][0] == "stand-in tool"


# --- the real private terms file's milestone patterns (only when it is configured) --------
#
# These lines are generic milestone/gate shapes, not the account or tool names, so they are
# fine to keep here. What must not live here is a second copy of the real regexes: instead
# these tests load the actual file the release audit will point HEINZEL_PRIVATE_TERMS_FILE at,
# so there is exactly one place the milestone patterns are written down.

_MILESTONE_CATCH_LINES = (
    "Plan 3B evidence",
    "blocked until Gate A closes",
    "the M0 thin thread",
    "gate b",
)

_MILESTONE_NEAR_MISS_LINES = (
    "xplan2",
    "plan20",
    "am0",
    "Gate A1",
    "m0de",
    "gated",
    "feature-gated route",
)

_requires_private_terms_file = pytest.mark.skipif(
    not os.environ.get("HEINZEL_PRIVATE_TERMS_FILE"),
    reason="exercises the real private terms file named by HEINZEL_PRIVATE_TERMS_FILE",
)


@_requires_private_terms_file
@pytest.mark.parametrize("line", _MILESTONE_CATCH_LINES)
def test_the_real_private_terms_catch_the_milestone_shapes(line: str) -> None:
    terms = _load_private_terms(os.environ["HEINZEL_PRIVATE_TERMS_FILE"])
    findings = scan_text("sample.md", line, terms=terms)
    assert findings and findings[0].reason == "internal milestone"


@_requires_private_terms_file
@pytest.mark.parametrize("line", _MILESTONE_NEAR_MISS_LINES)
def test_the_real_private_terms_allow_the_near_misses(line: str) -> None:
    terms = _load_private_terms(os.environ["HEINZEL_PRIVATE_TERMS_FILE"])
    assert scan_text("sample.md", line, terms=terms) == (), line


# --- scan_tree over a real git repository --------------------------------------------------


def test_scan_tree_covers_content_path_and_binary_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    (repo / "clean.txt").write_text("nothing to see here\n")
    (repo / "content_hit.txt").write_text("this mentions pillarmesh directly\n")
    (repo / "pillarmesh-secrets").mkdir()
    (repo / "pillarmesh-secrets" / "notes.txt").write_text("the path itself is the hit\n")

    unreviewed = bytes([0, 1, 2, 3]) + b"unreviewed"
    (repo / "unreviewed.bin").write_bytes(unreviewed)

    reviewed_ok = bytes([0, 4, 5, 6]) + b"reviewed-ok"
    (repo / "reviewed_ok.bin").write_bytes(reviewed_ok)

    reviewed_stale = bytes([0, 7, 8, 9]) + b"reviewed-stale"
    (repo / "reviewed_stale.bin").write_bytes(reviewed_stale)

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "s"],
        cwd=repo,
        check=True,
    )

    reviewed_ok_digest = hashlib.sha256(reviewed_ok).hexdigest()
    monkeypatch.setattr(
        sys.modules[__name__],
        "REVIEWED_BINARIES",
        {
            "reviewed_ok.bin": reviewed_ok_digest,
            "reviewed_stale.bin": "0" * 64,
        },
    )

    findings = scan_tree(root=repo, terms=_GENERIC_TERMS)
    by_path: dict[str, list[Finding]] = {}
    for finding in findings:
        by_path.setdefault(finding.path, []).append(finding)

    assert "clean.txt" not in by_path
    assert by_path["content_hit.txt"][0].reason == "company name outside allowed references"
    assert by_path["content_hit.txt"][0].line == 1
    assert by_path["pillarmesh-secrets/notes.txt"][0].line == 0
    assert by_path["unreviewed.bin"][0].line == 0
    assert by_path["unreviewed.bin"][0].reason == "unreviewed binary file"
    assert "reviewed_ok.bin" not in by_path
    assert by_path["reviewed_stale.bin"][0].line == 0


# --- the enforced whole-tree gate -----------------------------------------------------------


def test_the_public_tree_contains_nothing_internal() -> None:
    if os.environ.get("HEINZEL_ENFORCE_PUBLIC_TREE") != "1":
        pytest.skip("public-tree gate is enforced only when HEINZEL_ENFORCE_PUBLIC_TREE=1")
    if not os.environ.get("HEINZEL_PRIVATE_TERMS_FILE"):
        pytest.fail("HEINZEL_PRIVATE_TERMS_FILE must be set when enforcing")
    assert tracked_files()
    findings = scan_tree()
    report = "\n".join(f"{f.path}:{f.line}: {f.reason}: {f.text}" for f in findings[:200])
    assert findings == (), f"{len(findings)} internal references remain:\n{report}"


def test_the_enforced_gate_fails_closed_without_private_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEINZEL_ENFORCE_PUBLIC_TREE", "1")
    monkeypatch.delenv("HEINZEL_PRIVATE_TERMS_FILE", raising=False)
    with pytest.raises(pytest.fail.Exception, match="HEINZEL_PRIVATE_TERMS_FILE"):
        test_the_public_tree_contains_nothing_internal()
