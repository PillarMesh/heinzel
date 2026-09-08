from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tests.ci.run_plan3a_witness as witness_runner
from tests.acceptance.run_plan3a import (
    REPOSITORY_ROOT,
    Plan3AConfig,
    Plan3APrivateLedger,
    PrivateDockerResourceEntry,
    write_private_ledger,
)
from tests.ci.run_plan3a_witness import (
    DockerSamplingError,
    DockerUsageSample,
    parse_docker_size,
    run_bounded_witness,
    sample_docker_usage,
)


class _IncreasingSampler:
    def __init__(self) -> None:
        self.sample_count = 0

    def __call__(self, timeout_seconds: float) -> DockerUsageSample:
        assert 0 < timeout_seconds <= 5
        self.sample_count += 1
        return DockerUsageSample(
            memory_bytes=self.sample_count * 1024,
            disk_bytes=self.sample_count * 2000,
        )


def _python_command(*, exit_code: int, delay_seconds: float = 0.1) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"import time; time.sleep({delay_seconds}); raise SystemExit({exit_code})",
    )


def _read_cost(path: Path) -> dict[str, object]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _plan3a_environment(private_parent: Path) -> dict[str, str]:
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "PILLARMESH_PLAN3A_STATE_PATH": str(private_parent / "state"),
        "PILLARMESH_PLAN3A_SECRET_DIRECTORY": str(private_parent / "secrets"),
        "PILLARMESH_PLAN3A_BACKUP_DIRECTORY": str(private_parent / "backups"),
        "PILLARMESH_PLAN3A_EVIDENCE_DIRECTORY": str(private_parent / "evidence"),
        "PILLARMESH_PLAN3A_RESERVATION_PATH": str(private_parent / "reservation.json"),
        "PILLARMESH_PLAN3A_SOURCE_COMMIT": source_commit,
        "PILLARMESH_PLAN3A_POSTGRES_IMAGE": "postgres:18.6@sha256:" + "a" * 64,
        "PILLARMESH_PLAN3A_CLICKHOUSE_IMAGE": (
            "clickhouse/clickhouse-server:25.8@sha256:" + "b" * 64
        ),
        "PILLARMESH_PLAN3A_RETENTION_DEADLINE": "2026-08-28T12:00:00Z",
        "PILLARMESH_PLAN3A_CREDENTIAL_CANARIES": json.dumps(
            [f"private-credential-marker-{index}" for index in range(7)]
        ),
        "PILLARMESH_PLAN3A_STATE_ENCRYPTION_KEY": "private-state-encryption-marker",
        "PILLARMESH_PLAN3A_EVIDENCE_SIGNING_KEY": "private-evidence-signing-marker",
    }


