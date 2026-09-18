from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, runtime_checkable

MAX_WITNESS_SECONDS = 600
TIMEOUT_EXIT_CODE = 124
SAMPLER_FAILURE_EXIT_CODE = 125
TERMINATION_FAILURE_EXIT_CODE = 126
_SAMPLER_COMMAND_TIMEOUT_SECONDS = 15.0
# How long the runner politely waits for a signalled process to exit before it
# escalates. This is a behavioural choice about the command under witness.
_TERMINATION_GRACE_SECONDS = 5.0
# How long it then waits to *observe* that the process group is gone. That is a
# property of the machine, not of the command: on a loaded runner the kernel reaps
# well after the group has stopped existing for any practical purpose, and sharing
# the grace here made the runner report `126` termination-failure for a command that
# had merely timed out. The deadline exists only so the runner cannot hang.
_TERMINATION_VERIFICATION_SECONDS = 30.0
_SIZE_PATTERN = re.compile(
    r"^(?P<amount>(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)\s*"
    r"(?P<unit>B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$"
)
_SIZE_FACTORS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}
_DOCKER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DockerSamplingError(RuntimeError):
    pass


class ProcessTerminationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerUsageSample:
    memory_bytes: int
    disk_bytes: int


@dataclass(frozen=True)
class WitnessCost:
    duration_seconds: int
    peak_docker_memory_bytes: int
    peak_docker_disk_bytes: int
    sample_count: int
    timed_out: bool


@dataclass(frozen=True)
class DockerResourceScope:
    container_identifiers: tuple[str, ...]
    volume_identifiers: tuple[str, ...]

    def __post_init__(self) -> None:
        identifiers = (*self.container_identifiers, *self.volume_identifiers)
        if len(set(identifiers)) != len(identifiers) or any(
            _DOCKER_IDENTIFIER_PATTERN.fullmatch(identifier) is None for identifier in identifiers
        ):
            raise DockerSamplingError("Docker resource scope is invalid")


type DockerSampler = Callable[[float], DockerUsageSample]
type DockerScopeLoader = Callable[[], DockerResourceScope]


@runtime_checkable
class _DockerResourceBoundary(Protocol):
    @property
    def compose_resource_kind(self) -> str: ...

    @property
    def exact_identifier(self) -> str: ...


@runtime_checkable
class _AuthenticatedResourceLoaderBoundary(Protocol):
    def __call__(
        self,
        environment: Mapping[str, str],
    ) -> tuple[_DockerResourceBoundary, ...]: ...


def parse_docker_size(encoded: str) -> int:
    matched = _SIZE_PATTERN.fullmatch(encoded.strip())
    if matched is None:
        raise DockerSamplingError("Docker sampler returned an invalid numeric size")
    try:
        amount = Decimal(matched.group("amount"))
    except InvalidOperation:
        raise DockerSamplingError("Docker sampler returned an invalid numeric size") from None
    byte_count = amount * _SIZE_FACTORS[matched.group("unit")]
    return int(byte_count.to_integral_value(rounding=ROUND_CEILING))


def _run_docker_command(command: tuple[str, ...], *, timeout_seconds: float) -> str:
    if timeout_seconds <= 0:
        raise DockerSamplingError("Docker usage sampling failed")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DockerSamplingError("Docker usage sampling failed") from None
    if completed.returncode != 0:
        raise DockerSamplingError("Docker usage sampling failed")
    return completed.stdout


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DockerSamplingError("Docker usage sampling failed")
    return remaining


