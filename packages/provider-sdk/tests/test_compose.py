from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread
from typing import IO

import heinzel_provider_sdk.compose as compose_module
import pytest
from heinzel_provider_sdk import (
    MAX_COMPOSE_OUTPUT_BYTES,
    ComposeCommandError,
    DockerComposeProcess,
)


def _docker_process(process_source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(process_source)}\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


_HUNG_CHILD_SLEEP_SECONDS = 10.0


class _ChildNeverBecameReady(BaseException):
    """Raised from the process starter, where the boundary converts every `Exception`.

    The compose boundary reports any failure to start a child as `unavailable`, so a
    readiness failure raised as an ordinary exception would surface as a misleading
    classification instead of the assertion the test is making.
    """


class _StartedChild:
    """Record the child the compose boundary starts, so a test can watch its process id.

    Taking the process id from the parent instead of from a file the child writes
    as its first act keeps a termination test measuring termination, rather than
    whether interpreter startup beat the timeout on a loaded machine.

    A child that must reach a particular state before the timeout is meaningful --
    an installed signal handler, say -- names a `ready_marker` it writes on arrival.
    The boundary starts its timeout only once this starter returns, so waiting here
    gives the child unbounded startup time without spending the timeout budget. The
    wait ends as soon as the child exits, because a child that is gone cannot become
    ready and waiting out the deadline would only hide why it went.
    """

    def __init__(self, *, ready_marker: Path | None = None) -> None:
        self._ready_marker = ready_marker
        self._started: list[subprocess.Popen[bytes]] = []

    def __call__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        started = subprocess.Popen(command, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
        self._started.append(started)
        if self._ready_marker is not None:
            self._await_ready(started, self._ready_marker)
        return started

    @staticmethod
    def _await_ready(
        started: subprocess.Popen[bytes],
        marker: Path,
        *,
        deadline_seconds: float = 60.0,
    ) -> None:
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text():
                return
            exit_status = started.poll()
            if exit_status is not None:
                # Re-read first: a child that wrote its marker and exited immediately
                # is ready, and the two observations are not simultaneous.
                if marker.exists() and marker.read_text():
                    return
                raise _ChildNeverBecameReady(
                    f"the child exited with {exit_status} before reporting that it was ready"
                )
            time.sleep(0.02)
        started.kill()
        started.wait()
        raise _ChildNeverBecameReady("the child never reported that it was ready")

    @property
    def started(self) -> tuple[subprocess.Popen[bytes], ...]:
        return tuple(self._started)

    @property
    def process_id(self) -> int:
        return self._only_child().pid

    @property
    def exit_status(self) -> int | None:
        return self._only_child().returncode

    def _only_child(self) -> subprocess.Popen[bytes]:
        if len(self._started) != 1:
            raise AssertionError(f"expected exactly one child, started {len(self._started)}")
        return self._started[0]


class _FinishedProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdin: IO[bytes] | None = None
        # The test-double process owns these pipes until its test exits.
        self.stdout = tempfile.TemporaryFile()  # noqa: SIM115
        self.stderr = tempfile.TemporaryFile()  # noqa: SIM115
        self.stdout.write(stdout)
        self.stderr.write(stderr)
        self.stdout.seek(0)
        self.stderr.seek(0)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_provider_sdk_exports_compose_process_boundary() -> None:
    assert DockerComposeProcess


def test_compose_verifies_container_and_reference_image_content_ids(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        commands.append(command)
        return _FinishedProcess(stdout=b'"sha256:' + b"a" * 64 + b'"')

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    container_id = process.inspect_container_image_id(identifier="warehouse", environment={})
    reference_id = process.inspect_image_id(identifier="warehouse@sha256:pin", environment={})

    assert container_id == "sha256:" + "a" * 64
    assert reference_id == container_id
    assert commands[0][:6] == [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        "{{json .Image}}",
    ]
    assert commands[1][:4] == ["docker", "image", "inspect", "--format"]


def test_compose_exec_uses_the_explicit_environment_without_inherited_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_environments: list[object] = []
    monkeypatch.setenv("PATH", "/test/bin")
    monkeypatch.setenv("DOCKER_HOST", "tcp://caller.example.test:2376")
    monkeypatch.setenv("HEINZEL_AMBIENT_SECRET", "must-not-cross-boundary")

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        captured_environments.append(env)
        return _FinishedProcess(stdout=b"ready")

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    observed = process.exec(
        project_name="project-a",
        arguments=("ps", "--status", "running"),
        environment={"COMPOSE_PASSWORD": "operation-password"},
    )

    assert observed == b"ready"
    assert captured_environments == [
        {
            "PATH": "/test/bin",
            "DOCKER_HOST": "tcp://caller.example.test:2376",
            "COMPOSE_PASSWORD": "operation-password",
        }
    ]


def test_compose_exec_rejects_output_larger_than_its_bounded_result_limit(tmp_path: Path) -> None:
    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        return _FinishedProcess(stdout=b"x" * (MAX_COMPOSE_OUTPUT_BYTES + 1))

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "output_limit"
    assert str(captured.value) == "Docker Compose command output exceeded the bounded limit"


def test_compose_exec_failure_is_sanitized_and_classified_without_process_output(
    tmp_path: Path,
) -> None:
    private_stdout = b"stdout-with-password"
    private_stderr = b"stderr-with-password"

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        return _FinishedProcess(stdout=private_stdout, stderr=private_stderr, returncode=1)

    process = DockerComposeProcess(compose_file=tmp_path / "private-compose.yaml", run=start)

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "rejected"
    assert str(captured.value) == "Docker Compose command failed"
    assert private_stdout.decode() not in str(captured.value)
    assert private_stderr.decode() not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_compose_exec_routes_a_nonzero_observation_to_the_requested_classification(
    tmp_path: Path,
) -> None:
    process = DockerComposeProcess(
        compose_file=tmp_path / "private-compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: _FinishedProcess(returncode=1),
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(
            project_name="project-a",
            arguments=("inspect", "restore"),
            environment={},
            nonzero_classification="unavailable",
        )

    assert captured.value.classification == "unavailable"


def test_compose_resource_cleanup_is_exact_and_idempotent_and_unknown_inspection_is_visible(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        commands.append(command)
        if command[2] == "rm":
            return _FinishedProcess(
                stdout=b"private stdout",
                stderr=b"Error: No such volume: exact-volume-id",
                returncode=1,
            )
        if command[-1] == "exact-volume-id":
            return _FinishedProcess(
                stdout=b"private stdout", stderr=b"Docker daemon refused inspection", returncode=1
            )
        return _FinishedProcess()

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    process.remove_resource(resource_kind="volume", identifier="exact-volume-id", environment={})
    unknown = process.resource_is_absent(
        resource_kind="volume", identifier="exact-volume-id", environment={}
    )

    assert unknown is None
    assert commands == [
        ["docker", "volume", "rm", "--", "exact-volume-id"],
        ["docker", "volume", "inspect", "--", "exact-volume-id"],
    ]


def test_compose_network_cleanup_accepts_docker_network_not_found_as_absent(
    tmp_path: Path,
) -> None:
    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        return _FinishedProcess(
            stderr=b"Error response from daemon: network exact-network-id not found",
            returncode=1,
        )

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    process.remove_resource(
        resource_kind="network",
        identifier="exact-network-id",
        environment={},
    )


def test_compose_resource_cleanup_failure_is_ambiguous_after_a_mutating_command(
    tmp_path: Path,
) -> None:
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: _FinishedProcess(returncode=1),
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.remove_resource(
            resource_kind="network",
            identifier="exact-network-id",
            environment={},
        )

    assert captured.value.classification == "ambiguous"


def test_compose_inspects_container_networks_and_internal_flag_with_bounded_commands(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        commands.append(command)
        if ".NetworkSettings.Networks" in command[-3]:
            return _FinishedProcess(
                stdout=(
                    b'{"network-b":{"IPAddress":"172.20.0.3"},'
                    b'"network-a":{"IPAddress":"172.20.0.2"}}'
                )
            )
        return _FinishedProcess(stdout=b"true")

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    networks = process.inspect_container_networks(identifier="container-a", environment={})
    endpoint = process.inspect_container_network_ipv4_address(
        identifier="container-a",
        network_name="network-a",
        environment={},
    )
    internal = process.inspect_network_internal(identifier="network-a", environment={})

    assert networks == ("network-a", "network-b")
    assert endpoint == "172.20.0.2"
    assert internal is True
    assert commands == [
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            "--",
            "container-a",
        ],
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            "--",
            "container-a",
        ],
        [
            "docker",
            "inspect",
            "--type",
            "network",
            "--format",
            "{{json .Internal}}",
            "--",
            "network-a",
        ],
    ]


@pytest.mark.parametrize(("payload", "expected"), ((b"true", True), (b"false", False)))
def test_compose_inspects_actual_container_running_state(
    tmp_path: Path,
    payload: bytes,
    expected: bool,
) -> None:
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        commands.append(command)
        return _FinishedProcess(stdout=payload)

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    assert process.inspect_container_running(identifier="container-a", environment={}) is expected
    assert commands == [
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .State.Running}}",
            "--",
            "container-a",
        ]
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (b"{}", False),
        (b'{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"15432"}]}', True),
        (b"null", None),
        (b'{"5432/tcp":"private malformed"}', None),
    ),
)
def test_compose_inspects_whether_a_container_has_published_ports(
    tmp_path: Path,
    payload: bytes,
    expected: bool | None,
) -> None:
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        commands.append(command)
        return _FinishedProcess(stdout=payload)

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    assert (
        process.inspect_container_has_published_ports(
            identifier="container-a",
            environment={},
        )
        is expected
    )
    assert commands == [
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .NetworkSettings.Ports}}",
            "--",
            "container-a",
        ]
    ]


