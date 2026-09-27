from __future__ import annotations

import os
import secrets
import socket
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from tests.emulators.warehouses.postgresql.init_tls import (
    generate_tls_material,
    write_tls_material,
)

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = Path(__file__).with_name("compose.yaml")


@dataclass(frozen=True, slots=True)
class LocalSuperset:
    project_name: str
    environment: dict[str, str]
    base_url: str
    admin_password: str
    warehouse_password: str
    ca_certificate_path: Path

    def provision_access_principal(self) -> SupersetAccessPrincipal:
        password = secrets.token_urlsafe(24) + "Aa1!"
        with self._http_client() as client:
            headers = self._session_headers(
                client,
                username="admin",
                password=self.admin_password,
                mutation=True,
            )
            roles_response = client.get(
                "/api/v1/security/roles/",
                headers=headers,
                params={"q": "(page:0,page_size:100)"},
            )
            roles_response.raise_for_status()
            roles = roles_response.json()["result"]
            gamma_role_id = next(role["id"] for role in roles if role["name"] == "Gamma")
            created_role_ids: list[int] = []
            for name in ("pm-live-base-a", "pm-live-grant-a", "pm-live-base-b"):
                response = client.post(
                    "/api/v1/security/roles/",
                    headers=headers,
                    json={"name": name},
                )
                response.raise_for_status()
                created_role_ids.append(int(response.json()["id"]))
            user_response = client.post(
                "/api/v1/security/users/",
                headers=headers,
                json={
                    "username": "requester-a",
                    "first_name": "Request",
                    "last_name": "A",
                    "email": "requester-a@localhost.invalid",
                    "password": password,
                    "active": True,
                    "roles": [gamma_role_id, created_role_ids[1]],
                },
            )
            user_response.raise_for_status()
        return SupersetAccessPrincipal(
            username="requester-a",
            password=password,
            gamma_role_id=gamma_role_id,
            base_role_id=created_role_ids[0],
            grant_role_id=created_role_ids[1],
            isolated_base_role_id=created_role_ids[2],
        )

    def access_token(self, *, username: str, password: str) -> str:
        with self._http_client() as client:
            return self._session_headers(
                client,
                username=username,
                password=password,
                mutation=False,
            )["Authorization"].removeprefix("Bearer ")

    def visible_dashboard_ids(self, *, access_token: str) -> tuple[int, ...]:
        with self._http_client() as client:
            response = client.get(
                "/api/v1/dashboard/",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"q": "(page:0,page_size:100)"},
            )
            response.raise_for_status()
            result = response.json()["result"]
            return tuple(sorted(int(dashboard["id"]) for dashboard in result))

    @property
    def database_uri(self) -> str:
        return (
            "postgresql+psycopg2://superset_reader:"
            f"{self.warehouse_password}@warehouse:5432/heinzel_warehouse"
        )

    def resource_counts(self) -> dict[str, int]:
        tls_context = ssl.create_default_context(cafile=str(self.ca_certificate_path))
        with httpx.Client(
            base_url=self.base_url,
            verify=tls_context,
            timeout=30.0,
            trust_env=False,
        ) as client:
            login = client.post(
                "/api/v1/security/login",
                json={
                    "username": "admin",
                    "password": self.admin_password,
                    "provider": "db",
                    "refresh": True,
                },
            )
            login.raise_for_status()
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            counts: dict[str, int] = {}
            for kind in ("database", "dataset", "chart", "dashboard"):
                response = client.get(
                    f"/api/v1/{kind}/",
                    headers=headers,
                    params={"q": "(page:0,page_size:100)"},
                )
                response.raise_for_status()
                payload = response.json()
                counts[kind] = len(payload["result"])
            return counts

    def warehouse_rows(self) -> tuple[tuple[str, str], ...]:
        output = self._warehouse_query(
            "SELECT region, revenue FROM analytics.orders_current ORDER BY region"
        )
        rows: list[tuple[str, str]] = []
        for line in output.splitlines():
            region, revenue = line.split("|", maxsplit=1)
            rows.append((region, revenue))
        return tuple(rows)

    def reader_is_least_privilege(self) -> bool:
        output = self._warehouse_query(
            "SELECT has_table_privilege(current_user, "
            "'analytics.orders_current', 'SELECT') "
            "AND has_schema_privilege(current_user, 'analytics', 'USAGE') "
            "AND NOT has_schema_privilege(current_user, 'analytics', 'CREATE') "
            "AND NOT has_database_privilege(current_user, "
            "'heinzel_warehouse', 'CREATE')"
        )
        return output == "t"

    def assert_removed(self) -> None:
        for kind in ("container", "network", "volume"):
            list_arguments = ["docker", kind, "ls", "--quiet"]
            if kind == "container":
                list_arguments.append("--all")
            result = subprocess.run(
                [
                    *list_arguments,
                    "--filter",
                    f"label=com.docker.compose.project={self.project_name}",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or result.stdout.strip():
                raise RuntimeError(f"Superset {kind} cleanup was incomplete")

    def _warehouse_query(self, statement: str) -> str:
        result = _compose(
            self,
            "exec",
            "--no-TTY",
            "warehouse",
            "/bin/sh",
            "-ceu",
            'PGPASSWORD="$HEINZEL_SUPERSET_WAREHOUSE_PASSWORD" '
            "psql --host=127.0.0.1 --username=superset_reader "
            '--dbname=heinzel_warehouse --tuples-only --no-align --field-separator="|" '
            ' --command="$1"',
            "query",
            statement,
        )
        if result.returncode != 0:
            raise RuntimeError("Superset warehouse verification failed")
        return result.stdout.strip()

    def _http_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            verify=ssl.create_default_context(cafile=str(self.ca_certificate_path)),
            timeout=30.0,
            trust_env=False,
        )

    def _session_headers(
        self,
        client: httpx.Client,
        *,
        username: str,
        password: str,
        mutation: bool,
    ) -> dict[str, str]:
        login = client.post(
            "/api/v1/security/login",
            json={
                "username": username,
                "password": password,
                "provider": "db",
                "refresh": True,
            },
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        if mutation:
            csrf = client.get("/api/v1/security/csrf_token/", headers=headers)
            csrf.raise_for_status()
            headers.update(
                {
                    "X-CSRFToken": csrf.json()["result"],
                    "Referer": self.base_url + "/",
                }
            )
        return headers


@dataclass(frozen=True, slots=True, repr=False)
class SupersetAccessPrincipal:
    username: str
    password: str
    gamma_role_id: int
    base_role_id: int
    grant_role_id: int
    isolated_base_role_id: int


@contextmanager
def fresh_superset_stack() -> Iterator[LocalSuperset]:
    project_name = f"heinzel-superset-{secrets.token_hex(6)}"
    with TemporaryDirectory(prefix="heinzel-superset-private-") as root_text:
        private_directory = Path(root_text) / "tls"
        write_tls_material(private_directory, generate_tls_material())
        port = _available_port()
        admin_password = secrets.token_urlsafe(24)
        warehouse_password = secrets.token_urlsafe(24)
        environment = os.environ | {
            "HEINZEL_SUPERSET_ADMIN_PASSWORD": admin_password,
            "HEINZEL_SUPERSET_SECRET_KEY": secrets.token_urlsafe(48),
            "HEINZEL_SUPERSET_WAREHOUSE_PASSWORD": warehouse_password,
            "HEINZEL_SUPERSET_HOST_PORT": str(port),
            "HEINZEL_SUPERSET_PROJECT_NAME": project_name,
            "HEINZEL_SUPERSET_PRIVATE_DIRECTORY": str(private_directory),
        }
        stack = LocalSuperset(
            project_name=project_name,
            environment=environment,
            base_url=f"https://127.0.0.1:{port}",
            admin_password=admin_password,
            warehouse_password=warehouse_password,
            ca_certificate_path=private_directory / "ca.crt",
        )
        try:
            started = _compose(stack, "up", "--detach", "--build")
            if started.returncode != 0:
                raise RuntimeError("Superset cold start failed; inspect private Docker logs")
            _wait_ready(stack)
            yield stack
        finally:
            stopped = _compose(
                stack,
                "down",
                "--volumes",
                "--remove-orphans",
                "--rmi",
                "local",
            )
            if stopped.returncode != 0:
                raise RuntimeError("Superset teardown failed")
            stack.assert_removed()


def _compose(stack: LocalSuperset, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "--project-name",
            stack.project_name,
            *arguments,
        ],
        cwd=ROOT,
        env=stack.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _wait_ready(stack: LocalSuperset) -> None:
    deadline = time.monotonic() + 180
    last_error: httpx.HTTPError | None = None
    tls_context = ssl.create_default_context(cafile=str(stack.ca_certificate_path))
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"{stack.base_url}/health",
                verify=tls_context,
                timeout=3,
                trust_env=False,
            )
            response.raise_for_status()
            if response.text.strip() == "OK":
                return
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError("Superset HTTPS readiness deadline expired") from last_error


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
