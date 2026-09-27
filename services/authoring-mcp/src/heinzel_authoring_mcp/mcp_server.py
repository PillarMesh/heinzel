from __future__ import annotations

from typing import Any, Protocol, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .app import build_application
from .settings import AppSettings


def _assert_private_mcp_result(value: object, acceptance_key: int | None = None) -> None:
    if isinstance(value, dict):
        if "acceptance_key" in value:
            raise ToolError("private acceptance value in MCP result")
        for item in value.values():
            _assert_private_mcp_result(item, acceptance_key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_private_mcp_result(item, acceptance_key)
    elif acceptance_key is not None and (value == acceptance_key or value == str(acceptance_key)):
        raise ToolError("private acceptance value in MCP result")


class MCPApplication(Protocol):
    def create_draft(self, value: dict[str, object]) -> object: ...

    def get_draft(self, contract_id: str, version: int) -> object: ...

    def verify(self, contract_id: str, version: int) -> object: ...

    def get_activation_summary(self, summary_digest: str) -> object: ...

    def activate(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> object: ...

    def get_run(self, run_id: str) -> object: ...

    def get_trace(self, run_id: str) -> object: ...


def build_server(application: MCPApplication) -> MCPServer[object]:
    server: MCPServer[object] = MCPServer(
        "heinzel-authoring",
        instructions=(
            "Structured results are authoritative. Activation requires the exact verified "
            "contract and summary digests; prose cannot confer approval."
        ),
    )

    @server.tool(name="create_contract_draft", structured_output=True)
    def create_contract_draft(contract: dict[str, object]) -> dict[str, Any]:
        result = application.create_draft(contract)
        _assert_private_mcp_result(result)
        return cast(dict[str, Any], result)

    @server.tool(name="get_contract_draft", structured_output=True)
    def get_contract_draft(contract_id: str, version: int) -> dict[str, Any]:
        result = application.get_draft(contract_id, version)
        _assert_private_mcp_result(result)
        return cast(dict[str, Any], result)

    @server.tool(name="verify_contract", structured_output=True)
    def verify_contract(contract_id: str, version: int) -> dict[str, Any]:
        result = application.verify(contract_id, version)
        _assert_private_mcp_result(result)
        return cast(dict[str, Any], result)

    @server.tool(name="get_activation_summary", structured_output=True)
    def get_activation_summary(summary_digest: str) -> dict[str, Any]:
        result = application.get_activation_summary(summary_digest)
        _assert_private_mcp_result(result)
        return cast(dict[str, Any], result)

    @server.tool(name="activate_contract", structured_output=True)
    def activate_contract(
        contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> dict[str, Any]:
        result = application.activate(contract_digest, summary_digest, acceptance_key)
        _assert_private_mcp_result(result, acceptance_key)
        return cast(dict[str, Any], result)

    @server.tool(name="get_run", structured_output=True)
    def get_run(run_id: str) -> dict[str, Any]:
        result = application.get_run(run_id)
        _assert_private_mcp_result(result)
        return cast(dict[str, Any], result)

    @server.tool(name="get_trace", structured_output=True)
    def get_trace(run_id: str) -> list[dict[str, Any]]:
        result = application.get_trace(run_id)
        _assert_private_mcp_result(result)
        return cast(list[dict[str, Any]], result)

    @server.resource("heinzel://runs/{run_id}/trace", mime_type="application/json")
    def trace_resource(run_id: str) -> list[dict[str, Any]]:
        result = application.get_trace(run_id)
        _assert_private_mcp_result(result)
        return cast(list[dict[str, Any]], result)

    return server


def main() -> None:
    application = build_application(AppSettings())  # type: ignore[call-arg]
    build_server(application).run(transport="stdio")