def _nonempty_lines(output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _parse_container_disk_bytes(output: str) -> int:
    lines = _nonempty_lines(output)
    if len(lines) != 1 or re.fullmatch(r"0|[1-9][0-9]*", lines[0]) is None:
        raise DockerSamplingError("Docker sampler returned an invalid numeric size")
    return int(lines[0])


def _parse_exact_volume_bytes(output: str, exact_identifiers: frozenset[str]) -> int:
    try:
        payload = json.loads(output)
        if not isinstance(payload, Mapping):
            raise TypeError
        raw_volumes = payload.get("Volumes", ())
        if not isinstance(raw_volumes, list):
            raise TypeError
        observed: dict[str, int] = {}
        for raw_volume in raw_volumes:
            if not isinstance(raw_volume, Mapping):
                raise TypeError
            name = raw_volume.get("Name")
            size = raw_volume.get("Size")
            if not isinstance(name, str) or not isinstance(size, str):
                raise TypeError
            if name in exact_identifiers:
                if name in observed:
                    raise TypeError
                observed[name] = parse_docker_size(size)
    except (DockerSamplingError, TypeError, ValueError, json.JSONDecodeError):
        raise DockerSamplingError("Docker usage sampling failed") from None
    return sum(observed.values())


def _sample_exact_volume_bytes(
    exact_identifiers: frozenset[str],
    *,
    deadline: float,
) -> int:
    failure: DockerSamplingError | None = None
    for _attempt in range(2):
        try:
            return _parse_exact_volume_bytes(
                _run_docker_command(
                    ("docker", "system", "df", "--verbose", "--format", "json"),
                    timeout_seconds=_remaining_seconds(deadline),
                ),
                exact_identifiers,
            )
        except DockerSamplingError as error:
            failure = error
    if failure is None:
        raise AssertionError("Docker volume sampling did not run")
    raise failure


def _list_docker_container_names(deadline: float) -> frozenset[str]:
    return frozenset(
        _nonempty_lines(
            _run_docker_command(
                (
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--format",
                    "{{.Names}}",
                ),
                timeout_seconds=_remaining_seconds(deadline),
            )
        )
    )


def sample_docker_usage(
    scope: DockerResourceScope,
    timeout_seconds: float,
) -> DockerUsageSample:
    if timeout_seconds <= 0:
        raise DockerSamplingError("Docker usage sampling failed")
    if not scope.container_identifiers and not scope.volume_identifiers:
        return DockerUsageSample(memory_bytes=0, disk_bytes=0)
    deadline = time.monotonic() + timeout_seconds
    container_names = _list_docker_container_names(deadline)
    present_containers = tuple(
        identifier for identifier in scope.container_identifiers if identifier in container_names
    )
    memory_bytes = 0
    container_disk_bytes = 0
    for identifier in present_containers:
        try:
            memory_output = _run_docker_command(
                (
                    "docker",
                    "stats",
                    "--all",
                    "--no-stream",
                    "--format",
                    "{{.MemUsage}}",
                    identifier,
                ),
                timeout_seconds=_remaining_seconds(deadline),
            )
        except DockerSamplingError:
            if identifier not in _list_docker_container_names(deadline):
                continue
            raise
        memory_lines = _nonempty_lines(memory_output)
        if len(memory_lines) > 1:
            raise DockerSamplingError("Docker usage sampling failed")
        if memory_lines:
            used, separator, _ = memory_lines[0].partition("/")
            if not separator:
                raise DockerSamplingError("Docker sampler returned an invalid numeric size")
            memory_bytes += parse_docker_size(used)
        try:
            container_disk_bytes += _parse_container_disk_bytes(
                _run_docker_command(
                    (
                        "docker",
                        "container",
                        "inspect",
                        "--size",
                        "--format",
                        "{{.SizeRw}}",
                        identifier,
                    ),
                    timeout_seconds=_remaining_seconds(deadline),
                )
            )
        except DockerSamplingError:
            if identifier not in _list_docker_container_names(deadline):
                continue
            raise
    volume_bytes = 0
    if scope.volume_identifiers:
        volume_bytes = _sample_exact_volume_bytes(
            frozenset(scope.volume_identifiers),
            deadline=deadline,
        )
    return DockerUsageSample(
        memory_bytes=memory_bytes,
        disk_bytes=container_disk_bytes + volume_bytes,
    )


def _write_cost_artifact(output_path: Path, cost: WitnessCost) -> None:
    encoded = (json.dumps(asdict(cost), separators=(",", ":"), sort_keys=True) + "\n").encode()
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.temporary")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _signal_process_group(process_group_id: int, signal_number: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return
    except OSError as error:
        raise ProcessTerminationError("witness process-group signaling failed") from error


def _process_group_is_absent(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # `EPERM` says this process group is not ours to signal, which happens once
        # the identifier has been recycled away from us. The witnessed group is
        # therefore gone. Reading it as a verification failure reported `126`
        # termination-failure for commands that had timed out cleanly, and on a loaded
        # runner that fails the build.
        return True
    except OSError as error:
        raise ProcessTerminationError("witness process-group verification failed") from error
    return False


def _kill_and_reap_process_group(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    failures: list[str],
) -> None:
    try:
        _signal_process_group(process_group_id, signal.SIGKILL)
    except ProcessTerminationError as error:
        failures.append(str(error))
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            failures.append("witness process kill failed")
    try:
        process.wait(timeout=_TERMINATION_VERIFICATION_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        failures.append("witness process reaping failed")


def _terminate_process_group(process: subprocess.Popen[bytes], *, force_immediately: bool) -> None:
    process_group_id = process.pid
    failures: list[str] = []
    if force_immediately:
        _kill_and_reap_process_group(process, process_group_id, failures)
    else:
        try:
            _signal_process_group(process_group_id, signal.SIGTERM)
        except ProcessTerminationError as error:
            failures.append(str(error))
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    failures.append("witness process termination failed")

        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            failures.append("witness process reaping failed")

        try:
            group_absent = _process_group_is_absent(process_group_id)
        except ProcessTerminationError as error:
            failures.append(str(error))
            group_absent = False
        if not group_absent:
            _kill_and_reap_process_group(process, process_group_id, failures)

    if process.poll() is None:
        try:
            process.wait(timeout=_TERMINATION_VERIFICATION_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            failures.append("witness process reaping failed")
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError:
                failures.append("witness process kill failed")

    verification_deadline = time.monotonic() + _TERMINATION_VERIFICATION_SECONDS
    verified_absent = False
    while time.monotonic() < verification_deadline:
        try:
            verified_absent = _process_group_is_absent(process_group_id)
        except ProcessTerminationError as error:
            failures.append(str(error))
            break
        if verified_absent:
            break
        time.sleep(0.01)
    if process.poll() is None or not verified_absent:
        failures.append("witness process-group termination could not be verified")
    if failures:
        raise ProcessTerminationError(failures[0])


def _normalized_exit_code(return_code: int) -> int:
    if return_code >= 0:
        return return_code
    return min(255, 128 + abs(return_code))


def run_bounded_witness(
    command: Sequence[str],
    *,
    output_path: Path,
    timeout_seconds: float,
    sample_interval_seconds: float,
    sampler: DockerSampler | None = None,
    scope_loader: DockerScopeLoader | None = None,
) -> int:
    if not command:
        raise ValueError("witness command is required")
    if timeout_seconds <= 0 or timeout_seconds > MAX_WITNESS_SECONDS:
        raise ValueError("witness timeout cannot exceed 600 seconds")
    if sample_interval_seconds <= 0:
        raise ValueError("sample interval must be positive")
    active_sampler: DockerSampler
    if sampler is None:
        if scope_loader is None:
            raise ValueError("authenticated Docker resource scope is required")

        def load_and_sample(sample_timeout_seconds: float) -> DockerUsageSample:
            return sample_docker_usage(scope_loader(), sample_timeout_seconds)

        active_sampler = load_and_sample
    else:
        active_sampler = sampler

    started = time.monotonic()
    try:
        process = subprocess.Popen(tuple(command), start_new_session=True)
    except OSError:
        _write_cost_artifact(
            output_path,
            WitnessCost(
                duration_seconds=math.ceil(time.monotonic() - started),
                peak_docker_memory_bytes=0,
                peak_docker_disk_bytes=0,
                sample_count=0,
                timed_out=False,
            ),
        )
        return 127

    deadline = started + timeout_seconds
    stop_sampling = threading.Event()
    sampling_finished = threading.Event()
    samples: queue.Queue[DockerUsageSample | DockerSamplingError] = queue.Queue()

    def sample_until_stopped() -> None:
        try:
            while not stop_sampling.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    sample = active_sampler(min(_SAMPLER_COMMAND_TIMEOUT_SECONDS, remaining))
                    if sample.memory_bytes < 0 or sample.disk_bytes < 0:
                        raise DockerSamplingError("Docker usage sampling failed")
                except BaseException:
                    samples.put(DockerSamplingError("Docker usage sampling failed"))
                    return
                samples.put(sample)
                if stop_sampling.wait(sample_interval_seconds):
                    return
        finally:
            sampling_finished.set()

    sampler_thread = threading.Thread(target=sample_until_stopped, daemon=True)
    sampler_thread.start()
    peak_memory_bytes = 0
    peak_disk_bytes = 0
    sample_count = 0
    timed_out = False
    sampler_failed = False
    return_code: int | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            sample_result = samples.get(timeout=min(0.01, remaining))
        except queue.Empty:
            sample_result = None
        if isinstance(sample_result, DockerSamplingError):
            sampler_failed = True
            break
        if isinstance(sample_result, DockerUsageSample):
            peak_memory_bytes = max(peak_memory_bytes, sample_result.memory_bytes)
            peak_disk_bytes = max(peak_disk_bytes, sample_result.disk_bytes)
            sample_count += 1
        if return_code is None:
            return_code = process.poll()
            if return_code is not None:
                stop_sampling.set()
        if return_code is not None and sampling_finished.is_set() and samples.empty():
            break
        if sampling_finished.is_set() and return_code is None and samples.empty():
            if time.monotonic() >= deadline:
                timed_out = True
            else:
                sampler_failed = True
            break

    stop_sampling.set()
    termination_failed = False
    if timed_out or sampler_failed:
        try:
            _terminate_process_group(process, force_immediately=timed_out)
        except ProcessTerminationError:
            termination_failed = True
    elif return_code is None:
        return_code = process.wait()

    cost = WitnessCost(
        duration_seconds=math.ceil(max(0.0, time.monotonic() - started)),
        peak_docker_memory_bytes=peak_memory_bytes,
        peak_docker_disk_bytes=peak_disk_bytes,
        sample_count=sample_count,
        timed_out=timed_out,
    )
    _write_cost_artifact(output_path, cost)
    if termination_failed:
        return TERMINATION_FAILURE_EXIT_CODE
    if timed_out:
        return TIMEOUT_EXIT_CODE
    if sampler_failed:
        return SAMPLER_FAILURE_EXIT_CODE
    if return_code is None:
        return 127
    return _normalized_exit_code(return_code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bounded Plan 3A CI witness")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=MAX_WITNESS_SECONDS)
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _authenticated_scope_loader() -> DockerScopeLoader:
    authority = importlib.import_module("tests.acceptance.run_warehouse_lifecycle")
    candidate = vars(authority).get("load_authenticated_docker_resources_from_environment")
    if not isinstance(candidate, _AuthenticatedResourceLoaderBoundary):
        raise RuntimeError("Plan 3A authenticated resource loader is unavailable")

    def load_scope() -> DockerResourceScope:
        resources = candidate(os.environ)
        return DockerResourceScope(
            container_identifiers=tuple(
                resource.exact_identifier
                for resource in resources
                if resource.compose_resource_kind == "container"
            ),
            volume_identifiers=tuple(
                resource.exact_identifier
                for resource in resources
                if resource.compose_resource_kind == "volume"
            ),
        )

    return load_scope


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = tuple(arguments.command)
    if command[:1] == ("--",):
        command = command[1:]
    try:
        scope_loader = _authenticated_scope_loader()
        exit_code = run_bounded_witness(
            command,
            output_path=arguments.output,
            timeout_seconds=arguments.timeout_seconds,
            sample_interval_seconds=arguments.sample_interval_seconds,
            scope_loader=scope_loader,
        )
    except (OSError, RuntimeError, ValueError):
        print("ERROR: Plan 3A bounded witness configuration failed", file=sys.stderr)
        return 2
    if exit_code == TIMEOUT_EXIT_CODE:
        print("ERROR: Plan 3A witnessed command exceeded 600 seconds", file=sys.stderr)
    elif exit_code == SAMPLER_FAILURE_EXIT_CODE:
        print("ERROR: Plan 3A Docker usage sampling failed", file=sys.stderr)
    elif exit_code == TERMINATION_FAILURE_EXIT_CODE:
        print("ERROR: Plan 3A witness process termination was not verified", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
