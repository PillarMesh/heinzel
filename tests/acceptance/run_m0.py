from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.acceptance.config import (
    PRODUCT_VARIABLES,
    REPOSITORY_ROOT,
    REQUIRED_VARIABLES,
    AcceptanceConfig,
    HarnessError,
)
from tests.acceptance.orchestration import (
    AcceptanceHarness,
    AcceptanceResult,
    ExpectedRunResources,
    RunEvidence,
    RunProgress,
    SQLiteRunInspector,
    StateCounters,
    build_contract,
    opaque_label,
)
from tests.acceptance.private_files import EnvironmentReservation, RunReservation, read_private_file
from tests.acceptance.provider_adapter import (
    DedicatedEnvironmentAttestation,
    LiveProviderActions,
    NegativeObservation,
    ProviderResourceState,
    ReplayObservation,
    RuntimeIdentities,
    VisibilityObservation,
    preflight,
    snowflake_access_denied,
    validate_attestation,
)
from tests.acceptance.resource_ledger import PrivateResourceLedger, cleanup_status
from tests.acceptance.subprocess_cli import CliTimeout, SubprocessCli

__all__ = (
    "PRODUCT_VARIABLES",
    "REQUIRED_VARIABLES",
    "AcceptanceConfig",
    "AcceptanceHarness",
    "AcceptanceResult",
    "CliTimeout",
    "DedicatedEnvironmentAttestation",
    "EnvironmentReservation",
    "ExpectedRunResources",
    "HarnessError",
    "NegativeObservation",
    "PrivateResourceLedger",
    "ProviderResourceState",
    "ReplayObservation",
    "RunEvidence",
    "RunProgress",
    "RunReservation",
    "RuntimeIdentities",
    "StateCounters",
    "SubprocessCli",
    "VisibilityObservation",
    "build_contract",
    "cleanup_status",
    "opaque_label",
    "preflight",
    "snowflake_access_denied",
)


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError(f"{context} returned an invalid response")
    return cast(Mapping[str, Any], value)


def _revision() -> tuple[str, str, str]:
    git_environment = {
        name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL") if name in os.environ
    }
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY_ROOT,
        env=git_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        umask=0o077,
    ).stdout.strip()
    lock_digest = hashlib.sha256((REPOSITORY_ROOT / "uv.lock").read_bytes()).hexdigest()
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return commit, lock_digest, version


def _run_existing_verification(config: AcceptanceConfig) -> Mapping[str, Any]:
    try:
        private = json.loads(read_private_file(config.cleanup_ledger_path))
    except json.JSONDecodeError:
        raise HarnessError("private cleanup ledger is invalid") from None
    context = private.get("context")
    if not isinstance(context, dict):
        raise HarnessError("private cleanup ledger is invalid")
    package_path = context.get("package_path")
    acceptance_key = context.get("acceptance_key")
    if not isinstance(package_path, str) or not isinstance(acceptance_key, int):
        raise HarnessError("private cleanup ledger has no completed package")
    scan_input = {
        "credential_canaries": config.credential_canaries,
        "row_value_canaries": (config.environment["PILLARMESH_ROW_VALUE_CANARY"],),
        "acceptance_keys": (acceptance_key,),
        "local_path_prefixes": (str(config.state_path.parent), str(config.output_dir)),
    }
    environment = config.child_environment()
    environment["PILLARMESH_SCAN_INPUT_JSON"] = json.dumps(
        scan_input, sort_keys=True, separators=(",", ":")
    )
    return _mapping(
        SubprocessCli().invoke(("verify-evidence", package_path), environment=environment),
        "verify-evidence",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_m0.py")
    parser.add_argument("command", choices=("preflight", "run", "verify", "cleanup-status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    command = _parser().parse_args(argv).command
    try:
        if command == "cleanup-status":
            ledger_value = os.environ.get("PILLARMESH_CLEANUP_LEDGER_PATH")
            if not ledger_value:
                raise HarnessError("missing required variables: PILLARMESH_CLEANUP_LEDGER_PATH")
            result: object = cleanup_status(
                Path(ledger_value).expanduser(), repository_root=REPOSITORY_ROOT
            )
        elif command == "verify":
            config = AcceptanceConfig.from_environment(
                os.environ, repository_root=REPOSITORY_ROOT, allow_reuse=True
            )
            result = _run_existing_verification(config)
        else:
            config = AcceptanceConfig.from_environment(os.environ, repository_root=REPOSITORY_ROOT)
            providers = LiveProviderActions(config)
            if command == "preflight":
                with EnvironmentReservation(config.reservation_path) as reservation:
                    reservation.assert_intact()
                    with providers.admission(config.environment_identity):
                        validate_attestation(config, providers.preflight())
                    reservation.assert_intact()
                result = {"status": "ready"}
            else:
                harness = AcceptanceHarness(
                    config,
                    providers,
                    SubprocessCli(),
                    SQLiteRunInspector(config.state_path, config.output_dir),
                    revision_factory=_revision,
                )
                accepted = harness.run()
                result = {
                    "run_id": accepted.run_id,
                    "package_index_digest": accepted.package_index_digest,
                    "verification_result_digest": accepted.verification_result_digest,
                }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except HarnessError as error:
        print(str(error), file=sys.stderr)
        return 2
    except BaseException as error:
        print(f"acceptance command failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
