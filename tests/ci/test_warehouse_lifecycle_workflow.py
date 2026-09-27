"""Contract tests for the warehouse lifecycle CI gate.

The gate is a required check, so its job graph is a contract: the live lifecycle
must run whenever warehouse code changes, and the gate must fail closed whenever
it cannot prove that it did.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/warehouse-lifecycle.yml"
POSTGRES_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
CLICKHOUSE_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@sha256:"
    "7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
AFFECTED_PATHS = (
    "services/warehouse-control/",
    "providers/postgresql/",
    "providers/clickhouse/",
    "packages/provider-sdk/",
    "packages/contract-model/",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "tests/emulators/warehouses/",
    "tests/acceptance/",
    "tests/conformance/",
    "tests/fault-injection/",
    "tests/end-to-end/",
    "tests/integration/test_postgresql_warehouse_live",
    "tests/integration/test_clickhouse_warehouse_live",
    "tests/ci/run_warehouse_lifecycle_witness.py",
    "tests/ci/test_warehouse_lifecycle_workflow",
    ".github/workflows/warehouse-lifecycle",
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # PyYAML resolves an unquoted `on:` key to the boolean True. Normalise it here so
    # every key really is the str this returns, and callers can index "on" directly.
    return {("on" if key is True else key): value for key, value in parsed.items()}


@pytest.fixture(scope="module")
def gate_script(workflow: dict[str, Any]) -> str:
    steps = workflow["jobs"]["gate"]["steps"]
    return "\n".join(str(step.get("run", "")) for step in steps)


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _job_step(workflow: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)


def _install_fake_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    command.chmod(0o755)


def _install_fake_witness_toolchain(command_directory: Path) -> None:
    _install_fake_command(
        command_directory,
        "uv",
        """
printf '%s\n' "$*" >> "$FAKE_UV_LOG"
if test "${1:-}" = run && test "${2:-}" = python \
  && test "${3:-}" = -m && test "${4:-}" = tests.ci.run_warehouse_lifecycle_witness; then
  shift 2
  exec "$FAKE_PYTHON" "$@"
fi
sleep 0.1
exit "$FAKE_WITNESS_STATUS"
""",
    )
    _install_fake_command(
        command_directory,
        "docker",
        """
case "${1:-} ${2:-}" in
  'stats --no-stream') printf '2MiB / 4GiB\n' ;;
  'system df') printf '3MB\n' ;;
  *) exit 91 ;;
