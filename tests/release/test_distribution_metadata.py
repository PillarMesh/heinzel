from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_URL = "https://github.com/PillarMesh/heinzel"


def _members() -> list[Path]:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [ROOT / member for member in root["tool"]["uv"]["workspace"]["members"]]


@pytest.mark.parametrize("member", _members(), ids=lambda path: str(path.relative_to(ROOT)))
def test_every_distribution_can_be_published_as_is(member: Path) -> None:
    project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"].startswith("heinzel-"), project["name"]
    assert project.get("license") == "Apache-2.0"
    assert project.get("description"), "a one-line description is required"
    readme = project.get("readme")
    assert readme == "README.md" and (member / readme).is_file()
    assert {"name": "PillarMesh", "email": "karthik@pillarmesh.com"} in project.get("authors", [])
    urls = project.get("urls", {})
    assert urls.get("Repository") == REPOSITORY_URL
    assert urls.get("Issues") == f"{REPOSITORY_URL}/issues"
    assert "Programming Language :: Python :: 3.13" in project.get("classifiers", [])
