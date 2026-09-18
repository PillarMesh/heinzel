"""Every third-party dependency must be licensed compatibly with distributing Apache-2.0 code."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

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
FORBIDDEN = ("agpl", "affero", "sspl", "server side public", "gpl-2.0-only", "gpl-3.0-only")

# Distributions whose metadata carries no usable license field, each checked by hand and
# justified, or dependencies that are genuinely dual-licensed under a forbidden term but also
# offer a permissive alternative that Heinzel exercises.
REVIEWED_PYTHON: Mapping[str, str] = {
    "text-unidecode": (
        "dual-licensed GPL/GPLv2+ OR Artistic License per its LICENSE.txt; Heinzel takes the "
        "Artistic License option, checked 2026-09-18"
    ),
}
REVIEWED_NPM: Mapping[str, str] = {
    "@fontsource-variable/ibm-plex-sans": "OFL-1.1 per upstream LICENSE, checked 2026-09-18",
    "@fontsource/ibm-plex-mono": "OFL-1.1 per upstream LICENSE, checked 2026-09-18",
    "caniuse-lite": "CC-BY-4.0 per upstream LICENSE, checked 2026-09-18",
}

# Distributions that are locked (for another platform, e.g. a Windows-only wheel) but not
# installed in this environment, each with a one-line reason a reviewer can confirm.
NOT_INSTALLED_HERE: Mapping[str, str] = {
    "pywin32": "Windows-only dependency; this environment is not Windows",
    "tzdata": "platform-conditional tzdata backport; not needed on this platform's Python",
    "httpx2-jsfetch": "Pyodide/browser-only transport shim; not installable outside that runtime",
}


# uv.lock names are already PEP 503 normalised (lowercase, single hyphens), and none of the
# locked names here contain the "." or "_" characters that would otherwise need folding before
# an importlib.metadata lookup, so no extra normalisation step is applied.
def _license_text(dist: metadata.Distribution) -> str:
    fields = [dist.metadata.get("License-Expression") or "", dist.metadata.get("License") or ""]
    fields += [c for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
    return " ".join(fields).lower()


def _locked_python_packages() -> set[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages: set[str] = set()
    for package in lock["package"]:
        source = package.get("source", {})
        if "editable" in source or "virtual" in source:
            continue
        packages.add(package["name"])
    return packages


def _missing_python_packages() -> set[str]:
    missing: set[str] = set()
    for name in _locked_python_packages():
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.add(name)
    return missing


def _classify(text: str) -> str:
    # "LGPL" spells "GPL" plus a prefix, so a bare substring check against FORBIDDEN's
    # "gpl-2.0-only"/"gpl-3.0-only" would misclassify an actual "LGPL-3.0-only" expression
    # (e.g. psycopg) as forbidden. Strip the "lgpl" substring before that check so only a real,
    # standalone GPL-only term can match it.
    without_lgpl = text.replace("lgpl", "")
    if any(term in without_lgpl for term in FORBIDDEN):
        return "forbidden"
    if " gpl" in f" {text}" and "lgpl" not in text and "lesser" not in text:
        return "forbidden"
    return "permitted" if any(term in text for term in PERMITTED) else "unknown"


def test_every_python_dependency_is_permitted() -> None:
    problems: list[str] = []
    missing = _missing_python_packages()
    for name in sorted(_locked_python_packages()):
        if name in REVIEWED_PYTHON or name in missing:
            continue
        text = _license_text(metadata.distribution(name))
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


def test_every_npm_dependency_is_permitted() -> None:
    lock = json.loads((ROOT / "apps/console/package-lock.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for path, package in sorted(lock.get("packages", {}).items()):
        if not path or package.get("link"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if name in REVIEWED_NPM:
            continue
        verdict = _classify(str(package.get("license", "")).lower())
        if verdict != "permitted":
            problems.append(f"{name}: {verdict} ({package.get('license')})")
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
def test_classify_matches_the_license_policy(text: str, expected: str) -> None:
    assert _classify(text) == expected
