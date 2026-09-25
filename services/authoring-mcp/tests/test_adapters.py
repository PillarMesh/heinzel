import asyncio
import json
from io import StringIO
from pathlib import Path
from typing import Any

from heinzel_authoring_mcp.cli import run_cli
from heinzel_authoring_mcp.mcp_server import build_server
from heinzel_authoring_mcp.settings import AppSettings
from heinzel_evidence import PackageMetadata, ScanInput
from pydantic import SecretStr


class FakeApplication:
    """Stand in for the whole `CliApplication` protocol, not the part these tests use.

    `run_cli` dispatches every subcommand through this one object, so a double that
    implements only the reached branches is more permissive than the component it
    replaces. The evidence operations these tests never drive therefore refuse to
    answer rather than being omitted.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def create_draft(self, value: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create_draft", (value,)))
        return {"contract_id": value["contract_id"], "version": 1}

    def get_draft(self, contract_id: str, version: int) -> dict[str, object]:
        self.calls.append(("get_draft", (contract_id, version)))
        return {"contract_id": contract_id, "version": version}

    def verify(self, contract_id: str, version: int) -> dict[str, object]:
        self.calls.append(("verify", (contract_id, version)))
        return {"result": "no_valid_plan"}

    def get_activation_summary(self, summary_digest: str) -> dict[str, object]:
        self.calls.append(("get_activation_summary", (summary_digest,)))
        return {"summary_digest": summary_digest}

    def activate(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> dict[str, object]:
        self.calls.append(("activate", (contract_digest, summary_digest, acceptance_key)))
        return {"run_id": "run-1", "state": "succeeded"}

    def get_run(self, run_id: str) -> dict[str, object]:
        self.calls.append(("get_run", (run_id,)))
        return {"run_id": run_id, "state": "succeeded"}

    def get_trace(self, run_id: str) -> list[dict[str, object]]:
        self.calls.append(("get_trace", (run_id,)))
        return [{"run_id": run_id, "event_type": "terminal_success"}]

    def export_evidence(
        self,
        run_id: str,
        package_dir: Path,
        metadata: PackageMetadata,
        scan_input: ScanInput,
    ) -> dict[str, object]:
        raise AssertionError("export_evidence is not exercised by these tests")

    def verify_evidence(self, package_dir: Path, scan_input: ScanInput) -> dict[str, object]:
        raise AssertionError("verify_evidence is not exercised by these tests")


def test_cli_delegates_structured_operations_and_emits_json(tmp_path: Path) -> None:
    application = FakeApplication()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text('{"contract_id":"contract-001"}', encoding="utf-8")
    output = StringIO()

    code = run_cli(["create-draft", str(contract_path)], application, output)

    assert code == 0
    assert json.loads(output.getvalue()) == {"contract_id": "contract-001", "version": 1}
    assert application.calls == [("create_draft", ({"contract_id": "contract-001"},))]


def test_mcp_tools_delegate_without_conversation_state() -> None:
    application = FakeApplication()
    server = build_server(application)

    result = asyncio.run(
        server.call_tool(
            "activate_contract",
            {"contract_digest": "a" * 64, "summary_digest": "b" * 64, "acceptance_key": 7},
        )
    )

    assert application.calls == [("activate", ("a" * 64, "b" * 64, 7))]
    assert "run-1" in str(result)


def test_settings_repr_and_validation_never_expose_credentials(tmp_path: Path) -> None:
    settings = AppSettings(
        state_path=tmp_path / "state.db",
        output_dir=tmp_path / "output",
        signing_key_id="key-1",
        signing_private_key_b64=SecretStr("private-canary"),
        postgres_dsn=SecretStr("postgres-canary"),
        postgres_connection_handle="pg-snapshot",
        postgres_schema="snapshot_source",
        postgres_table="orders",
        snowflake_account="account",
        snowflake_user="user",
        snowflake_password=SecretStr("snowflake-canary"),
        snowflake_role="ROLE",
        snowflake_warehouse="WAREHOUSE",
        snowflake_database="DATABASE",
        snowflake_schema="SCHEMA",
        snowflake_stage="STAGE",
        snowflake_target_table="ORDERS",
        snowflake_ledger_table="COMMIT_LEDGER",
        snowflake_connection_handle="sf-snapshot",
    )

    rendered = repr(settings)

    assert "private-canary" not in rendered
    assert "postgres-canary" not in rendered
    assert "snowflake-canary" not in rendered
