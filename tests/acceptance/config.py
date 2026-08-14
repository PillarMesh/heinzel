from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_VARIABLES = (
    "PILLARMESH_STATE_PATH",
    "PILLARMESH_OUTPUT_DIR",
    "PILLARMESH_CLEANUP_LEDGER_PATH",
    "PILLARMESH_SIGNING_KEY_ID",
    "PILLARMESH_SIGNING_PRIVATE_KEY_B64",
    "PILLARMESH_POSTGRES_DSN",
    "PILLARMESH_POSTGRES_DATABASE",
    "PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL",
    "PILLARMESH_POSTGRES_OWNER_PRINCIPAL",
    "PILLARMESH_POSTGRES_CONNECTION_HANDLE",
    "PILLARMESH_POSTGRES_SCHEMA",
    "PILLARMESH_POSTGRES_TABLE",
    "PILLARMESH_POSTGRES_DENIAL_SCHEMA",
    "PILLARMESH_POSTGRES_FIXTURE_DSN",
    "PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL",
    "PILLARMESH_SNOWFLAKE_ACCOUNT",
    "PILLARMESH_SNOWFLAKE_USER",
    "PILLARMESH_SNOWFLAKE_PASSWORD",
    "PILLARMESH_SNOWFLAKE_OWNER_USER",
    "PILLARMESH_SNOWFLAKE_ROLE",
    "PILLARMESH_SNOWFLAKE_WAREHOUSE",
    "PILLARMESH_SNOWFLAKE_DATABASE",
    "PILLARMESH_SNOWFLAKE_SCHEMA",
    "PILLARMESH_SNOWFLAKE_STAGE",
    "PILLARMESH_SNOWFLAKE_TARGET_TABLE",
    "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
    "PILLARMESH_SNOWFLAKE_LEDGER_TABLE",
    "PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE",
    "PILLARMESH_SNOWFLAKE_DENIAL_DATABASE",
    "PILLARMESH_CREDENTIAL_CANARIES_JSON",
    "PILLARMESH_ROW_VALUE_CANARY",
    "PILLARMESH_OPERATOR_PSEUDONYM",
    "PILLARMESH_HOST_PSEUDONYM",
    "PILLARMESH_MCP_PROTOCOL_VERSION",
    "PILLARMESH_OWNER_AUTHORIZATION_REFERENCE",
)

PRODUCT_VARIABLES = (
    "PILLARMESH_STATE_PATH",
    "PILLARMESH_OUTPUT_DIR",
    "PILLARMESH_SIGNING_KEY_ID",
    "PILLARMESH_SIGNING_PRIVATE_KEY_B64",
    "PILLARMESH_POSTGRES_DSN",
    "PILLARMESH_POSTGRES_CONNECTION_HANDLE",
    "PILLARMESH_POSTGRES_SCHEMA",
    "PILLARMESH_POSTGRES_TABLE",
    "PILLARMESH_SNOWFLAKE_ACCOUNT",
    "PILLARMESH_SNOWFLAKE_USER",
    "PILLARMESH_SNOWFLAKE_PASSWORD",
    "PILLARMESH_SNOWFLAKE_ROLE",
    "PILLARMESH_SNOWFLAKE_WAREHOUSE",
    "PILLARMESH_SNOWFLAKE_DATABASE",
    "PILLARMESH_SNOWFLAKE_SCHEMA",
    "PILLARMESH_SNOWFLAKE_STAGE",
    "PILLARMESH_SNOWFLAKE_TARGET_TABLE",
    "PILLARMESH_SNOWFLAKE_LEDGER_TABLE",
    "PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE",
)

PASSTHROUGH_VARIABLES = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "UV_CACHE_DIR",
    "VIRTUAL_ENV",
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
POSTGRES_MARKER_TABLE = "environment_marker"
POSTGRES_AUDIT_FUNCTION = "runtime_source_read_count"
SNOWFLAKE_OWNER_ROLE = "PILLARMESH_M0_OWNER"
SNOWFLAKE_FILE_FORMAT = "M0_CSV"
SNOWFLAKE_ENVIRONMENT_MARKER = "ENVIRONMENT_MARKER"


