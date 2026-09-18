"""Contract tests for the M0 offline assurance workflow's triggers.

The offline job runs the whole lint, type, test and boundary suite, several minutes per run.
Unfiltered, a push to a pull request branch fires it twice, once for `push` and once for
`pull_request`, which spent the repository's included Actions minutes twice as fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/repository-structure.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves an unquoted `on:` key to the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_workflow_runs_on_pull_request_main_push_and_manual_dispatch(
    workflow: dict[str, Any],
) -> None:
    assert set(_triggers(workflow)) == {"pull_request", "push", "workflow_dispatch"}


def test_push_trigger_does_not_duplicate_the_pull_request_run(workflow: dict[str, Any]) -> None:
    push = _triggers(workflow)["push"]

    assert isinstance(push, dict)
    assert push["branches"] == ["main"]


def test_pull_request_runs_are_unfiltered_so_every_change_is_checked(
    workflow: dict[str, Any],
) -> None:
    assert _triggers(workflow)["pull_request"] is None


def test_a_newer_pull_request_push_supersedes_the_outdated_run(workflow: dict[str, Any]) -> None:
    concurrency = workflow["concurrency"]

    assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}"
    # A main-branch run is left to finish so its evidence is not discarded.
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_the_offline_job_is_bounded(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"]["validate"]["timeout-minutes"] == 30
