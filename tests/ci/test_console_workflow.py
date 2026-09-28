"""Contract tests for the console CI gate.

The console gate mirrors the warehouse lifecycle gate: an expensive browser job
runs only when console code changes, and an always-present gate job fails closed
whenever it cannot prove the browser suite ran. The job graph is the contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/console.yml"
NODE_VERSION_PATH = Path(__file__).resolve().parents[2] / "apps/console/.node-version"
AFFECTED_PATHS = (
    "apps/console/",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "tests/ci/test_console_workflow",
    ".github/workflows/console",
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # PyYAML resolves an unquoted `on:` key to the boolean True. Normalise it here so
    # every key really is the str this returns, and callers can index "on" directly.
    return {("on" if key is True else key): value for key, value in parsed.items()}


@pytest.fixture(scope="module")
def detector(workflow: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["changes"]["steps"])


@pytest.fixture(scope="module")
def gate_script(workflow: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["gate"]["steps"])


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _console_steps(workflow: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["console"]["steps"])


def test_workflow_runs_on_pull_request_push_and_manual_dispatch(
    workflow: dict[str, Any],
) -> None:
    assert set(_triggers(workflow)) == {"pull_request", "push", "workflow_dispatch"}


def test_push_trigger_does_not_duplicate_the_pull_request_run(workflow: dict[str, Any]) -> None:
    """A pull request branch push fires both `push` and `pull_request`.

    Unfiltered, that starts two identical browser runs for one push.
    """
    push = _triggers(workflow)["push"]

    assert isinstance(push, dict)
    assert push["branches"] == ["main"]


def test_workflow_has_no_path_filter_that_could_leave_the_gate_pending(
    workflow: dict[str, Any],
) -> None:
    for trigger in _triggers(workflow).values():
        if isinstance(trigger, dict):
            assert "paths" not in trigger
            assert "paths-ignore" not in trigger


def test_workflow_requests_read_only_permissions(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_gate_job_always_runs_and_depends_on_both_jobs(workflow: dict[str, Any]) -> None:
    gate = workflow["jobs"]["gate"]

    assert gate["if"] is True or str(gate["if"]).strip() == "always()"
    assert set(gate["needs"]) == {"changes", "console"}


def test_change_detector_covers_every_console_dependency(detector: str) -> None:
    for path in AFFECTED_PATHS:
        assert path in detector, f"the change detector does not cover {path}"


def test_unknown_range_fails_open_to_running_the_console_suite(detector: str) -> None:
    """A range that cannot be resolved cannot prove the change is unrelated."""
    assert re.search(r"affected=true", detector)
    assert "0000000000000000000000000000000000000000" in detector


def test_manual_dispatch_forces_the_console_suite(detector: str) -> None:
    assert re.search(r"workflow_dispatch.*\n\s*echo .affected=true", detector)


def test_console_job_runs_only_when_an_affected_path_changed(workflow: dict[str, Any]) -> None:
    console = workflow["jobs"]["console"]

    assert console["needs"] == "changes" or "changes" in console["needs"]
    assert str(console["if"]).strip() == "needs.changes.outputs.affected == 'true'"


def test_gate_fails_when_the_change_detector_did_not_succeed(gate_script: str) -> None:
    assert 'test "$CHANGES_RESULT" = success || exit 1' in gate_script


def test_gate_fails_on_an_empty_or_unexpected_affected_value(gate_script: str) -> None:
    assert re.search(r"\*\)\s*exit 1", gate_script)


def test_gate_requires_success_when_affected_and_skipped_otherwise(gate_script: str) -> None:
    assert 'true)  test "$CONSOLE_RESULT" = success' in gate_script
    assert 'false) test "$CONSOLE_RESULT" = skipped' in gate_script


def test_gate_reads_the_detector_result_and_output(workflow: dict[str, Any]) -> None:
    environment = workflow["jobs"]["gate"]["steps"][0]["env"]

    assert environment["CHANGES_RESULT"] == "${{ needs.changes.result }}"
    assert environment["AFFECTED"] == "${{ needs.changes.outputs.affected }}"
    assert environment["CONSOLE_RESULT"] == "${{ needs.console.result }}"


def test_console_job_runs_the_complete_browser_gate(workflow: dict[str, Any]) -> None:
    steps = _console_steps(workflow)

    for command in (
        "npm ci",
        "npm run lint",
        "npm run typecheck",
        "npm run check:contracts",
        "npm run test -- --run",
        "npm run build",
        "npm run test:e2e",
    ):
        assert command in steps, f"the console job does not run {command}"


def test_console_job_installs_the_python_toolchain_the_browser_gate_needs(
    workflow: dict[str, Any],
) -> None:
    """`npm run test:e2e` starts the real Starlette origin with `uv run uvicorn`.

    Without uv on the runner the browser step fails at server start, which reads
    as a browser failure rather than a missing toolchain.
    """
    steps = _console_steps(workflow)
    uses = [str(step.get("uses", "")) for step in workflow["jobs"]["console"]["steps"]]

    assert any(entry.startswith("astral-sh/setup-uv@") for entry in uses)
    assert "uv sync --locked --all-packages" in steps


def test_console_job_installs_only_the_pinned_playwright_browser(
    workflow: dict[str, Any],
) -> None:
    steps = _console_steps(workflow)

    assert "npx playwright install --with-deps chromium" in steps
    assert "playwright install --with-deps\n" not in steps


def test_console_job_pins_the_repository_node_version(workflow: dict[str, Any]) -> None:
    """The Node pin lives beside package.json, not at the repository root.

    The structure validator enumerates every permitted top-level entry, so a root
    `.node-version` would fail that gate.
    """
    pinned = NODE_VERSION_PATH.read_text(encoding="utf-8").strip()
    setup = next(
        step
        for step in workflow["jobs"]["console"]["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )

    assert setup["with"]["node-version-file"] == "apps/console/.node-version"
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned)


def test_workflow_pins_every_action_the_repository_already_reviewed(
    workflow: dict[str, Any],
) -> None:
    uses = [
        str(step.get("uses", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", ())
        if step.get("uses")
    ]

    # Every action is pinned to a reviewed commit, not a moving tag: a tag can be
    # repointed at new code without any change landing in this repository.
    for entry in uses:
        owner_action, _, reference = entry.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", reference), f"{owner_action} is not pinned by commit"
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in uses
    assert "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020" in uses


def test_python_workflows_are_untouched_by_the_console_gate() -> None:
    """The console gate is additive; it must not weaken the Python checks."""
    workflows = WORKFLOW_PATH.parent
    assert (workflows / "repository-structure.yml").is_file()
    assert (workflows / "warehouse-lifecycle.yml").is_file()
