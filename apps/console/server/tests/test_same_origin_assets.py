"""The built application and its API must be served from one origin.

A production-shaped local build is the only thing that proves the browser bundle
and the API agree on origin, which is what the same-origin and CSRF checks in the
command routes depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from heinzel_console import create_app
from starlette.testclient import TestClient


def _build_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index.js").write_text("export default null\n", encoding="utf-8")
    return dist


def test_api_routes_win_over_the_static_mount(tmp_path: Path) -> None:
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        session = client.get("/api/v1/session")

        assert session.status_code == 200
        assert session.json()["meta"]["data_provenance"] == "demo_fixture"


def test_the_built_application_is_served_from_the_same_origin(tmp_path: Path) -> None:
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        page = client.get("/")
        asset = client.get("/assets/index.js")

        assert page.status_code == 200
        assert '<div id="root">' in page.text
        assert asset.status_code == 200


def test_an_unknown_browser_route_falls_back_to_the_application_shell(tmp_path: Path) -> None:
    """Client-side routes must not 404 when the server has no matching path."""
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        deep_route = client.get("/inbox")

        assert deep_route.status_code == 200
        assert '<div id="root">' in deep_route.text


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_unknown_api_path_is_a_json_not_found_rather_than_the_application_shell(
    tmp_path: Path, method: str
) -> None:
    """An API client must never read the browser shell as a successful response."""
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        response = client.request(method, "/api/v1/does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == "not_found"
        assert '<div id="root">' not in response.text


def test_the_bare_api_prefix_is_not_the_application_shell(tmp_path: Path) -> None:
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        response = client.get("/api")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_a_known_api_path_asked_with_the_wrong_method_names_the_allowed_methods(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(dist_directory=_build_dist(tmp_path))) as client:
        response = client.delete("/api/v1/requests/mine")

        assert response.status_code == 405
        assert set(response.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}


def test_no_static_mount_exists_without_a_configured_build(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        page = client.get("/")

        assert page.status_code == 404


def test_a_missing_build_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dist directory"):
        create_app(dist_directory=tmp_path / "absent")
