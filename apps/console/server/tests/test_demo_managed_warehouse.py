"""The demonstration's opt-in warehouse-control path, proved as far as it can be without Docker.

Nothing here provisions a warehouse. There is no Docker daemon in the environment these tests
run in, so the live path -- a PostgreSQL container over TLS, eight separated principal classes,
their probes, a backup restored into a second instance and verified -- is not exercised and is
not claimed. What is proved is everything up to the first database connection: the Compose
operations the composition issues and their order, that a failure at each of them is classified
and surfaced rather than swallowed, that an unreachable daemon is reported as the socket to
mount, and that a binding warehouse-control already carried to `ready` makes the console's setup
surface answer.

The fake process starter is the seam `DockerComposeProcess` already offers for standing in for
the process it starts. It is deliberately no more permissive than the real one: it answers only
the exact commands the provider issues, it echoes back the host port the provider actually
published rather than one of its own, and it reports the pinned image digest the provider
verifies. Answering loosely would let a provisioning pass here that Docker would refuse.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import IO

import pytest
import yaml
from cryptography import x509
from heinzel_console.auth import TrustedActorContext
from heinzel_console.demo import ManagedWarehouseOption, ManagedWarehouseRefused
from heinzel_console.demo.console import DemoConsole
from heinzel_console.demo.managed_warehouse import (
    _PURPOSE_FIELDS,
    DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE,
    DEMO_WAREHOUSE_REGION,
    provision_demo_managed_warehouse,
)
from heinzel_console.demo.publication import DEMO_TENANT_ID
from heinzel_console.demo.warehouse_tls import generate_demo_warehouse_tls_material
from heinzel_console.errors import ConsoleUnavailable
from heinzel_console.governed_backend import CAPABILITY_NOT_DELIVERED
from heinzel_contract_model import digest
from heinzel_provider_postgresql import warehouse as warehouse_provider
from heinzel_provider_postgresql.warehouse_settings import POSTGRESQL_WAREHOUSE_IMAGE
from heinzel_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
)
from heinzel_warehouse_control import secrets as warehouse_secrets
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository

_ARCHITECT = TrustedActorContext(
    tenant_id=DEMO_TENANT_ID,
    actor_id="architect-demo",
    roles=("data_architect",),
    active_role="data_architect",
    session_id="session-managed-warehouse",
)

# What the Docker CLI prints when it is installed and its daemon is not listening. Reproduced
# verbatim because the compose boundary reads stderr to tell "this resource is absent" from "I
# could not ask", and a paraphrase would be answered as absence.
_DAEMON_UNREACHABLE = (
    b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    b"Is the docker daemon running?\n"
)

# The nine Compose operations provisioning issues, in order, with the project's own identifiers
# removed: the project name is derived from a binding identifier a sequence allocates, and the
# published port from a free loopback port, so neither is the same twice.
_EXPECTED_OPERATIONS = (
    ("docker", "container", "inspect", "--", "<container>"),
    ("docker", "compose", "--project-name", "<project>", "--file", "<compose>", "up", "--detach"),
    ("docker", "container", "inspect", "--", "<container>"),
    ("docker", "network", "inspect", "--", "<network>"),
    ("docker", "network", "inspect", "--", "<loopback-network>"),
    ("docker", "volume", "inspect", "--", "<volume>"),
    (
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        "{{json .State.Running}}",
        "--",
        "<container>",
    ),
    (
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        "{{json .Config.Image}}",
        "--",
        "<container>",
    ),
    (
        "docker",
        "compose",
        "--project-name",
        "<project>",
        "--file",
        "<compose>",
        "port",
        "postgresql",
        "5432",
    ),
)


def _clock() -> datetime:
    return datetime.now(UTC)


class _CompletedProcess:
    """One finished `docker` invocation, with its output on real pipes.

    Real descriptors rather than byte buffers, because the compose boundary bounds its reads
    with `select` on `fileno()`: a stand-in that offered only `read()` would never be read at
    all.
    """

    def __init__(self, *, returncode: int, standard_output: bytes, standard_error: bytes) -> None:
        self._returncode = returncode
        # Annotated optional because `ComposeProcess` declares these three as mutable optional
        # attributes, and a stand-in that narrowed them would not satisfy it.
        self.stdin: IO[bytes] | None = None
        self.stdout: IO[bytes] | None = self._pipe(standard_output)
        self.stderr: IO[bytes] | None = self._pipe(standard_error)

    @staticmethod
    def _pipe(payload: bytes) -> IO[bytes]:
        read_descriptor, write_descriptor = os.pipe()
        os.write(write_descriptor, payload)
        os.close(write_descriptor)
        return os.fdopen(read_descriptor, "rb")

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class _FakeDockerRunner:
    """A Docker surface that answers exactly what a working daemon would, and nothing else.

    `fails_at` is the index of the operation this runner cannot start, which is what a missing
    executable or an unreachable socket looks like to `DockerComposeProcess`: the classification
    is the real one for that failure rather than one chosen here. `unreachable` instead answers
    every command the way the installed CLI does when no daemon is listening.
    """

    def __init__(self, *, fails_at: int | None = None, unreachable: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._fails_at = fails_at
        self._unreachable = unreachable
        self._project_is_up = False

    def __call__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str],
        stdin: int | IO[bytes] | None,
        stdout: int,
        stderr: int,
    ) -> _CompletedProcess:
        index = len(self.commands)
        self.commands.append(tuple(command))
        if index == self._fails_at:
            raise OSError("no docker executable")
        if self._unreachable:
            return _CompletedProcess(
                returncode=1, standard_output=b"", standard_error=_DAEMON_UNREACHABLE
            )
        returncode, standard_output = self._answer(command, env)
        return _CompletedProcess(
            returncode=returncode,
            standard_output=standard_output,
            standard_error=(
                b"" if returncode == 0 else f"Error: No such {command[1]}: absent".encode()
            ),
        )

    def _answer(self, command: list[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        if command[:2] == ["docker", "compose"]:
            if command[-2:] == ["up", "--detach"]:
                self._project_is_up = True
                return 0, b""
            if command[-3:] == ["port", "postgresql", "5432"]:
                # The provider compares this to the port it published and refuses anything
                # else, so the answer is read back out of the environment it supplied.
                return 0, f"127.0.0.1:{env['HEINZEL_POSTGRES_HOST_PORT']}\n".encode()
            raise AssertionError(f"unexpected compose command: {command}")
        if command[1:3] in (
            ["container", "inspect"],
            ["network", "inspect"],
            ["volume", "inspect"],
        ):
            return (0, b"[{}]") if self._project_is_up else (1, b"")
        if command[1] == "inspect":
            if "{{json .State.Running}}" in command:
                return 0, b"true"
            if "{{json .Config.Image}}" in command:
                return 0, json.dumps(POSTGRESQL_WAREHOUSE_IMAGE).encode()
        raise AssertionError(f"unexpected docker command: {command}")


def _unreachable_connect(**keywords: object) -> object:
    raise AssertionError("no database connection is reachable in this environment")


def _option(runner: _FakeDockerRunner) -> ManagedWarehouseOption:
    return ManagedWarehouseOption(run=runner, connect=_unreachable_connect)


def _generalized(commands: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """Each recorded command with this run's own identifiers replaced by their role."""
    project = next(command[3] for command in commands if command[:2] == ("docker", "compose"))
    replacements = {
        project: "<project>",
        f"{project}-database": "<container>",
        f"{project}-private": "<network>",
        f"{project}-loopback": "<loopback-network>",
        f"{project}-data": "<volume>",
        str(DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE): "<compose>",
    }
    return tuple(
        tuple(replacements.get(argument, argument) for argument in command) for command in commands
    )


