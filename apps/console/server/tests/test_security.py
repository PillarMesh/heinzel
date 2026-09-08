from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from pillarmesh_console import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.contracts import ActorRole
from pillarmesh_console.fixture_backend import FixtureConsoleBackend
from pillarmesh_console.fixture_data import build_fixture_seed
from starlette.testclient import TestClient

_CSP = "default-src 'self'; img-src 'self'; frame-src 'self'"


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
    context: TrustedActorContext | None = None,
    *,
    backend: FixtureConsoleBackend | None = None,
    authenticated: bool = True,
    raise_server_exceptions: bool = True,
) -> Iterator[TestClient]:
    provider = (lambda _: context or _context()) if authenticated else (lambda _: None)
    with TestClient(
        create_app(
            backend=backend or FixtureConsoleBackend(),
            context_provider=provider,
            allowed_origin="http://testserver",
        ),
        raise_server_exceptions=raise_server_exceptions,
    ) as client:
        yield client


def _warehouse_payload() -> dict[str, object]:
    return {
        "expected_revision": 1,
        "reviewed_digest": build_fixture_seed().setup.setup_digest,
        "active_role": "data_architect",
        "engine": "postgresql",
        "region": "us-west-2",
        "capacity": "fixed-small",
    }


def test_session_issues_a_token_bound_to_the_trusted_session() -> None:
    with _client(_context(actor_id="actor-one")) as first_client:
        first = first_client.get("/api/v1/session")
    with _client(_context(actor_id="actor-two")) as second_client:
        second = second_client.get("/api/v1/session")

    first_token = first.json()["data"]["csrf_token"]
    second_token = second.json()["data"]["csrf_token"]
    assert first.status_code == second.status_code == 200
    assert len(first_token) >= 32
    assert first_token != second_token
    assert "actor-one" not in first_token


def test_session_reuses_the_token_for_the_same_trusted_context() -> None:
    with _client() as client:
        first_token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        second_token = client.get("/api/v1/session").json()["data"]["csrf_token"]

    assert second_token == first_token


def test_csrf_binding_cannot_be_reused_by_another_actor_with_the_same_session_id() -> None:
    first_context = TrustedActorContext(
        tenant_id="tenant-primary",
        actor_id="actor-one",
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-shared-canary",
    )
    second_context = TrustedActorContext(
        tenant_id="tenant-primary",
        actor_id="actor-two",
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-shared-canary",
    )
    current_context = [first_context]
    app = create_app(
        backend=FixtureConsoleBackend(),
        context_provider=lambda _: current_context[0],
        allowed_origin="http://testserver",
    )
    with TestClient(app) as client:
        first_token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        current_context[0] = second_context
        second_token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": first_token,
                "Idempotency-Key": "idem-cross-actor-csrf",
            },
            json=_warehouse_payload(),
        )

    assert first_token != second_token
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "csrf_invalid"


def test_same_origin_validation_does_not_trust_a_forged_host_header() -> None:
    with _client() as client:
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers={
                "Host": "attacker.invalid",
                "Origin": "http://attacker.invalid",
                "X-CSRF-Token": token,
                "Idempotency-Key": "idem-forged-host",
            },
            json=_warehouse_payload(),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "same_origin_required"


@pytest.mark.parametrize(
    ("header_changes", "expected_code"),
    (
        ({"X-CSRF-Token": None}, "csrf_required"),
        ({"X-CSRF-Token": "wrong-token-value"}, "csrf_invalid"),
        ({"Idempotency-Key": None}, "idempotency_key_required"),
        ({"Origin": None}, "same_origin_required"),
        ({"Origin": "https://attacker.invalid"}, "same_origin_required"),
    ),
)
def test_commands_require_bound_csrf_idempotency_and_same_origin(
    header_changes: dict[str, str | None], expected_code: str
) -> None:
    with _client() as client:
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        headers: dict[str, str] = {
            "Origin": "http://testserver",
            "X-CSRF-Token": token,
            "Idempotency-Key": "idem-security-0001",
        }
        for name, value in header_changes.items():
            if value is None:
                headers.pop(name, None)
            else:
                headers[name] = value

        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers=headers,
            json=_warehouse_payload(),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize(
    ("key_length", "expected_status"),
    ((7, 422), (8, 202), (256, 202), (257, 422)),
)
def test_idempotency_key_length_boundaries(key_length: int, expected_status: int) -> None:
    with _client() as client:
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": token,
                "Idempotency-Key": "k" * key_length,
            },
            json=_warehouse_payload(),
        )

    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["error"]["code"] == "idempotency_key_required"


@pytest.mark.parametrize("authority_field", ("tenant_id", "actor_id"))
def test_command_json_rejects_browser_supplied_authority(authority_field: str) -> None:
    with _client() as client:
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        response = client.post(
            "/api/v1/setup/warehouse-binding",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": token,
                "Idempotency-Key": "idem-authority-0001",
            },
            json={**_warehouse_payload(), authority_field: "browser-authority-canary"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["field"] == authority_field
    assert "browser-authority-canary" not in response.text


def test_wrong_role_and_wrong_tenant_have_the_same_non_enumerating_denial() -> None:
    with _client(_context(actor_id="requester", active_role="requester")) as role_client:
        wrong_role = role_client.get("/api/v1/reviews/review-meaning")
    with _client(_context(tenant_id="tenant-other")) as tenant_client:
        wrong_tenant = tenant_client.get("/api/v1/reviews/review-meaning")

    assert wrong_role.status_code == wrong_tenant.status_code == 404
    assert (
        wrong_role.json()["error"]
        == wrong_tenant.json()["error"]
        == {
            "code": "not_found",
            "safe_message": "The requested resource is unavailable.",
            "recovery_action": "none",
            "field": None,
        }
    )
    assert "review-meaning" not in wrong_role.text
    assert "tenant-other" not in wrong_tenant.text


def test_unauthenticated_read_returns_typed_401() -> None:
    with _client(authenticated=False) as client:
        response = client.get("/api/v1/workspace")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("path", ("/healthz", "/api/v1/session", "/missing"))
def test_every_response_has_the_exact_minimum_content_security_policy(path: str) -> None:
    with _client() as client:
        response = client.get(path)

    assert response.headers["content-security-policy"] == _CSP
    assert response.headers["x-content-type-options"] == "nosniff"


def test_raw_exceptions_request_canaries_and_private_values_never_cross_the_boundary() -> None:
    class ExplodingBackend(FixtureConsoleBackend):
        def get_workspace(self, context: TrustedActorContext):  # type: ignore[no-untyped-def]
            raise RuntimeError(
                "CANARY-SECRET provider-operation-private-123 tenant-primary actor-architect"
            )

    with _client(
        backend=ExplodingBackend(),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/api/v1/workspace",
            headers={"X-Correlation-ID": "CANARY-CLIENT-CORRELATION"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["meta"]["correlation_id"].startswith("correlation-")
    for canary in (
        "CANARY-SECRET",
        "provider-operation-private-123",
        "tenant-primary",
        "actor-architect",
        "CANARY-CLIENT-CORRELATION",
        "RuntimeError",
        "Traceback",
    ):
        assert canary not in response.text
