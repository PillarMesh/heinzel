from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock

import pytest
from pillarmesh_console import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.backend import AuthorizedLink, PreviewContent
from pillarmesh_console.contracts import (
    ActorRole,
    OperationState,
    OperationView,
    WarehouseBindingCommand,
)
from pillarmesh_console.fixture_backend import FixtureConsoleBackend
from pillarmesh_console.fixture_data import build_fixture_seed
from starlette.testclient import TestClient


def _seed_conversation_digest(request_id: str = "request-answer") -> str:
    """The digest the projection actually returns for a seeded thread.

    Binding to the read is the point: a constant would let the command and the
    projection drift apart, which is what the backend used to paper over.
    """
    return build_fixture_seed().request_details[request_id].conversation.conversation_digest


def _context(
    *,
    actor_id: str = "actor-architect",
    tenant_id: str = "tenant-primary",
    active_role: ActorRole = "data_architect",
) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=(active_role,),
        active_role=active_role,
        session_id=f"session-{actor_id}",
    )


@contextmanager
def _client(
    *,
    backend: FixtureConsoleBackend | None = None,
    context: TrustedActorContext | None = None,
) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            backend=backend or FixtureConsoleBackend(),
            context_provider=lambda _: context or _context(),
            allowed_origin="http://testserver",
        )
    ) as client:
        yield client


def _command_headers(client: TestClient, key: str) -> dict[str, str]:
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    return {
        "Origin": "http://testserver",
        "X-CSRF-Token": token,
        "Idempotency-Key": key,
    }


def _warehouse_payload(engine: str = "postgresql") -> dict[str, object]:
    return {
        "expected_revision": 1,
        "reviewed_digest": build_fixture_seed().setup.setup_digest,
        "active_role": "data_architect",
        "engine": engine,
        "region": "us-west-2",
        "capacity": "fixed-small",
    }


def test_app_registers_every_reviewed_read_command_preview_and_link_route() -> None:
    app = create_app(backend=FixtureConsoleBackend(), context_provider=lambda _: _context())

    route_contract = {
        (path, method)
        for route in app.routes
        # Starlette types `routes` as `BaseRoute`, which carries neither attribute;
        # only the mounted `Route` instances contribute to the contract.
        for path in (getattr(route, "path", None),)
        if isinstance(path, str)
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }

    assert route_contract == {
        ("/healthz", "GET"),
        ("/api/v1/session", "GET"),
        ("/api/v1/workspace", "GET"),
        ("/api/v1/setup", "GET"),
        ("/api/v1/reviews/{review_id}", "GET"),
        ("/api/v1/inbox", "GET"),
        ("/api/v1/inbox/{request_id}", "GET"),
        ("/api/v1/requests/mine", "GET"),
        ("/api/v1/requests/{request_id}/conversation", "GET"),
        ("/api/v1/requests/{request_id}/clarified-outcome", "GET"),
        ("/api/v1/data-products/{data_product_id}", "GET"),
        ("/api/v1/runs", "GET"),
        ("/api/v1/acquisition-receipts", "GET"),
        ("/api/v1/catalog/{asset_ref}", "GET"),
        ("/api/v1/dashboards/{dashboard_ref}", "GET"),
        ("/api/v1/evidence/{evidence_ref}", "GET"),
        ("/api/v1/operations/{operation_id}", "GET"),
        ("/api/v1/previews/{preview_ref}", "GET"),
        ("/api/v1/links/{link_ref}", "GET"),
        ("/api/v1/setup/warehouse-binding", "POST"),
        ("/api/v1/setup/process-packages", "POST"),
        ("/api/v1/reviews/{review_id}/decisions", "POST"),
        ("/api/v1/inbox/{request_id}/decisions", "POST"),
        ("/api/v1/inbox/{request_id}/admission", "POST"),
        ("/api/v1/inbox/{request_id}/clarification", "POST"),
        ("/api/v1/inbox/{request_id}/proposal", "POST"),
        ("/api/v1/inbox/{request_id}/proposal/submission", "POST"),
        ("/api/v1/requests", "POST"),
        ("/api/v1/requests/{request_id}/conversation", "POST"),
        ("/api/v1/requests/{request_id}/clarified-outcome/acceptance", "POST"),
        ("/api/v1/operations/{operation_id}/retry", "POST"),
        ("/api/v1/demo/reset", "POST"),
    }


