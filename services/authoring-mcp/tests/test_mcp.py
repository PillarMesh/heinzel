import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pillarmesh_authoring_mcp.mcp_server import build_server


class PrivateApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

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
    server = build_server(application)  # type: ignore[arg-type]
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
    server = build_server(application)  # type: ignore[arg-type]

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
    server = build_server(PrivateApplication())  # type: ignore[arg-type]
    canary = "984201"

    run_result = asyncio.run(server.call_tool("get_run", {"run_id": "run-private"}))
    trace_result = asyncio.run(server.read_resource("pillarmesh://runs/run-private/trace"))

    assert canary not in json.dumps(run_result.model_dump(mode="json"))
    assert canary not in repr(trace_result)


def test_mcp_run_lookup_fails_closed_on_private_acceptance_field() -> None:
    application = PrivateApplication()
    application.get_run = lambda _run_id: {"acceptance_key": 984201}  # type: ignore[method-assign]
    server = build_server(application)  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="private acceptance value in MCP result") as caught:
        asyncio.run(server.call_tool("get_run", {"run_id": "run-private"}))

    assert "984201" not in str(caught.value)