esac
""",
    )


def _witness_private_environment(runner_directory: Path) -> dict[str, str]:
    private_root = runner_directory / "warehouse-lifecycle-private"
    private_root.mkdir(mode=0o700)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=WORKFLOW_PATH.parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH": str(private_root / "state"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_SECRET_DIRECTORY": str(private_root / "secrets"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_BACKUP_DIRECTORY": str(private_root / "backups"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY": str(private_root / "evidence"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_RESERVATION_PATH": str(private_root / "reservation.json"),
        "HEINZEL_WAREHOUSE_LIFECYCLE_SOURCE_COMMIT": source_commit,
        "HEINZEL_WAREHOUSE_LIFECYCLE_POSTGRES_IMAGE": POSTGRES_IMAGE,
        "HEINZEL_WAREHOUSE_LIFECYCLE_CLICKHOUSE_IMAGE": CLICKHOUSE_IMAGE,
        "HEINZEL_WAREHOUSE_LIFECYCLE_RETENTION_DEADLINE": "2026-08-28T12:00:00Z",
        "HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES": json.dumps(
            [f"private-credential-marker-{index}" for index in range(8)]
        ),
        "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_ENCRYPTION_KEY": "private-state-encryption-marker",
        "HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_SIGNING_KEY": "private-evidence-signing-marker",
    }


def _run_step(
    step: dict[str, Any],
    *,
    tmp_path: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", str(step["run"])],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env={**os.environ, **environment},
        text=True,
    )


def _commit_pure_rename(
    repository: Path,
    *,
    source_path: str,
    destination_path: str,
) -> str:
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "workflow-test@heinzel.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Heinzel Workflow Test"],
        cwd=repository,
        check=True,
    )
    source = repository / source_path
    source.parent.mkdir(parents=True)
    source.write_text("rename detector fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture: add source"],
        cwd=repository,
        check=True,
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    destination = repository / destination_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture: rename source"],
        cwd=repository,
        check=True,
    )
    return before


def test_workflow_runs_on_every_pull_request_push_and_manual_dispatch(
    workflow: dict[str, Any],
) -> None:
    assert set(_triggers(workflow)) == {"pull_request", "push", "workflow_dispatch"}


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
    assert set(gate["needs"]) == {"changes", "lifecycle"}


def test_change_detector_covers_every_component_the_lifecycle_depends_on(
    workflow: dict[str, Any],
) -> None:
    detector = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["changes"]["steps"])
    for path in AFFECTED_PATHS:
        assert path in detector, f"the change detector does not cover {path}"


@pytest.mark.parametrize(
    "changed_path",
    (
        "packages/provider-sdk/tests/test_models.py",
        "packages/contract-model/src/heinzel_contract_model/artifacts.py",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "tests/fault-injection/test_warehouse_lifecycle_fault_matrix.py",
        "tests/end-to-end/test_managed_warehouse_lifecycle.py",
        "tests/integration/test_postgresql_warehouse_live.py",
        "tests/integration/test_clickhouse_warehouse_live.py",
        "tests/ci/run_warehouse_lifecycle_witness.py",
    ),
)
def test_change_detector_runs_for_every_lifecycle_dependency(
    workflow: dict[str, Any],
    tmp_path: Path,
    changed_path: str,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    _install_fake_command(
        command_directory,
        "git",
        """
case "${1:-}" in
  cat-file) exit 0 ;;
  diff) printf '%s\0' "$FAKE_CHANGED_PATH" ;;
  *) exit 0 ;;
esac
""",
    )
    output = tmp_path / "github-output"
    completed = _run_step(
        _job_step(workflow, "changes", "Detect warehouse lifecycle changes"),
        tmp_path=tmp_path,
        environment={
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "EVENT_NAME": "push",
            "BASE_REF": "",
            "BEFORE": "a" * 40,
            "FAKE_CHANGED_PATH": changed_path,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "affected=true\n"


@pytest.mark.parametrize("failure", ("fetch", "diff"))
def test_change_detector_failure_fails_open_to_running_the_lifecycle(
    workflow: dict[str, Any],
    tmp_path: Path,
    failure: str,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    _install_fake_command(
        command_directory,
        "git",
        """
case "${1:-}" in
  fetch) test "$FAKE_FAILURE" != fetch ;;
  cat-file) exit 0 ;;
  diff)
    test "$FAKE_FAILURE" != diff
    printf '%s\0' 'docs/unrelated.md'
    ;;
  *) exit 0 ;;
esac
""",
    )
    output = tmp_path / "github-output"
    event_environment = (
        {"EVENT_NAME": "pull_request", "BASE_REF": "main", "BEFORE": ""}
        if failure == "fetch"
        else {"EVENT_NAME": "push", "BASE_REF": "", "BEFORE": "a" * 40}
    )
    completed = _run_step(
        _job_step(workflow, "changes", "Detect warehouse lifecycle changes"),
        tmp_path=tmp_path,
        environment={
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "FAKE_FAILURE": failure,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
            **event_environment,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "affected=true\n"


def test_change_detector_consumes_a_large_diff_after_an_early_match(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    _install_fake_command(
        command_directory,
        "git",
        """
case "${1:-}" in
  cat-file) exit 0 ;;
  diff)
    printf '%s\0' 'packages/provider-sdk/first.py'
    for index in $(seq 1 50000); do
      printf 'docs/unrelated-%s-with-padding-to-fill-the-pipe-buffer.md\0' "$index"
    done
    ;;
  *) exit 0 ;;