def _binding_state(state_dir: Path) -> WarehouseBindingState | None:
    """The lifecycle state the binding store under `state_dir` holds, read as the console would."""
    record = state_dir / "warehouse-control" / "binding.json"
    if not record.is_file():
        return None
    binding_id = json.loads(record.read_text(encoding="utf-8"))["warehouse_binding_id"]
    repository = SQLiteWarehouseRepository(
        connection=sqlite3.connect(str(state_dir / "warehouse-control" / "bindings.sqlite3"))
    )
    try:
        binding = repository.load(DEMO_TENANT_ID, binding_id)
    finally:
        repository.close()
    return None if binding is None else binding.lifecycle_state


def _seed_ready_binding(state_dir: Path) -> WarehouseBinding:
    """A binding a previous start carried to `ready`, written through the owning repository.

    The real `WarehouseBinding` and the real `SQLiteWarehouseRepository`, so this stands in for
    a start that succeeded rather than for a record shaped like one.
    """
    directory = state_dir / "warehouse-control"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = _clock()
    binding = WarehouseBinding(
        binding_id="whb-" + digest({"domain": "test-seeded-ready-binding"})[:24],
        tenant_id=DEMO_TENANT_ID,
        engine_kind=EngineKind.POSTGRESQL,
        region=DEMO_WAREHOUSE_REGION,
        capability_profile_digest=digest({"domain": "test-seeded-capability-profile"}),
        lifecycle_state=WarehouseBindingState.READY,
        revision=1,
        created_at=now,
        updated_at=now,
        provisioned_at=now,
    )
    repository = SQLiteWarehouseRepository(
        connection=sqlite3.connect(str(directory / "bindings.sqlite3"))
    )
    try:
        repository.save(binding)
    finally:
        repository.close()
    (directory / "binding.json").write_text(
        json.dumps({"warehouse_binding_id": binding.binding_id}) + "\n", encoding="utf-8"
    )
    return binding