@pytest.mark.parametrize(
    ("path", "expected_key", "expected_value"),
    (
        ("/api/v1/workspace", "state", "setup"),
        ("/api/v1/setup", "active_stage", "foundation"),
        ("/api/v1/reviews/review-meaning", "kind", "meaning"),
        ("/api/v1/inbox", "selected_request_id", "request-answer"),
        ("/api/v1/inbox/request-answer", "kind", "stakeholder_question"),
        ("/api/v1/requests/request-answer/conversation", "revision", 2),
        ("/api/v1/data-products/product-revenue", "version", 1),
        ("/api/v1/runs", "runs", None),
        ("/api/v1/acquisition-receipts", "receipts", None),
        ("/api/v1/catalog/asset-revenue", "asset_ref", "asset-revenue"),
        ("/api/v1/dashboards/dashboard-revenue", "state", "not_delivered"),
    ),
)
def test_architect_read_routes_return_typed_fixture_envelopes(
    path: str, expected_key: str, expected_value: object
) -> None:
    with _client() as client:
        response = client.get(path)

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["data_provenance"] == "demo_fixture"
    assert payload["meta"]["correlation_id"].startswith("correlation-")
    if expected_value is None:
        assert isinstance(payload["data"][expected_key], list)
    else:
        assert payload["data"][expected_key] == expected_value


def test_requester_reads_only_their_projection_and_clarified_outcome() -> None:
    context = _context(actor_id="actor-requester", active_role="requester")
    with _client(context=context) as client:
        mine = client.get("/api/v1/requests/mine")
        outcome = client.get("/api/v1/requests/request-blocked-acceptance/clarified-outcome")

    assert mine.status_code == 200
    assert outcome.status_code == 200
    serialized = mine.text
    assert "candidate" not in serialized
    assert "effective_scope" not in serialized
    assert outcome.json()["data"]["accepted"] is False


def test_long_running_command_returns_202_and_operation_poll_uses_opaque_handle() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=_command_headers(client, "idem-warehouse-0001"),
            json=_warehouse_payload(),
        )
        operation_id = response.json()["data"]["operation_id"]
        polled = client.get(f"/api/v1/operations/{operation_id}")

    assert response.status_code == 202
    assert operation_id == "operation-0001"
    assert response.json()["data"]["state"] == "accepted"
    assert polled.status_code == 200
    assert polled.json()["data"] == response.json()["data"]
    assert "private" not in response.text.lower()
    assert "provider" not in response.text.lower()


@pytest.mark.parametrize(
    ("state", "expected_status"),
    (
        ("accepted", 202),
        ("running", 202),
        ("outcome_unknown", 202),
        ("succeeded", 200),
        ("failed", 200),
    ),
)
def test_operation_command_status_follows_the_authoritative_state(
    state: OperationState, expected_status: int
) -> None:
    class OperationStateBackend(FixtureConsoleBackend):
        def confirm_warehouse_binding(
            self,
            context: TrustedActorContext,
            command: WarehouseBindingCommand,
        ) -> OperationView:
            return OperationView(
                operation_id="operation-status",
                revision=1,
                state=state,
                phase="status_test",
                summary="Authoritative fixture operation state.",
            )

    with _client(backend=OperationStateBackend()) as client:
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=_command_headers(client, f"idem-status-{state}"),
            json=_warehouse_payload(),
        )

    assert response.status_code == expected_status
    assert response.json()["data"]["state"] == state


