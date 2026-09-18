"""The console bundles IBM Plex under OFL-1.1; its notice and licence text must ship with it.

`@fontsource-variable/ibm-plex-sans` and `@fontsource/ibm-plex-mono` are built into the console's
`dist` output. OFL condition 2 requires the copyright notice and the licence text to travel with
any redistributed copy, so `THIRD_PARTY_NOTICES.md` must name the component and its licence, and
the licence text itself must ship from the console's public directory into the build.

The shipped licence files are pinned by locked package version and by the sha256 of the file this
repository ships, not by comparing against `node_modules` -- CI runs this suite without `npm
install`, so a `node_modules` comparison alone would silently skip on every CI run. Pinning both
values means a `package-lock.json` upgrade of a covered package fails this suite until someone
reviews the new licence text and re-copies it; the byte comparison against `node_modules` still
runs as an extra check whenever `node_modules` happens to be present.

The covered set is derived from `package-lock.json` rather than hand-maintained: every non-dev
`@fontsource*` entry in the lock file must appear in `SHIPPED_LICENSES` and be named in
`THIRD_PARTY_NOTICES.md`, so a new bundled font package cannot go unnoticed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = ROOT / "apps" / "console"
NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
PUBLIC_DIR = CONSOLE_DIR / "web" / "public"
LOCKFILE_PATH = CONSOLE_DIR / "package-lock.json"

_IBM_COPYRIGHT_MARKER = "IBM Corp."
_FONTSOURCE_PREFIX = "node_modules/@fontsource"


@dataclass(frozen=True)
class _ShippedLicense:
    """A font package's licence text as this repository ships it, pinned to a known-good state."""

    relative_path: str
    pinned_version: str
    sha256: str


# Every bundled `@fontsource*` package. Bumping the pinned version or the pinned hash requires
# reviewing the new upstream licence text and re-copying it into `web/public/licenses/` --
# `test_every_locked_fontsource_package_is_covered` fails otherwise on any lock-file upgrade.
SHIPPED_LICENSES: dict[str, _ShippedLicense] = {
    "@fontsource-variable/ibm-plex-sans": _ShippedLicense(
        relative_path="licenses/ibm-plex-sans-OFL.txt",
        pinned_version="5.3.0",
        sha256="d0283623ef57e722fd0eb688a8041589670c608ab780cd3612d06ba6f153d3fd",
    ),
    "@fontsource/ibm-plex-mono": _ShippedLicense(
        relative_path="licenses/ibm-plex-mono-OFL.txt",
        pinned_version="5.3.0",
        sha256="23b0a9d0c6d3f140a0b77e483c5cfa6bba574325ef5cb189ed9f2fec4884533f",
    ),
}


def _locked_fontsource_packages() -> dict[str, str]:
    """Non-dev `@fontsource*` package names to their locked version, read from the lock file."""

    lock = json.loads(LOCKFILE_PATH.read_text(encoding="utf-8"))
    packages: dict[str, Any] = lock["packages"]

    locked: dict[str, str] = {}
    for key, entry in packages.items():
        if not key.startswith(_FONTSOURCE_PREFIX):
            continue
        if entry.get("dev"):
            continue
        name = key.removeprefix("node_modules/")
        locked[name] = entry["version"]
    return locked


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_third_party_notices_names_ibm_plex_and_its_license() -> None:
    text = NOTICES_PATH.read_text(encoding="utf-8")

    assert "IBM Plex" in text
    assert "OFL-1.1" in text


@pytest.mark.parametrize("package", sorted(SHIPPED_LICENSES))
def test_shipped_license_file_contains_the_ofl_grant_and_ibm_copyright(package: str) -> None:
    shipped = PUBLIC_DIR / SHIPPED_LICENSES[package].relative_path

    assert shipped.exists(), f"missing shipped licence file: {shipped}"
    text = shipped.read_text(encoding="utf-8")

    assert "SIL OPEN FONT LICENSE" in text
    assert _IBM_COPYRIGHT_MARKER in text


@pytest.mark.parametrize("package", sorted(SHIPPED_LICENSES))
def test_shipped_license_matches_the_pinned_locked_version(package: str) -> None:
    locked = _locked_fontsource_packages()

    assert package in locked, f"{package} is no longer in package-lock.json; drop its pin"
    assert locked[package] == SHIPPED_LICENSES[package].pinned_version, (
        f"{package} was upgraded to {locked[package]} in package-lock.json but the shipped "
        f"licence is pinned to {SHIPPED_LICENSES[package].pinned_version}; review the new "
        "upstream licence text, re-copy it, and update the pin"
    )


@pytest.mark.parametrize("package", sorted(SHIPPED_LICENSES))
def test_shipped_license_file_matches_its_pinned_hash(package: str) -> None:
    entry = SHIPPED_LICENSES[package]
    shipped = PUBLIC_DIR / entry.relative_path

    assert shipped.exists(), f"missing shipped licence file: {shipped}"
    actual = _sha256(shipped)
    assert actual == entry.sha256, (
        f"{shipped} no longer matches its pinned sha256 ({entry.sha256}); if this is an "
        "intentional upstream licence update, review the new text and update the pin"
    )


@pytest.mark.parametrize("package", sorted(SHIPPED_LICENSES))
def test_shipped_license_file_matches_the_installed_package_byte_for_byte(package: str) -> None:
    node_modules_license = CONSOLE_DIR / "node_modules" / package / "LICENSE"
    if not node_modules_license.exists():
        pytest.skip(f"{package} is not installed under node_modules; nothing to compare against")

    shipped = PUBLIC_DIR / SHIPPED_LICENSES[package].relative_path

    assert shipped.exists(), f"missing shipped licence file: {shipped}"
    assert shipped.read_bytes() == node_modules_license.read_bytes()


def test_every_locked_fontsource_package_is_covered() -> None:
    locked = _locked_fontsource_packages()
    uncovered = sorted(set(locked) - set(SHIPPED_LICENSES))

    assert not uncovered, (
        f"package-lock.json locks {uncovered} but SHIPPED_LICENSES has no entry for them; add a "
        "pinned licence copy and a THIRD_PARTY_NOTICES.md entry for each"
    )


@pytest.mark.parametrize("package", sorted(SHIPPED_LICENSES))
def test_notices_name_every_covered_package_and_its_shipped_path(package: str) -> None:
    text = NOTICES_PATH.read_text(encoding="utf-8")
    entry = SHIPPED_LICENSES[package]

    assert package in text, f"{package} does not appear by name in {NOTICES_PATH}"
    dist_path = entry.relative_path.replace("licenses/", "dist/licenses/", 1)
    assert dist_path in text, f"{dist_path} does not appear in {NOTICES_PATH}"