class HarnessError(RuntimeError):
    pass


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _inside(path: Path, parent: Path) -> bool:
    try:
        _absolute_lexical(path).relative_to(_absolute_lexical(parent))
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    current = _absolute_lexical(path)
    while True:
        if (current.exists() or current.is_symlink()) and current.is_symlink():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _identifier(value: str, variable: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise HarnessError(f"invalid identifier variable: {variable}")
    return value


def derive_environment_identity(environment: Mapping[str, str]) -> str:
    shared_boundary = {
        "postgres_database": environment["PILLARMESH_POSTGRES_DATABASE"],
        "postgres_schema": environment["PILLARMESH_POSTGRES_SCHEMA"],
        "postgres_table": environment["PILLARMESH_POSTGRES_TABLE"],
        "snowflake_account": environment["PILLARMESH_SNOWFLAKE_ACCOUNT"],
        "snowflake_database": environment["PILLARMESH_SNOWFLAKE_DATABASE"],
        "snowflake_schema": environment["PILLARMESH_SNOWFLAKE_SCHEMA"],
        "snowflake_stage": environment["PILLARMESH_SNOWFLAKE_STAGE"],
        "snowflake_target": environment["PILLARMESH_SNOWFLAKE_TARGET_TABLE"],
        "snowflake_negative_target": environment["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"],
        "snowflake_ledger": environment["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"],
    }
    normalized = {name: value.casefold() for name, value in shared_boundary.items()}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"pillarmesh-m0-environment-v1\0" + payload).hexdigest()


def _reservation_path(environment_identity: str) -> Path:
    parent = Path(tempfile.gettempdir()).resolve() / f"pillarmesh-m0-reservations-{os.getuid()}"
    return parent / f"{environment_identity}.lock"


