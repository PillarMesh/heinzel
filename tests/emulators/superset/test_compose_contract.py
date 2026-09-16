from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
EMULATOR_ROOT = ROOT / "tests" / "emulators" / "superset"
COMPOSE_FILE = EMULATOR_ROOT / "compose.yaml"


def _render_compose_config() -> dict[str, Any]:
    environment = {
        "PILLARMESH_SUPERSET_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "PILLARMESH_SUPERSET_SECRET_KEY": secrets.token_urlsafe(48),
        "PILLARMESH_SUPERSET_WAREHOUSE_PASSWORD": secrets.token_urlsafe(24),
        "PILLARMESH_SUPERSET_HOST_PORT": "18088",
        "PILLARMESH_SUPERSET_PROJECT_NAME": "pillarmesh-superset-contract-test",
        "PILLARMESH_SUPERSET_PRIVATE_DIRECTORY": "/private/tmp/pillarmesh-superset-test",
    }
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
        env=os.environ | environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert isinstance(config, dict)
    return config


def test_superset_stack_is_loopback_only_and_waits_for_governed_postgresql() -> None:
    config = _render_compose_config()
    services = config["services"]
    superset = services["superset"]
    warehouse = services["warehouse"]

    assert superset["ports"] == [
        {
            "mode": "ingress",
            "target": 8088,
            "published": "18088",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert warehouse.get("ports", []) == []
    assert superset["depends_on"]["warehouse"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert superset["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--cacert",
        "/pillarmesh-private/ca.crt",
        "https://127.0.0.1:8088/health",
    ]


def test_superset_stack_uses_pinned_images_and_private_secret_inputs() -> None:
    config = _render_compose_config()
    services = config["services"]
    superset = services["superset"]
    warehouse = services["warehouse"]

    assert "@sha256:" in warehouse["image"]
    assert superset["build"]["dockerfile"] == "Dockerfile"
    assert superset["build"]["context"] == str(EMULATOR_ROOT)
    assert superset["restart"] == "unless-stopped"
    assert warehouse["restart"] == "unless-stopped"
    assert int(superset["mem_limit"]) >= 1024 * 1024 * 1024
    assert int(warehouse["mem_limit"]) >= 256 * 1024 * 1024
    assert set(superset["environment"]) >= {
        "PILLARMESH_SUPERSET_ADMIN_PASSWORD",
        "PILLARMESH_SUPERSET_SECRET_KEY",
        "PILLARMESH_SUPERSET_WAREHOUSE_PASSWORD",
    }
