from __future__ import annotations

from pillarmesh_console import create_app
from starlette.testclient import TestClient


def test_create_app_exposes_loopback_health_without_product_state() -> None:
    response = TestClient(create_app()).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_does_not_expose_product_routes() -> None:
    response = TestClient(create_app()).get("/")

    assert response.status_code == 404
