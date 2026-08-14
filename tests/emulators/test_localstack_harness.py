from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pillarmesh_provider_sdk import ColumnObservation, ProviderObservation

from tests.emulators import localstack_support

ROOT = Path(__file__).resolve().parents[2]
EMULATOR_ROOT = ROOT / "tests" / "emulators" / "localstack-snowflake"
COMPOSE_FILE = EMULATOR_ROOT / "compose.yaml"
LAUNCHER = EMULATOR_ROOT / "run.sh"


def test_launcher_refuses_missing_token_before_docker_is_touched(tmp_path: Path) -> None:
    assert LAUNCHER.is_file(), "LocalStack launcher is missing"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_marker = tmp_path / "docker-was-called"
    docker = fake_bin / "docker"
    docker.write_text(f"#!/bin/sh\ntouch '{docker_marker}'\nexit 99\n")
    docker.chmod(0o755)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(LAUNCHER)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "ERROR: LOCALSTACK_AUTH_TOKEN is required\n"
    assert not docker_marker.exists()


def test_compose_configuration_is_pinned_and_host_private() -> None:
    assert COMPOSE_FILE.is_file(), "LocalStack Compose configuration is missing"
    environment = os.environ | {"LOCALSTACK_AUTH_TOKEN": "test-token"}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)
    service = configuration["services"]["snowflake"]
    assert service["image"] == "localstack/snowflake:2026.06.0"
    assert service["ports"] == [
        {
            "mode": "ingress",
            "target": 4566,
            "published": "4566",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert service["environment"]["LOCALSTACK_AUTH_TOKEN"] == "test-token"
    assert all("docker.sock" not in volume["source"] for volume in service["volumes"])
    init_mount = next(
        volume
        for volume in service["volumes"]
        if volume["target"] == "/etc/localstack/init/ready.d/10-m0.sf.sql"
    )
    assert init_mount["type"] == "bind"
    assert init_mount["read_only"] is True


def test_emulator_provider_smoke_test_is_collectable() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "emulator",
            "tests/emulators/test_localstack_snowflake.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "test_provider_stages_commits_verifies_and_replays" in result.stdout


def test_readiness_timeout_reports_last_observation_failure() -> None:
    clock: Iterator[float] = iter((0.0, 0.0, 1.0))

    def observe() -> ProviderObservation:
        raise ValueError("schema hook failed")

    with pytest.raises(
        RuntimeError,
        match="LocalStack Snowflake initialization did not complete; "
        "last observation failure: ValueError: schema hook failed",
    ):
        localstack_support.wait_for_expected_observation(
            observe,
            deadline=0.5,
            monotonic=lambda: next(clock),
            sleep=lambda _: None,
        )


def test_observation_mismatch_names_the_field_and_safe_actual_value() -> None:
    observation = cast(ProviderObservation, SimpleNamespace(object_kind="view"))

    with pytest.raises(
        AssertionError,
        match="object_kind mismatch: expected='base_table', actual='view'",
    ):
        localstack_support.assert_expected_observation(observation)


def test_expected_localstack_observation_records_unavailable_key_metadata() -> None:
    observation = ProviderObservation.model_construct(
        object_kind="base_table",
        columns=(
            ColumnObservation(name="order_id", type_name="NUMBER(19,0)", nullable=False),
            ColumnObservation(name="customer_ref", type_name="VARCHAR(65535)", nullable=False),
            ColumnObservation(name="amount", type_name="NUMBER(18,2)", nullable=False),
            ColumnObservation(name="currency", type_name="VARCHAR(3)", nullable=False),
            ColumnObservation(name="order_status", type_name="VARCHAR(65535)", nullable=False),
            ColumnObservation(name="updated_at", type_name="TIMESTAMP_TZ(6)", nullable=False),
        ),
        key_name=None,
        key_type=None,
        key_nullable=None,
        key_constraint="none",
        commit_ledger_object_kind="base_table",
        commit_ledger_columns=(
            ColumnObservation(name="batch_id", type_name="VARCHAR(16777216)", nullable=False),
            ColumnObservation(name="manifest_digest", type_name="VARCHAR(64)", nullable=False),
            ColumnObservation(name="committed_at", type_name="TIMESTAMP_TZ(9)", nullable=False),
        ),
        commit_ledger_key_name=None,
        commit_ledger_key_constraint="none",
    )

    localstack_support.assert_expected_observation(observation)


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        (
            "CREATE TABLE PILLARMESH_M0.TRANSFER.ORDERS "
            '(ORDER_ID NUMBER NOT NULL, CONSTRAINT PK_ORDERS PRIMARY KEY ("ORDER_ID"))',
            ("order_id", "primary_key"),
        ),
        (
            "CREATE TABLE PILLARMESH_M0.TRANSFER.ORDERS "
            "(ORDER_ID NUMBER, CUSTOMER_REF TEXT, PRIMARY KEY (ORDER_ID, CUSTOMER_REF))",
            (None, "none"),
        ),
        (
            "CREATE TABLE PILLARMESH_M0.TRANSFER.ORDERS (ORDER_ID NUMBER NOT NULL)",
            (None, "none"),
        ),
    ],
)
def test_localstack_key_observation_uses_ddl_and_fails_closed(
    ddl: str,
    expected: tuple[str | None, str],
) -> None:
    class Cursor:
        def __init__(self) -> None:
            self.call: tuple[str, tuple[str, ...]] | None = None

        def execute(self, query: str, params: tuple[str, ...]) -> None:
            self.call = (query, params)

        def fetchone(self) -> tuple[str]:
            return (ddl,)

    cursor = Cursor()
    provider = localstack_support.localstack_provider()

    assert provider._key_constraint(cursor, "ORDERS") == expected
    assert cursor.call == (
        "SELECT GET_DDL('TABLE', %s)",
        ("PILLARMESH_M0.TRANSFER.ORDERS",),
    )


