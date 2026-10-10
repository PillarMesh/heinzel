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
# The requester the demonstration seeds its question as, and so the only actor its answer
# belongs to. Named rather than defaulted, because an unknown actor resolves to the architect.
REQUESTER = "requester-demo"

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
    """The decoded envelope, or an assertion naming the error the console returned.

    The body matters on a failure: every refusal the console makes carries a code, and
    `urllib` raises `HTTPError` without it, which leaves a CI log saying `HTTP Error 422`
    and nothing about which rule was broken.
    """
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200, response.status
            payload: dict[str, Any] = json.loads(response.read())
    except urllib.error.HTTPError as refused:
        body = refused.read().decode("utf-8", errors="replace")
        raise AssertionError(
            f"{request.method} {request.full_url} -> {refused.code}: {body}"
        ) from refused
    return payload


def _inbox_items() -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/inbox", headers={"x-heinzel-actor": ARCHITECT}
    )
    items = _read_json(request)["data"]["items"]
    assert isinstance(items, list)
    return items


def _csrf_token(actor: str) -> str:
    """The CSRF token for this actor's session, which is the only one their commands pass.

    `CsrfTokenIssuer.matches` checks the token against the acting context, so a token read
    without the actor header is the architect's and is refused `csrf_invalid` -- a `422` -- on
    any command sent as the requester. Every caller names its actor for that reason.
    """
    token = _read_json(
        urllib.request.Request(f"{BASE_URL}/api/v1/session", headers={"x-heinzel-actor": actor})
    )["data"]["csrf_token"]
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
        "restated_request": "Daily order value for the weekly operations review.",
        "in_scope_summary": "Confirmed order value per calendar day.",
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
            "x-csrf-token": _csrf_token(ARCHITECT),
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