def _process_is_absent(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return True
    return False


# Absence is the property; how quickly the kernel reaps is not. Three seconds was
# enough on an idle machine and not on a loaded one, where this failed while the
# runner was behaving correctly.
_ABSENCE_DEADLINE_SECONDS = 30
# Long enough that the witnessed command has recorded its process ids before the
# deadline fires, and far below the sixty seconds the command would otherwise sleep,
# so the timeout is still what ends it.
_STARTUP_TOLERANT_TIMEOUT_SECONDS = 3.0


def _wait_for_process_absence(process_id: int) -> None:
    deadline = time.monotonic() + _ABSENCE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if _process_is_absent(process_id):
            return
        time.sleep(0.02)
    pytest.fail(f"bounded witness left process {process_id} running")


@pytest.mark.parametrize(
    ("encoded", "expected_bytes"),
    (
        ("0B", 0),
        ("1kB", 1000),
        ("1.5MB", 1_500_000),
        ("2GB", 2_000_000_000),
        ("1KiB", 1024),
        ("1.5MiB", 1_572_864),
        ("2GiB", 2_147_483_648),
        ("0.1B", 1),
    ),
)
def test_docker_size_parser_returns_exact_rounded_up_bytes(
    encoded: str,
    expected_bytes: int,
) -> None:
    assert parse_docker_size(encoded) == expected_bytes


@pytest.mark.parametrize(
    "encoded",
    ("", "-1B", "NaNMB", "1XB", "1,024B", "1MB trailing"),
)
def test_docker_size_parser_rejects_ambiguous_or_invalid_values(encoded: str) -> None:
    with pytest.raises(DockerSamplingError, match="invalid numeric size"):
        parse_docker_size(encoded)


def test_docker_sampler_sums_exact_resource_memory_and_disk_with_bounded_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = witness_runner.DockerResourceScope(
        container_identifiers=("pm-exact-a", "pm-exact-b"),
        volume_identifiers=("pm-volume-a", "pm-volume-b"),
    )
    responses = iter(
        (
            "pm-exact-a\npm-exact-b\nunrelated\n",
            "1MiB / 4GiB\n",
            "1024\n",
            "512KiB / 4GiB\n",
            "2048\n",
            json.dumps(
                {
                    "Volumes": [
                        {"Name": "pm-volume-a", "Size": "1.5GB"},
                        {"Name": "pm-volume-b", "Size": "2MB"},
                    ]
                }
            ),
        )
    )
    observed_timeouts: list[float] = []

    def completed_command(
        command: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == "docker"
        assert capture_output is True
        assert check is False
        assert text is True
        observed_timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, next(responses), "")

    monkeypatch.setattr(witness_runner.subprocess, "run", completed_command)

    sample = sample_docker_usage(scope, timeout_seconds=2)

    assert sample == DockerUsageSample(
        memory_bytes=1_572_864,
        disk_bytes=1_502_003_072,
    )
    assert len(observed_timeouts) == 6
    assert all(0 < timeout <= 2 for timeout in observed_timeouts)


def test_docker_sampler_reports_a_sanitized_failure_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=("docker", "stats"), timeout=1)

    monkeypatch.setattr(witness_runner.subprocess, "run", timed_out_command)

    with pytest.raises(DockerSamplingError, match="Docker usage sampling failed"):
        sample_docker_usage(
            witness_runner.DockerResourceScope(
                container_identifiers=("pm-exact",),
                volume_identifiers=(),
            ),
            timeout_seconds=1,
        )