def test_compose_boundary_does_not_expose_unneeded_network_mutation(tmp_path: Path) -> None:
    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml")

    assert not hasattr(process, "disconnect_container_network")
    assert not hasattr(process, "connect_container_network")


@pytest.mark.parametrize(
    "payload",
    (
        b"null",
        b"[]",
        b'{"":{}}',
        b'{"network-a":null}',
        b'{"network-a":[]}',
        b'{"network-a":"malformed"}',
        b'{"network-a":{"IPAddress":null}}',
        b'{"network-a":{"IPAddress":[]}}',
        b'{"network-a":{"IPAddress":"restore-service"}}',
        b"private malformed",
    ),
)
def test_compose_network_inspection_fails_closed_without_exposing_output(
    tmp_path: Path,
    payload: bytes,
) -> None:
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: _FinishedProcess(stdout=payload),
    )

    assert process.inspect_container_networks(identifier="container-a", environment={}) is None
    assert (
        process.inspect_container_network_ipv4_address(
            identifier="container-a",
            network_name="network-a",
            environment={},
        )
        is None
    )
    assert process.inspect_network_internal(identifier="network-a", environment={}) is None


@pytest.mark.parametrize(
    "payload",
    (
        b'{"network-a":{}}',
        b'{"network-a":{"IPAddress":null}}',
        b'{"network-a":{"IPAddress":"restore-service"}}',
        b'{"network-a":{"IPAddress":"0.0.0.0"}}',
        b'{"network-a":{"IPAddress":"0.1.2.3"}}',
        b'{"network-a":{"IPAddress":"192.0.2.3"}}',
        b'{"network-a":{"IPAddress":"198.18.0.1"}}',
        b'{"network-a":{"IPAddress":"240.0.0.1"}}',
        b'{"network-a":{"IPAddress":"255.255.255.255"}}',
        b'{"network-a":{"IPAddress":"::1"}}',
    ),
)
def test_compose_network_endpoint_rejects_missing_or_non_routable_ipv4_addresses(
    tmp_path: Path,
    payload: bytes,
) -> None:
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: _FinishedProcess(stdout=payload),
    )

    assert (
        process.inspect_container_network_ipv4_address(
            identifier="container-a",
            network_name="network-a",
            environment={},
        )
        is None
    )


