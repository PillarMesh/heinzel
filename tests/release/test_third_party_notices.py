"""The console bundles IBM Plex under OFL-1.1; its notice and licence text must ship with it.

`@fontsource-variable/ibm-plex-sans` and `@fontsource/ibm-plex-mono` are built into the console's
`dist` output. OFL condition 2 requires the copyright notice and the licence text to travel with
any redistributed copy, so `THIRD_PARTY_NOTICES.md` must name the component and its licence, and
the licence text itself must ship from the console's public directory into the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
PUBLIC_DIR = ROOT / "apps" / "console" / "web" / "public"

# Each shipped licence file, and the upstream npm package whose `node_modules` copy it must
# match byte for byte when that package is installed.
SHIPPED_LICENSES: tuple[tuple[str, str], ...] = (
    ("licenses/ibm-plex-sans-OFL.txt", "@fontsource-variable/ibm-plex-sans"),
    ("licenses/ibm-plex-mono-OFL.txt", "@fontsource/ibm-plex-mono"),
)

_IBM_COPYRIGHT_MARKER = "IBM Corp."


def test_third_party_notices_names_ibm_plex_and_its_license() -> None:
    text = NOTICES_PATH.read_text(encoding="utf-8")

    assert "IBM Plex" in text
    assert "OFL-1.1" in text


@pytest.mark.parametrize(("relative_path", "_package"), SHIPPED_LICENSES)
def test_shipped_license_file_contains_the_ofl_grant_and_ibm_copyright(
    relative_path: str, _package: str
) -> None:
    shipped = PUBLIC_DIR / relative_path

    assert shipped.exists(), f"missing shipped licence file: {shipped}"
    text = shipped.read_text(encoding="utf-8")

    assert "SIL OPEN FONT LICENSE" in text
    assert _IBM_COPYRIGHT_MARKER in text


@pytest.mark.parametrize(("relative_path", "package"), SHIPPED_LICENSES)
def test_shipped_license_file_matches_the_installed_package_byte_for_byte(
    relative_path: str, package: str
) -> None:
    node_modules_license = ROOT / "apps" / "console" / "node_modules" / package / "LICENSE"
    if not node_modules_license.exists():
        pytest.skip(f"{package} is not installed under node_modules; nothing to compare against")

    shipped = PUBLIC_DIR / relative_path

    assert shipped.exists(), f"missing shipped licence file: {shipped}"
    assert shipped.read_bytes() == node_modules_license.read_bytes()