def test_adapter_does_not_turn_an_idempotency_key_into_replay_authority() -> None:
    with _client() as client:
        headers = _command_headers(client, "idem-reused-key-0001")
        first = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=headers,
            json=_warehouse_payload("postgresql"),
        )
        changed = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=headers,
            json=_warehouse_payload("clickhouse"),
        )

    assert first.status_code == 202
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "immutable_warehouse_binding"


@pytest.mark.parametrize(
    (
        "path",
        "owner",
        "attacker",
        "owner_payload",
        "attacker_payload",
        "mismatched_payload",
        "protected_canary",
    ),
    (
        (
            "/api/v1/reviews/review-data-product/decisions",
            _context(actor_id="actor-owner", active_role="data_owner"),
            _context(actor_id="actor-architect", active_role="data_architect"),
            {
                "expected_revision": 1,
                "reviewed_digest": "2" * 64,
                "active_role": "data_owner",
                "decision": "approve",
            },
            {
                "expected_revision": 1,
                "reviewed_digest": "2" * 64,
                "active_role": "data_architect",
                "decision": "approve",
            },
            {
                "expected_revision": 1,
                "reviewed_digest": "2" * 64,
                "active_role": "data_architect",
                "decision": "reject",
            },
            "Data product approval",
        ),
        (
            "/api/v1/requests/request-blocked-acceptance/conversation",
            _context(actor_id="actor-requester", active_role="requester"),
            _context(actor_id="actor-other-requester", active_role="requester"),
            {
                "expected_revision": 2,
                "conversation_digest": _seed_conversation_digest("request-blocked-acceptance"),
                "active_role": "requester",
                "body": "Owner replay projection canary.",
            },
            {
                "expected_revision": 2,
                "conversation_digest": _seed_conversation_digest("request-blocked-acceptance"),
                "active_role": "requester",
                "body": "Owner replay projection canary.",
            },
            {
                "expected_revision": 2,
                "conversation_digest": _seed_conversation_digest("request-blocked-acceptance"),
                "active_role": "requester",
                "body": "Mismatched attacker body.",
            },
            "Owner replay projection canary.",
        ),
        (
            "/api/v1/requests/request-blocked-acceptance/clarified-outcome/acceptance",
            _context(actor_id="actor-requester", active_role="requester"),
            _context(actor_id="actor-other-requester", active_role="requester"),
            {
                "expected_revision": 2,
                "clarified_outcome_digest": "b" * 64,
                "active_role": "requester",
                "decision": "approve",
            },
            {
                "expected_revision": 2,
                "clarified_outcome_digest": "b" * 64,
                "active_role": "requester",
                "decision": "approve",
            },
            {
                "expected_revision": 2,
                "clarified_outcome_digest": "b" * 64,
                "active_role": "requester",
                "decision": "request_changes",
            },
            "Explain the weekly net revenue movement.",
        ),
    ),
    ids=("review-authority", "conversation-owner", "acceptance-owner"),
)
def test_protected_command_replay_authorizes_resource_before_lookup(
    path: str,
    owner: TrustedActorContext,
    attacker: TrustedActorContext,
    owner_payload: dict[str, object],
    attacker_payload: dict[str, object],
    mismatched_payload: dict[str, object],
    protected_canary: str,
) -> None:
    backend = FixtureConsoleBackend()
    selected_context = {"value": owner}
    app = create_app(
        backend=backend,
        context_provider=lambda _: selected_context["value"],
        allowed_origin="http://testserver",
    )
    with TestClient(app) as client:
        owner_headers = _command_headers(client, "idem-protected-replay")
        committed = client.post(path, headers=owner_headers, json=owner_payload)
        authorized_replay = client.post(path, headers=owner_headers, json=owner_payload)

        selected_context["value"] = attacker
        reused_headers = _command_headers(client, "idem-protected-replay")
        denials = (
            client.post(path, headers=reused_headers, json=attacker_payload),
            client.post(
                path,
                headers=_command_headers(client, "idem-protected-new"),
                json=attacker_payload,
            ),
            client.post(path, headers=reused_headers, json=mismatched_payload),
        )

    assert committed.status_code == authorized_replay.status_code == 200
    assert authorized_replay.json()["data"] == committed.json()["data"]
    expected_error = {
        "code": "not_found",
        "safe_message": "The requested resource is unavailable.",
        "recovery_action": "none",
        "field": None,
    }
    for denial in denials:
        assert denial.status_code == 404
        assert "data" not in denial.json()
        assert denial.json()["error"] == expected_error
        assert protected_canary not in denial.text