@pytest.mark.parametrize("address", ("10.42.0.8", "172.20.0.2", "192.168.50.9"))
def test_compose_network_endpoint_accepts_private_docker_unicast_addresses(
    tmp_path: Path,
    address: str,
) -> None:
    payload = f'{{"network-a":{{"IPAddress":"{address}"}}}}'.encode("ascii")
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: _FinishedProcess(stdout=payload),
    )

    assert (
        process.inspect_container_network_ipv4_address(
            identifier="container-a",
            network_name="network-a",
            environment={},
        )
        == address
    )


class _StreamingChild(_FinishedProcess):
    def __init__(self, *, payload: bytes, interrupt_on_wait: bool = False) -> None:
        super().__init__(stdout=payload)
        self.wait_calls = 0
        self._interrupt_on_wait = interrupt_on_wait

    def poll(self) -> int | None:
        return None if self._interrupt_on_wait else super().poll()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._interrupt_on_wait:
            raise KeyboardInterrupt
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class _UnreapableProcess(_FinishedProcess):
    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("docker", timeout)


class _SimulatedCancellation(BaseException):
    pass


class _CloseCancellation(BaseException):
    pass


class _CancellationOnCloseStream(io.RawIOBase):
    def __init__(self, source: IO[bytes]) -> None:
        self._source = source
        self._close_failed = False

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._source.fileno()

    def close(self) -> None:
        if not self._close_failed:
            self._close_failed = True
            raise _CloseCancellation
        self._source.close()
        super().close()


class _PollFailureProcess(_UnreapableProcess):
    def poll(self) -> int | None:
        raise OSError("private poll failure")


