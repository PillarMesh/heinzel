"""Contract tests for the Live journeys workflow.

The live journeys start pinned engine images in Docker, several minutes per run, so they run
nightly, on demand, and only on pull requests a maintainer labels `run-live`. The job needs no
repository secrets and must keep a read-only token, because a fork's pull request can run it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/live.yml"

_SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_JOURNEY_PATH = re.compile(r"^(?:tests|providers|services)/\S+\.py$")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # PyYAML resolves an unquoted `on:` key to the boolean True. Normalise it here so
    # every key really is the str this returns, and callers can index "on" directly.
    return {("on" if key is True else key): value for key, value in parsed.items()}


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _job(workflow: dict[str, Any]) -> dict[str, Any]:
    job = workflow["jobs"]["live"]
    assert isinstance(job, dict)
    return job


def _step(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _job(workflow)["steps"] if step.get("name") == name]
    assert len(matches) == 1, name
    step = matches[0]
    assert isinstance(step, dict)
    return step


def _journey_paths(workflow: dict[str, Any]) -> tuple[str, ...]:
    command = _step(workflow, "Run live journeys")["run"]
    return tuple(token for token in command.split() if _JOURNEY_PATH.match(token))


def test_workflow_runs_nightly_on_demand_and_on_labelled_pull_requests(
    workflow: dict[str, Any],
) -> None:
    triggers = _triggers(workflow)

    assert set(triggers) == {"schedule", "pull_request", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "0 3 * * *"}]
    # No `synchronize`: a new push, possibly from a fork, runs only once a maintainer re-applies
    # the label after reviewing it.
    assert triggers["pull_request"] == {"types": ["labeled"]}


def test_pull_requests_run_only_with_the_run_live_label(workflow: dict[str, Any]) -> None:
    condition = " ".join(_job(workflow)["if"].split())

    # Only `labeled` triggers a pull request run, so the label just applied must be run-live;
    # adding some other label to a run-live pull request starts nothing.
    assert condition == (
        "github.event_name != 'pull_request' || github.event.label.name == 'run-live'"
    )


def test_the_token_is_read_only_and_no_secret_is_referenced(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _job(workflow)
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")


def test_a_newer_labelled_run_supersedes_the_outdated_one(workflow: dict[str, Any]) -> None:
    # Job-level: a workflow-level group would let a skipped run, started by adding some other
    # label, cancel the live run in progress.
    assert "concurrency" not in workflow
    concurrency = _job(workflow)["concurrency"]

    assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_every_action_is_pinned_to_a_full_commit_sha(workflow: dict[str, Any]) -> None:
    uses = [step["uses"] for step in _job(workflow)["steps"] if "uses" in step]

    assert uses
    for reference in uses:
        assert _SHA_PIN.match(reference), reference


def test_every_listed_journey_exists(workflow: dict[str, Any]) -> None:
    paths = _journey_paths(workflow)

    assert len(paths) == len(set(paths))
    for path in paths:
        assert (ROOT / path).is_file(), path


def test_the_core_postgresql_journeys_are_listed(workflow: dict[str, Any]) -> None:
    paths = set(_journey_paths(workflow))

    assert {
        "tests/integration/test_postgresql_checked_sum_evidence.py",
        "tests/integration/test_postgresql_compiled_product_journey_live.py",
        "tests/integration/test_postgresql_composed_acquisition_live.py",
        "tests/integration/test_postgresql_acquisition_land_live.py",
        "tests/integration/test_postgresql_leased_run_resume_live.py",
        "tests/integration/test_postgresql_rejected_credentials_live.py",
    } <= paths


def test_the_quickstart_smoke_test_is_not_a_live_journey(workflow: dict[str, Any]) -> None:
    command = _step(workflow, "Run live journeys")["run"]

    # The quickstart smoke test carries `live` and `emulator` too, but it builds the published
    # image and has its own workflow. The selection is an explicit file list, so the exclusion
    # is simply that no listed path is one of its modules.
    assert "tests/quickstart" not in command
    for path in _journey_paths(workflow):
        assert not path.startswith("tests/quickstart/"), path


def test_emulator_marked_journeys_are_selected_and_skips_fail_the_run(
    workflow: dict[str, Any],
) -> None:
    command = _step(workflow, "Run live journeys")["run"]
    check = _step(workflow, "Require every selected journey to have run")["run"]

    # `-m live` alone deselects the files that mark their tests `emulator`.
    assert '-m "live or emulator"' in command
    assert '--junitxml="$RUNNER_TEMP/live-journeys.xml"' in command
    assert "skipped == 0" in check


def test_the_checkout_does_not_persist_the_token(workflow: dict[str, Any]) -> None:
    checkout = _step(workflow, "Check out repository")

    assert checkout["with"]["persist-credentials"] is False
