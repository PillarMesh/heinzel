"""Every third-party dependency must be licensed compatibly with distributing Apache-2.0 code."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal, TypedDict, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]

type Verdict = Literal["forbidden", "permitted", "unknown"]

PERMITTED = (
    "apache",
    "mit",
    "bsd",
    "isc",
    "psf",
    "python software foundation",
    "python-2.0",
    "unlicense",
    "0bsd",
    "cc0",
    "zlib",
    "blueoak",
    "mpl-2.0",
    "mozilla public license 2.0",
    "lgpl",
    "gnu lesser general public license",
    "hpnd",
    "public domain",
)
FORBIDDEN = ("agpl", "affero", "sspl", "server side public")

# Signals that mean the dependency is not actually usable under an open license, regardless of
# any PERMITTED term that also happens to appear in the same string -- e.g. "apache-2.0 with
# commons-clause" contains "apache" but is not the plain Apache-2.0 grant it looks like. Checked
# as substrings (not word-bounded) because each is a distinctive multi-character token that does
# not occur inside a legitimate permissive license name.
_FORBIDDEN_SIGNALS = (
    "unlicensed",  # distinct from the PERMITTED "Unlicense" public-domain dedication
    "proprietary",
    "commons-clause",
    "commons clause",
    "licenseref-",  # SPDX convention for a custom, non-standard license text
    "see license in",  # npm's "see license in <file>" placeholder names no actual license
)

# A free-text `License` field can be an entire embedded license body (e.g. the full text of the
# GPL) rather than a short name, and a long body is likely to contain incidental matches for
# unrelated terms ("permitted", "disclaim", even "GNU Lesser General Public License" quoted in a
# preamble). Past this length it stops being usable evidence; classify from License-Expression
# and the OSI trove classifiers instead, which are curated, short, and don't have this problem.
_MAX_LICENSE_FIELD_LENGTH = 200

# A bare "gpl" is standalone GPL, forbidden; it must not fire inside "lgpl" or a spelled-out
# "lesser" grant, which are permitted. Word-bounded so "gpl" doesn't also match inside "gplv2"
# (handled separately below) or an unrelated word.
_GPL_TOKEN = re.compile(r"(?<![a-z0-9])gpl(?![a-z0-9])")
_GPL_VERSIONED_TOKEN = re.compile(r"(?<![a-z0-9])gplv\d")
_GPL_PHRASE = "gnu general public license"
_LGPL_MARKERS = ("lgpl", "lesser", "gnu lesser general public license")


@dataclass(frozen=True)
class Reviewed:
    """A hand-checked exception to automatic classification.

    `reported` freezes the exact evidence text the check saw at review time. If the dependency
    is later upgraded and its declared license changes, the recorded text stops matching and the
    review test fails instead of silently continuing to trust stale evidence.
    """

    reported: str
    elected: str
    checked: str


@dataclass(frozen=True)
class NotInstalled:
    """A distribution that is locked for another platform and never installs here."""

    license: str
    reason: str


class _LockSource(TypedDict, total=False):
    editable: str
    virtual: str
    registry: str


class _LockPackage(TypedDict):
    name: str
    version: str
    source: _LockSource


class _NpmPackage(TypedDict, total=False):
    license: object
    licenses: object
    link: bool
    version: str


# Distributions whose metadata carries no usable license field, or that are genuinely
# dual-licensed under a forbidden term but also offer a permissive alternative that Heinzel
# exercises, each checked by hand and justified.
REVIEWED_PYTHON: Mapping[str, Reviewed] = {
    "text-unidecode": Reviewed(
        reported=(
            "artistic license license :: osi approved :: artistic license "
            "license :: osi approved :: gnu general public license (gpl) "
            "license :: osi approved :: gnu general public license v2 or later (gplv2+)"
        ),
        elected="OSI-approved Artistic-1.0 elected; not modified or redistributed",
        checked="2026-09-18",
    ),
}
REVIEWED_NPM: Mapping[str, Reviewed] = {
    "@fontsource-variable/ibm-plex-sans": Reviewed(
        reported="ofl-1.1", elected="OFL-1.1", checked="2026-09-18"
    ),
    "@fontsource/ibm-plex-mono": Reviewed(
        reported="ofl-1.1", elected="OFL-1.1", checked="2026-09-18"
    ),
    "caniuse-lite": Reviewed(reported="cc-by-4.0", elected="CC-BY-4.0", checked="2026-09-18"),
}

# Distributions that are locked (for another platform) but not installed in this environment,
# each with the hand-checked upstream license and a one-line reason a reviewer can confirm.
NOT_INSTALLED_HERE: Mapping[str, NotInstalled] = {
    "pywin32": NotInstalled(
        license="PSF", reason="Windows-only wheels (win32/win_amd64/win_arm64); not Windows here"
    ),
    "tzdata": NotInstalled(
        license="Apache-2.0", reason="every uv.lock marker for it is sys_platform == 'win32'"
    ),
    "httpx2-jsfetch": NotInstalled(
        license="BSD-3-Clause",
        reason="every uv.lock marker for it is sys_platform == 'emscripten'",
    ),
}


def _term_present(term: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _has_forbidden_signal(text: str) -> bool:
    return any(signal in text for signal in _FORBIDDEN_SIGNALS)


def _has_forbidden_term(text: str) -> bool:
    return any(_term_present(term, text) for term in FORBIDDEN)


def _has_gpl_signal(text: str) -> bool:
    stripped = text
    for marker in _LGPL_MARKERS:
        stripped = stripped.replace(marker, "")
    if _GPL_TOKEN.search(stripped) or _GPL_VERSIONED_TOKEN.search(stripped):
        return True
    return _GPL_PHRASE in stripped


def _classify(text: str) -> Verdict:
    if _has_forbidden_signal(text):
        return "forbidden"
    if _has_forbidden_term(text):
        return "forbidden"
    if _has_gpl_signal(text):
        return "forbidden"
    if any(_term_present(term, text) for term in PERMITTED):
        return "permitted"
    return "unknown"


def _combine_license_fields(
    license_expression: str, license_field: str, classifiers: list[str]
) -> str:
    if len(license_field) > _MAX_LICENSE_FIELD_LENGTH:
        license_field = ""
    fields = [field for field in (license_expression, license_field, *classifiers) if field]
    return " ".join(fields).lower()


def _license_text(dist: metadata.Distribution) -> str:
    classifiers = [
        c for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License ::")
    ]
    return _combine_license_fields(
        dist.metadata.get("License-Expression") or "",
        dist.metadata.get("License") or "",
        classifiers,
    )


def _npm_license_evidence(package: _NpmPackage) -> str | None:
    """The lowercase license text to classify, or None when the value isn't trustworthy.

    A `licenses` key is npm's legacy array form and is never trusted here, regardless of
    whether a `license` string also happens to be present. A `license` value that isn't a
    plain string (a dict such as {"type": "MIT"}, or a list) is rejected outright rather than
    stringified: str({"type": "mit"}) contains "mit" at word boundaries and would otherwise
    slip past _classify as if it were a real declaration.
    """
    if "licenses" in package:
        return None
    license_value = package.get("license")
    if not isinstance(license_value, str):
        return None
    return license_value.lower()


def _load_lock_packages() -> list[_LockPackage]:
    # tomllib.loads returns dict[str, Any]; this narrows to the shape this module actually
    # reads (name/version/source) rather than modelling the whole uv.lock schema.
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = data["package"]
    assert isinstance(packages, list)
    return cast(list[_LockPackage], packages)


def _load_npm_packages() -> Mapping[str, _NpmPackage]:
    data = json.loads((ROOT / "apps/console/package-lock.json").read_text(encoding="utf-8"))
    packages = data.get("packages", {})
    assert isinstance(packages, dict)
    return cast(Mapping[str, _NpmPackage], packages)


def _locked_python_packages() -> Mapping[str, str]:
    """Locked, non-workspace package name -> locked version."""
    packages: dict[str, str] = {}
    for package in _load_lock_packages():
        source = package["source"]
        if "editable" in source or "virtual" in source:
            continue
        packages[package["name"]] = package["version"]
    return packages


def _missing_python_packages() -> set[str]:
    missing: set[str] = set()
    for name in _locked_python_packages():
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.add(name)
    return missing


def test_every_python_dependency_is_permitted() -> None:
    problems: list[str] = []
    missing = _missing_python_packages()
    for name, locked_version in sorted(_locked_python_packages().items()):
        if name in missing:
            continue
        dist = metadata.distribution(name)
        if dist.version != locked_version:
            problems.append(f"{name}: installed {dist.version} but uv.lock has {locked_version}")
        if name in REVIEWED_PYTHON:
            continue
        text = _license_text(dist)
        verdict = _classify(text)
        if verdict != "permitted":
            problems.append(f"{name}: {verdict} ({text or 'no license metadata'})")
    assert problems == [], "\n".join(problems)


def test_every_locked_but_uninstalled_python_dependency_is_accounted_for() -> None:
    missing = _missing_python_packages()
    unexplained = missing - set(NOT_INSTALLED_HERE)
    stale = set(NOT_INSTALLED_HERE) - missing
    problems: list[str] = []
    if unexplained:
        problems.append(f"missing without a reason in NOT_INSTALLED_HERE: {sorted(unexplained)}")
    if stale:
        problems.append(f"listed in NOT_INSTALLED_HERE but actually installed: {sorted(stale)}")
    assert problems == [], "\n".join(problems)


def test_every_reviewed_python_entry_matches_its_recorded_license_text() -> None:
    missing = _missing_python_packages()
    problems: list[str] = []
    for name, reviewed in REVIEWED_PYTHON.items():
        if name in missing:
            problems.append(f"{name}: listed in REVIEWED_PYTHON but not installed here")
            continue
        actual = _license_text(metadata.distribution(name))
        if actual != reviewed.reported:
            problems.append(
                f"{name}: license text changed since review; "
                f"recorded {reviewed.reported!r}, now {actual!r}"
            )
        if _classify(actual) == "permitted":
            problems.append(f"{name}: now classifies as permitted automatically; drop the entry")
    assert problems == [], "\n".join(problems)


def test_every_npm_dependency_is_permitted() -> None:
    packages = _load_npm_packages()
    problems: list[str] = []
    for path, package in sorted(packages.items()):
        if not path or package.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if name in REVIEWED_NPM:
            continue
        evidence = _npm_license_evidence(package)
        if evidence is None:
            problems.append(
                f"{name}: unknown (license={package.get('license')!r}, "
                f"licenses={package.get('licenses')!r})"
            )
            continue
        verdict = _classify(evidence)
        if verdict != "permitted":
            problems.append(f"{name}: {verdict} ({package.get('license')!r})")
    assert problems == [], "\n".join(problems)


def test_every_reviewed_npm_entry_matches_its_recorded_license_text() -> None:
    packages = _load_npm_packages()
    by_name = {
        path.rsplit("node_modules/", 1)[-1]: package
        for path, package in packages.items()
        if path and not package.get("link")
    }
    problems: list[str] = []
    for name, reviewed in REVIEWED_NPM.items():
        package = by_name.get(name)
        if package is None:
            problems.append(f"{name}: listed in REVIEWED_NPM but not found in package-lock.json")
            continue
        evidence = _npm_license_evidence(package)
        if evidence != reviewed.reported:
            problems.append(
                f"{name}: license text changed since review; "
                f"recorded {reviewed.reported!r}, now {evidence!r}"
            )
        elif _classify(evidence) == "permitted":
            problems.append(f"{name}: now classifies as permitted automatically; drop the entry")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mit", "permitted"),
        ("apache-2.0", "permitted"),
        ("gpl-3.0-or-later", "forbidden"),
        ("lgpl-2.1", "permitted"),
        ("lgpl-3.0-only", "permitted"),
        ("agpl-3.0", "forbidden"),
        ("sspl-1.0", "forbidden"),
        ("", "unknown"),
        # A dual-licence expression that offers no permissive branch is forbidden outright;
        # a package that is genuinely dual-licensed with a permissive option (like
        # text-unidecode above) is resolved by hand in REVIEWED_PYTHON/REVIEWED_NPM instead.
        ("mit or gpl-2.0", "forbidden"),
    ],
)
def test_classify_matches_the_license_policy(text: str, expected: Verdict) -> None:
    assert _classify(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "unlicense" is permitted (public-domain dedication); "unlicensed" means the
        # opposite -- no license was granted at all -- and must not match as a substring.
        ("unlicensed", "forbidden"),
        # "mit" must not match inside "permitted".
        ("commercial; redistribution not permitted", "unknown"),
        ("licenseref-proprietary-limited", "forbidden"),
        # "isc" must not match inside "disclaimer".
        ("all rights reserved. see disclaimer", "unknown"),
        # "apache" is present, but Commons Clause revokes the open-source terms.
        ("apache-2.0 with commons-clause", "forbidden"),
    ],
)
def test_classify_does_not_treat_bare_substrings_as_permitted(text: str, expected: Verdict) -> None:
    assert _classify(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gpl-3.0-or-later and lgpl-2.1-or-later", "forbidden"),
        ("license :: osi approved :: gnu general public license v2 (gplv2)", "forbidden"),
        ("gpl-2.0-or-later with classpath-exception-2.0", "forbidden"),
        ("apache-2.0 with llvm-exception", "permitted"),
    ],
)
def test_classify_gpl_detection_ignores_lgpl_but_not_gpl(text: str, expected: Verdict) -> None:
    assert _classify(text) == expected


def test_classify_ignores_an_overlong_free_text_license_field() -> None:
    # A full GPLv2 COPYING-style body contains "permitted", "disclaim", and even the phrase
    # "GNU Lesser General Public License" quoted in its preamble; none of that is evidence once
    # the field is this long. With no License-Expression or classifiers to fall back on, the
    # combined evidence is empty and the verdict is unknown, not permitted and not forbidden.
    long_gpl_text = "GNU GENERAL PUBLIC LICENSE\n" + "This program is distributed in the hope " * 20
    assert len(long_gpl_text) > _MAX_LICENSE_FIELD_LENGTH
    combined = _combine_license_fields("", long_gpl_text, [])
    assert _classify(combined) == "unknown"


def test_classify_still_resolves_isodate_and_libcst_from_their_classifiers() -> None:
    # Both ship a long free-text License body (ignored past the length cutoff) but also carry a
    # short, trustworthy OSI classifier, which must still be enough to classify them.
    isodate_combined = _combine_license_fields(
        "", "x" * 300, ["License :: OSI Approved :: BSD License"]
    )
    libcst_combined = _combine_license_fields(
        "", "x" * 300, ["License :: OSI Approved :: MIT License"]
    )
    assert _classify(isodate_combined) == "permitted"
    assert _classify(libcst_combined) == "permitted"


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ({"license": "MIT"}, "mit"),
        ({"license": {"type": "MIT"}}, None),
        ({"license": ["MIT", "Apache-2.0"]}, None),
        ({"licenses": [{"type": "MIT"}]}, None),
        ({"license": "MIT", "licenses": [{"type": "MIT"}]}, None),
        ({}, None),
    ],
)
def test_npm_license_evidence_rejects_anything_that_is_not_a_plain_string(
    package: _NpmPackage, expected: str | None
) -> None:
    assert _npm_license_evidence(package) == expected
