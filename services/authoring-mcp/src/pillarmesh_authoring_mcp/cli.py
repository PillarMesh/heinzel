from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Never, Protocol, TextIO

from pillarmesh_evidence import PackageMetadata, ScanInput

from .app import build_application
from .settings import AppSettings


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "pillarmesh-m0: error: command arguments are invalid\n")


class CliApplication(Protocol):
    def create_draft(self, value: dict[str, object]) -> object: ...

    def get_draft(self, contract_id: str, version: int) -> object: ...

    def verify(self, contract_id: str, version: int) -> object: ...

    def get_activation_summary(self, summary_digest: str) -> object: ...

    def activate(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> object: ...

    def get_run(self, run_id: str) -> object: ...

    def get_trace(self, run_id: str) -> object: ...

    def export_evidence(
        self,
        run_id: str,
        package_dir: Path,
        metadata: PackageMetadata,
        scan_input: ScanInput,
    ) -> object: ...

    def verify_evidence(self, package_dir: Path, scan_input: ScanInput) -> object: ...


def parser() -> argparse.ArgumentParser:
    root = PrivateArgumentParser(prog="pillarmesh-m0")
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-draft")
    create.add_argument("file", type=Path)
    get = commands.add_parser("get-draft")
    get.add_argument("contract_id")
    get.add_argument("version", type=int)
    verify = commands.add_parser("verify")
    verify.add_argument("contract_id")
    verify.add_argument("version", type=int)
    summary = commands.add_parser("get-summary")
    summary.add_argument("summary_digest")
    activate = commands.add_parser("activate-stdin")
    activate.add_argument("contract_digest")
    activate.add_argument("summary_digest")
    run = commands.add_parser("get-run")
    run.add_argument("run_id")
    trace = commands.add_parser("get-trace")
    trace.add_argument("run_id")
    export = commands.add_parser("export-evidence")
    export.add_argument("run_id")
    export.add_argument("package_dir", type=Path)
    export.add_argument("metadata", type=Path)
    verify_evidence = commands.add_parser("verify-evidence")
    verify_evidence.add_argument("package_dir", type=Path)
    return root


def _read_acceptance_key(input_stream: TextIO) -> int:
    raw = input_stream.read()
    if re.fullmatch(r"[0-9]+\n", raw) is None:
        raise ValueError("acceptance key input is invalid")
    return int(raw[:-1])


def _scan_input(environ: Mapping[str, str]) -> ScanInput:
    raw = environ.get("PILLARMESH_SCAN_INPUT_JSON")
    if raw is None:
        return ScanInput()
    try:
        return ScanInput.model_validate_json(raw)
    except ValueError:
        raise ValueError("scan input is invalid") from None


def run_cli(
    argv: Sequence[str],
    application: CliApplication,
    output: TextIO,
    *,
    input_stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    arguments = parser().parse_args(argv)
    private_input = input_stream or sys.stdin
    process_environment = environ if environ is not None else os.environ
    if arguments.command == "create-draft":
        raw = json.loads(arguments.file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("contract input must be a JSON object")
        result = application.create_draft(raw)
    elif arguments.command == "get-draft":
        result = application.get_draft(arguments.contract_id, arguments.version)
    elif arguments.command == "verify":
        result = application.verify(arguments.contract_id, arguments.version)
    elif arguments.command == "get-summary":
        result = application.get_activation_summary(arguments.summary_digest)
    elif arguments.command == "activate-stdin":
        result = application.activate(
            arguments.contract_digest,
            arguments.summary_digest,
            _read_acceptance_key(private_input),
        )
    elif arguments.command == "get-run":
        result = application.get_run(arguments.run_id)
    elif arguments.command == "get-trace":
        result = application.get_trace(arguments.run_id)
    elif arguments.command == "export-evidence":
        metadata = PackageMetadata.model_validate_json(arguments.metadata.read_bytes())
        result = application.export_evidence(
            arguments.run_id,
            arguments.package_dir,
            metadata,
            _scan_input(process_environment),
        )
    else:
        result = application.verify_evidence(
            arguments.package_dir, _scan_input(process_environment)
        )
    json.dump(result, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    output.write("\n")
    return 0


def main() -> None:
    arguments = sys.argv[1:]
    parser().parse_args(arguments)
    application = build_application(AppSettings())  # type: ignore[call-arg]
    raise SystemExit(
        run_cli(
            arguments,
            application,
            sys.stdout,
            input_stream=sys.stdin,
            environ=os.environ,
        )
    )
