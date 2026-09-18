import json
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any

import heinzel_authoring_mcp.cli as cli_module
import pytest
from heinzel_authoring_mcp.cli import run_cli
from heinzel_evidence import PackageMetadata, ScanInput


class FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def activate(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> dict[str, object]:
        self.calls.append(("activate", (contract_digest, summary_digest, acceptance_key)))
        return {"run_id": "run-private", "state": "succeeded"}

    def export_evidence(
        self,
        run_id: str,
        package_dir: Path,
        metadata: PackageMetadata,
        scan_input: ScanInput,
    ) -> dict[str, object]:
        self.calls.append(("export_evidence", (run_id, package_dir, metadata, scan_input)))
        return {
            "package_index_digest": "a" * 64,
            "verification_result_digest": "b" * 64,
            "checks": [{"name": "package_structure", "outcome": "passed"}],
        }

    def verify_evidence(self, package_dir: Path, scan_input: ScanInput) -> dict[str, object]:
        self.calls.append(("verify_evidence", (package_dir, scan_input)))
        return {
            "package_index_digest": "a" * 64,
            "verification_result_digest": "b" * 64,
            "checks": [{"name": "package_structure", "outcome": "passed"}],
        }


def test_activate_stdin_delegates_integer_without_echoing_it() -> None:
    application = FakeApplication()
    output = StringIO()

    code = run_cli(
        ["activate-stdin", "a" * 64, "b" * 64],
        application,
        output,
        input_stream=StringIO("42\n"),
        environ={},
    )

    assert code == 0
    assert application.calls == [("activate", ("a" * 64, "b" * 64, 42))]
    assert "42" not in output.getvalue()
    assert json.loads(output.getvalue()) == {"run_id": "run-private", "state": "succeeded"}


@pytest.mark.parametrize("raw", ["", "word\n", "-1\n", "42 trailing\n", "42\n43\n"])
def test_activate_stdin_rejects_invalid_private_input_without_echoing_it(raw: str) -> None:
    application = FakeApplication()
    output = StringIO()

    with pytest.raises(ValueError, match="acceptance key input is invalid") as caught:
        run_cli(
            ["activate-stdin", "a" * 64, "b" * 64],
            application,
            output,
            input_stream=StringIO(raw),
            environ={},
        )

    assert raw.strip() not in str(caught.value) or not raw.strip()
    assert application.calls == []
    assert raw.strip() not in output.getvalue() or not raw.strip()


def test_raw_acceptance_key_is_not_a_positional_cli_argument() -> None:
    application = FakeApplication()
    canary = "984201"
    output = StringIO()
    error = StringIO()

    with redirect_stderr(error), pytest.raises(SystemExit) as caught:
        run_cli(
            ["activate-stdin", "a" * 64, "b" * 64, canary],
            application,
            output,
            input_stream=StringIO(),
            environ={},
        )

    assert caught.value.code == 2
    assert application.calls == []
    assert canary not in output.getvalue()
    assert canary not in error.getvalue()
    assert error.getvalue() == "heinzel-authoring: error: command arguments are invalid\n"


def test_unknown_cli_argument_is_rejected_without_echoing_its_value() -> None:
    application = FakeApplication()
    canary = "--credential-argv-canary-984201"
    output = StringIO()
    error = StringIO()

    with redirect_stderr(error), pytest.raises(SystemExit) as caught:
        run_cli(
            ["get-run", "run-private", canary],
            application,
            output,
            input_stream=StringIO(),
            environ={},
        )

    assert caught.value.code == 2
    assert application.calls == []
    assert canary not in output.getvalue()
    assert canary not in error.getvalue()
    assert error.getvalue() == "heinzel-authoring: error: command arguments are invalid\n"


def test_entrypoint_rejects_invalid_argv_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "984201"
    error = StringIO()
    monkeypatch.setattr(
        cli_module,
        "AppSettings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must not be loaded")),
    )
    monkeypatch.setattr(
        cli_module.sys,
        "argv",
        ["heinzel-authoring", "activate-stdin", "a" * 64, "b" * 64, canary],
    )

    with redirect_stderr(error), pytest.raises(SystemExit) as caught:
        cli_module.main()

    assert caught.value.code == 2
    assert canary not in error.getvalue()
    assert error.getvalue() == "heinzel-authoring: error: command arguments are invalid\n"


def test_export_evidence_reads_non_secret_metadata_path_and_canaries_from_environment(
    tmp_path: Path,
) -> None:
    application = FakeApplication()
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "commit_sha": "c" * 40,
                "uv_lock_digest": "d" * 64,
                "python_version": "3.13.7",
                "mcp_protocol_version": "2025-06-18",
                "operator_pseudonym": "operator-one",
                "host_pseudonym": "host-one",
                "transport_decision": "cli-fallback",
            }
        ),
        encoding="utf-8",
    )
    package_dir = tmp_path / "package"
    output = StringIO()
    canary = "credential-canary-never-print"

    code = run_cli(
        ["export-evidence", "run-1", str(package_dir), str(metadata_path)],
        application,
        output,
        input_stream=StringIO(),
        environ={"HEINZEL_SCAN_INPUT_JSON": json.dumps({"credential_canaries": [canary]})},
    )

    assert code == 0
    assert application.calls[0][0] == "export_evidence"
    assert application.calls[0][1][3] == ScanInput(credential_canaries=(canary,))
    assert set(json.loads(output.getvalue())) == {
        "checks",
        "package_index_digest",
        "verification_result_digest",
    }
    assert canary not in output.getvalue()


def test_verify_evidence_passes_environment_canaries_without_echoing_them(tmp_path: Path) -> None:
    application = FakeApplication()
    output = StringIO()
    canary = "row-canary-never-print"

    code = run_cli(
        ["verify-evidence", str(tmp_path / "package")],
        application,
        output,
        input_stream=StringIO(),
        environ={"HEINZEL_SCAN_INPUT_JSON": json.dumps({"row_value_canaries": [canary]})},
    )

    assert code == 0
    assert application.calls == [
        (
            "verify_evidence",
            (tmp_path / "package", ScanInput(row_value_canaries=(canary,))),
        )
    ]
    assert canary not in output.getvalue()
