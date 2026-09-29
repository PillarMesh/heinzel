"""Find internal material in the public tree.

Heinzel publishes no internal documents, account names, milestone names or personal addresses.
This module lists the tracked text files and reports every forbidden term, after removing the few
places where the company name is allowed to appear.

Only two kinds of pattern live in this file: the company name (outside its allowed references)
and personal email addresses. Everything else that is specific to how this company operates
internally -- account names, tool names, process names, milestone and gate codes -- is loaded at
run time from a private terms file, named by the HEINZEL_PRIVATE_TERMS_FILE environment variable.
That file lives outside the public repository, so public CI never has it.

The whole-tree gate always runs, with the generic patterns at least, and adds the private terms
whenever the variable is set. The release audit also sets HEINZEL_REQUIRE_PRIVATE_TERMS=1, which
turns a missing variable into a configuration error rather than a generic-only pass.

A second variable, HEINZEL_PRIVATE_TERMS_SHA256, carries the sha256 of that file. Every other
check here accepts a file that is well formed but incomplete, so a terms file that lost lines on
its way in would scan the tree with less coverage than was configured and still report a pass.
Comparing the digest is what makes that a failure. It is verified whenever it is set, and is
required whenever HEINZEL_REQUIRE_PRIVATE_TERMS=1.
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
# boundaries, so it also catches identifiers such as PILLARMESH_EXAMPLE.
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
    # These two exist for package author metadata: a pyproject.toml `authors` entry, covering
    # the TOML inline-table form in each pyproject.toml, and the dict/JSON form used by the
    # release metadata test.
    re.compile(r'"name":\s*"PillarMesh"'),
    re.compile(r'(?<![\w.-])name\s*=\s*"PillarMesh"'),
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

# The sha256 of the private terms file, as 64 hexadecimal characters, carried beside the file
# itself rather than committed here. That file lives outside this repository and changes
# independently of it, so a digest in the tree would need a matching commit on every change to a
# file this repository never sees, and would be stale the first time someone forgot.
_PRIVATE_TERMS_DIGEST_VARIABLE = "HEINZEL_PRIVATE_TERMS_SHA256"

_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

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


def _verify_private_terms_digest(path_str: str, raw: bytes, expected: str) -> None:
    """Fail unless the bytes actually loaded hash to the digest that was configured.

    The structural checks below all pass on a file that is well formed but short. A terms file
    truncated at a line boundary parses cleanly with fewer terms, and one truncated inside a
    pattern can still compile to a valid but weaker regex. In both cases the scan would cover
    less than was configured and the gate would report a pass, which is the one failure this
    gate must never have.
    """
    normalized = expected.strip().lower()
    if not _SHA256_PATTERN.match(normalized):
        raise ValueError(
            f"{_PRIVATE_TERMS_DIGEST_VARIABLE} must be 64 hexadecimal characters, got: {expected!r}"
        )
    actual = hashlib.sha256(raw).hexdigest()
    if actual != normalized:
        raise ValueError(
            f"private terms file does not match {_PRIVATE_TERMS_DIGEST_VARIABLE}: "
            f"{path_str} hashes to {actual}, expected {normalized}. It did not survive "
            f"transport intact, so a gate run against it proves nothing."
        )


def _load_private_terms(path_str: str, *, expected_digest: str | None = None) -> _Terms:
    path = Path(path_str)
    if not path.is_file():
        raise ValueError(f"private terms file not found: {path_str}")
    # Read once, then hash and parse those same bytes. Hashing a second read would leave a
    # window in which the file changed, so a verified digest need not describe what was loaded.
    raw = path.read_bytes()
    if expected_digest is not None:
        _verify_private_terms_digest(path_str, raw, expected_digest)
    terms: list[_Term] = []
    for number, raw_line in enumerate(raw.decode("utf-8").splitlines(), start=1):
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
    expected_digest = os.environ.get(_PRIVATE_TERMS_DIGEST_VARIABLE)
    if not expected_digest and os.environ.get("HEINZEL_REQUIRE_PRIVATE_TERMS") == "1":
        raise ValueError(
            f"{_PRIVATE_TERMS_DIGEST_VARIABLE} must be set when HEINZEL_REQUIRE_PRIVATE_TERMS=1"
        )
    # Pass the value through as read. Collapsing an empty string to None here would let an
    # exported-but-unpopulated variable -- an unprovisioned secret, or `export V="$UNSET"` --
    # read as "no digest configured" and skip verification, which is the fail-open this
    # digest exists to close. Only a genuinely absent variable disables the check.
    return _GENERIC_TERMS + _load_private_terms(private_path, expected_digest=expected_digest)


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
        ("import pillarmesh_widget", "company name outside allowed references"),
        ("PILLARMESH_EXAMPLE=1", "company name outside allowed references"),
        ("Pillar Mesh docs", "company name outside allowed references"),
        ("Pillar-Mesh", "company name outside allowed references"),
        ("pillar_mesh", "company name outside allowed references"),
        ("x@pillarmesh.company", "company name outside allowed references"),
        ("https://pillarmesh.com.evil.io", "company name outside allowed references"),
        ("sub.pillarmesh.com", "company name outside allowed references"),
        ("github.com/PillarMesh/pillarmesh-example", "company name outside allowed references"),
        ('"team": "PillarMesh"', "company name outside allowed references"),
        ('team_name = "PillarMesh"', "company name outside allowed references"),
        ('display_name = "PillarMesh"', "company name outside allowed references"),
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
        '{"name": "PillarMesh", "email": "karthik@pillarmesh.com"}',
        'name = "PillarMesh"',
        'authors = [{ name = "PillarMesh", email = "karthik@pillarmesh.com" }]',
        'authors=[{name="PillarMesh"}]',
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
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError):
        _active_terms()


def test_active_terms_loads_the_env_file_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    terms = _active_terms()
    assert terms[: len(_GENERIC_TERMS)] == _GENERIC_TERMS
    assert terms[-1][0] == "stand-in tool"


# --- scan_tree over a real git repository --------------------------------------------------


def test_scan_tree_covers_content_path_and_binary_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    (repo / "clean.txt").write_text("nothing to see here\n")
    (repo / "content_hit.txt").write_text("this mentions pillarmesh directly\n")
    (repo / "pillarmesh-widget").mkdir()
    (repo / "pillarmesh-widget" / "notes.txt").write_text("the path itself is the hit\n")

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
    assert by_path["pillarmesh-widget/notes.txt"][0].line == 0
    assert by_path["unreviewed.bin"][0].line == 0
    assert by_path["unreviewed.bin"][0].reason == "unreviewed binary file"
    assert "reviewed_ok.bin" not in by_path
    assert by_path["reviewed_stale.bin"][0].line == 0


# --- the whole-tree gate ------------------------------------------------------------------


def test_the_public_tree_contains_nothing_internal() -> None:
    if os.environ.get("HEINZEL_REQUIRE_PRIVATE_TERMS") == "1" and not os.environ.get(
        "HEINZEL_PRIVATE_TERMS_FILE"
    ):
        pytest.fail("HEINZEL_PRIVATE_TERMS_FILE must be set when HEINZEL_REQUIRE_PRIVATE_TERMS=1")
    assert tracked_files()
    findings = scan_tree()
    report = "\n".join(f"{f.path}:{f.line}: {f.reason}: {f.text}" for f in findings[:200])
    assert findings == (), f"{len(findings)} internal references remain:\n{report}"


def test_the_gate_runs_with_generic_terms_only_when_no_private_terms_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv("HEINZEL_PRIVATE_TERMS_FILE", raising=False)
    test_the_public_tree_contains_nothing_internal()


def test_the_gate_fails_closed_when_private_terms_are_required_but_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEINZEL_REQUIRE_PRIVATE_TERMS", "1")
    monkeypatch.delenv("HEINZEL_PRIVATE_TERMS_FILE", raising=False)
    with pytest.raises(pytest.fail.Exception, match="HEINZEL_PRIVATE_TERMS_FILE"):
        test_the_public_tree_contains_nothing_internal()


def test_the_gate_applies_the_private_terms_file_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A stand-in term that matches a tracked path proves the file's terms reach the tree scan.
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in private term\t\\bLICENSE\\b\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    with pytest.raises(AssertionError, match="stand-in private term"):
        test_the_public_tree_contains_nothing_internal()


def test_the_gate_fails_closed_on_a_malformed_private_terms_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    malformed = tmp_path / "malformed.txt"
    malformed.write_text("no tab on this line\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(malformed))
    with pytest.raises(ValueError):
        test_the_public_tree_contains_nothing_internal()


# --- the private terms digest ---------------------------------------------------------------


def _digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_loader_accepts_a_file_whose_digest_matches(tmp_path: Path) -> None:
    content = "stand-in account\tacct-[0-9]+\n"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text(content)
    terms = _load_private_terms(str(terms_file), expected_digest=_digest_of(content))
    assert [reason for reason, _ in terms] == ["stand-in account"]


def test_the_loader_accepts_a_digest_with_surrounding_space_or_capitals(tmp_path: Path) -> None:
    content = "stand-in account\tacct-[0-9]+\n"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text(content)
    padded = f"  {_digest_of(content).upper()}\n"
    assert _load_private_terms(str(terms_file), expected_digest=padded)


def test_the_loader_rejects_a_file_missing_whole_lines_that_would_otherwise_parse(
    tmp_path: Path,
) -> None:
    # The gap the digest closes. Dropping trailing lines leaves a well-formed file, so every
    # other check passes and the scan silently runs with less coverage than was configured.
    full = "stand-in account\tacct-[0-9]+\nstand-in tool\tsuper-widget\n"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in account\tacct-[0-9]+\n")
    assert len(_load_private_terms(str(terms_file))) == 1
    with pytest.raises(ValueError, match="does not match"):
        _load_private_terms(str(terms_file), expected_digest=_digest_of(full))


def test_the_loader_rejects_a_file_cut_inside_a_regex_that_still_compiles(tmp_path: Path) -> None:
    # Truncation inside a pattern can leave a valid but weaker regex, which raises nothing and
    # quietly stops matching the strings the full pattern covered.
    full = "stand-in account\tacct-[0-9]+\n"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in account\tacct-[0-9]")
    assert _load_private_terms(str(terms_file))[0][1].pattern == "acct-[0-9]"
    with pytest.raises(ValueError, match="does not match"):
        _load_private_terms(str(terms_file), expected_digest=_digest_of(full))


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "0" * 63, "0" * 65, "0" * 32])
def test_the_loader_rejects_a_digest_that_is_not_64_hexadecimal_characters(
    tmp_path: Path, bad: str
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in account\tacct-[0-9]+\n")
    with pytest.raises(ValueError, match="64 hexadecimal characters"):
        _load_private_terms(str(terms_file), expected_digest=bad)


def test_active_terms_requires_a_digest_when_private_terms_are_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.setenv("HEINZEL_REQUIRE_PRIVATE_TERMS", "1")
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    with pytest.raises(ValueError, match=_PRIVATE_TERMS_DIGEST_VARIABLE):
        _active_terms()


def test_active_terms_does_not_require_a_digest_when_private_terms_are_optional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.delenv(_PRIVATE_TERMS_DIGEST_VARIABLE, raising=False)
    assert _active_terms()[-1][0] == "stand-in tool"


def test_active_terms_verifies_the_digest_whenever_it_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = "stand-in tool\tsuper-widget\n"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text(content)
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.setenv(_PRIVATE_TERMS_DIGEST_VARIABLE, _digest_of(content))
    assert _active_terms()[-1][0] == "stand-in tool"
    monkeypatch.setenv(_PRIVATE_TERMS_DIGEST_VARIABLE, "0" * 64)
    with pytest.raises(ValueError, match="does not match"):
        _active_terms()


def test_active_terms_rejects_a_digest_variable_that_is_set_but_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An exported-but-unpopulated digest must fail closed rather than read as "not configured".
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.setenv(_PRIVATE_TERMS_DIGEST_VARIABLE, "")
    with pytest.raises(ValueError, match="64 hexadecimal characters"):
        _active_terms()


def test_active_terms_rejects_a_whitespace_only_digest_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in tool\tsuper-widget\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.setenv(_PRIVATE_TERMS_DIGEST_VARIABLE, "   ")
    with pytest.raises(ValueError, match="64 hexadecimal characters"):
        _active_terms()


def test_the_gate_fails_closed_when_the_private_terms_digest_does_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("stand-in private term\t\\bLICENSE\\b\n")
    monkeypatch.delenv("HEINZEL_REQUIRE_PRIVATE_TERMS", raising=False)
    monkeypatch.setenv("HEINZEL_PRIVATE_TERMS_FILE", str(terms_file))
    monkeypatch.setenv(_PRIVATE_TERMS_DIGEST_VARIABLE, "0" * 64)
    with pytest.raises(ValueError, match="does not match"):
        test_the_public_tree_contains_nothing_internal()