def test_launcher_owns_only_its_project_and_strips_child_only_environment(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    uv_log = tmp_path / "uv.log"
    docker = fake_bin / "docker"
    docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{docker_log}'\n")
    docker.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"printf 'token=%s venv=%s args=%s\\n' \"${{LOCALSTACK_AUTH_TOKEN:-absent}}\" "
        '"${VIRTUAL_ENV:-absent}" "$*" '
        f">>'{uv_log}'\n"
        'case "$*" in\n'
        "  *wait_ready.py*) exit 0 ;;\n"
        "  *) exit 7 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)
    environment = {
        "LOCALSTACK_AUTH_TOKEN": "secret-token-value",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "VIRTUAL_ENV": "/tmp/unrelated-project/.venv",
    }

    result = subprocess.run(
        [str(LAUNCHER)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    docker_calls = docker_log.read_text().splitlines()
    assert len(docker_calls) == 3
    project_pattern = r"--project-name (pillarmesh-m0-localstack-[0-9]+)"
    up_project = re.search(project_pattern, docker_calls[0])
    logs_project = re.search(project_pattern, docker_calls[1])
    down_project = re.search(project_pattern, docker_calls[2])
    assert up_project is not None
    assert logs_project is not None
    assert down_project is not None
    assert up_project.group(1) == logs_project.group(1) == down_project.group(1)
    assert " up --detach --wait" in docker_calls[0]
    assert " logs --no-color snowflake" in docker_calls[1]
    assert " down --volumes --remove-orphans" in docker_calls[2]
    assert all(not call.startswith("compose --file") for call in docker_calls)
    uv_calls = uv_log.read_text().splitlines()
    assert len(uv_calls) == 2
    assert all(call.startswith("token=absent venv=absent ") for call in uv_calls)
    assert "run python" in uv_calls[0]
    assert "wait_ready.py" in uv_calls[0]
    assert "run pytest -m emulator" in uv_calls[1]
