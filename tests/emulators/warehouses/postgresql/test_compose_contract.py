from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

COMPOSE_PATH = Path(__file__).with_name("compose.yaml")
CONTAINER_PRIVATE_DIRECTORY = "/var/lib/postgresql/pillarmesh-tls"


def _postgresql_entrypoint() -> str:
    compose: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    entrypoint = compose["x-postgresql-service"]["entrypoint"]

    assert isinstance(entrypoint, list)
    assert isinstance(entrypoint[-1], str)
    return entrypoint[-1]


def test_postgresql_bootstrap_copies_hba_configuration_out_of_private_bind_mount() -> None:
    entrypoint = _postgresql_entrypoint()

    assert (
        "install -m 0600 -o postgres -g postgres /pillarmesh-private/pg_hba.conf "
        f"{CONTAINER_PRIVATE_DIRECTORY}/pg_hba.conf"
    ) in entrypoint
    assert f"-c hba_file={CONTAINER_PRIVATE_DIRECTORY}/pg_hba.conf" in entrypoint
    assert "-c hba_file=/pillarmesh-private/pg_hba.conf" not in entrypoint