def test_docker_sampler_ignores_unrelated_daemon_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = witness_runner.DockerResourceScope(
        container_identifiers=("pm-exact-container",),
        volume_identifiers=("pm-exact-volume",),
    )
    responses = iter(
        (
            "pm-exact-container\nunrelated-container\n",
            "1MiB / 4GiB\n",
            "2048\n",
            json.dumps(
                {
                    "Volumes": [
                        {"Name": "pm-exact-volume", "Size": "2MB"},
                        {"Name": "unrelated-volume", "Size": "9GB"},
                    ]
                }
            ),
        )
    )

    def completed_command(
        command: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == "docker"
        assert 0 < timeout <= 2
        return subprocess.CompletedProcess(command, 0, next(responses), "")

    monkeypatch.setattr(witness_runner.subprocess, "run", completed_command)

    sample = sample_docker_usage(scope, timeout_seconds=2)

    assert sample == DockerUsageSample(
        memory_bytes=1_048_576,
        disk_bytes=2_002_048,
    )


def test_docker_sampler_tolerates_an_exact_container_removed_during_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = witness_runner.DockerResourceScope(
        container_identifiers=("pm-exact-container",),
        volume_identifiers=(),
    )
    responses = iter(
        (
            subprocess.CompletedProcess(("docker",), 0, "pm-exact-container\n", ""),
            subprocess.CompletedProcess(("docker",), 1, "", "not found"),
            subprocess.CompletedProcess(("docker",), 0, "", ""),
        )
    )

    def completed_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(responses)

    monkeypatch.setattr(witness_runner.subprocess, "run", completed_command)

    sample = sample_docker_usage(scope, timeout_seconds=2)

    assert sample == DockerUsageSample(memory_bytes=0, disk_bytes=0)


def test_docker_sampler_retries_volume_measurement_during_an_exact_removal_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = witness_runner.DockerResourceScope(
        container_identifiers=(),
        volume_identifiers=("pm-exact-volume",),
    )
    responses = iter(
        (
            subprocess.CompletedProcess(("docker",), 0, "", ""),
            subprocess.CompletedProcess(("docker",), 1, "", "volume changed"),
            subprocess.CompletedProcess(
                ("docker",),
                0,
                json.dumps({"Volumes": []}),
                "",
            ),
        )
    )

    def completed_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(responses)

    monkeypatch.setattr(witness_runner.subprocess, "run", completed_command)

    sample = sample_docker_usage(scope, timeout_seconds=2)

    assert sample == DockerUsageSample(memory_bytes=0, disk_bytes=0)


def test_docker_sampler_rejects_a_persistent_volume_measurement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = witness_runner.DockerResourceScope(
        container_identifiers=(),
        volume_identifiers=("pm-exact-volume",),
    )

    def failed_command(
        command: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ("container", "ls"):
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "volume changed")

    monkeypatch.setattr(witness_runner.subprocess, "run", failed_command)

    with pytest.raises(DockerSamplingError, match="Docker usage sampling failed"):
        sample_docker_usage(scope, timeout_seconds=2)


def test_successful_witness_records_sanitized_numeric_peaks(tmp_path: Path) -> None:
    output = tmp_path / "plan3a-cost.json"
    sampler = _IncreasingSampler()

    exit_code = run_bounded_witness(
        _python_command(exit_code=0),
        output_path=output,
        timeout_seconds=2,
        sample_interval_seconds=0.01,
        sampler=sampler,
    )
    cost = _read_cost(output)

    assert exit_code == 0
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
    assert cost["peak_docker_memory_bytes"] >= 1024
    assert cost["peak_docker_disk_bytes"] >= 2000
    assert cost["sample_count"] == sampler.sample_count
    assert cost["sample_count"] >= 1
    assert cost["timed_out"] is False


def test_failing_witness_preserves_exit_status_and_still_records_cost(tmp_path: Path) -> None:
    output = tmp_path / "plan3a-cost.json"

    exit_code = run_bounded_witness(
        _python_command(exit_code=7),
        output_path=output,
        timeout_seconds=2,
        sample_interval_seconds=0.01,
        sampler=_IncreasingSampler(),
    )

    assert exit_code == 7
    assert _read_cost(output)["timed_out"] is False


def test_timeout_terminates_the_witness_process_group_and_records_timeout(tmp_path: Path) -> None:
    output = tmp_path / "plan3a-cost.json"
    process_ids = tmp_path / "process-ids"
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}'); "
            "time.sleep(60)"
        ),
        str(process_ids),
    )

    exit_code = run_bounded_witness(
        command,
        output_path=output,
        timeout_seconds=_STARTUP_TOLERANT_TIMEOUT_SECONDS,
        sample_interval_seconds=0.01,
        sampler=_IncreasingSampler(),
    )
    parent_process_id, child_process_id = (
        int(value) for value in process_ids.read_text(encoding="utf-8").split()
    )

    assert exit_code == 124
    assert _read_cost(output)["timed_out"] is True
    _wait_for_process_absence(parent_process_id)
    _wait_for_process_absence(child_process_id)


