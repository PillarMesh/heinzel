from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from heinzel_provider_openmetadata import UPSTREAM_IMAGES

from tests.emulators.openmetadata import wait_ready
from tests.integration import openmetadata_live_harness as live_harness

ROOT = Path(__file__).resolve().parents[2]
EMULATOR_ROOT = ROOT / "tests" / "emulators" / "openmetadata"
COMPOSE_FILE = EMULATOR_ROOT / "compose.yaml"
LAUNCHER = EMULATOR_ROOT / "run.sh"


def _compose_environment() -> dict[str, str]:
    return {
        "HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD": secrets.token_urlsafe(24),
        "HEINZEL_OPENMETADATA_DATABASE_PASSWORD": secrets.token_urlsafe(24),
        "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD": secrets.token_urlsafe(24),
    }


def _render_compose_config(environment: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "--profile",
            "ingestion",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=os.environ | environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert isinstance(config, dict)
    return config


@pytest.fixture
def compose_config() -> dict[str, object]:
    assert COMPOSE_FILE.is_file(), "OpenMetadata Compose configuration is missing"
    return _render_compose_config(_compose_environment())


def published_hosts(compose_config: dict[str, object]) -> set[str]:
    services = compose_config["services"]
    assert isinstance(services, dict)
    hosts: set[str] = set()
    for service in services.values():
        assert isinstance(service, dict)
        ports = service.get("ports", [])
        assert isinstance(ports, list)
        for port in ports:
            assert isinstance(port, dict)
            host = port.get("host_ip")
            if isinstance(host, str):
                hosts.add(host)
    return hosts


def image_tags(compose_config: dict[str, object]) -> frozenset[str]:
    services = compose_config["services"]
    assert isinstance(services, dict)
    images: set[str] = set()
    for service in services.values():
        assert isinstance(service, dict)
        image = service.get("image")
        if isinstance(image, str):
            images.add(image)
    return frozenset(images)


def test_openmetadata_ports_are_bound_to_loopback(compose_config: dict[str, object]) -> None:
    assert published_hosts(compose_config) == {"127.0.0.1"}


def test_openmetadata_images_are_pinned_to_the_upstream_release(
    compose_config: dict[str, object],
) -> None:
    assert image_tags(compose_config) == UPSTREAM_IMAGES
    assert all("@sha256:" in image for image in image_tags(compose_config))


def test_elasticsearch_does_not_force_a_foreign_processor_architecture(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    elasticsearch = services["elasticsearch"]
    assert isinstance(elasticsearch, dict)

    assert "platform" not in elasticsearch


def test_core_catalog_services_have_memory_budgets_and_restart_supervision(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)

    for service_name in ("mysql", "elasticsearch", "openmetadata-server"):
        service = services[service_name]
        assert isinstance(service, dict)
        assert service["restart"] == "unless-stopped"
        assert int(service["mem_limit"]) >= 512 * 1024 * 1024


def test_elasticsearch_has_headroom_beyond_its_managed_heap(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    elasticsearch = services["elasticsearch"]
    assert isinstance(elasticsearch, dict)

    assert elasticsearch["environment"]["ES_JAVA_OPTS"] == "-Xms512m -Xmx512m"
    assert int(elasticsearch["mem_limit"]) >= 1536 * 1024 * 1024


def test_openmetadata_server_has_headroom_for_restored_search_rebuild(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    server = services["openmetadata-server"]
    assert isinstance(server, dict)

    assert server["environment"]["JAVA_OPTS"] == "-Xms512m -Xmx1024m"
    assert int(server["mem_limit"]) >= 2048 * 1024 * 1024


def test_search_health_requires_a_non_red_cluster_and_is_loopback_observable(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    elasticsearch = services["elasticsearch"]
    assert isinstance(elasticsearch, dict)

    healthcheck = elasticsearch["healthcheck"]
    assert isinstance(healthcheck, dict)
    assert "green|yellow" in " ".join(healthcheck["test"])
    assert elasticsearch["ports"] == [
        {
            "mode": "ingress",
            "target": 9200,
            "published": "9200",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]


def test_ingestion_is_opt_in_until_source_acquisition_is_composed(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    ingestion = services["ingestion"]
    assert isinstance(ingestion, dict)

    assert ingestion["profiles"] == ["ingestion"]
    assert int(ingestion["mem_limit"]) >= 1024 * 1024 * 1024


def test_database_credential_rotation_uses_shared_mysql_socket_and_gates_consumers() -> None:
    environment = {
        "HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD": "test-only-root-password",
        "HEINZEL_OPENMETADATA_DATABASE_PASSWORD": "test-only-database-password",
        "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD": "test-only-airflow-password",
    }
    compose_config = _render_compose_config(environment)
    services = compose_config["services"]
    assert isinstance(services, dict)
    mysql = services["mysql"]
    rotation = services["rotate-database-credentials"]
    migrations = services["execute-migrate-all"]
    ingestion = services["ingestion"]
    assert isinstance(mysql, dict)
    assert isinstance(rotation, dict)
    assert isinstance(migrations, dict)
    assert isinstance(ingestion, dict)

    assert rotation["image"] == mysql["image"]
    assert rotation["environment"] == {
        "MYSQL_PWD": environment["HEINZEL_OPENMETADATA_MYSQL_ROOT_PASSWORD"],
        "OPENMETADATA_DATABASE_PASSWORD": environment["HEINZEL_OPENMETADATA_DATABASE_PASSWORD"],
        "AIRFLOW_DATABASE_PASSWORD": environment["HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD"],
    }
    assert rotation["depends_on"] == {"mysql": {"condition": "service_healthy", "required": True}}
    assert rotation.get("volumes") == mysql["volumes"]

    command = rotation["command"]
    assert isinstance(command, list)
    assert command[:2] == ["sh", "-ec"]
    assert all(isinstance(argument, str) for argument in command)
    command_text = " ".join(command)
    assert "$$OPENMETADATA_DATABASE_PASSWORD" in command_text
    assert "$$AIRFLOW_DATABASE_PASSWORD" in command_text
    assert "| mysql --socket=/var/lib/mysql/mysql.sock --user=root" in command_text
    assert "--protocol=TCP" not in command_text
    assert "--host" not in command_text
    assert "--password" not in command_text
    assert "--execute" not in command_text
    assert all(secret not in command_text for secret in environment.values())

    assert migrations["depends_on"]["rotate-database-credentials"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    assert ingestion["depends_on"]["rotate-database-credentials"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }


@pytest.mark.parametrize(
    "missing_variable",
    (
        "HEINZEL_OPENMETADATA_DATABASE_PASSWORD",
        "HEINZEL_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD",
    ),
)
def test_compose_refuses_missing_private_internal_credentials(missing_variable: str) -> None:
    environment = os.environ | _compose_environment()
    environment.pop(missing_variable)

    result = subprocess.run(
        ["docker", "compose", "--file", str(COMPOSE_FILE), "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0


def test_launcher_refuses_missing_docker_config_before_docker_is_touched(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    touched = tmp_path / "docker-was-invoked"
    docker.write_text('#!/bin/sh\ntouch "$HEINZEL_DOCKER_TOUCHED"\nexit 1\n')
    docker.chmod(0o755)

    result = subprocess.run(
        [str(LAUNCHER), "true"],
        cwd=ROOT,
        env={
            "PATH": str(tmp_path),
            "HEINZEL_DOCKER_TOUCHED": str(touched),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == "ERROR: DOCKER_CONFIG is required\n"
    assert not touched.exists()


def test_launcher_refuses_missing_secret_store_key_before_docker_is_touched(
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker"
    touched = tmp_path / "docker-was-invoked"
    docker.write_text('#!/bin/sh\ntouch "$HEINZEL_DOCKER_TOUCHED"\nexit 1\n')
    docker.chmod(0o755)

    result = subprocess.run(
        [str(LAUNCHER), "true"],
        cwd=ROOT,
        env={
            "PATH": str(tmp_path),
            "DOCKER_CONFIG": str(tmp_path / "docker-config"),
            "HEINZEL_DOCKER_TOUCHED": str(touched),
            "HEINZEL_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD": "test-bootstrap-password",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == "ERROR: HEINZEL_OPENMETADATA_SECRET_STORE_KEY is required\n"
    assert not touched.exists()


def test_readiness_accepts_the_openmetadata_terminal_text_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(200, text="OK"),
    )

    assert wait_ready.main(["--url", "http://127.0.0.1:8585", "--timeout-seconds", "0.5"]) == 0


def test_an_unset_readiness_budget_leaves_the_readiness_default_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(live_harness.READINESS_TIMEOUT_ENVIRONMENT_NAME, raising=False)

    assert live_harness._readiness_timeout_seconds() is None


def test_a_configured_readiness_budget_widens_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(live_harness.READINESS_TIMEOUT_ENVIRONMENT_NAME, "600")

    budget = live_harness._readiness_timeout_seconds()

    assert budget == 600.0


@pytest.mark.parametrize("configured", ["", "0", "-5", "soon", "600s"])
def test_an_unusable_readiness_budget_fails_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    # Falling back to the default would let a run that was configured for slow hardware time
    # out exactly as though it had never been configured, which is the failure this prevents.
    monkeypatch.setenv(live_harness.READINESS_TIMEOUT_ENVIRONMENT_NAME, configured)

    with pytest.raises(ValueError, match=live_harness.READINESS_TIMEOUT_ENVIRONMENT_NAME):
        live_harness._readiness_timeout_seconds()