def _command(request_id: str, path: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Issue one console command the way a browser does, and return the state it left behind."""
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/{path.format(request_id=request_id)}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Idempotency-Key": uuid.uuid4().hex,
            "x-csrf-token": _csrf_token(actor),
            "x-heinzel-actor": actor,
        },
    )
    data = _read_json(request)["data"]
    assert isinstance(data, dict), data
    return data


def _read(path: str, *, actor: str) -> dict[str, Any]:
    payload = _read_json(
        urllib.request.Request(f"{BASE_URL}{path}", headers={"x-heinzel-actor": actor})
    )["data"]
    assert isinstance(payload, dict)
    return payload


def _drive_to_admission(request_id: str) -> dict[str, Any]:
    """Clarify, propose, submit, accept and approve, and return the reviewed proposal.

    The journey the README describes, issued as commands rather than described: every step is
    held to the origin, CSRF and idempotency rules the console enforces, so a step that stopped
    working in the image fails here rather than in front of whoever runs the demonstration.
    """
    opened = _read(f"/api/v1/inbox/{request_id}", actor=ARCHITECT)
    clarified = _command(
        request_id,
        "inbox/{request_id}/clarification",
        {
            "expected_revision": opened["revision"],
            "active_role": "data_architect",
            "restated_request": "Daily order value for the weekly operations review.",
            "in_scope_summary": "Confirmed order value per calendar day.",
            "out_of_scope_summary": "Cancelled orders and refunds.",
        },
        actor=ARCHITECT,
    )
    proposed = _command(
        request_id,
        "inbox/{request_id}/proposal",
        {"expected_revision": clarified["revision"], "active_role": "data_architect"},
        actor=ARCHITECT,
    )
    _command(
        request_id,
        "inbox/{request_id}/proposal/submission",
        {"expected_revision": proposed["revision"], "active_role": "data_architect"},
        actor=ARCHITECT,
    )
    statement = _read(f"/api/v1/requests/{request_id}/clarified-outcome", actor=REQUESTER)
    _command(
        request_id,
        "requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": statement["revision"],
            "clarified_outcome_digest": statement["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        actor=REQUESTER,
    )
    detail = _read(f"/api/v1/inbox/{request_id}", actor=ARCHITECT)
    return _command(
        request_id,
        "inbox/{request_id}/decisions",
        {
            "expected_revision": detail["revision"],
            "reviewed_digest": detail["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        actor=ARCHITECT,
    )


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
def test_the_quickstart_answers_the_question_it_seeds() -> None:
    """The whole promise, in the published image: a seeded question becomes a governed answer.

    The console provisions the warehouse beside it, acquires its seeded source through the
    acquisition provider, lands it under a receipt, materializes a product with dbt and publishes
    it -- all before it listens. Then this drives the request to admission, where the governed
    query plan is compiled and admitted, and reads the answer back as the requester who asked.

    This is the only test that runs that against the image someone would actually pull.
    """
    _compose("up", "--build", "-d")
    try:
        _wait_for_health()
        request_id = _seeded_request_id()
        reviewed = _drive_to_admission(request_id)

        admitted = _command(
            request_id,
            "inbox/{request_id}/admission",
            {
                "expected_revision": reviewed["revision"],
                "reviewed_digest": reviewed["proposal_digest"],
                "active_role": "data_architect",
            },
            actor=ARCHITECT,
        )
        # Past `execution_ready`: the governed admission left a plan and the execution ran it.
        assert admitted["state"] == "delivered", admitted

        result = _read(f"/api/v1/requests/{request_id}/result", actor=REQUESTER)
        assert [column["name"] for column in result["columns"]] == [
            "ordered_on",
            "total_order_value",
        ], result
        # The demonstration's own seeded numbers, read out of the product it materialized.
        assert [[str(value) for value in row] for row in result["rows"]] == [
            ["2026-09-10", "30.000000000"],
            ["2026-09-11", "125.500000000"],
            ["2026-09-12", "99.000000000"],
        ], result

        # A result belongs to whoever asked for it, and the architect did not.
        refused, _ = _get(f"/api/v1/requests/{request_id}/result", actor=ARCHITECT)
        assert refused == 404, refused

        _assert_the_production_chain_reads_back(request_id)
        _assert_the_gap_register_states_what_this_deployment_reports()
    finally:
        _compose("down", "-v")


def _assert_the_gap_register_states_what_this_deployment_reports() -> None:
    """The managed column of the register's capability table, against the warehouse it describes.

    The warehouse-less column is held by `test_gap_register.py` in the ordinary suite. This is
    the other one, and it is the column that went stale: the register claimed dashboard
    publication was unwired while a deployment exactly like this one was publishing a dashboard.
    It is asserted here because only here is there a warehouse to report against.
    """
    claim = re.compile(r"^\|\s*`([a-z-]+)`\s*\|\s*\w+\s*\|\s*(\w+)\s*\|$")
    register = (ROOT / "docs" / "demonstration-gaps.md").read_text(encoding="utf-8")
    claimed = {
        match[1]: match[2] for line in register.splitlines() if (match := claim.match(line.strip()))
    }
    assert claimed, "the gap register carries no capability claims to check"

    workspace = _read("/api/v1/workspace", actor=ARCHITECT)
    reported = {
        capability["capability_id"]: capability["state"] for capability in workspace["capabilities"]
    }

    assert set(claimed) == set(reported), (
        "the gap register and this deployment disagree about which capabilities exist; "
        f"only the console has {sorted(set(reported) - set(claimed))}, "
        f"only the register has {sorted(set(claimed) - set(reported))}"
    )
    drifted = {
        capability: (state, reported[capability])
        for capability, state in claimed.items()
        if state != reported[capability]
    }
    assert not drifted, (
        "the gap register claims a managed-warehouse capability state this deployment does not "
        f"report (capability: claimed, reported): {drifted}"
    )

    # And the sentence under the table: seven stages, the warehouse complete, the sources stage
    # waiting on an architect with a connection enrolled for it, and the other five blocked.
    setup = _read("/api/v1/setup", actor=ARCHITECT)
    stages = {stage["stage"]: stage["state"] for stage in setup["stages"]}
    assert len(stages) == 7, stages
    assert stages["foundation"] == "complete", stages
    assert stages["sources"] == "current", stages
    assert sum(state == "blocked" for state in stages.values()) == 5, stages
    # The stage is current rather than blocked because there is something to do in it: the
    # deployment enrolled its own source, and registering it is the architect's step. It is the
    # stage the console opens on for the same reason.
    assert setup["active_stage"] == "sources", setup["active_stage"]
    assert [item["connection_handle"] for item in setup["enrollable_sources"]] == ["demo-source"]
    assert setup["sources"] == [], setup["sources"]


def _assert_the_production_chain_reads_back(request_id: str) -> None:
    """Every stage from the warehouse to the rows, joined from what each service recorded.

    This is the one place the chain can be checked against services that really ran: the
    reader joins six stores, and a field renamed in any one of them would otherwise surface as
    a console section that silently went missing.
    """
    chain = _read(f"/api/v1/inbox/{request_id}/provenance", actor=ARCHITECT)

    warehouse = chain["warehouse"]
    assert warehouse is not None, chain
    assert warehouse["engine_kind"] == "postgresql", warehouse
    assert warehouse["lifecycle_state"] == "ready", warehouse

    # The shape the tenant's acquisition contract agreed to read, and its record key.
    source = chain["source"]
    assert [field["name"] for field in source["fields"]] == [
        "order_id",
        "customer_id",
        "ordered_on",
        "order_total",
        "updated_at",
    ], source
    assert source["record_key_fields"] == ["order_id"], source
    assert source["operation_semantics"] == "upsert_only", source

    # The landing run's own receipt: the five seeded records, in the table it named.
    landing = chain["landing"]
    assert landing["target_table_ref"] == "raw_customer_orders", landing
    assert landing["record_count"] == 5, landing

    # The compiled transform, as the compiler signed it, and the receipt for the run of it.
    product = chain["product"]
    assert product["output_columns"] == ["ordered_on", "total_order_value"], product
    assert product["quality_tests"], product
    assert product["compiled_sql"].startswith("SELECT"), product
    assert chain["materialization"]["output_row_count"] == 3, chain["materialization"]
    assert chain["materialization"]["quality_disposition"] == "passed", chain["materialization"]

    # The governed query, its ceilings, and the receipt for the rows the result page showed.
    query = chain["query"]
    assert query["engine_kind"] == "postgresql", query
    assert query["statement"].startswith("SELECT"), query
    assert query["routing"] == "policy_admitted", query
    assert query["row_limit"] > 0, query
    assert chain["execution"]["row_count"] == 3, chain["execution"]

    # A requester is told the request does not exist: the chain carries the compiled statements
    # and the warehouse's own identifiers, which are the architect's to read and not theirs.
    refused, _ = _get(f"/api/v1/inbox/{request_id}/provenance", actor=REQUESTER)
    assert refused == 404, refused


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