def test_hard_deadline_immediately_kills_a_sigterm_ignoring_process_group(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan3a-cost.json"
    process_ids = tmp_path / "process-ids"
    child_ready = tmp_path / "child-ready"
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib,signal,subprocess,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)',sys.argv[2]])\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.001)\n"
            "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
            "time.sleep(60)"
        ),
        str(process_ids),
        str(child_ready),
    )

    started = time.monotonic()
    exit_code = run_bounded_witness(
        command,
        output_path=output,
        timeout_seconds=_STARTUP_TOLERANT_TIMEOUT_SECONDS,
        sample_interval_seconds=0.01,
        sampler=_IncreasingSampler(),
    )
    elapsed_seconds = time.monotonic() - started
    parent_process_id, child_process_id = (
        int(value) for value in process_ids.read_text(encoding="utf-8").split()
    )

    assert exit_code == 124
    assert _read_cost(output)["timed_out"] is True
    # The property is that the runner escalated instead of waiting out its own
    # termination grace. An implementation that waits takes the timeout plus the whole
    # grace; this bound leaves half the grace as slack, which is about the
    # implementation rather than about how fast the machine starts interpreters.
    assert elapsed_seconds < _STARTUP_TOLERANT_TIMEOUT_SECONDS + (
        witness_runner._TERMINATION_GRACE_SECONDS / 2
    )
    _wait_for_process_absence(parent_process_id)
    _wait_for_process_absence(child_process_id)
    assert witness_runner._process_group_is_absent(parent_process_id)