def test_provisioning_through_warehouse_control_issues_the_compose_operations_in_order(
    tmp_path: Path,
) -> None:
    """Every Compose operation provisioning needs, in the order the provider needs them.

    The run stops at the ninth because that is the last thing Compose is asked: everything past
    it is a TLS connection to a PostgreSQL that does not exist here. The runner cannot start
    that ninth command, so the sequence recorded is exactly the one provisioning issued.
    """
    runner = _FakeDockerRunner(fails_at=len(_EXPECTED_OPERATIONS) - 1)

    with pytest.raises(ManagedWarehouseRefused):
        provision_demo_managed_warehouse(tmp_path, option=_option(runner), clock=_clock)

    assert _generalized(runner.commands) == _EXPECTED_OPERATIONS


@pytest.mark.parametrize("step", range(len(_EXPECTED_OPERATIONS)))
def test_a_compose_failure_at_any_step_is_classified_and_surfaced_rather_than_swallowed(
    tmp_path: Path, step: int
) -> None:
    """A Compose operation that cannot be run stops provisioning and says so, at every step.

    The classification is the provider's and the orchestrator's, not this test's: a command that
    cannot be started is `transient_unavailable`, and the console turns that into the one
    sentence an operator can act on. The binding must not be left reporting `ready` either --
    a console that reported a managed warehouse after this would be claiming one that was never
    validated.
    """
    runner = _FakeDockerRunner(fails_at=step)

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(tmp_path, option=_option(runner), clock=_clock)

    assert "transient_unavailable" in str(refused.value)
    assert "socket" in str(refused.value)
    assert len(runner.commands) == step + 1
    assert _binding_state(tmp_path) is not WarehouseBindingState.READY


def test_an_unreachable_docker_daemon_names_the_socket_rather_than_raising_a_traceback(
    tmp_path: Path,
) -> None:
    """The installed CLI with no daemon behind it is advice, not a classification.

    This is the failure an operator meets first, and the compose boundary can only call it
    ambiguous: Docker answered, and what it said was that it could not ask. The refusal has to
    name the socket, because that is the thing to fix.
    """
    runner = _FakeDockerRunner(unreachable=True)

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(tmp_path, option=_option(runner), clock=_clock)

    message = str(refused.value)
    assert "ambiguous_outcome" in message
    assert "socket bind-mounted" in message
    assert "HEINZEL_DEMO_WAREHOUSE_CONTROL" in message


def test_a_missing_docker_executable_is_refused_before_a_binding_is_created(
    tmp_path: Path,
) -> None:
    """A missing command will not come back, so it is not recorded as a transient failure."""
    runner = _FakeDockerRunner()

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(
            tmp_path,
            option=ManagedWarehouseOption(
                run=runner, connect=_unreachable_connect, executable_lookup=lambda _name: None
            ),
            clock=_clock,
        )

    assert "no `docker` executable is on PATH" in str(refused.value)
    assert runner.commands == []
    assert _binding_state(tmp_path) is None


def test_a_compose_project_that_is_not_there_is_refused_before_anything_is_opened(
    tmp_path: Path,
) -> None:
    runner = _FakeDockerRunner()

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(
            tmp_path,
            option=ManagedWarehouseOption(
                compose_file=tmp_path / "absent" / "compose.yaml",
                run=runner,
                connect=_unreachable_connect,
            ),
            clock=_clock,
        )

    assert "Compose project is not a file" in str(refused.value)
    assert runner.commands == []


