"""Every workflow checkout is pinned alike and never leaves the token in the checkout.

`actions/checkout` writes the job token into `.git/config` unless told not to, where every later
step, including third-party actions and scripts run from a pull request's code, can read it. No
workflow here pushes, so no checkout needs to persist it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"
_CHECKOUT_PIN = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_CHECKOUT_LINE = re.compile(r"uses:\s*actions/checkout@\S+(?P<comment>.*)$")


def _workflow_paths() -> tuple[Path, ...]:
    paths = tuple(sorted(WORKFLOW_DIRECTORY.glob("*.yml")))
    assert paths, "no workflows found"
    return paths


def _checkout_steps(path: Path) -> tuple[dict[str, Any], ...]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    steps: list[dict[str, Any]] = []
    for job in parsed["jobs"].values():
        for step in job.get("steps", ()):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                steps.append(step)
    return tuple(steps)


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda path: path.name)
def test_every_checkout_is_pinned_and_does_not_persist_credentials(path: Path) -> None:
    for step in _checkout_steps(path):
        assert step["uses"] == _CHECKOUT_PIN, path.name
        assert step.get("with", {}).get("persist-credentials") is False, (
            f"{path.name}: {step.get('name')} persists the job token"
        )


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda path: path.name)
def test_every_checkout_pin_names_the_same_release(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _CHECKOUT_LINE.search(line)
        if match:
            assert match.group("comment").strip() == "# v7.0.1", f"{path.name}: {line.strip()}"


def test_the_workflows_check_out_the_repository_at_all() -> None:
    assert sum(len(_checkout_steps(path)) for path in _workflow_paths()) >= 6