def test_same_idempotency_key_serializes_in_flight_calls_without_caching_a_response() -> None:
    class ObservedBackend(FixtureConsoleBackend):
        def __init__(self) -> None:
            super().__init__()
            self.first_entered = Event()
            self.release_first = Event()
            self.second_entered = Event()
            self._observation_lock = Lock()
            self.calls = 0

        def confirm_warehouse_binding(
            self,
            context: TrustedActorContext,
            command: WarehouseBindingCommand,
        ) -> OperationView:
            with self._observation_lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_entered.set()
                assert self.release_first.wait(timeout=2)
            else:
                self.second_entered.set()
            return super().confirm_warehouse_binding(context, command)

    backend = ObservedBackend()
    app = create_app(
        backend=backend,
        context_provider=lambda _: _context(),
        allowed_origin="http://testserver",
    )
    with TestClient(app) as client:
        headers = _command_headers(client, "idem-concurrent-0001")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                client.post,
                "/api/v1/setup/warehouse-binding",
                headers=headers,
                json=_warehouse_payload(),
            )
            assert backend.first_entered.wait(timeout=2)
            second = executor.submit(
                client.post,
                "/api/v1/setup/warehouse-binding",
                headers=headers,
                json=_warehouse_payload(),
            )
            assert not backend.second_entered.wait(timeout=0.05)
            backend.release_first.set()
            responses = (first.result(timeout=2), second.result(timeout=2))

    assert backend.second_entered.is_set()
    assert backend.calls == 2
    assert [response.status_code for response in responses] == [202, 202]
    assert {response.json()["data"]["operation_id"] for response in responses} == {"operation-0001"}


def test_remaining_reviewed_commands_return_authoritative_typed_projections() -> None:
    backend = FixtureConsoleBackend()
    with _client(backend=backend) as client:
        warehouse = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=_command_headers(client, "idem-flow-warehouse"),
            json=_warehouse_payload(),
        )
        process_package = client.post(
            "/api/v1/setup/process-packages",
            headers=_command_headers(client, "idem-flow-package"),
            json={
                "expected_revision": 2,
                "package_digest": "f" * 64,
                "active_role": "data_architect",
                "file_name": "revenue-to-cash.pdf",
                "media_type": "application/pdf",
            },
        )
        review = client.post(
            "/api/v1/reviews/review-meaning/decisions",
            headers=_command_headers(client, "idem-flow-review"),
            json={
                "expected_revision": 1,
                "reviewed_digest": "1" * 64,
                "active_role": "data_architect",
                "decision": "approve",
            },
        )
        request = client.post(
            "/api/v1/inbox/request-answer/decisions",
            headers=_command_headers(client, "idem-flow-request"),
            json={
                "expected_revision": 2,
                "reviewed_digest": "d" * 64,
                "active_role": "data_architect",
                "decision": "approve",
            },
        )
        admission = client.post(
            "/api/v1/inbox/request-answer/admission",
            headers=_command_headers(client, "idem-flow-admission"),
            json={
                "expected_revision": 3,
                "reviewed_digest": "d" * 64,
                "active_role": "data_architect",
            },
        )
        conversation = client.post(
            "/api/v1/requests/request-answer/conversation",
            headers=_command_headers(client, "idem-flow-message"),
            json={
                "expected_revision": 2,
                "conversation_digest": _seed_conversation_digest(),
                "active_role": "data_architect",
                "body": "Please confirm the synthetic metric version.",
            },
        )
        setup_for_reset = client.get("/api/v1/setup").json()["data"]
        reset = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-flow-reset"),
            json={
                "expected_revision": setup_for_reset["revision"],
                "setup_digest": setup_for_reset["setup_digest"],
                "reset_token": setup_for_reset["reset_token"],
                "active_role": "data_architect",
            },
        )

    assert warehouse.status_code == process_package.status_code == 202
    assert process_package.json()["data"]["operation_id"] == "operation-0002"
    assert review.status_code == 200
    assert review.json()["data"]["decisions"][0]["decision"] == "approve"
    assert request.status_code == 200
    # Approving records one authority's approval. Admission is the separate
    # transaction that carries the proposal into execution.
    assert request.json()["data"]["state"] == "awaiting_approval"
    assert request.json()["data"]["admission"] == {"available": True, "blocking_reason": None}
    assert admission.status_code == 200
    assert admission.json()["data"]["state"] == "execution_ready"
    assert admission.json()["data"]["admission"] is None
    assert conversation.status_code == 200
    assert conversation.json()["data"]["revision"] == 3
    assert conversation.json()["data"]["messages"][-1]["body"].startswith("Please confirm")
    assert reset.status_code == 200
    assert reset.json()["data"]["active_stage"] == "foundation"
    assert reset.json()["data"]["warehouse_binding"] is None


