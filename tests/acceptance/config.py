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
    "HEINZEL_STATE_PATH",
    "HEINZEL_OUTPUT_DIR",
    "HEINZEL_CLEANUP_LEDGER_PATH",
    "HEINZEL_SIGNING_KEY_ID",
    "HEINZEL_SIGNING_PRIVATE_KEY_B64",
    "HEINZEL_POSTGRES_DSN",
    "HEINZEL_POSTGRES_DATABASE",
    "HEINZEL_POSTGRES_RUNTIME_PRINCIPAL",
    "HEINZEL_POSTGRES_OWNER_PRINCIPAL",
    "HEINZEL_POSTGRES_CONNECTION_HANDLE",
    "HEINZEL_POSTGRES_SCHEMA",
    "HEINZEL_POSTGRES_TABLE",
    "HEINZEL_POSTGRES_DENIAL_SCHEMA",
    "HEINZEL_POSTGRES_FIXTURE_DSN",
    "HEINZEL_POSTGRES_FIXTURE_PRINCIPAL",
    "HEINZEL_SNOWFLAKE_ACCOUNT",
    "HEINZEL_SNOWFLAKE_USER",
    "HEINZEL_SNOWFLAKE_PASSWORD",
    "HEINZEL_SNOWFLAKE_OWNER_USER",
    "HEINZEL_SNOWFLAKE_ROLE",
    "HEINZEL_SNOWFLAKE_WAREHOUSE",
    "HEINZEL_SNOWFLAKE_DATABASE",
    "HEINZEL_SNOWFLAKE_SCHEMA",
    "HEINZEL_SNOWFLAKE_STAGE",
    "HEINZEL_SNOWFLAKE_TARGET_TABLE",
    "HEINZEL_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
    "HEINZEL_SNOWFLAKE_LEDGER_TABLE",
    "HEINZEL_SNOWFLAKE_CONNECTION_HANDLE",
    "HEINZEL_SNOWFLAKE_DENIAL_DATABASE",
    "HEINZEL_CREDENTIAL_CANARIES_JSON",
    "HEINZEL_ROW_VALUE_CANARY",
    "HEINZEL_OPERATOR_PSEUDONYM",
    "HEINZEL_HOST_PSEUDONYM",
    "HEINZEL_MCP_PROTOCOL_VERSION",
    "HEINZEL_OWNER_AUTHORIZATION_REFERENCE",
)

PRODUCT_VARIABLES = (
    "HEINZEL_STATE_PATH",
    "HEINZEL_OUTPUT_DIR",
    "HEINZEL_SIGNING_KEY_ID",
    "HEINZEL_SIGNING_PRIVATE_KEY_B64",
    "HEINZEL_POSTGRES_DSN",
    "HEINZEL_POSTGRES_CONNECTION_HANDLE",
    "HEINZEL_POSTGRES_SCHEMA",
    "HEINZEL_POSTGRES_TABLE",
    "HEINZEL_SNOWFLAKE_ACCOUNT",
    "HEINZEL_SNOWFLAKE_USER",
    "HEINZEL_SNOWFLAKE_PASSWORD",
    "HEINZEL_SNOWFLAKE_ROLE",
    "HEINZEL_SNOWFLAKE_WAREHOUSE",
    "HEINZEL_SNOWFLAKE_DATABASE",
    "HEINZEL_SNOWFLAKE_SCHEMA",
    "HEINZEL_SNOWFLAKE_STAGE",
    "HEINZEL_SNOWFLAKE_TARGET_TABLE",
    "HEINZEL_SNOWFLAKE_LEDGER_TABLE",
    "HEINZEL_SNOWFLAKE_CONNECTION_HANDLE",
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
SNOWFLAKE_OWNER_ROLE = "HEINZEL_M0_OWNER"
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
        "postgres_database": environment["HEINZEL_POSTGRES_DATABASE"],
        "postgres_schema": environment["HEINZEL_POSTGRES_SCHEMA"],
        "postgres_table": environment["HEINZEL_POSTGRES_TABLE"],
        "snowflake_account": environment["HEINZEL_SNOWFLAKE_ACCOUNT"],
        "snowflake_database": environment["HEINZEL_SNOWFLAKE_DATABASE"],
        "snowflake_schema": environment["HEINZEL_SNOWFLAKE_SCHEMA"],
        "snowflake_stage": environment["HEINZEL_SNOWFLAKE_STAGE"],
        "snowflake_target": environment["HEINZEL_SNOWFLAKE_TARGET_TABLE"],
        "snowflake_negative_target": environment["HEINZEL_SNOWFLAKE_NEGATIVE_TARGET_TABLE"],
        "snowflake_ledger": environment["HEINZEL_SNOWFLAKE_LEDGER_TABLE"],
    }
    normalized = {name: value.casefold() for name, value in shared_boundary.items()}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"heinzel-environment-v1\0" + payload).hexdigest()