class _GracefulWaitFailureProcess(_FinishedProcess):
    def __init__(self, *, reap_after_kill: bool, stdout: bytes) -> None:
        super().__init__(stdout=stdout)
        self._reap_after_kill = reap_after_kill
        self.wait_calls = 0

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise OSError("private graceful wait failure")
        if self._reap_after_kill:
            return self.returncode
        raise subprocess.TimeoutExpired("docker", timeout)


class _OrdinaryWaitFailureProcess(_FinishedProcess):
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__(stdout=stdout, stderr=stderr)
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise OSError("private wait driver detail")
        return self.returncode


class _CloseFailureStdin(io.RawIOBase):
    def __init__(self, error_detail: str) -> None:
        # The test-double process owns this descriptor until the test releases it.
        self._stream = tempfile.TemporaryFile()  # noqa: SIM115
        self._error_detail = error_detail
        self.close_calls = 0
        self._released = False

    def fileno(self) -> int:
        return self._stream.fileno()

    def close(self) -> None:
        self.close_calls += 1
        if not self._released:
            raise OSError(self._error_detail)

    def release(self) -> None:
        self._released = True
        self._stream.close()


class _FilenoFailureStream(io.BytesIO):
    def __init__(self, error_detail: str, error_type: type[Exception] = OSError) -> None:
        super().__init__(b"output")
        self._error_detail = error_detail
        self._error_type = error_type

    def fileno(self) -> int:
        raise self._error_type(self._error_detail)


def _process_with_stdin(stdin: IO[bytes]) -> _FinishedProcess:
    process = _FinishedProcess()
    process.stdin = stdin
    return process


def test_compose_exec_stream_yields_customer_sized_output_without_buffering_and_reaps_child(
    tmp_path: Path,
) -> None:
    child = _StreamingChild(payload=b"customer-sized-backup")
    commands: list[list[str]] = []

    def start(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _StreamingChild:
        commands.append(command)
        return child

    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml", run=start)

    with process.exec_stream(
        project_name="project-a",
        arguments=("exec", "-T", "database", "dump"),
        environment={},
    ) as output:
        assert output.read(8) == b"customer"
        assert output.read() == b"-sized-backup"

    assert child.wait_calls == 1
    assert commands == [
        [
            "docker",
            "compose",
            "--project-name",
            "project-a",
            "--file",
            str(tmp_path / "compose.yaml"),
            "exec",
            "-T",
            "database",
            "dump",
        ]
    ]


def test_compose_exec_sanitizes_an_ordinary_wait_failure(tmp_path: Path) -> None:
    child = _OrdinaryWaitFailureProcess()
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command result was ambiguous"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert child.wait_calls == 2


def test_compose_exec_stream_sanitizes_an_ordinary_wait_failure(tmp_path: Path) -> None:
    child = _OrdinaryWaitFailureProcess(stdout=b"backup")
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with (
        pytest.raises(ComposeCommandError) as captured,
        process.exec_stream(
            project_name="project-a",
            arguments=("exec", "backup"),
            environment={},
        ) as output,
    ):
        assert output.read() == b"backup"

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command result was ambiguous"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert child.wait_calls == 2


def test_compose_exec_preserves_a_deadline_expiring_immediately_before_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = _FinishedProcess()
    end_of_file_count = 0
    real_read = os.read

    def track_end_of_file(descriptor: int, size: int) -> bytes:
        nonlocal end_of_file_count
        chunk = real_read(descriptor, size)
        if not chunk:
            end_of_file_count += 1
        return chunk

    def remaining_seconds(_deadline: float) -> float:
        if end_of_file_count == 2:
            raise compose_module._OperationTimedOut
        return 1

    monkeypatch.setattr(os, "read", track_end_of_file)
    monkeypatch.setattr(
        DockerComposeProcess,
        "_remaining_seconds",
        staticmethod(remaining_seconds),
    )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "timeout"
    assert str(captured.value) == "Docker Compose command timed out"


def test_compose_exec_stream_preserves_a_deadline_expiring_immediately_before_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = _FinishedProcess(stdout=b"backup")
    end_of_file_seen = False
    real_read = os.read

    def track_end_of_file(descriptor: int, size: int) -> bytes:
        nonlocal end_of_file_seen
        chunk = real_read(descriptor, size)
        if not chunk:
            end_of_file_seen = True
        return chunk

    def remaining_seconds(_deadline: float) -> float:
        if end_of_file_seen:
            raise compose_module._OperationTimedOut
        return 1

    monkeypatch.setattr(os, "read", track_end_of_file)
    monkeypatch.setattr(
        DockerComposeProcess,
        "_remaining_seconds",
        staticmethod(remaining_seconds),
    )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with (
        pytest.raises(ComposeCommandError) as captured,
        process.exec_stream(
            project_name="project-a",
            arguments=("exec", "backup"),
            environment={},
        ) as output,
    ):
        assert output.read() == b"backup"

    assert captured.value.classification == "timeout"
    assert str(captured.value) == "Docker Compose command timed out"


def test_compose_process_propagates_interrupts_and_reaps_stream_children(tmp_path: Path) -> None:
    def interrupt(
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _FinishedProcess:
        raise KeyboardInterrupt

    interrupted_child = _StreamingChild(payload=b"", interrupt_on_wait=True)
    interrupted_process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml", run=interrupt
    )
    stream_process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: interrupted_child,
    )

    with pytest.raises(KeyboardInterrupt):
        interrupted_process.exec(project_name="project-a", arguments=("ps",), environment={})

    with (
        pytest.raises(KeyboardInterrupt),
        stream_process.exec_stream(project_name="project-a", arguments=("ps",), environment={}),
    ):
        pass

    assert interrupted_child.terminated is True


