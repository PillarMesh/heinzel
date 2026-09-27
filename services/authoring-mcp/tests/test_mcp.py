import asyncio
import json
from typing import Any

import pytest
from heinzel_authoring_mcp.mcp_server import build_server
from mcp.server.mcpserver.exceptions import ToolError


class PrivateApplication:
    """Stand in for the whole `MCPApplication` protocol, not the part these tests use.

    `build_server` registers a tool per protocol operation against this one object, so
    a double implementing only the tools these tests call is more permissive than the
    component it replaces: a signature the real application would reject stays
    invisible. The operations these tests never drive therefore refuse to answer
    rather than being omitted, which keeps an unexpected dispatch loud.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def create_draft(self, value: dict[str, object]) -> dict[str, object]:
        raise AssertionError("create_draft is not exercised by these tests")

    def get_draft(self, contract_id: str, version: int) -> dict[str, object]:
        raise AssertionError("get_draft is not exercised by these tests")

    def verify(self, contract_id: str, version: int) -> dict[str, object]:
        raise AssertionError("verify is not exercised by these tests")

    def get_activation_summary(self, summary_digest: str) -> dict[str, object]:
        raise AssertionError("get_activation_summary is not exercised by these tests")

    def activate(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> dict[str, object]:
        self.calls.append(("activate", (contract_digest, summary_digest, acceptance_key)))
        return {"run_id": "run-private", "state": "succeeded"}

    def get_run(self, run_id: str) -> dict[str, object]:
        return {"run_id": run_id, "state": "succeeded"}

    def get_trace(self, run_id: str) -> list[dict[str, object]]:
        return [{"run_id": run_id, "event_type": "terminal_success"}]


def test_mcp_activation_result_does_not_echo_private_acceptance_key() -> None:
    application = PrivateApplication()
    server = build_server(application)
    canary = 984201

    result = asyncio.run(
        server.call_tool(
            "activate_contract",
            {
                "contract_digest": "a" * 64,
                "summary_digest": "b" * 64,
                "acceptance_key": canary,
            },
        )
    )

    assert application.calls == [("activate", ("a" * 64, "b" * 64, canary))]
    assert str(canary) not in json.dumps(result.model_dump(mode="json"))


def test_mcp_activation_fails_closed_if_application_returns_private_key() -> None:
    application = PrivateApplication()
    application.activate = lambda *_args: {"acceptance_key": 984201}  # type: ignore[method-assign]
    server = build_server(application)

    with pytest.raises(ToolError, match="private acceptance value in MCP result") as caught:
        asyncio.run(
            server.call_tool(
                "activate_contract",
                {
                    "contract_digest": "a" * 64,
                    "summary_digest": "b" * 64,
                    "acceptance_key": 984201,
                },
            )
        )

    assert "984201" not in str(caught.value)


def test_mcp_run_and_trace_resource_do_not_return_private_acceptance_key() -> None:
    server = build_server(PrivateApplication())
    canary = "984201"

    run_result = asyncio.run(server.call_tool("get_run", {"run_id": "run-private"}))
    trace_result = asyncio.run(server.read_resource("heinzel://runs/run-private/trace"))

    assert canary not in json.dumps(run_result.model_dump(mode="json"))
    assert canary not in repr(trace_result)


def test_mcp_run_lookup_fails_closed_on_private_acceptance_field() -> None:
    application = PrivateApplication()
    application.get_run = lambda run_id: {"acceptance_key": 984201}  # type: ignore[method-assign]
    server = build_server(application)

    with pytest.raises(ToolError, match="private acceptance value in MCP result") as caught:
        asyncio.run(server.call_tool("get_run", {"run_id": "run-private"}))

    assert "984201" not in str(caught.value)