def test_requester_commands_create_request_and_accept_clarified_outcome() -> None:
    requester = _context(actor_id="actor-requester", active_role="requester")
    with _client(context=requester) as client:
        created = client.post(
            "/api/v1/requests",
            headers=_command_headers(client, "idem-requester-create"),
            json={
                "expected_revision": 1,
                "request_digest": (
                    "0022a11a86c939c770a3d876ee58b1f450040edbc26b1b176c749255406def2d"
                ),
                "active_role": "requester",
                "title": "Explain synthetic revenue",
                "request": {
                    "kind": "stakeholder_question",
                    "purpose": "Prepare a synthetic board update.",
                    "question": "Why did synthetic revenue change?",
                },
            },
        )
        accepted = client.post(
            "/api/v1/requests/request-blocked-acceptance/clarified-outcome/acceptance",
            headers=_command_headers(client, "idem-requester-accept"),
            json={
                "expected_revision": 2,
                "clarified_outcome_digest": "b" * 64,
                "active_role": "requester",
                "decision": "approve",
            },
        )

    assert created.status_code == 200
    assert created.json()["data"]["request_id"] == "request-0001"
    assert created.json()["data"]["state"] == "submitted"
    assert accepted.status_code == 200
    assert accepted.json()["data"]["accepted"] is True


def test_retry_uses_a_fixture_failure_and_returns_new_accepted_operation() -> None:
    with _client() as client:
        operation = client.get("/api/v1/operations/operation-retryable").json()["data"]
        retry = client.post(
            "/api/v1/operations/operation-retryable/retry",
            headers=_command_headers(client, "idem-operation-retry"),
            json={
                "expected_revision": operation["revision"],
                "operation_digest": operation["operation_digest"],
                "retry_token": operation["retry_token"],
                "active_role": "data_architect",
            },
        )

    assert retry.status_code == 202
    assert retry.json()["data"]["operation_id"] == "operation-0001"
    assert retry.json()["data"]["state"] == "accepted"