def test_compose_exec_stops_a_running_child_when_stdout_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _docker_process(
        "import sys, time\n"
        f"sys.stdout.buffer.write(b'x' * {MAX_COMPOSE_OUTPUT_BYTES + 1})\n"
        "sys.stdout.flush()\n"
        f"time.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml")
    started_at = time.monotonic()

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "output_limit"
    # Returning before the child's own sleep would have ended it is what proves the
    # limit stopped the call. The previous one-second bound left roughly nothing for
    # interpreter startup, so a cold or loaded run failed here reproducibly.
    assert time.monotonic() - started_at < _HUNG_CHILD_SLEEP_SECONDS


def test_compose_exec_stops_a_running_child_when_stderr_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _docker_process(
        "import sys, time\n"
        f"sys.stderr.buffer.write(b'x' * {MAX_COMPOSE_OUTPUT_BYTES + 1})\n"
        "sys.stderr.flush()\n"
        f"time.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    process = DockerComposeProcess(compose_file=tmp_path / "compose.yaml")

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "output_limit"


def test_compose_exec_timeout_terminates_and_reaps_a_hung_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _docker_process(
        f"import time\ntime.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    child = _StartedChild()
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=child,
        timeout_seconds=0.5,
        termination_grace_seconds=0.1,
    )
    started_at = time.monotonic()

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "timeout"
    # Returning before the child's own sleep would have ended it is what proves the
    # timeout ended the call; a tighter bound would measure the host's load instead.
    assert time.monotonic() - started_at < _HUNG_CHILD_SLEEP_SECONDS
    _assert_process_exits(child.process_id)


def test_compose_stream_timeout_terminates_and_reaps_a_hung_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _docker_process(
        f"import time\ntime.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    child = _StartedChild()
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=child,
        timeout_seconds=0.1,
        termination_grace_seconds=0.1,
    )
    started_at = time.monotonic()

    with (
        pytest.raises(ComposeCommandError) as captured,
        process.exec_stream(
            project_name="project-a", arguments=("exec", "backup"), environment={}
        ) as output,
    ):
        output.read(1)

    assert captured.value.classification == "timeout"
    assert time.monotonic() - started_at < _HUNG_CHILD_SLEEP_SECONDS
    _assert_process_exits(child.process_id)


def _assert_process_exits(process_id: int, *, deadline_seconds: float = 10.0) -> None:
    """Assert the process is gone, allowing for the kill to land.

    Signal delivery and reaping are asynchronous, so checking once immediately
    after the parent returns asserts the scheduler's timing, not the escalation.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError("the child was still running after kill escalation")


def test_compose_timeout_escalates_to_kill_for_a_child_that_ignores_terminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ignoring_terminate = tmp_path / "ignoring-terminate"
    _docker_process(
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ignoring_terminate)!r}).write_text('ready')\n"
        f"time.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    child = _StartedChild(ready_marker=ignoring_terminate)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=child,
        timeout_seconds=0.5,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "timeout"
    _assert_process_exits(child.process_id)
    # The child is only known to have ignored the terminate, rather than never having
    # installed its handler, because the kill signal is the one that ended it.
    assert child.exit_status == -signal.SIGKILL


def test_compose_exec_drains_output_while_writing_input_and_honors_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_drained = tmp_path / "output-drained"
    _docker_process(
        "import pathlib, sys, time\n"
        "sys.stdout.buffer.write(b'o' * (256 * 1024))\n"
        "sys.stdout.buffer.flush()\n"
        f"pathlib.Path({str(output_drained)!r}).write_text('drained')\n"
        f"time.sleep({_HUNG_CHILD_SLEEP_SECONDS})\n",
        tmp_path,
        monkeypatch,
    )
    child = _StartedChild()

    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=child,
        timeout_seconds=2.0,
        termination_grace_seconds=0.1,
    )
    completed = Event()
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            process.exec(
                project_name="project-a",
                arguments=("exec", "restore"),
                environment={},
                input_bytes=b"i" * (2 * 1024 * 1024),
            )
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()

    worker = Thread(target=execute, daemon=True)
    worker.start()
    try:
        assert completed.wait(timeout=20)
    finally:
        for started in child.started:
            if started.poll() is None:
                started.kill()
                started.wait(timeout=1)
        worker.join(timeout=1)

    assert len(failures) == 1
    assert isinstance(failures[0], ComposeCommandError)
    assert failures[0].classification == "timeout"
    assert output_drained.read_text() == "drained"
    _assert_process_exits(child.process_id)


def test_compose_stream_reports_an_unreaped_child_after_kill_escalation(tmp_path: Path) -> None:
    child = _UnreapableProcess()
    child.stdout = None
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with (
        pytest.raises(ComposeCommandError) as captured,
        process.exec_stream(project_name="project-a", arguments=("ps",), environment={}),
    ):
        pass

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose child could not be reaped"
    assert child.terminated is True
    assert child.killed is True


@pytest.mark.parametrize(
    ("stdout", "input_bytes", "expected_classification", "expected_message"),
    (
        pytest.param(
            b"",
            None,
            "timeout",
            "Docker Compose command timed out",
            id="timeout",
        ),
        pytest.param(
            b"x" * (MAX_COMPOSE_OUTPUT_BYTES + 1),
            None,
            "output_limit",
            "Docker Compose command output exceeded the bounded limit",
            id="output-limit",
        ),
        pytest.param(
            b"",
            b"input",
            "ambiguous",
            "Docker Compose command input was unavailable",
            id="command-error",
        ),
    ),
)
def test_compose_exec_preserves_a_primary_command_error_during_unreaped_cleanup(
    stdout: bytes,
    input_bytes: bytes | None,
    expected_classification: str,
    expected_message: str,
    tmp_path: Path,
) -> None:
    child = _UnreapableProcess(stdout=stdout)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(
            project_name="project-a",
            arguments=("ps",),
            environment={},
            input_bytes=input_bytes,
        )

    assert captured.value.classification == expected_classification
    assert str(captured.value) == expected_message
    assert child.terminated is True
    assert child.killed is True


def test_compose_exec_preserves_interrupt_when_child_cleanup_cannot_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = _UnreapableProcess()

    def interrupt_select(
        readable: object, writable: object, exceptional: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        raise KeyboardInterrupt

    monkeypatch.setattr("heinzel_provider_sdk.compose.select.select", interrupt_select)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(KeyboardInterrupt):
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert child.terminated is True
    assert child.killed is True


def test_compose_exec_preserves_cancellation_when_child_cleanup_cannot_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = _UnreapableProcess()

    def cancel_select(
        readable: object, writable: object, exceptional: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        raise _SimulatedCancellation

    monkeypatch.setattr("heinzel_provider_sdk.compose.select.select", cancel_select)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(_SimulatedCancellation):
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert child.terminated is True
    assert child.killed is True


def test_compose_exec_sanitizes_an_ordinary_io_error_before_unreaped_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = _UnreapableProcess()

    def fail_select(
        readable: object, writable: object, exceptional: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        raise RuntimeError("private command failure")

    monkeypatch.setattr("heinzel_provider_sdk.compose.select.select", fail_select)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command result was ambiguous"
    assert "private command failure" not in str(captured.value)


def test_compose_exec_preserves_output_limit_when_reaping_poll_fails(
    tmp_path: Path,
) -> None:
    child = _PollFailureProcess(stdout=b"x" * (MAX_COMPOSE_OUTPUT_BYTES + 1))
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "output_limit"
    assert str(captured.value) == "Docker Compose command output exceeded the bounded limit"
    assert child.terminated is True
    assert child.killed is True


@pytest.mark.parametrize("reap_after_kill", (True, False))
def test_compose_exec_continues_cleanup_after_graceful_wait_oserror(
    reap_after_kill: bool,
    tmp_path: Path,
) -> None:
    child = _GracefulWaitFailureProcess(
        reap_after_kill=reap_after_kill,
        stdout=b"x" * (MAX_COMPOSE_OUTPUT_BYTES + 1),
    )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "output_limit"
    assert str(captured.value) == "Docker Compose command output exceeded the bounded limit"
    assert child.terminated is True
    assert child.killed is True
    assert child.wait_calls == 2


def test_compose_stream_preserves_interrupt_when_child_cleanup_cannot_reap(tmp_path: Path) -> None:
    child = _UnreapableProcess(stdout=b"stream")
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with (
        pytest.raises(KeyboardInterrupt),
        process.exec_stream(project_name="project-a", arguments=("ps",), environment={}) as output,
    ):
        assert output.read() == b"stream"
        raise KeyboardInterrupt

    assert child.terminated is True
    assert child.killed is True


def test_compose_stream_preserves_cancellation_when_child_cleanup_cannot_reap(
    tmp_path: Path,
) -> None:
    child = _UnreapableProcess(stdout=b"stream")
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with (
        pytest.raises(_SimulatedCancellation),
        process.exec_stream(project_name="project-a", arguments=("ps",), environment={}) as output,
    ):
        assert output.read() == b"stream"
        raise _SimulatedCancellation

    assert child.terminated is True
    assert child.killed is True


def test_compose_stream_close_cannot_replace_a_primary_cancellation(tmp_path: Path) -> None:
    child = _FinishedProcess(stdout=b"stream")
    child.stdout = _CancellationOnCloseStream(child.stdout)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )
    primary = _SimulatedCancellation()

    with (
        pytest.raises(_SimulatedCancellation) as captured,
        process.exec_stream(project_name="project-a", arguments=("ps",), environment={}) as output,
    ):
        assert output.read() == b"stream"
        raise primary

    assert captured.value is primary


def test_compose_stream_reports_close_cancellation_after_normal_exit(tmp_path: Path) -> None:
    child = _FinishedProcess(stdout=b"stream")
    child.stdout = _CancellationOnCloseStream(child.stdout)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with (
        pytest.raises(_CloseCancellation),
        process.exec_stream(project_name="project-a", arguments=("ps",), environment={}) as output,
    ):
        assert output.read() == b"stream"


def test_compose_stream_reports_close_cancellation_inside_an_outer_exception(
    tmp_path: Path,
) -> None:
    child = _FinishedProcess(stdout=b"stream")
    child.stdout = _CancellationOnCloseStream(child.stdout)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    try:
        raise RuntimeError("outer caller failure")
    except RuntimeError:
        with (
            pytest.raises(_CloseCancellation),
            process.exec_stream(
                project_name="project-a", arguments=("ps",), environment={}
            ) as output,
        ):
            assert output.read() == b"stream"


def test_compose_exec_sanitizes_non_epipe_stdin_write_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_error = "private stdin write driver failure"
    with tempfile.TemporaryFile() as stdin:
        child = _process_with_stdin(stdin)
        original_write = os.write

        def fail_write(descriptor: int, data: bytes) -> int:
            if descriptor == stdin.fileno():
                raise OSError(private_error)
            return original_write(descriptor, data)

        monkeypatch.setattr(os, "write", fail_write)
        process = DockerComposeProcess(
            compose_file=tmp_path / "compose.yaml",
            run=lambda command, *, env, stdin, stdout, stderr: child,
        )

        with pytest.raises(ComposeCommandError) as captured:
            process.exec(
                project_name="project-a",
                arguments=("exec", "restore"),
                environment={},
                input_bytes=b"x",
            )

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command input was unavailable"
    assert private_error not in str(captured.value)


@pytest.mark.parametrize("input_bytes", (b"", b"x"), ids=("empty-input", "written-input"))
def test_compose_exec_sanitizes_stdin_close_errors(
    input_bytes: bytes,
    tmp_path: Path,
) -> None:
    private_error = "private stdin close driver failure"
    stdin = _CloseFailureStdin(private_error)
    child = _process_with_stdin(stdin)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    try:
        with pytest.raises(ComposeCommandError) as captured:
            process.exec(
                project_name="project-a",
                arguments=("exec", "restore"),
                environment={},
                input_bytes=input_bytes,
            )
    finally:
        stdin.release()

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command input was unavailable"
    assert private_error not in str(captured.value)


def test_compose_exec_preserves_keyboard_interrupt_when_stdin_cleanup_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdin = _CloseFailureStdin("private stdin cleanup failure")
    child = _process_with_stdin(stdin)

    def interrupt_select(
        readable: object, writable: object, exceptional: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        raise KeyboardInterrupt

    monkeypatch.setattr("heinzel_provider_sdk.compose.select.select", interrupt_select)
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            process.exec(
                project_name="project-a",
                arguments=("exec", "restore"),
                environment={},
                input_bytes=b"x",
            )
    finally:
        stdin.release()

    assert stdin.close_calls == 1


@pytest.mark.parametrize(
    ("boundary", "expected_message"),
    (
        pytest.param(
            "stdout_fileno",
            "Docker Compose command result was ambiguous",
            id="stdout-fileno",
        ),
        pytest.param(
            "stdin_fileno",
            "Docker Compose command input was unavailable",
            id="stdin-fileno",
        ),
        pytest.param(
            "set_blocking",
            "Docker Compose command result was ambiguous",
            id="set-blocking",
        ),
        pytest.param(
            "select",
            "Docker Compose command result was ambiguous",
            id="select",
        ),
        pytest.param(
            "read",
            "Docker Compose command result was ambiguous",
            id="read",
        ),
    ),
)
@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_compose_exec_sanitizes_every_bounded_io_error(
    boundary: str,
    expected_message: str,
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_error = f"private {boundary} driver detail"
    child = _FinishedProcess(stdout=b"output")
    input_bytes: bytes | None = None
    if boundary == "stdout_fileno":
        child.stdout = _FilenoFailureStream(private_error, error_type)
    elif boundary == "stdin_fileno":
        child.stdin = _FilenoFailureStream(private_error, error_type)
        input_bytes = b"input"
    elif boundary == "set_blocking":
        monkeypatch.setattr(
            os,
            "set_blocking",
            lambda descriptor, blocking: (_ for _ in ()).throw(error_type(private_error)),
        )
    elif boundary == "select":
        monkeypatch.setattr(
            "heinzel_provider_sdk.compose.select.select",
            lambda readable, writable, exceptional, timeout: (_ for _ in ()).throw(
                error_type(private_error)
            ),
        )
    else:
        monkeypatch.setattr(
            os,
            "read",
            lambda descriptor, size: (_ for _ in ()).throw(error_type(private_error)),
        )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(
            project_name="project-a",
            arguments=("exec", "restore"),
            environment={},
            input_bytes=input_bytes,
        )

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == expected_message
    assert private_error not in str(captured.value)


@pytest.mark.parametrize("boundary", ("fileno", "select", "read"))
@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_compose_stream_sanitizes_every_bounded_io_error(
    boundary: str,
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_error = f"private stream {boundary} driver detail"
    child = _StreamingChild(payload=b"output")
    if boundary == "fileno":
        child.stdout = _FilenoFailureStream(private_error, error_type)
    elif boundary == "select":
        monkeypatch.setattr(
            "heinzel_provider_sdk.compose.select.select",
            lambda readable, writable, exceptional, timeout: (_ for _ in ()).throw(
                error_type(private_error)
            ),
        )
    else:
        monkeypatch.setattr(
            os,
            "read",
            lambda descriptor, size: (_ for _ in ()).throw(error_type(private_error)),
        )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
    )

    with (
        pytest.raises(ComposeCommandError) as captured,
        process.exec_stream(
            project_name="project-a",
            arguments=("exec", "backup"),
            environment={},
        ) as output,
    ):
        output.read(1)

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose stream was unavailable"
    assert private_error not in str(captured.value)


def test_compose_io_cleanup_cannot_replace_the_sanitized_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_error = "private select driver detail"
    child = _UnreapableProcess()
    monkeypatch.setattr(
        "heinzel_provider_sdk.compose.select.select",
        lambda readable, writable, exceptional, timeout: (_ for _ in ()).throw(
            OSError(private_error)
        ),
    )
    process = DockerComposeProcess(
        compose_file=tmp_path / "compose.yaml",
        run=lambda command, *, env, stdin, stdout, stderr: child,
        termination_grace_seconds=0.1,
    )

    with pytest.raises(ComposeCommandError) as captured:
        process.exec(project_name="project-a", arguments=("ps",), environment={})

    assert captured.value.classification == "ambiguous"
    assert str(captured.value) == "Docker Compose command result was ambiguous"
    assert private_error not in str(captured.value)
    assert child.terminated is True
    assert child.killed is True


def test_readiness_waiting_gives_up_as_soon_as_the_child_is_gone() -> None:
    """A child that died cannot become ready, so waiting for it only hides why.

    Waiting the whole deadline out costs a minute per broken run and reports a
    readiness failure, when what a reader needs is that the child exited and what
    it exited with -- a typo in the inlined source, or an interpreter that could
    not start under the monkeypatched PATH, looks identical otherwise.
    """
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    dead.wait()
    started_at = time.monotonic()

    with pytest.raises(_ChildNeverBecameReady, match="exited with 3"):
        _StartedChild._await_ready(dead, Path("/nonexistent/marker"), deadline_seconds=30.0)

    assert time.monotonic() - started_at < 5
