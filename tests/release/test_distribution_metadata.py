from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_URL = "https://github.com/PillarMesh/heinzel"
REQUIRED_AUTHOR = {"name": "PillarMesh", "email": "karthik@pillarmesh.com"}
REQUIRED_CLASSIFIER = "Programming Language :: Python :: 3.13"


def _members() -> list[Path]:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [ROOT / member for member in root["tool"]["uv"]["workspace"]["members"]]


def _problems(member: Path, project: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    name = project.get("name", "")
    if not name.startswith("heinzel-"):
        problems.append(f"name {name!r} does not start with 'heinzel-'")

    if project.get("license") != "Apache-2.0":
        problems.append(f"license is {project.get('license')!r}, expected 'Apache-2.0'")

    if not project.get("description"):
        problems.append("a one-line description is required")

    readme = project.get("readme")
    if readme != "README.md":
        problems.append(f"readme is {readme!r}, expected 'README.md'")
    elif not (member / readme).is_file():
        problems.append(f"{member / readme} does not exist")

    if REQUIRED_AUTHOR not in project.get("authors", []):
        problems.append(f"authors does not contain {REQUIRED_AUTHOR!r}")

    urls = project.get("urls", {})
    if urls.get("Repository") != REPOSITORY_URL:
        problems.append(
            f"urls.Repository is {urls.get('Repository')!r}, expected {REPOSITORY_URL!r}"
        )
    expected_issues = f"{REPOSITORY_URL}/issues"
    if urls.get("Issues") != expected_issues:
        problems.append(f"urls.Issues is {urls.get('Issues')!r}, expected {expected_issues!r}")

    classifiers = project.get("classifiers", [])
    if REQUIRED_CLASSIFIER not in classifiers:
        problems.append(f"classifiers is missing {REQUIRED_CLASSIFIER!r}")
    license_classifiers = [c for c in classifiers if c.startswith("License ::")]
    if license_classifiers:
        problems.append(
            "classifiers must not include a 'License ::' entry alongside the SPDX license "
            f"expression (hatchling rejects the combination): {license_classifiers!r}"
        )

    if not (member / "LICENSE").is_file():
        problems.append(f"{member / 'LICENSE'} does not exist")

    return problems


@pytest.mark.parametrize("member", _members(), ids=lambda path: str(path.relative_to(ROOT)))
def test_every_distribution_can_be_published_as_is(member: Path) -> None:
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    problems = _problems(member, project)

    assert not problems, "\n".join(problems)
