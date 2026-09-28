"""Contract tests for the Offline assurance workflow: triggers, action pins and the DCO check.

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
    # PyYAML resolves an unquoted `on:` key to the boolean True. Normalise it here so
    # every key really is the str this returns, and callers can index "on" directly.
    return {("on" if key is True else key): value for key, value in parsed.items()}


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow["on"]
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


_CHECKOUT_PIN = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps = workflow["jobs"]["validate"]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _steps(workflow) if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def test_checkout_is_pinned_to_a_reviewed_commit_with_full_history(
    workflow: dict[str, Any],
) -> None:
    checkout = _step(workflow, "Check out repository")

    assert checkout["uses"] == _CHECKOUT_PIN
    # check_dco.py refuses a shallow clone, which would hide commits outside the fetched depth.
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False


def test_every_action_is_pinned_to_a_full_commit_sha(workflow: dict[str, Any]) -> None:
    for step in _steps(workflow):
        uses = step.get("uses")
        if uses is None:
            continue
        _, _, ref = uses.partition("@")
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), uses


def test_pull_requests_check_dco_sign_off_on_the_head_commit_right_after_checkout(
    workflow: dict[str, Any],
) -> None:
    names = [step.get("name") for step in _steps(workflow)]
    dco = _step(workflow, "Check DCO sign-off")

    assert names.index("Check DCO sign-off") == names.index("Check out repository") + 1
    # Dependabot cannot sign off. Both the pull request author and the triggering actor must be
    # Dependabot, so a person pushing to a Dependabot branch is still checked.
    assert " ".join(dco["if"].split()) == (
        "github.event_name == 'pull_request' && "
        "!(github.event.pull_request.user.login == 'dependabot[bot]' && "
        "github.actor == 'dependabot[bot]')"
    )
    # github.sha on a pull request is GitHub's unsigned test-merge commit, never the head.
    assert dco["env"] == {
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    # Expressions reach the shell only through the environment, never interpolated into it.
    assert dco["run"] == 'python3 tests/ci/check_dco.py "$BASE_SHA" "$HEAD_SHA"'
    assert "${{" not in dco["run"]


def test_every_pull_request_scans_the_committed_tree_with_a_digest_pinned_gitleaks(
    workflow: dict[str, Any],
) -> None:
    scan = _step(workflow, "Scan the tree for secrets")["run"]

    assert "git archive HEAD" in scan
    assert (
        "ghcr.io/gitleaks/gitleaks@sha256:"
        "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
    ) in scan
    assert "--config /repo/.gitleaks.toml" in scan
    assert "--redact" in scan
    assert "if" not in _step(workflow, "Scan the tree for secrets")