def test_fixture_preview_and_authorized_link_use_opaque_references() -> None:
    with _client() as client:
        preview = client.get("/api/v1/previews/preview-dashboard-revenue")
        link = client.get(
            "/api/v1/links/link-catalog-revenue",
            follow_redirects=False,
        )

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert preview.headers["x-correlation-id"].startswith("correlation-")
    assert preview.headers["x-pillarmesh-data-provenance"] == "demo_fixture"
    assert link.status_code == 307
    assert link.headers["location"] == "/demo/catalog/revenue"
    assert link.headers["x-correlation-id"].startswith("correlation-")
    assert link.headers["x-pillarmesh-data-provenance"] == "demo_fixture"


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    (
        ("get", "/healthz", None),
        ("get", "/api/v1/workspace", None),
        ("get", "/api/v1/evidence/evidence-synthetic", None),
        ("post", "/api/v1/demo/reset", {}),
    ),
)
def test_every_json_response_has_generated_correlation_and_provenance_headers(
    method: str, path: str, json_body: object
) -> None:
    with _client() as client:
        headers = _command_headers(client, f"idem-json-header-{method}") if method == "post" else {}
        response = client.request(method, path, headers=headers, json=json_body)

    assert response.headers["x-correlation-id"].startswith("correlation-")
    assert response.headers["x-pillarmesh-data-provenance"] == "demo_fixture"


@pytest.mark.parametrize("kind", ("preview", "link"))
def test_unsafe_preview_and_link_backend_values_fail_closed(kind: str) -> None:
    class UnsafeBackend(FixtureConsoleBackend):
        def get_preview(self, context: TrustedActorContext, preview_ref: str) -> PreviewContent:
            return PreviewContent(body=b"<html>private canary</html>", media_type="text/html")

        def authorize_link(self, context: TrustedActorContext, link_ref: str) -> AuthorizedLink:
            return AuthorizedLink(location="https://attacker.invalid/private-canary")

    path = (
        "/api/v1/previews/preview-dashboard-revenue"
        if kind == "preview"
        else "/api/v1/links/link-catalog-revenue"
    )
    with _client(backend=UnsafeBackend()) as client:
        response = client.get(path, follow_redirects=False)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == f"unsafe_{kind}_response"
    assert "attacker.invalid" not in response.text
    assert "private canary" not in response.text


def test_backslash_network_path_cannot_escape_the_approved_link_origin() -> None:
    class BackslashLinkBackend(FixtureConsoleBackend):
        def authorize_link(self, context: TrustedActorContext, link_ref: str) -> AuthorizedLink:
            return AuthorizedLink(location="/\\attacker.invalid/private-canary")

    with _client(backend=BackslashLinkBackend()) as client:
        response = client.get(
            "/api/v1/links/link-catalog-revenue",
            follow_redirects=False,
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unsafe_link_response"
    assert "attacker.invalid" not in response.text


def test_success_route_revalidates_backend_data_against_the_exact_response_model() -> None:
    class PrivateWorkspaceBackend(FixtureConsoleBackend):
        def get_workspace(self, context: TrustedActorContext):  # type: ignore[no-untyped-def]
            return {
                "workspace": {"ref": "workspace-revenue", "display_name": "Revenue to cash"},
                "state": "setup",
                "capabilities": [],
                "recovery_message": None,
                "private_provider_id": "provider-private-canary",
            }

    app = create_app(
        backend=PrivateWorkspaceBackend(),
        context_provider=lambda _: _context(),
        allowed_origin="http://testserver",
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/workspace")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "provider-private-canary" not in response.text


def test_tuple_success_route_rejects_a_private_backend_field() -> None:
    class PrivateRequesterBackend(FixtureConsoleBackend):
        def get_requester_requests(self, context: TrustedActorContext):  # type: ignore[no-untyped-def]
            return (
                {
                    "request_id": "request-private",
                    "kind": "stakeholder_question",
                    "state": "submitted",
                    "title": "Private response",
                    "requested_outcome": "Must fail closed",
                    "revision": 1,
                    "updated_at": "2026-09-01T16:00:00Z",
                    "own_decisions": [],
                    "clarified_outcome": None,
                    "denial_explanation": None,
                    "owner_principal": "principal-private-canary",
                },
            )

    requester = _context(actor_id="actor-requester", active_role="requester")
    app = create_app(
        backend=PrivateRequesterBackend(),
        context_provider=lambda _: requester,
        allowed_origin="http://testserver",
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/requests/mine")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "principal-private-canary" not in response.text


@pytest.mark.parametrize("unknown_field", ("tenant_id", "actor_id", "unexpected"))
def test_reset_rejects_every_unknown_body_field(unknown_field: str) -> None:
    with _client() as client:
        setup = client.get("/api/v1/setup").json()["data"]
        response = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, f"idem-reset-{unknown_field}"),
            json={
                "expected_revision": setup["revision"],
                "setup_digest": setup["setup_digest"],
                "reset_token": setup["reset_token"],
                "active_role": "data_architect",
                unknown_field: "authority-canary",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["field"] == unknown_field
    assert "authority-canary" not in response.text


def test_reset_route_requires_exact_current_setup_authority() -> None:
    with _client() as client:
        setup = client.get("/api/v1/setup").json()["data"]
        valid = {
            "expected_revision": setup["revision"],
            "setup_digest": setup["setup_digest"],
            "reset_token": setup["reset_token"],
            "active_role": "data_architect",
        }
        omitted = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-omitted"),
            json={
                "setup_digest": setup["setup_digest"],
                "reset_token": setup["reset_token"],
                "active_role": "data_architect",
            },
        )
        stale = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-stale"),
            json=valid | {"expected_revision": setup["revision"] + 1},
        )
        wrong_digest = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-digest"),
            json=valid | {"setup_digest": "f" * 64},
        )
        after = client.get("/api/v1/setup").json()["data"]

    assert omitted.status_code == 422
    assert stale.status_code == wrong_digest.status_code == 409
    assert after == setup


