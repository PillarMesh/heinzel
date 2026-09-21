"""Contract tests for the Quickstart workflow.

The smoke test builds the published image, so this workflow is path-triggered rather
than run on every pull request. The hazard it is written around is not a failing build
but a silent one: `tests/quickstart` skips itself unless `HEINZEL_RUN_QUICKSTART=1` is
set and Docker is on the runner, and pytest then exits 0 having built nothing. The job
must fail in that case, which is what the junit-report check below is for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/quickstart.yml"

_SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")

# What the image is built from, read off the Dockerfile's own `COPY` lines. A path
# triggering the workflow has to cover each of these, or a change can alter the image
# with nothing rebuilding it.
_IMAGE_INPUTS = ("pyproject.toml", "uv.lock", "apps/console")


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


def _job(workflow: dict[str, Any]) -> dict[str, Any]:
    job = workflow["jobs"]["quickstart"]
    assert isinstance(job, dict)
    return job


def _step(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _job(workflow)["steps"] if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def test_the_workflow_runs_on_its_own_paths_and_on_demand(workflow: dict[str, Any]) -> None:
    triggers = _triggers(workflow)

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert set(triggers["pull_request"]["paths"]) == {
        "deploy/quickstart/**",
        "apps/console/**",
        "tests/quickstart/**",
        "pyproject.toml",
        "uv.lock",
        ".dockerignore",
        ".github/workflows/quickstart.yml",
    }


def test_every_path_the_image_is_built_from_triggers_a_rebuild(workflow: dict[str, Any]) -> None:
    """A build input outside the trigger list changes the image with nothing rebuilding it."""
    paths = _triggers(workflow)["pull_request"]["paths"]
    prefixes = tuple(path.removesuffix("/**") for path in paths)

    for build_input in _IMAGE_INPUTS:
        assert any(
            build_input == prefix or build_input.startswith(f"{prefix}/") for prefix in prefixes
        ), f"{build_input} is copied into the image but triggers no rebuild"


def test_every_trigger_path_names_something_that_exists(workflow: dict[str, Any]) -> None:
    """A misspelled path silently never matches, and the smoke test never runs again."""
    for path in _triggers(workflow)["pull_request"]["paths"]:
        assert (ROOT / path.removesuffix("/**")).exists(), path


def test_the_token_is_read_only_and_no_secret_is_referenced(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _job(workflow)
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")


def test_a_newer_run_supersedes_the_outdated_one(workflow: dict[str, Any]) -> None:
    concurrency = workflow["concurrency"]

    assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_the_build_cannot_run_unbounded(workflow: dict[str, Any]) -> None:
    assert isinstance(_job(workflow)["timeout-minutes"], int)


def test_every_action_is_pinned_to_a_full_commit_sha(workflow: dict[str, Any]) -> None:
    uses = [step["uses"] for step in _job(workflow)["steps"] if "uses" in step]

    assert uses
    for reference in uses:
        assert _SHA_PIN.match(reference), reference


def test_the_checkout_does_not_persist_the_token(workflow: dict[str, Any]) -> None:
    checkout = _step(workflow, "Check out repository")

    assert checkout["with"]["persist-credentials"] is False


def test_the_workflow_opts_into_the_smoke_test(workflow: dict[str, Any]) -> None:
    """Read from the parsed step rather than from the file's text.

    An assertion on the raw text would fail on a reformatting that changes nothing about
    what the job does, and would pass on `HEINZEL_RUN_QUICKSTART: "1"` written in a
    comment or in some other job.
    """
    step = _step(workflow, "Smoke test the quickstart")

    assert step["env"]["HEINZEL_RUN_QUICKSTART"] == "1"
    assert "tests/quickstart" in step["run"].split()
    # `-m live` alone deselects nothing here, but the smoke test carries `emulator` too and
    # a later marker change must not quietly deselect it.
    assert '-m "live or emulator"' in step["run"]


def test_a_skipped_smoke_test_fails_the_job(workflow: dict[str, Any]) -> None:
    """The defect this workflow exists to prevent: a green job that built nothing.

    Both of the suite's conditions -- the environment variable and Docker on PATH -- fail
    open, into a skip. Only a report of what actually ran can tell a passing build from an
    absent one.
    """
    smoke = _step(workflow, "Smoke test the quickstart")
    check = _step(workflow, "Require every smoke test to have run")

    report = '--junitxml="$RUNNER_TEMP/quickstart-smoke.xml"'
    assert report in smoke["run"]
    assert report.removeprefix("--junitxml=").strip('"') in check["run"]
    assert "tests > 0 and skipped == 0" in check["run"]


def test_the_containers_are_torn_down_even_when_the_suite_dies(workflow: dict[str, Any]) -> None:
    teardown = _step(workflow, "Exact teardown")

    assert teardown["if"] == "always()"
    assert "down -v" in teardown["run"]