def test_a_binding_left_part_way_through_provisioning_is_refused_rather_than_adopted(
    tmp_path: Path,
) -> None:
    """The credentials went with the process that created the warehouse.

    A second start holds none of them, so adopting the binding would mean connecting to a
    warehouse with an administration password it never had. The refusal says what to remove.
    """
    with pytest.raises(ManagedWarehouseRefused):
        provision_demo_managed_warehouse(
            tmp_path, option=_option(_FakeDockerRunner(fails_at=2)), clock=_clock
        )
    assert _binding_state(tmp_path) is WarehouseBindingState.PROVISIONING
    second = _FakeDockerRunner()

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(tmp_path, option=_option(second), clock=_clock)

    assert "did not finish provisioning" in str(refused.value)
    assert second.commands == []


def test_a_ready_binding_is_resumed_without_asking_docker_for_anything(tmp_path: Path) -> None:
    """A warehouse that was already provisioned is reported, not provisioned again.

    `WarehouseLifecycleOrchestrator.provision` closes a `ready` binding as a completed replay
    without calling the provider, which is what makes a restart work at all here: this start
    minted none of the credentials the warehouse was created with.
    """
    seeded = _seed_ready_binding(tmp_path)
    runner = _FakeDockerRunner()

    with provision_demo_managed_warehouse(
        tmp_path, option=_option(runner), clock=_clock
    ) as managed:
        assert managed.binding.binding_id == seeded.binding_id
        assert managed.binding.lifecycle_state is WarehouseBindingState.READY
        assert managed.bindings.current_binding(DEMO_TENANT_ID) == seeded

    assert runner.commands == []


