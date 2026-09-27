from __future__ import annotations

import io
import json
import os
import select
import subprocess
import time
from collections.abc import Buffer, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from pathlib import Path
from typing import IO, Literal, Protocol

type ComposeResourceKind = Literal["container", "volume", "network"]
type ComposeErrorClassification = Literal[
    "unavailable", "timeout", "rejected", "ambiguous", "output_limit"
]

MAX_COMPOSE_OUTPUT_BYTES = 1_048_576
DEFAULT_COMPOSE_TIMEOUT_SECONDS = 30.0
DEFAULT_TERMINATION_GRACE_SECONDS = 1.0
_CAPTURE_CHUNK_BYTES = 64 * 1024
_DOCKER_PRIVATE_IPV4_NETWORKS = tuple(
    IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_DOCKER_CALLER_ENVIRONMENT_KEYS = (
    "PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


@dataclass(frozen=True, slots=True)
class ComposeResource:
    resource_kind: ComposeResourceKind
    identifier: str


class ComposeCommandError(RuntimeError):
    classification: ComposeErrorClassification

    def __init__(self, message: str, *, classification: ComposeErrorClassification) -> None:
        super().__init__(message)
        self.classification = classification


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ComposeProcess(Protocol):
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ProcessStarter(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> ComposeProcess: ...


class CommandRunner(ProcessStarter, Protocol):
    pass


class _OutputLimitExceeded(Exception):
    pass


class _OperationTimedOut(Exception):
    pass


class _ProcessReapFailed(Exception):
    pass


class _DeadlineStream(io.RawIOBase):
    def __init__(self, *, source: IO[bytes], deadline: float) -> None:
        self._source = source
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        try:
            return self._source.fileno()
        except Exception:
            raise ComposeCommandError(
                "Docker Compose stream was unavailable", classification="ambiguous"
            ) from None

    def readinto(self, buffer: Buffer) -> int | None:
        target = memoryview(buffer)
        if not target:
            return 0
        remaining_seconds = self._deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise ComposeCommandError("Docker Compose command timed out", classification="timeout")
        descriptor = self.fileno()
        try:
            readable, _, _ = select.select([descriptor], [], [], remaining_seconds)
        except Exception:
            raise ComposeCommandError(
                "Docker Compose stream was unavailable", classification="ambiguous"
            ) from None
        if not readable:
            raise ComposeCommandError("Docker Compose command timed out", classification="timeout")
        try:
            chunk = os.read(descriptor, len(target))
        except Exception:
            raise ComposeCommandError(
                "Docker Compose stream was unavailable", classification="ambiguous"
            ) from None
        target[: len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        with suppress(Exception):
            self._source.close()
        super().close()


def _start_subprocess(
    command: list[str],
    *,
    env: Mapping[str, str],
    stdin: int | IO[bytes] | None,
    stdout: int,
    stderr: int,
) -> ComposeProcess:
    return subprocess.Popen(command, env=env, stdin=stdin, stdout=stdout, stderr=stderr)


# The compose surface, split by role rather than declared as one interface.
#
# A consumer annotates the role it actually calls, so a stand-in implements that much
# and no more: `_RestoreLoopbackTunnel` streams and never inspects anything, and the
# module functions that verify cleanup probe resources without running commands.
# Declaring it whole instead made every stand-in owe all fourteen methods, including
# `down`, which no consumer in this repository calls.
#
# `DockerComposeProcess` is the only implementation that talks to Docker and satisfies
# every role. The roles exist because a concrete class can only be stood in for by a
# subclass of itself, and a provider's compose failure paths -- a container that never
# starts, a network left attached, a stream that dies mid-restore -- are reachable
# only by standing in for compose.


class ComposeCommand(Protocol):
    """Run one command to completion and return its output."""

    def exec(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        nonzero_classification: ComposeErrorClassification = "rejected",
    ) -> bytes: ...


class ComposeStream(Protocol):
    """Run one command and borrow its stdout while it runs."""

    def exec_stream(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: IO[bytes] | None = None,
    ) -> AbstractContextManager[IO[bytes]]: ...


class ComposeLifecycle(Protocol):
    """Move a project between running and stopped."""

    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def stop(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def start(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def down(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...


class ComposeResourceProbe(Protocol):
    """Confirm whether one resource is gone. None when Docker cannot say."""

    def resource_is_absent(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> bool | None: ...


class ComposeResourceRemoval(ComposeResourceProbe, Protocol):
    """Remove one resource and confirm it went. Removing without confirming is not a
    capability any caller wants, so this extends the probe rather than standing alone."""

    def remove_resource(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> None: ...


class ComposeResources(ComposeResourceRemoval, Protocol):
    """Enumerate a project's resources, as well as removing and probing them."""

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]: ...


# Every inspection returns None when the resource is absent, so a caller distinguishes
# "absent" from an answer about a resource that exists. They are split three ways
# because callers ask three different questions: whether a container is up, where it is
# attached, and what it was built from.


class ComposeContainerState(Protocol):
    """Whether one container is running."""

    def inspect_container_running(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None: ...


class ComposeNetworkPlacement(Protocol):
    """Which networks a container is attached to, and whether a network is internal."""

    def inspect_container_networks(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> tuple[str, ...] | None: ...

    def inspect_network_internal(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None: ...


class ComposeContainerProvenance(Protocol):
    """What a container was built from and whether it publishes ports."""

    def inspect_container_image(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> str | None: ...

    def inspect_container_has_published_ports(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None: ...


class ComposeInspection(
    ComposeContainerState, ComposeNetworkPlacement, ComposeContainerProvenance, Protocol
):
    """Every inspection Docker is asked for."""


# The composites below are the roles callers actually need. They are unions of the
# roles above rather than new methods, and each exists because a caller reaches that
# far -- often transitively, by handing compose to a helper. Splitting the surface made
# those chains visible: `_observe_restore_isolation` looks like a pure probe until you
# notice it delegates to `_observe_restore_route_denial`, which runs a command.


class ComposeIsolationProbe(
    ComposeCommand, ComposeResourceProbe, ComposeContainerState, ComposeNetworkPlacement, Protocol
):
    """Establishing that a restore container is isolated: where it is attached, whether
    it is up, whether its resources are gone, and one command to prove a route denied."""


class ComposeResourceVerification(
    ComposeCommand,
    ComposeResourceProbe,
    ComposeContainerState,
    ComposeContainerProvenance,
    Protocol,
):
    """Verifying a project's resources are the expected ones, which also runs a command
    against them. Does not enumerate or remove: verification only reads."""


class ComposeProjectControl(
    ComposeLifecycle, ComposeResources, ComposeIsolationProbe, ComposeResourceVerification, Protocol
):
    """Driving a project's lifecycle and everything it owns."""


class ComposeBackupControl(
    ComposeStream, ComposeResources, ComposeIsolationProbe, ComposeResourceVerification, Protocol
):
    """Streaming a backup or restore, and verifying the resources around it."""


class ComposeBoundary(ComposeStream, ComposeProjectControl, Protocol):
    """Every compose role at once.

    `DockerComposeProcess` is checked against this, so a role that drifts from the
    implementation fails here rather than at whichever consumer happens to call it.
    A consumer should annotate the narrower role it uses instead.
    """


class DockerComposeProcess:
    def __init__(
        self,
        *,
        compose_file: Path,
        run: CommandRunner = _start_subprocess,
        timeout_seconds: float = DEFAULT_COMPOSE_TIMEOUT_SECONDS,
        termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or termination_grace_seconds <= 0:
            raise ValueError("Docker Compose timeouts must be positive")
        self._compose_file = compose_file
        self._run = run
        self._timeout_seconds = timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds

    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self.exec(project_name=project_name, arguments=("up", "--detach"), environment=environment)

    def stop(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self.exec(project_name=project_name, arguments=("stop",), environment=environment)

    def start(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self.exec(project_name=project_name, arguments=("start",), environment=environment)

    def down(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self.exec(
            project_name=project_name,
            arguments=("down", "--volumes", "--remove-orphans"),
            environment=environment,
        )

    def exec(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        nonzero_classification: ComposeErrorClassification = "rejected",
    ) -> bytes:
        result = self._bounded_command(
            command=self._compose_command(project_name, *arguments),
            environment=environment,
            input_bytes=input_bytes,
        )
        self._raise_for_nonzero(result, classification=nonzero_classification)
        return result.stdout

    @contextmanager
    def exec_stream(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: IO[bytes] | None = None,
    ) -> Iterator[IO[bytes]]:
        process = self._start_process(
            command=self._compose_command(project_name, *arguments),
            environment=environment,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = process.stdout
        if stdout is None:
            try:
                self._terminate_and_reap(process)
            except _ProcessReapFailed:
                raise ComposeCommandError(
                    "Docker Compose child could not be reaped", classification="ambiguous"
                ) from None
            raise ComposeCommandError(
                "Docker Compose stream was unavailable", classification="ambiguous"
            )
        deadline = time.monotonic() + self._timeout_seconds
        stream = io.BufferedReader(_DeadlineStream(source=stdout, deadline=deadline))
        primary_failure = False
        try:
            yield stream
            wait_failed = False
            try:
                returncode = process.wait(timeout=self._remaining_seconds(deadline))
            except _OperationTimedOut:
                raise
            except subprocess.TimeoutExpired:
                raise _OperationTimedOut from None
            except Exception:
                wait_failed = True
                returncode = 0
            if wait_failed:
                raise ComposeCommandError(
                    "Docker Compose command result was ambiguous",
                    classification="ambiguous",
                ) from None
        except _OperationTimedOut:
            primary_failure = True
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise ComposeCommandError(
                "Docker Compose command timed out", classification="timeout"
            ) from None
        except ComposeCommandError:
            primary_failure = True
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise
        except (GeneratorExit, KeyboardInterrupt, SystemExit):
            primary_failure = True
            # An active interrupt or exit remains primary under Python cleanup semantics.
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise
        except BaseException:
            primary_failure = True
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise
        finally:
            if primary_failure:
                with suppress(BaseException):
                    stream.close()
            else:
                stream.close()
        if returncode != 0:
            raise ComposeCommandError("Docker Compose command failed", classification="rejected")

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]:
        payload = self.exec(
            project_name=project_name,
            arguments=("config", "--format", "json"),
            environment=environment,
        )
        try:
            config = json.loads(payload)
        except (TypeError, ValueError):
            raise ComposeCommandError(
                "Docker Compose resource planning was ambiguous", classification="ambiguous"
            ) from None
        if not isinstance(config, dict):
            raise ComposeCommandError(
                "Docker Compose resource planning was ambiguous", classification="ambiguous"
            )
        resources: list[ComposeResource] = []
        resource_specs: tuple[tuple[str, ComposeResourceKind], ...] = (
            ("services", "container"),
            ("volumes", "volume"),
            ("networks", "network"),
        )
        for config_key, resource_kind in resource_specs:
            configured = config.get(config_key, {})
            if not isinstance(configured, dict):
                raise ComposeCommandError(
                    "Docker Compose resource planning was ambiguous", classification="ambiguous"
                )
            resources.extend(
                ComposeResource(
                    resource_kind=resource_kind,
                    identifier=f"planned:{resource_kind}:{name}",
                )
                for name in sorted(configured)
                if isinstance(name, str) and name
            )
        if not resources:
            raise ComposeCommandError(
                "Docker Compose resource planning was ambiguous", classification="ambiguous"
            )
        return tuple(resources)

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]:
        resources: list[ComposeResource] = []
        discovery_specs: tuple[tuple[ComposeResourceKind, str, str], ...] = (
            ("container", "container", "{{.ID}}"),
            ("volume", "volume", "{{.Name}}"),
            ("network", "network", "{{.ID}}"),
        )
        for resource_kind, object_type, output_format in discovery_specs:
            result = self._bounded_command(
                command=[
                    "docker",
                    object_type,
                    "ls",
                    "--all" if object_type == "container" else "--filter",
                    *(["--filter"] if object_type == "container" else []),
                    f"label=com.docker.compose.project={project_name}",
                    "--format",
                    output_format,
                ],
                environment=environment,
            )
            self._raise_for_nonzero(result)
            resources.extend(
                ComposeResource(resource_kind=resource_kind, identifier=identifier)
                for raw_identifier in self._decode(result.stdout).splitlines()
                if (identifier := raw_identifier.strip())
            )
        return tuple(resources)

    def remove_resource(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> None:
        arguments = ["docker", resource_kind, "rm"]
        if resource_kind == "container":
            arguments.append("--force")
        result = self._bounded_command(
            command=[*arguments, "--", identifier], environment=environment
        )
        if result.returncode != 0 and not self._reports_absent(resource_kind, result.stderr):
            raise ComposeCommandError(
                "Docker Compose resource cleanup outcome was ambiguous",
                classification="ambiguous",
            )

    def resource_is_absent(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> bool | None:
        result = self._bounded_command(
            command=["docker", resource_kind, "inspect", "--", identifier], environment=environment
        )
        if result.returncode == 0:
            return False
        if self._reports_absent(resource_kind, result.stderr):
            return True
        return None

    def inspect_container_image(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> str | None:
        result = self._bounded_command(
            command=[
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .Config.Image}}",
                "--",
                identifier,
            ],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            image = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        return image if isinstance(image, str) and image else None

    def inspect_container_image_id(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> str | None:
        return self._inspect_image_value(
            command=("docker", "inspect", "--type", "container"),
            output_format="{{json .Image}}",
            identifier=identifier,
            environment=environment,
        )

    def inspect_image_id(self, *, identifier: str, environment: Mapping[str, str]) -> str | None:
        return self._inspect_image_value(
            command=("docker", "image", "inspect"),
            output_format="{{json .Id}}",
            identifier=identifier,
            environment=environment,
        )

    def _inspect_image_value(
        self,
        *,
        command: tuple[str, ...],
        output_format: str,
        identifier: str,
        environment: Mapping[str, str],
    ) -> str | None:
        result = self._bounded_command(
            command=[*command, "--format", output_format, "--", identifier],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, str) and value.startswith("sha256:") else None

    def inspect_container_running(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        result = self._bounded_command(
            command=[
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .State.Running}}",
                "--",
                identifier,
            ],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            running = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        return running if isinstance(running, bool) else None

    def inspect_container_has_published_ports(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        result = self._bounded_command(
            command=[
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                "--",
                identifier,
            ],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            ports = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        if not isinstance(ports, dict):
            return None
        published = False
        for container_port, bindings in ports.items():
            if not isinstance(container_port, str) or not container_port:
                return None
            if bindings is None:
                continue
            if not isinstance(bindings, list):
                return None
            for binding in bindings:
                if (
                    not isinstance(binding, dict)
                    or not isinstance(binding.get("HostIp"), str)
                    or not isinstance(binding.get("HostPort"), str)
                ):
                    return None
            published = published or bool(bindings)
        return published

    def inspect_container_networks(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> tuple[str, ...] | None:
        networks = self._inspect_container_network_settings(
            identifier=identifier,
            environment=environment,
        )
        return tuple(sorted(networks)) if networks is not None else None

    def inspect_container_network_ipv4_address(
        self,
        *,
        identifier: str,
        network_name: str,
        environment: Mapping[str, str],
    ) -> str | None:
        networks = self._inspect_container_network_settings(
            identifier=identifier,
            environment=environment,
        )
        if networks is None:
            return None
        endpoint = networks.get(network_name)
        if endpoint is None:
            return None
        return self._validated_network_ipv4_address(endpoint)

    @staticmethod
    def _validated_network_ipv4_address(endpoint: Mapping[str, object]) -> str | None:
        value = endpoint.get("IPAddress")
        if not isinstance(value, str) or not value:
            return None
        try:
            address = IPv4Address(value)
        except AddressValueError:
            return None
        if not any(address in network for network in _DOCKER_PRIVATE_IPV4_NETWORKS):
            return None
        return str(address)

    def _inspect_container_network_settings(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> dict[str, Mapping[str, object]] | None:
        result = self._bounded_command(
            command=[
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                "--",
                identifier,
            ],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            networks = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        if not isinstance(networks, dict) or not networks:
            return None
        validated: dict[str, Mapping[str, object]] = {}
        for name, endpoint in networks.items():
            if not isinstance(name, str) or not name or not isinstance(endpoint, dict):
                return None
            if self._validated_network_ipv4_address(endpoint) is None:
                return None
            validated[name] = endpoint
        return validated

    def inspect_network_internal(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        result = self._bounded_command(
            command=[
                "docker",
                "inspect",
                "--type",
                "network",
                "--format",
                "{{json .Internal}}",
                "--",
                identifier,
            ],
            environment=environment,
        )
        if result.returncode != 0:
            return None
        try:
            internal = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        return internal if isinstance(internal, bool) else None

    def _bounded_command(
        self,
        *,
        command: list[str],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
    ) -> _CommandResult:
        process = self._start_process(
            command=command,
            environment=environment,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + self._timeout_seconds
        try:
            return self._capture_bounded_output(process, deadline, input_bytes)
        except _OutputLimitExceeded:
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise ComposeCommandError(
                "Docker Compose command output exceeded the bounded limit",
                classification="output_limit",
            ) from None
        except _OperationTimedOut:
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise ComposeCommandError(
                "Docker Compose command timed out", classification="timeout"
            ) from None
        except ComposeCommandError:
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise
        except (GeneratorExit, KeyboardInterrupt, SystemExit):
            # An active interrupt or exit remains primary under Python cleanup semantics.
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise
        except BaseException:
            with suppress(BaseException):
                self._cleanup_after_failure(process)
            raise

    def _start_process(
        self,
        *,
        command: list[str],
        environment: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> ComposeProcess:
        try:
            return self._run(
                command,
                env=self._docker_environment(environment),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        except Exception:
            raise ComposeCommandError(
                "Docker Compose command is unavailable", classification="unavailable"
            ) from None

    def _capture_bounded_output(
        self, process: ComposeProcess, deadline: float, input_bytes: bytes | None
    ) -> _CommandResult:
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            raise ComposeCommandError(
                "Docker Compose command result was ambiguous", classification="ambiguous"
            )
        captured_stdout = bytearray()
        captured_stderr = bytearray()
        try:
            pending = {
                stdout.fileno(): (stdout, captured_stdout),
                stderr.fileno(): (stderr, captured_stderr),
            }
        except Exception:
            raise ComposeCommandError(
                "Docker Compose command result was ambiguous", classification="ambiguous"
            ) from None
        stdin = process.stdin
        if input_bytes is not None and stdin is None:
            raise ComposeCommandError(
                "Docker Compose command input was unavailable", classification="ambiguous"
            )
        try:
            input_descriptor = (
                stdin.fileno() if input_bytes is not None and stdin is not None else None
            )
        except Exception:
            raise ComposeCommandError(
                "Docker Compose command input was unavailable", classification="ambiguous"
            ) from None
        input_offset = 0
        descriptors = tuple(pending)
        try:
            for descriptor in descriptors:
                os.set_blocking(descriptor, False)
        except Exception:
            raise ComposeCommandError(
                "Docker Compose command result was ambiguous", classification="ambiguous"
            ) from None
        if input_descriptor is not None:
            try:
                os.set_blocking(input_descriptor, False)
            except Exception:
                raise ComposeCommandError(
                    "Docker Compose command input was unavailable", classification="ambiguous"
                ) from None
        if input_bytes == b"" and stdin is not None:
            self._close_input(stdin)
            input_descriptor = None
        while pending or input_descriptor is not None:
            try:
                readable, writable, _ = select.select(
                    tuple(pending),
                    [input_descriptor] if input_descriptor is not None else [],
                    [],
                    self._remaining_seconds(deadline),
                )
            except Exception:
                raise ComposeCommandError(
                    "Docker Compose command result was ambiguous", classification="ambiguous"
                ) from None
            if not readable and not writable:
                raise _OperationTimedOut
            for descriptor in readable:
                _, captured = pending[descriptor]
                try:
                    chunk = os.read(
                        descriptor,
                        min(
                            _CAPTURE_CHUNK_BYTES,
                            MAX_COMPOSE_OUTPUT_BYTES - len(captured) + 1,
                        ),
                    )
                except Exception:
                    raise ComposeCommandError(
                        "Docker Compose command result was ambiguous",
                        classification="ambiguous",
                    ) from None
                if not chunk:
                    del pending[descriptor]
                    continue
                if len(captured) + len(chunk) > MAX_COMPOSE_OUTPUT_BYTES:
                    raise _OutputLimitExceeded
                captured.extend(chunk)
            if input_descriptor in writable and stdin is not None and input_bytes is not None:
                try:
                    input_offset += os.write(
                        input_descriptor,
                        input_bytes[input_offset : input_offset + _CAPTURE_CHUNK_BYTES],
                    )
                except Exception:
                    raise ComposeCommandError(
                        "Docker Compose command input was unavailable", classification="ambiguous"
                    ) from None
                if input_offset == len(input_bytes):
                    self._close_input(stdin)
                    input_descriptor = None
        wait_failed = False
        try:
            returncode = process.wait(timeout=self._remaining_seconds(deadline))
        except _OperationTimedOut:
            raise
        except subprocess.TimeoutExpired:
            raise _OperationTimedOut from None
        except Exception:
            wait_failed = True
            returncode = 0
        if wait_failed:
            raise ComposeCommandError(
                "Docker Compose command result was ambiguous",
                classification="ambiguous",
            ) from None
        return _CommandResult(
            returncode=returncode,
            stdout=bytes(captured_stdout),
            stderr=bytes(captured_stderr),
        )

    def _compose_command(self, project_name: str, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--file",
            str(self._compose_file),
            *arguments,
        ]

    @staticmethod
    def _decode(value: bytes) -> str:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ComposeCommandError(
                "Docker Compose command result was ambiguous", classification="ambiguous"
            ) from None

    @staticmethod
    def _docker_environment(environment: Mapping[str, str]) -> dict[str, str]:
        caller_environment = {
            key: os.environ[key] for key in _DOCKER_CALLER_ENVIRONMENT_KEYS if key in os.environ
        }
        return dict(environment) | caller_environment

    @staticmethod
    def _raise_for_nonzero(
        result: _CommandResult,
        *,
        classification: ComposeErrorClassification = "rejected",
    ) -> None:
        if result.returncode != 0:
            raise ComposeCommandError(
                "Docker Compose command failed", classification=classification
            )

    @staticmethod
    def _reports_absent(resource_kind: ComposeResourceKind, standard_error: bytes) -> bool:
        message = standard_error.decode("utf-8", errors="replace").lower()
        return f"no such {resource_kind}" in message or (
            resource_kind == "network" and "network " in message and " not found" in message
        )

    @staticmethod
    def _remaining_seconds(deadline: float) -> float:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise _OperationTimedOut
        return remaining_seconds

    @staticmethod
    def _close_input(stdin: IO[bytes]) -> None:
        try:
            stdin.close()
        except Exception:
            raise ComposeCommandError(
                "Docker Compose command input was unavailable", classification="ambiguous"
            ) from None

    def _cleanup_after_failure(self, process: ComposeProcess) -> None:
        if process.stdin is not None:
            with suppress(BaseException):
                process.stdin.close()
        try:
            self._terminate_and_reap(process)
        except _ProcessReapFailed:
            raise ComposeCommandError(
                "Docker Compose child could not be reaped", classification="ambiguous"
            ) from None

    def _terminate_and_reap(self, process: ComposeProcess) -> None:
        try:
            returncode = process.poll()
        except OSError:
            returncode = None
        if returncode is not None:
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                raise _ProcessReapFailed from None
            else:
                return
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=self._termination_grace_seconds)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        with suppress(OSError):
            process.kill()
        try:
            process.wait(timeout=self._termination_grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            raise _ProcessReapFailed from None