def test_a_slow_reap_is_waited_out_rather_than_reported_as_a_termination_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loaded machine reaps a killed process group slowly; that is not a failure.

    The runner used one constant for two different things: how long it politely waits
    before escalating, and how long it waits to observe that the kernel has reaped the
    group. Five seconds is right for the first and far too short for the second -
    under load the group was gone but not yet observed absent, so the runner reported
    `126` termination-failure for a command that had simply timed out, and a loaded CI
    runner would fail the build on it.

    Both constants are shortened here so the test stays fast; what it pins is that the
    observation is bounded by the verification deadline and not by the grace.
    """
    output = tmp_path / "plan3a-cost.json"
    real_absence = witness_runner._process_group_is_absent
    monkeypatch.setattr(witness_runner, "_TERMINATION_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(witness_runner, "_TERMINATION_VERIFICATION_SECONDS", 3.0)
    unobservable_until = time.monotonic() + 1.0

    def slow_to_observe(process_group_id: int) -> bool:
        # Gone in truth, but not yet observable - longer than the grace, well inside
        # the verification deadline.
        if time.monotonic() < unobservable_until:
            return False
        return bool(real_absence(process_group_id))

    monkeypatch.setattr(witness_runner, "_process_group_is_absent", slow_to_observe)

    exit_code = run_bounded_witness(
        _python_command(exit_code=0, delay_seconds=60),
        output_path=output,
        timeout_seconds=0.2,
        sample_interval_seconds=0.01,
        sampler=_IncreasingSampler(),
    )

    assert exit_code == witness_runner.TIMEOUT_EXIT_CODE
    assert _read_cost(output)["timed_out"] is True


def test_a_process_group_the_runner_may_no_longer_signal_counts_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EPERM` from `killpg` means the group is not ours, not that it is running.

    Under load `os.killpg(pgid, 0)` answers `EPERM` once the group identifier has
    been recycled away from us. The runner treated every errno except `ESRCH` as a
    verification failure, so a command that had timed out cleanly, with its group
    gone, was reported as `126` termination-failure and would fail a build.

    Anything else stays a failure: the runner must not decide a group is gone because
    it could not tell.
    """
    absent_calls: list[int] = []

    def denied(process_group_id: int, signal_number: int) -> None:
        absent_calls.append(signal_number)
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(witness_runner.os, "killpg", denied)
    assert witness_runner._process_group_is_absent(4242) is True
    assert absent_calls == [0]

    def broken(process_group_id: int, signal_number: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(witness_runner.os, "killpg", broken)
    with pytest.raises(witness_runner.ProcessTerminationError):
        witness_runner._process_group_is_absent(4242)


def test_command_ceiling_is_enforced_even_when_the_sampler_blocks(tmp_path: Path) -> None:
    output = tmp_path / "plan3a-cost.json"

    def blocking_sampler(timeout_seconds: float) -> DockerUsageSample:
        time.sleep(timeout_seconds + 0.4)
        return DockerUsageSample(memory_bytes=1, disk_bytes=1)

    exit_code = run_bounded_witness(
        _python_command(exit_code=0, delay_seconds=0.3),
        output_path=output,
        timeout_seconds=0.2,
        sample_interval_seconds=0.01,
        sampler=blocking_sampler,
    )

    assert exit_code == 124
    assert _read_cost(output)["timed_out"] is True


def test_command_ceiling_wins_when_command_exits_during_an_indefinitely_blocked_sampler(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan3a-cost.json"
    sampler_release = threading.Event()

    def blocking_sampler(timeout_seconds: float) -> DockerUsageSample:
        assert timeout_seconds <= 0.2
        sampler_release.wait()
        return DockerUsageSample(memory_bytes=1, disk_bytes=1)

    started = time.monotonic()
    try:
        exit_code = run_bounded_witness(
            _python_command(exit_code=0, delay_seconds=0.05),
            output_path=output,
            timeout_seconds=0.2,
            sample_interval_seconds=0.01,
            sampler=blocking_sampler,
        )
    finally:
        sampler_release.set()

    # Only a deadline that never applied could exceed this: the sampler stays
    # blocked until the `finally`, so the call returning at all is the property.
    # A bound near the 0.2s timeout measures interpreter startup instead.
    assert time.monotonic() - started < 10
    assert exit_code == 124
    assert _read_cost(output)["timed_out"] is True


def test_sampler_failure_after_the_wall_clock_deadline_is_a_timeout(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan3a-cost.json"

    def late_sampler_failure(timeout_seconds: float) -> DockerUsageSample:
        time.sleep(timeout_seconds + 0.2)
        raise DockerSamplingError("Docker usage sampling failed")

    exit_code = run_bounded_witness(
        _python_command(exit_code=0, delay_seconds=0.05),
        output_path=output,
        timeout_seconds=0.2,
        sample_interval_seconds=0.01,
        sampler=late_sampler_failure,
    )

    assert exit_code == 124
    assert _read_cost(output)["timed_out"] is True


def test_sampler_failure_fails_and_terminates_the_witness_without_fake_zero_success(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan3a-cost.json"
    process_id_path = tmp_path / "process-id"
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "time.sleep(60)"
        ),
        str(process_id_path),
    )

    def failed_sampler(timeout_seconds: float) -> DockerUsageSample:
        deadline = time.monotonic() + timeout_seconds
        while not process_id_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise DockerSamplingError("private diagnostic must not be emitted")

    exit_code = run_bounded_witness(
        command,
        output_path=output,
        timeout_seconds=2,
        sample_interval_seconds=0.01,
        sampler=failed_sampler,
    )
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    cost = _read_cost(output)

    assert exit_code == 125
    assert cost["sample_count"] == 0
    assert cost["timed_out"] is False
    assert "private diagnostic" not in output.read_text(encoding="utf-8")
    _wait_for_process_absence(process_id)


def test_unverified_process_group_termination_returns_a_distinct_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "plan3a-cost.json"
    process_id_path = tmp_path / "process-ids"
    command = (
        sys.executable,
        "-c",
        (
            "import os,pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}'); "
            "time.sleep(60)"
        ),
        str(process_id_path),
    )
    original_killpg = os.killpg

    def denied_group_signal(process_group_id: int, signal_number: int) -> None:
        if signal_number == signal.SIGTERM:
            raise PermissionError("simulated denied graceful termination")
        original_killpg(process_group_id, signal_number)

    def failed_sampler(timeout_seconds: float) -> DockerUsageSample:
        deadline = time.monotonic() + timeout_seconds
        while not process_id_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise DockerSamplingError("Docker usage sampling failed")

    monkeypatch.setattr(witness_runner.os, "killpg", denied_group_signal)

    exit_code = run_bounded_witness(
        command,
        output_path=output,
        timeout_seconds=2,
        sample_interval_seconds=0.01,
        sampler=failed_sampler,
    )
    parent_process_id, child_process_id = (
        int(value) for value in process_id_path.read_text(encoding="utf-8").split()
    )

    assert exit_code == 126
    assert _read_cost(output)["sample_count"] == 0
    _wait_for_process_absence(parent_process_id)
    _wait_for_process_absence(child_process_id)


def test_cost_artifact_contains_no_command_private_marker_or_path(tmp_path: Path) -> None:
    private_marker = "tenant-private-marker"
    output = tmp_path / "private-path" / "plan3a-cost.json"
    output.parent.mkdir()

    exit_code = run_bounded_witness(
        _python_command(exit_code=0),
        output_path=output,
        timeout_seconds=2,
        sample_interval_seconds=0.01,
        sampler=_IncreasingSampler(),
    )
    encoded = output.read_text(encoding="utf-8")

    assert exit_code == 0
    assert private_marker not in encoded
    assert str(output.parent) not in encoded
    assert "python" not in encoded


def test_runner_rejects_a_ceiling_above_ten_minutes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot exceed 600 seconds"):
        run_bounded_witness(
            _python_command(exit_code=0),
            output_path=tmp_path / "plan3a-cost.json",
            timeout_seconds=601,
            sample_interval_seconds=0.01,
            sampler=_IncreasingSampler(),
        )


def test_cli_runs_the_bounded_command_and_writes_only_numeric_cost(tmp_path: Path) -> None:
    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    docker = command_directory / "docker"
    docker.write_text(
        """#!/usr/bin/env sh
set -eu
case "${1:-} ${2:-}" in
  'container ls') printf 'pm-exact-container\\nunrelated-container\\n' ;;
  'stats --all') printf '1MiB / 4GiB\\n' ;;
  'container inspect') printf '1024\\n' ;;
  'system df')
    printf '%s' '{"Volumes":[{"Name":"pm-exact-volume","Size":"2MB"},'
    printf '%s\\n' '{"Name":"unrelated-volume","Size":"9GB"}]}'
    ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    output = tmp_path / "plan3a-cost.json"
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    environment = _plan3a_environment(private_parent)
    config = Plan3AConfig.from_environment(environment)
    deadline = datetime(2026, 8, 28, 12, tzinfo=UTC)
    ledger = Plan3APrivateLedger.create(
        run_id="6" * 64,
        reservation_digest="7" * 64,
        resources=(
            PrivateDockerResourceEntry.create(
                engine_kind="postgresql",
                compose_resource_kind="container",
                exact_identifier="pm-exact-container",
                retention_deadline=deadline,
            ),
            PrivateDockerResourceEntry.create(
                engine_kind="postgresql",
                compose_resource_kind="volume",
                exact_identifier="pm-exact-volume",
                retention_deadline=deadline,
            ),
        ),
        signing_key=config.signing_key,
    )
    write_private_ledger(config, ledger)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.ci.run_plan3a_witness",
            "--output",
            str(output),
            # Generous on purpose: this asserts the CLI runs its command and writes
            # numeric cost, and the budget has to cover two interpreter startups
            # before the command even begins. A tight value measures host load and
            # fails as a spurious timeout.
            "--timeout-seconds",
            "60",
            "--sample-interval-seconds",
            "0.01",
            "--",
            *_python_command(exit_code=0),
        ],
        capture_output=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            **environment,
            "PATH": f"{command_directory}:{os.environ['PATH']}",
        },
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    cost = _read_cost(output)
    assert cost["peak_docker_memory_bytes"] == 1_048_576
    assert cost["peak_docker_disk_bytes"] == 2_001_024
    assert cost["sample_count"] >= 1
    assert cost["timed_out"] is False