def test_a_binding_record_naming_a_binding_the_store_does_not_hold_is_refused(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "warehouse-control"
    directory.mkdir(mode=0o700, parents=True)
    (directory / "binding.json").write_text(
        json.dumps({"warehouse_binding_id": "whb-" + "0" * 24}), encoding="utf-8"
    )

    with pytest.raises(ManagedWarehouseRefused) as refused:
        provision_demo_managed_warehouse(
            tmp_path, option=_option(_FakeDockerRunner()), clock=_clock
        )

    assert "does not hold" in str(refused.value)


def test_the_setup_surface_answers_once_warehouse_control_owns_a_ready_warehouse(
    tmp_path: Path,
) -> None:
    """The reason this path exists: `get_setup` stops refusing and `foundation` completes.

    On the connection path the same read answers `capability_not_delivered`, which the test
    below pins. Both are the honest answer for their path: there, no governing service owns the
    database the demonstration provisioned.
    """
    _seed_ready_binding(tmp_path)

    with DemoConsole(tmp_path, managed_warehouse=_option(_FakeDockerRunner())) as console:
        setup = console.backend.get_setup(_ARCHITECT)
        workspace = console.backend.get_workspace(_ARCHITECT)

    assert setup.warehouse_binding is not None
    assert setup.warehouse_binding.engine == EngineKind.POSTGRESQL.value
    assert {stage.stage: stage.state for stage in setup.stages}["foundation"] == "complete"
    warehouse = next(
        item for item in workspace.capabilities if item.capability_id == "warehouse-binding"
    )
    assert warehouse.state == "ready"


def test_the_demonstration_minted_tls_material_parses_as_the_provider_reads_it() -> None:
    """The two bundles are the provider's own format, checked against its own models.

    A field renamed here and not there would otherwise surface as a provisioning that wrote an
    unreadable key into a container, hours of debugging away from the line that caused it.
    """
    material = generate_demo_warehouse_tls_material(now=_clock())

    private_keys = warehouse_provider._TLSPrivateKeyBundle.model_validate_json(
        material.private_key_bundle
    )
    certificates = warehouse_provider._TLSCertificateBundle.model_validate_json(
        material.certificate_bundle
    )

    for value in (
        private_keys.server_private_key_pem,
        private_keys.client_private_key_pem,
    ):
        assert value.startswith("-----BEGIN PRIVATE KEY-----")
    for value in (
        certificates.ca_certificate_pem,
        certificates.server_certificate_pem,
        certificates.client_certificate_pem,
    ):
        assert value.startswith("-----BEGIN CERTIFICATE-----")


def test_the_server_certificate_covers_the_name_a_client_on_its_network_reaches_it_by() -> None:
    """`verify-full` checks the name a client connected to against the certificate.

    A BI tool in another container reaches the warehouse by its name on a network they share, not
    by the loopback port the Compose project publishes -- which, from inside any container, is that
    container. Without this name the connection fails hostname verification, and the alternative
    would be verifying only the authority and not who answered.
    """
    material = generate_demo_warehouse_tls_material(
        now=_clock(), internal_hostnames=("pm-pg-abc123-database",)
    )

    certificates = warehouse_provider._TLSCertificateBundle.model_validate_json(
        material.certificate_bundle
    )
    server = x509.load_pem_x509_certificate(certificates.server_certificate_pem.encode("ascii"))
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    assert "pm-pg-abc123-database" in names.get_values_for_type(x509.DNSName)
    # The loopback names the provider itself connects by are still carried.
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert ip_address("127.0.0.1") in names.get_values_for_type(x509.IPAddress)


def test_a_certificate_asked_for_no_internal_name_carries_only_the_loopback_ones() -> None:
    """Named rather than always added, so a deployment that needs none asserts none."""
    material = generate_demo_warehouse_tls_material(now=_clock())

    certificates = warehouse_provider._TLSCertificateBundle.model_validate_json(
        material.certificate_bundle
    )
    server = x509.load_pem_x509_certificate(certificates.server_certificate_pem.encode("ascii"))
    names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    assert names.get_values_for_type(x509.DNSName) == ["localhost"]


def test_tls_material_is_refused_an_issue_time_that_is_not_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        generate_demo_warehouse_tls_material(now=datetime(2026, 1, 1))


def test_every_secret_purpose_resolves_the_credential_warehouse_control_stores_under_it() -> None:
    """The purpose-to-credential mapping is warehouse-control's, not a second opinion on it.

    Its own copy is private to the encrypted secret store this path does not use, so the two
    are compared rather than shared. A purpose renamed there would otherwise leave this
    resolving some other credential, and a provider connecting as one principal with another
    principal's password fails as an authorization denial nobody would trace back here.
    """
    assert _PURPOSE_FIELDS == warehouse_secrets._PURPOSE_FIELDS


def test_the_compose_project_pins_the_image_the_provider_verifies() -> None:
    """The provider inspects the running container against this exact string and refuses others.

    Pinned by digest as well as tag for the reason every other image in `deploy/` is: a rebuilt
    tag would change what the demonstration runs with no edit anywhere.
    """
    project = yaml.safe_load(DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE.read_text(encoding="utf-8"))

    images = {service["image"] for service in project["services"].values()}
    assert images == {POSTGRESQL_WAREHOUSE_IMAGE}
    assert "@sha256:" in POSTGRESQL_WAREHOUSE_IMAGE


def test_the_compose_project_declares_everything_the_provider_addresses() -> None:
    """The provider names a container, two networks and a volume, and restores under a profile.

    Each is addressed by the name the provider supplies, so a project that declared one of them
    without its variable would have the provider verify a resource Compose never created.
    """
    project = yaml.safe_load(DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE.read_text(encoding="utf-8"))

    assert set(project["services"]) == {"postgresql", "postgresql_restore"}
    assert project["services"]["postgresql_restore"]["profiles"] == ["restore"]
    assert project["services"]["postgresql"]["ports"] == [
        "127.0.0.1:${HEINZEL_POSTGRES_HOST_PORT:?required}:5432"
    ]
    assert "ports" not in project["services"]["postgresql_restore"]
    assert project["networks"]["warehouse_internal"]["internal"] is True
    assert set(project["volumes"]) == {"warehouse_data"}


def test_the_demonstration_takes_a_warehouse_connection_or_provisions_one_but_never_both(
    tmp_path: Path,
) -> None:
    """Two complete answers to where the warehouse comes from, and no way to reconcile them."""
    with pytest.raises(ValueError, match="not both"):
        DemoConsole(
            tmp_path,
            warehouse_dsn="postgresql://postgres@127.0.0.1:5432/heinzel",
            managed_warehouse=_option(_FakeDockerRunner()),
        )


def test_without_either_setting_the_managed_warehouse_is_still_not_delivered(
    tmp_path: Path,
) -> None:
    """The default demonstration is unchanged by this path existing.

    No binding reader is wired, so the setup surface refuses and the workspace card reports the
    capability as not delivered -- which is what it reported before the warehouse-control path
    was built, and has to keep reporting, because a console given neither setting owns no
    warehouse any governing service made.
    """
    with DemoConsole(tmp_path) as console:
        workspace = console.backend.get_workspace(_ARCHITECT)
        with pytest.raises(ConsoleUnavailable) as refused:
            console.backend.get_setup(_ARCHITECT)

    assert refused.value.code == CAPABILITY_NOT_DELIVERED
    warehouse = next(
        item for item in workspace.capabilities if item.capability_id == "warehouse-binding"
    )
    assert warehouse.state == "not_delivered"
    assert warehouse.dependency == "warehouse-control read wiring"
    assert not (tmp_path / "warehouse-control").exists()
