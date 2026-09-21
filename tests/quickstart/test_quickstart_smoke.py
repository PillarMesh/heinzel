"""Prove the published quickstart serves the demonstration it promises.

The contract tests beside this file read `deploy/quickstart`; these start it. They
build the image, drive the seeded request through a real command, and check what
survives a restart and what a reset discards.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.live, pytest.mark.emulator]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/quickstart/compose.yaml"
BASE_URL = "http://127.0.0.1:8000"
# The demonstration's architect. An unknown or absent actor resolves to the architect
# too, so naming the real one is what keeps this test honest about the header.
ARCHITECT = "architect-demo"

# The console's bootstrap and the client it reads through. Both are read at run time
# rather than summarised here, so that a change to either is picked up instead of
# leaving this test asserting a list that has moved on.
APP_ENTRY = ROOT / "apps/console/web/src/app.tsx"
API_CLIENT = ROOT / "apps/console/web/src/api/client.ts"

_RUN = os.environ.get("HEINZEL_RUN_QUICKSTART") == "1"

requires_quickstart = pytest.mark.skipif(
    not _RUN or shutil.which("docker") is None,
    reason="set HEINZEL_RUN_QUICKSTART=1 and provide docker to run the quickstart",
)


def _compose(*arguments: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *arguments], check=True)


def _wait_for_health(timeout_seconds: int = 300) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=5) as response:
                if response.status == 200:
                    return
        # A published port answers before the server does, so a refused connection, a
        # reset one and a truncated response are all states to keep waiting through.
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(3)
    raise AssertionError("the quickstart did not become healthy")


def _read_json(request: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200, response.status
        payload: dict[str, Any] = json.loads(response.read())
    return payload


def _inbox_items() -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/inbox", headers={"x-heinzel-actor": ARCHITECT}
    )
    items = _read_json(request)["data"]["items"]
    assert isinstance(items, list)
    return items


def _csrf_token() -> str:
    token = _read_json(urllib.request.Request(f"{BASE_URL}/api/v1/session"))["data"]["csrf_token"]
    assert isinstance(token, str)
    return token


def _record_clarification(request_id: str) -> str:
    """Advance the seeded request from `submitted` to `investigating`, and say so.

    A command carries the browser's origin, the session's CSRF token and an
    idempotency key; the origin must equal the allowed origin exactly or the server
    refuses it `same_origin_required`.
    """
    body = {
        "expected_revision": 1,
        "active_role": "data_architect",
        "restated_request": "Daily order count for the weekly operations review.",
        "in_scope_summary": "Completed orders per calendar day.",
        "out_of_scope_summary": "Cancelled orders and refunds.",
    }
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/inbox/{request_id}/clarification",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Idempotency-Key": uuid.uuid4().hex,
            "x-csrf-token": _csrf_token(),
            "x-heinzel-actor": ARCHITECT,
        },
    )
    state = _read_json(request)["data"]["state"]
    assert isinstance(state, str)
    return state


def _seeded_request_id() -> str:
    items = _inbox_items()
    assert len(items) == 1, items
    assert items[0]["state"] == "submitted", items[0]
    request_id = items[0]["request_id"]
    assert isinstance(request_id, str)
    return request_id


def _get(path: str, actor: str | None = None) -> tuple[int, bytes]:
    """Fetch `path` and report the status the browser would see, error or not."""
    headers = {} if actor is None else {"x-heinzel-actor": actor}
    request = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _bootstrap_reads() -> dict[str, str]:
    """The reads the console issues before it can render anything.

    Derived, not listed: every `client.getX()` named in `app.tsx` is a call the
    bootstrap makes, and `client.ts` holds the path each one requests. Moving a read
    into or out of the bootstrap changes what this returns, so the test follows the
    bootstrap rather than a copy of it that can go stale.
    """
    called = dict.fromkeys(re.findall(r"\.\s*(get[A-Z]\w*)\s*\(", APP_ENTRY.read_text()))
    assert called, "no console client reads found in app.tsx"
    client = API_CLIENT.read_text()
    reads: dict[str, str] = {}
    for name in called:
        located = re.search(
            rf"\b{name}\([^)]*\):[^{{]*\{{\s*return this\.#request\(\s*\"([^\"]+)\"",
            client,
        )
        assert located is not None, f"no request path for {name} in client.ts"
        reads[name] = located.group(1)
    return reads


def _tolerated_bootstrap_error_codes() -> frozenset[str]:
    """The error codes `app.tsx` continues past instead of failing the bootstrap.

    A bootstrap read that answers anything else takes the whole console down to the
    recovery boundary, whatever the rest of the demonstration serves. Read out of
    `app.tsx` for the same reason as the read list above.
    """
    return frozenset(re.findall(r"\.code\s*===\s*\"([^\"]+)\"", APP_ENTRY.read_text()))


@requires_quickstart
def test_the_quickstart_serves_a_console_the_browser_can_start() -> None:
    _compose("up", "--build", "-d")
    try:
        _wait_for_health()

        status, document = _get("/")
        assert status == 200, status
        markup = document.decode("utf-8")
        assets = re.findall(r"(?:src|href)=\"(/assets/[^\"]+)\"", markup)
        assert assets, markup
        for asset in assets:
            asset_status, _ = _get(asset)
            assert asset_status == 200, (asset, asset_status)

        tolerated = _tolerated_bootstrap_error_codes()
        for name, path in _bootstrap_reads().items():
            read_status, body = _get(path, actor=ARCHITECT)
            if read_status == 200:
                continue
            code = json.loads(body).get("error", {}).get("code")
            assert code in tolerated, (name, path, read_status, code)
    finally:
        _compose("down", "-v")


@requires_quickstart
def test_the_quickstart_keeps_recorded_work_across_a_restart() -> None:
    _compose("up", "--build", "-d")
    try:
        _wait_for_health()
        request_id = _seeded_request_id()
        assert _record_clarification(request_id) == "investigating"

        _compose("restart")
        _wait_for_health()

        # A restart that reseeded would show `submitted` again, or two requests.
        items = _inbox_items()
        assert len(items) == 1, items
        assert items[0]["request_id"] == request_id
        assert items[0]["state"] == "investigating"
    finally:
        _compose("down", "-v")


@requires_quickstart
def test_resetting_the_quickstart_discards_the_recorded_work() -> None:
    # The seeded request identifier is deterministic -- the same bytes after every
    # reset -- so comparing identifiers cannot tell a reset from a restart. Recorded
    # work can: the clarification below exists only in the volume being removed.
    _compose("up", "--build", "-d")
    try:
        _wait_for_health()
        assert _record_clarification(_seeded_request_id()) == "investigating"

        _compose("down", "-v")
        _compose("up", "-d")
        _wait_for_health()

        items = _inbox_items()
        assert len(items) == 1, items
        assert items[0]["state"] == "submitted"
    finally:
        _compose("down", "-v")