def _reservation_path(environment_identity: str) -> Path:
    parent = Path(tempfile.gettempdir()).resolve() / f"heinzel-reservations-{os.getuid()}"
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
            ("HEINZEL_STATE_PATH", Path(environment["HEINZEL_STATE_PATH"])),
            ("HEINZEL_OUTPUT_DIR", Path(environment["HEINZEL_OUTPUT_DIR"])),
            (
                "HEINZEL_CLEANUP_LEDGER_PATH",
                Path(environment["HEINZEL_CLEANUP_LEDGER_PATH"]),
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

        state_path = path_by_name["HEINZEL_STATE_PATH"]
        output_dir = path_by_name["HEINZEL_OUTPUT_DIR"]
        ledger_path = path_by_name["HEINZEL_CLEANUP_LEDGER_PATH"]
        environment_identity = derive_environment_identity(environment)
        reservation_path = _reservation_path(environment_identity)
        if not allow_reuse:
            if state_path.exists() or state_path.is_symlink():
                errors.append("HEINZEL_STATE_PATH")
            if output_dir.exists() or output_dir.is_symlink():
                errors.append("HEINZEL_OUTPUT_DIR")
            if ledger_path.exists() or ledger_path.is_symlink():
                errors.append("HEINZEL_CLEANUP_LEDGER_PATH")
        if errors:
            raise HarnessError("invalid or reused private paths: " + ", ".join(sorted(set(errors))))

        postgres_runtime = environment["HEINZEL_POSTGRES_RUNTIME_PRINCIPAL"].casefold()
        postgres_fixture = environment["HEINZEL_POSTGRES_FIXTURE_PRINCIPAL"].casefold()
        postgres_owner = environment["HEINZEL_POSTGRES_OWNER_PRINCIPAL"].casefold()
        if postgres_fixture == postgres_owner:
            raise HarnessError("fixture principal is not isolated from declared owner")
        if postgres_runtime in {postgres_fixture, postgres_owner}:
            raise HarnessError("runtime principal is not isolated")
        if (
            environment["HEINZEL_SNOWFLAKE_USER"].casefold()
            == environment["HEINZEL_SNOWFLAKE_OWNER_USER"].casefold()
        ):
            raise HarnessError("runtime principal is not isolated")

        identifier_variables = (
            "HEINZEL_POSTGRES_RUNTIME_PRINCIPAL",
            "HEINZEL_POSTGRES_OWNER_PRINCIPAL",
            "HEINZEL_POSTGRES_FIXTURE_PRINCIPAL",
            "HEINZEL_POSTGRES_SCHEMA",
            "HEINZEL_POSTGRES_TABLE",
            "HEINZEL_POSTGRES_DENIAL_SCHEMA",
            "HEINZEL_SNOWFLAKE_USER",
            "HEINZEL_SNOWFLAKE_OWNER_USER",
            "HEINZEL_SNOWFLAKE_ROLE",
            "HEINZEL_SNOWFLAKE_WAREHOUSE",
            "HEINZEL_SNOWFLAKE_DATABASE",
            "HEINZEL_SNOWFLAKE_SCHEMA",
            "HEINZEL_SNOWFLAKE_STAGE",
            "HEINZEL_SNOWFLAKE_TARGET_TABLE",
            "HEINZEL_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
            "HEINZEL_SNOWFLAKE_LEDGER_TABLE",
            "HEINZEL_SNOWFLAKE_DENIAL_DATABASE",
        )
        for name in identifier_variables:
            _identifier(environment[name], name)
        fixed_boundary = {
            "HEINZEL_POSTGRES_SCHEMA": "heinzel_m0",
            "HEINZEL_POSTGRES_TABLE": "orders",
            "HEINZEL_SNOWFLAKE_ROLE": "HEINZEL_M0_RUNTIME",
            "HEINZEL_SNOWFLAKE_WAREHOUSE": "HEINZEL_M0_WH",
            "HEINZEL_SNOWFLAKE_DATABASE": "HEINZEL_M0",
            "HEINZEL_SNOWFLAKE_SCHEMA": "TRANSFER",
            "HEINZEL_SNOWFLAKE_STAGE": "M0_STAGE",
            "HEINZEL_SNOWFLAKE_TARGET_TABLE": "ORDERS",
            "HEINZEL_SNOWFLAKE_NEGATIVE_TARGET_TABLE": "ORDERS_UNSUPPORTED_KEY",
            "HEINZEL_SNOWFLAKE_LEDGER_TABLE": "COMMIT_LEDGER",
        }
        if any(
            environment[name].casefold() != expected.casefold()
            for name, expected in fixed_boundary.items()
        ):
            raise HarnessError("provider object is outside the dedicated acceptance boundary")
        try:
            raw_canaries = json.loads(environment["HEINZEL_CREDENTIAL_CANARIES_JSON"])
        except json.JSONDecodeError:
            raise HarnessError("invalid variable: HEINZEL_CREDENTIAL_CANARIES_JSON") from None
        if (
            not isinstance(raw_canaries, list)
            or not raw_canaries
            or not all(isinstance(item, str) and item for item in raw_canaries)
        ):
            raise HarnessError("invalid variable: HEINZEL_CREDENTIAL_CANARIES_JSON")
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