def test_reset_route_replays_canonically_for_the_same_or_changed_idempotency_key() -> None:
    with _client() as client:
        setup = client.get("/api/v1/setup").json()["data"]
        command = {
            "expected_revision": setup["revision"],
            "setup_digest": setup["setup_digest"],
            "reset_token": setup["reset_token"],
            "active_role": "data_architect",
        }
        first = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-replay"),
            json=command,
        )
        same_key = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-replay"),
            json=command,
        )
        changed_key = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-replay-changed-key"),
            json=command,
        )

    assert first.status_code == same_key.status_code == changed_key.status_code == 200
    assert first.json()["data"] == same_key.json()["data"] == changed_key.json()["data"]


def test_reset_route_conflicts_on_consumed_token_mismatch_and_accepts_reloaded_token() -> None:
    with _client() as client:
        initial = client.get("/api/v1/setup").json()["data"]
        initial_command = {
            "expected_revision": initial["revision"],
            "setup_digest": initial["setup_digest"],
            "reset_token": initial["reset_token"],
            "active_role": "data_architect",
        }
        first = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-first"),
            json=initial_command,
        )
        mismatch = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-mismatch"),
            json=initial_command | {"setup_digest": "f" * 64},
        )
        reloaded = client.get("/api/v1/setup").json()["data"]
        second = client.post(
            "/api/v1/demo/reset",
            headers=_command_headers(client, "idem-reset-second"),
            json={
                "expected_revision": reloaded["revision"],
                "setup_digest": reloaded["setup_digest"],
                "reset_token": reloaded["reset_token"],
                "active_role": "data_architect",
            },
        )

    assert first.status_code == second.status_code == 200
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "command_identity_mismatch"
    assert reloaded == first.json()["data"]
    assert second.json()["data"]["reset_token"] != reloaded["reset_token"]


def test_fixture_evidence_route_reports_typed_unavailability() -> None:
    with _client() as client:
        response = client.get("/api/v1/evidence/evidence-synthetic")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "fixture_evidence_unavailable",
        "safe_message": "Authoritative evidence is unavailable in fixture mode.",
        "recovery_action": "none",
        "field": None,
    }


def test_demo_reset_is_absent_when_backend_mode_is_not_fixture() -> None:
    class GovernedModeBackend(FixtureConsoleBackend):
        fixture_mode = False

    app = create_app(
        backend=GovernedModeBackend(),
        context_provider=lambda _: _context(),
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/demo/reset")

    assert response.status_code == 404