esac
""",
    )
    output = tmp_path / "github-output"

    completed = _run_step(
        _job_step(workflow, "changes", "Detect warehouse lifecycle changes"),
        tmp_path=tmp_path,
        environment={
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "EVENT_NAME": "push",
            "BASE_REF": "",
            "BEFORE": "a" * 40,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "affected=true\n"

    detector = str(_job_step(workflow, "changes", "Detect warehouse lifecycle changes")["run"])
    assert "grep -q" not in detector


def test_change_detector_fails_closed_for_a_newline_in_a_path(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    _install_fake_command(
        command_directory,
        "git",
        """
case "${1:-}" in
  cat-file) exit 0 ;;
  diff) printf 'docs/unrelated\nambiguous.md\0' ;;
  *) exit 0 ;;
esac
""",
    )
    output = tmp_path / "github-output"

    completed = _run_step(
        _job_step(workflow, "changes", "Detect warehouse lifecycle changes"),
        tmp_path=tmp_path,
        environment={
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "EVENT_NAME": "push",
            "BASE_REF": "",
            "BEFORE": "a" * 40,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "affected=true\n"


@pytest.mark.parametrize(
    ("source_path", "destination_path"),
    (
        ("packages/provider-sdk/source.py", "docs/unrelated.py"),
        ("docs/unrelated.py", "packages/provider-sdk/destination.py"),
        ("packages/provider-sdk/source [odd]\nname.py", "docs/unrelated [odd].py"),
        ("docs/unrelated [odd]\nname.py", "packages/provider-sdk/destination [odd].py"),
        ("tests/ci/run_warehouse_lifecycle_witness.py", "docs/unrelated-witness.py"),
        ("docs/unrelated-witness.py", "tests/ci/run_warehouse_lifecycle_witness.py"),
    ),
)
def test_change_detector_evaluates_both_paths_of_a_pure_rename(
    workflow: dict[str, Any],
    tmp_path: Path,
    source_path: str,
    destination_path: str,
) -> None:
    repository = tmp_path / "repository"
    before = _commit_pure_rename(
        repository,
        source_path=source_path,
        destination_path=destination_path,
    )
    output = tmp_path / "github-output"

    completed = _run_step(
        _job_step(workflow, "changes", "Detect warehouse lifecycle changes"),
        tmp_path=repository,
        environment={
            "EVENT_NAME": "push",
            "BASE_REF": "",
            "BEFORE": before,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == "affected=true\n"


def test_live_job_runs_only_when_an_affected_path_changed(workflow: dict[str, Any]) -> None:
    lifecycle = workflow["jobs"]["lifecycle"]
    assert lifecycle["needs"] == "changes" or "changes" in lifecycle["needs"]
    assert str(lifecycle["if"]).strip() == "needs.changes.outputs.affected == 'true'"


def test_manual_dispatch_forces_the_live_job(workflow: dict[str, Any]) -> None:
    detector = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["changes"]["steps"])
    assert re.search(r"workflow_dispatch.*\n\s*echo .affected=true", detector)


def test_gate_fails_when_the_change_detector_did_not_succeed(gate_script: str) -> None:
    assert 'test "$CHANGES_RESULT" = success || exit 1' in gate_script


def test_gate_fails_on_an_empty_or_unexpected_affected_value(gate_script: str) -> None:
    # A `case` default that exits nonzero is what stops an empty AFFECTED from
    # reading as "unrelated change".
    assert re.search(r"\*\)\s*exit 1", gate_script)


def test_gate_requires_success_when_affected_and_skipped_otherwise(gate_script: str) -> None:
    assert 'true)  test "$LIFECYCLE_RESULT" = success' in gate_script
    assert 'false) test "$LIFECYCLE_RESULT" = skipped' in gate_script


def test_gate_reads_the_detector_result_and_output(workflow: dict[str, Any]) -> None:
    environment = workflow["jobs"]["gate"]["steps"][0]["env"]
    assert environment["CHANGES_RESULT"] == "${{ needs.changes.result }}"
    assert environment["AFFECTED"] == "${{ needs.changes.outputs.affected }}"
    assert environment["LIFECYCLE_RESULT"] == "${{ needs.lifecycle.result }}"


def test_live_job_pins_both_engine_images_by_digest(workflow: dict[str, Any]) -> None:
    lifecycle = "\n".join(
        str(step.get("run", "")) for step in workflow["jobs"]["lifecycle"]["steps"]
    )
    assert POSTGRES_IMAGE in lifecycle
    assert CLICKHOUSE_IMAGE in lifecycle


def test_private_environment_masks_generated_credentials_before_export(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    prepare_step = next(
        step
        for step in workflow["jobs"]["lifecycle"]["steps"]
        if step.get("name") == "Prepare the private witness environment"
    )
    github_environment = tmp_path / "github-environment"
    runner_directory = tmp_path / "runner"
    runner_directory.mkdir()
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(runner_directory),
        "GITHUB_ENV": str(github_environment),
    }

    completed = subprocess.run(
        ["bash", "-c", str(prepare_step["run"])],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    exported = dict(
        line.split("=", 1) for line in github_environment.read_text(encoding="utf-8").splitlines()
    )
    generated_values = (
        exported["HEINZEL_WAREHOUSE_LIFECYCLE_STATE_ENCRYPTION_KEY"],
        exported["HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_SIGNING_KEY"],
        *json.loads(exported["HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES"]),
    )
    masked_values = tuple(
        line.removeprefix("::add-mask::")
        for line in completed.stdout.splitlines()
        if line.startswith("::add-mask::")
    )

    def digest_value(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    assert len(masked_values) == len(generated_values) == 10
    assert {digest_value(value) for value in masked_values} == {
        digest_value(value) for value in generated_values
    }


def test_generated_canaries_satisfy_the_acceptance_parser(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The workflow and the harness must agree on what a valid canary set is.

    The harness raised its minimum from seven to eight canaries when the answer runtime
    principal was added, but the workflow kept generating seven. Every live lifecycle run then
    failed before doing any work, surfacing only as "Docker usage sampling failed" because
    the witness parses the environment inside its scope loader. Execute the real prepare step
    and feed its output to the real parser, so the two cannot drift apart again.
    """
    from tests.acceptance.run_warehouse_lifecycle import _parse_canaries

    prepare_step = next(
        step
        for step in workflow["jobs"]["lifecycle"]["steps"]
        if step.get("name") == "Prepare the private witness environment"
    )
    github_environment = tmp_path / "github-environment"
    runner_directory = tmp_path / "runner"
    runner_directory.mkdir()
    completed = subprocess.run(
        ["bash", "-c", str(prepare_step["run"])],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "RUNNER_TEMP": str(runner_directory),
            "GITHUB_ENV": str(github_environment),
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    exported = dict(
        line.split("=", 1) for line in github_environment.read_text(encoding="utf-8").splitlines()
    )

    _parse_canaries(exported["HEINZEL_WAREHOUSE_LIFECYCLE_CREDENTIAL_CANARIES"])


def test_live_job_executes_only_the_task9_acceptance_authority(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    log = tmp_path / "uv-log"
    _install_fake_witness_toolchain(command_directory)
    runner_directory = tmp_path / "runner"
    runner_directory.mkdir()
    completed = _run_step(
        _job_step(workflow, "lifecycle", "Run witnessed warehouse lifecycle acceptance"),
        tmp_path=tmp_path,
        environment={
            **_witness_private_environment(runner_directory),
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
            "FAKE_PYTHON": sys.executable,
            "FAKE_WITNESS_STATUS": "0",
            "PYTHONPATH": str(WORKFLOW_PATH.parents[2]),
            "RUNNER_TEMP": str(runner_directory),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
    )

    assert completed.returncode == 0, completed.stderr
    invocations = log.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 2
    assert invocations[0].startswith("run python -m tests.ci.run_warehouse_lifecycle_witness ")
    assert "--timeout-seconds 600" in invocations[0]
    assert invocations[1] == (
        "run python -m tests.acceptance.run_warehouse_lifecycle run --authorize-retention-cleanup"
    )
    cost = json.loads(
        (runner_directory / "warehouse-lifecycle-cost.json").read_text(encoding="utf-8")
    )
    assert cost["sample_count"] >= 1


def test_witness_failure_records_numeric_cost_without_changing_exit_status(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    log = tmp_path / "uv-log"
    _install_fake_witness_toolchain(command_directory)
    runner_directory = tmp_path / "runner"
    runner_directory.mkdir()
    completed = _run_step(
        _job_step(workflow, "lifecycle", "Run witnessed warehouse lifecycle acceptance"),
        tmp_path=tmp_path,
        environment={
            **_witness_private_environment(runner_directory),
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
            "FAKE_PYTHON": sys.executable,
            "FAKE_WITNESS_STATUS": "7",
            "PYTHONPATH": str(WORKFLOW_PATH.parents[2]),
            "RUNNER_TEMP": str(runner_directory),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
    )

    cost = json.loads(
        (runner_directory / "warehouse-lifecycle-cost.json").read_text(encoding="utf-8")
    )
    assert completed.returncode == 7
    assert set(cost) == {
        "duration_seconds",
        "peak_docker_memory_bytes",
        "peak_docker_disk_bytes",
        "sample_count",
        "timed_out",
    }
    assert type(cost["duration_seconds"]) is int
    assert type(cost["peak_docker_memory_bytes"]) is int
    assert type(cost["peak_docker_disk_bytes"]) is int
    assert type(cost["sample_count"]) is int
    assert type(cost["timed_out"]) is bool
    assert cost["duration_seconds"] >= 0
    assert cost["peak_docker_memory_bytes"] >= 0
    assert cost["peak_docker_disk_bytes"] >= 0
    assert cost["sample_count"] >= 1
    assert cost["timed_out"] is False


def test_witness_ceiling_leaves_job_time_for_always_teardown(workflow: dict[str, Any]) -> None:
    lifecycle = workflow["jobs"]["lifecycle"]
    witness = _job_step(workflow, "lifecycle", "Run witnessed warehouse lifecycle acceptance")

    assert lifecycle["timeout-minutes"] > 10
    assert "--timeout-seconds 600" in str(witness["run"])


def test_live_job_tears_down_exactly_even_after_failure(workflow: dict[str, Any]) -> None:
    teardown = [
        step
        for step in workflow["jobs"]["lifecycle"]["steps"]
        if "teardown" in str(step.get("name", "")).lower()
    ]
    assert teardown, "the live job has no teardown step"
    for step in teardown:
        assert str(step["if"]).strip() == "always()"
        assert "run_warehouse_lifecycle teardown" in str(step["run"])


def test_teardown_recovers_from_private_state_without_public_evidence(
    workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    log = tmp_path / "uv-log"
    _install_fake_command(
        command_directory,
        "uv",
        'printf \'%s\\n\' "$*" >> "$FAKE_UV_LOG"\n',
    )
    reservation = tmp_path / "reservation.json"
    reservation.write_text("private reservation", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    completed = _run_step(
        _job_step(workflow, "lifecycle", "Exact teardown"),
        tmp_path=tmp_path,
        environment={
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
            "HEINZEL_WAREHOUSE_LIFECYCLE_RESERVATION_PATH": str(reservation),
            "HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH": str(state),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "run python -m tests.acceptance.run_warehouse_lifecycle teardown "
        "--authorize-retention-cleanup"
    ]


def test_successful_witness_strictly_validates_and_uploads_only_public_evidence(
    workflow: dict[str, Any],
) -> None:
    lifecycle_steps = workflow["jobs"]["lifecycle"]["steps"]
    validation = _job_step(workflow, "lifecycle", "Validate sanitized warehouse lifecycle evidence")
    evidence_upload = _job_step(
        workflow, "lifecycle", "Upload sanitized warehouse lifecycle evidence"
    )
    cost_upload = _job_step(workflow, "lifecycle", "Upload the sanitized cost artifact")

    assert str(validation["run"]).strip() == (
        "uv run python -m tests.acceptance.run_warehouse_lifecycle validate-evidence"
    )
    assert evidence_upload["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert cost_upload["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert str(evidence_upload["if"]).strip() == "always()"
    assert evidence_upload["with"] == {
        "name": "warehouse-lifecycle-evidence",
        "path": (
            "${{ env.HEINZEL_WAREHOUSE_LIFECYCLE_EVIDENCE_DIRECTORY }}"
            "/warehouse-lifecycle-evidence.json"
        ),
        "if-no-files-found": "warn",
    }
    assert str(cost_upload["if"]).strip() == "always()"
    assert cost_upload["with"] == {
        "name": "warehouse-lifecycle-cost",
        "path": "${{ runner.temp }}/warehouse-lifecycle-cost.json",
        "if-no-files-found": "error",
    }
    assert lifecycle_steps.index(validation) < lifecycle_steps.index(evidence_upload)
    assert all(
        "private-ledger" not in str(step.get("with", {}).get("path", ""))
        and "reservation" not in str(step.get("with", {}).get("path", ""))
        for step in lifecycle_steps
    )


def test_failed_evidence_validation_still_uploads_the_evidence(workflow: dict[str, Any]) -> None:
    """The validation failure modes are the ones that need the artifact.

    A canary in the evidence, a non-terminal engine result, or a residual
    resource all fail `validate-evidence`. Without `always()` the upload step is
    skipped and the file dies with the runner, leaving a one-line error and
    nothing to inspect.
    """
    steps = workflow["jobs"]["lifecycle"]["steps"]
    validation = _job_step(workflow, "lifecycle", "Validate sanitized warehouse lifecycle evidence")
    evidence_upload = _job_step(
        workflow, "lifecycle", "Upload sanitized warehouse lifecycle evidence"
    )

    assert steps.index(validation) < steps.index(evidence_upload)
    assert str(evidence_upload["if"]).strip() == "always()"
    assert evidence_upload["with"]["if-no-files-found"] == "warn"


def test_every_checkout_records_a_resolvable_source_commit(workflow: dict[str, Any]) -> None:
    """run_warehouse_lifecycle records the checked-out commit as the evidence provenance.

    On a pull_request event `actions/checkout` defaults to the ephemeral
    refs/pull/N/merge commit, which is unreachable from any branch and is
    replaced on the next push, so evidence naming it cannot be reproduced.
    """
    checkouts = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", ())
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts, "the workflow checks out no repository"
    for step in checkouts:
        assert step["with"]["ref"] == ("${{ github.event.pull_request.head.sha || github.sha }}")


def test_push_trigger_does_not_duplicate_the_pull_request_lifecycle(
    workflow: dict[str, Any],
) -> None:
    """A pull request branch push fires both `push` and `pull_request`.

    Unfiltered, that starts two full two-engine Docker lifecycles for one push,
    and a first push to a new branch forces one as well because the detector
    cannot resolve a previous commit.
    """
    push = _triggers(workflow)["push"]

    assert isinstance(push, dict)
    assert push["branches"] == ["main"]


def test_superseded_pull_request_runs_are_cancelled(workflow: dict[str, Any]) -> None:
    concurrency = workflow["concurrency"]

    assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == ("${{ github.event_name == 'pull_request' }}")


def test_live_job_uses_the_repository_pinned_toolchain_actions(workflow: dict[str, Any]) -> None:
    all_steps = tuple(step for job in workflow["jobs"].values() for step in job.get("steps", ()))
    uses = [str(step.get("uses", "")) for step in all_steps]
    assert uses.count("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803") == 2
    assert uses.count("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a") == 2
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in uses, (
        "setup-uv must stay pinned to the repository's reviewed commit"
    )
    workflow_source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0" in workflow_source
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1" in workflow_source
    )