@dataclass(frozen=True)
class AcceptanceConfig:
    environment: Mapping[str, str]
    repository_root: Path
    state_path: Path
    output_dir: Path
    cleanup_ledger_path: Path
    reservation_path: Path
    environment_identity: str
    credential_canaries: tuple[str, ...]

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        repository_root: Path,
        allow_reuse: bool = False,
    ) -> AcceptanceConfig:
        missing = tuple(name for name in REQUIRED_VARIABLES if not environment.get(name))
        if missing:
            raise HarnessError("missing required variables: " + ", ".join(missing))

        raw_paths = (
            ("PILLARMESH_STATE_PATH", Path(environment["PILLARMESH_STATE_PATH"])),
            ("PILLARMESH_OUTPUT_DIR", Path(environment["PILLARMESH_OUTPUT_DIR"])),
            (
                "PILLARMESH_CLEANUP_LEDGER_PATH",
                Path(environment["PILLARMESH_CLEANUP_LEDGER_PATH"]),
            ),
        )
        checked_paths = tuple((name, _absolute_lexical(path)) for name, path in raw_paths)
        path_by_name = dict(checked_paths)
        errors: list[str] = []
        for (name, raw), (_, checked) in zip(raw_paths, checked_paths, strict=True):
            if not raw.expanduser().is_absolute() or _inside(checked, repository_root):
                errors.append(name)
            if _has_symlink_component(checked):
                errors.append(name)
        if len(set(path_by_name.values())) != 3:
            errors.extend(name for name, _path in checked_paths)

        state_path = path_by_name["PILLARMESH_STATE_PATH"]
        output_dir = path_by_name["PILLARMESH_OUTPUT_DIR"]
        ledger_path = path_by_name["PILLARMESH_CLEANUP_LEDGER_PATH"]
        environment_identity = derive_environment_identity(environment)
        reservation_path = _reservation_path(environment_identity)
        if not allow_reuse:
            if state_path.exists() or state_path.is_symlink():
                errors.append("PILLARMESH_STATE_PATH")
            if output_dir.exists() or output_dir.is_symlink():
                errors.append("PILLARMESH_OUTPUT_DIR")
            if ledger_path.exists() or ledger_path.is_symlink():
                errors.append("PILLARMESH_CLEANUP_LEDGER_PATH")
        if errors:
            raise HarnessError("invalid or reused private paths: " + ", ".join(sorted(set(errors))))

        postgres_runtime = environment["PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL"].casefold()
        postgres_fixture = environment["PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL"].casefold()
        postgres_owner = environment["PILLARMESH_POSTGRES_OWNER_PRINCIPAL"].casefold()
        if postgres_fixture == postgres_owner:
            raise HarnessError("fixture principal is not isolated from declared owner")
        if postgres_runtime in {postgres_fixture, postgres_owner}:
            raise HarnessError("runtime principal is not isolated")
        if (
            environment["PILLARMESH_SNOWFLAKE_USER"].casefold()
            == environment["PILLARMESH_SNOWFLAKE_OWNER_USER"].casefold()
        ):
            raise HarnessError("runtime principal is not isolated")

        identifier_variables = (
            "PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL",
            "PILLARMESH_POSTGRES_OWNER_PRINCIPAL",
            "PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL",
            "PILLARMESH_POSTGRES_SCHEMA",
            "PILLARMESH_POSTGRES_TABLE",
            "PILLARMESH_POSTGRES_DENIAL_SCHEMA",
            "PILLARMESH_SNOWFLAKE_USER",
            "PILLARMESH_SNOWFLAKE_OWNER_USER",
            "PILLARMESH_SNOWFLAKE_ROLE",
            "PILLARMESH_SNOWFLAKE_WAREHOUSE",
            "PILLARMESH_SNOWFLAKE_DATABASE",
            "PILLARMESH_SNOWFLAKE_SCHEMA",
            "PILLARMESH_SNOWFLAKE_STAGE",
            "PILLARMESH_SNOWFLAKE_TARGET_TABLE",
            "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
            "PILLARMESH_SNOWFLAKE_LEDGER_TABLE",
            "PILLARMESH_SNOWFLAKE_DENIAL_DATABASE",
        )
        for name in identifier_variables:
            _identifier(environment[name], name)
        fixed_boundary = {
            "PILLARMESH_POSTGRES_SCHEMA": "pillarmesh_m0",
            "PILLARMESH_POSTGRES_TABLE": "orders",
            "PILLARMESH_SNOWFLAKE_ROLE": "PILLARMESH_M0_RUNTIME",
            "PILLARMESH_SNOWFLAKE_WAREHOUSE": "PILLARMESH_M0_WH",
            "PILLARMESH_SNOWFLAKE_DATABASE": "PILLARMESH_M0",
            "PILLARMESH_SNOWFLAKE_SCHEMA": "TRANSFER",
            "PILLARMESH_SNOWFLAKE_STAGE": "M0_STAGE",
            "PILLARMESH_SNOWFLAKE_TARGET_TABLE": "ORDERS",
            "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE": "ORDERS_UNSUPPORTED_KEY",
            "PILLARMESH_SNOWFLAKE_LEDGER_TABLE": "COMMIT_LEDGER",
        }
        if any(
            environment[name].casefold() != expected.casefold()
            for name, expected in fixed_boundary.items()
        ):
            raise HarnessError("provider object is outside the dedicated acceptance boundary")
        try:
            raw_canaries = json.loads(environment["PILLARMESH_CREDENTIAL_CANARIES_JSON"])
        except json.JSONDecodeError:
            raise HarnessError("invalid variable: PILLARMESH_CREDENTIAL_CANARIES_JSON") from None
        if (
            not isinstance(raw_canaries, list)
            or not raw_canaries
            or not all(isinstance(item, str) and item for item in raw_canaries)
        ):
            raise HarnessError("invalid variable: PILLARMESH_CREDENTIAL_CANARIES_JSON")
        return cls(
            environment=dict(environment),
            repository_root=_absolute_lexical(repository_root),
            state_path=state_path,
            output_dir=output_dir,
            cleanup_ledger_path=ledger_path,
            reservation_path=reservation_path,
            environment_identity=environment_identity,
            credential_canaries=tuple(raw_canaries),
        )

    def child_environment(self) -> dict[str, str]:
        child = {name: os.environ[name] for name in PASSTHROUGH_VARIABLES if name in os.environ}
        child.update({name: self.environment[name] for name in PRODUCT_VARIABLES})
        return child
