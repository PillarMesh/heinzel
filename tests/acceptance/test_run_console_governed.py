from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pillarmesh_request_management import SQLiteRequestRepository
from starlette.testclient import TestClient

from tests.acceptance.run_console_governed import (
    ARCHITECT,
    REQUESTER,
    TENANT,
    GovernedConsoleDeployment,
    default_state_directory,
)

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def deployment(tmp_path: Path):
    running = GovernedConsoleDeployment(tmp_path)
    try:
        yield running
    finally:
        running.close()


def test_the_deployment_serves_governed_reads_rather_than_fixtures(
    deployment: GovernedConsoleDeployment,
) -> None:
    with TestClient(deployment.build_app()) as client:
        session = client.get("/api/v1/session")
        workspace = client.get("/api/v1/workspace")

    assert session.status_code == 200
    assert session.headers["X-PillarMesh-Data-Provenance"] == "governed_local"
    assert workspace.json()["meta"]["data_provenance"] == "governed_local"


def test_the_seeded_decision_reaches_the_architect_inbox(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        inbox = client.get("/api/v1/inbox")
        detail = client.get(f"/api/v1/inbox/{seeded.request_id}")

    assert inbox.status_code == 200
    assert seeded.request_id in {item["request_id"] for item in inbox.json()["data"]["items"]}
    assert detail.status_code == 200
    assert detail.json()["data"]["state"] == "awaiting_approval"
    assert detail.json()["data"]["proposal_digest"] == seeded.proposal_digest


def test_the_seeded_request_is_owned_by_the_request_service_on_disk(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The console projection is never the evidence that a transaction committed."""
    seeded = deployment.seed()
    deployment.close()

    reopened = SQLiteRequestRepository.open(deployment.request_path)
    try:
        stored = reopened.load(TENANT, seeded.request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.requester_id == REQUESTER


def test_the_actor_header_selects_the_requester_surface(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The local harness switches actor so one browser can walk both sides."""
    deployment.seed()

    with TestClient(deployment.build_app()) as client:
        architect = client.get("/api/v1/session")
        requester = client.get("/api/v1/session", headers={"x-pillarmesh-actor": REQUESTER})
        unknown = client.get("/api/v1/session", headers={"x-pillarmesh-actor": "nobody"})

    assert architect.json()["data"]["actor"]["display_name"] == ARCHITECT
    assert requester.json()["data"]["active_role"] == "requester"
    assert unknown.json()["data"]["active_role"] == "data_architect"


def test_the_catalog_capability_is_delivered_rather_than_reported_as_unwired(
    deployment: GovernedConsoleDeployment,
) -> None:
    """`CatalogControlBindingReader` was built and never composed.

    The console reported `catalog-binding: not_delivered - catalog-control read
    wiring` because the harness passed no reader, not because anything was missing.
    Composing catalog-control turns the capability live and is the cheapest real
    progress the console has left: the owning service already exists.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    catalog = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "catalog-binding"
    )
    assert catalog["state"] != "not_delivered"


def test_the_meaning_review_capability_is_delivered_rather_than_reported_as_unwired(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The console reads review bundles from the semantic registry itself.

    Both seams existed on the governed backend and the harness wired neither, so the
    console reported `semantic-review: not_delivered - semantic-registry review
    wiring` for a service that has been implemented since Plan 2.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    review = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "semantic-review"
    )
    assert review["state"] == "ready"
    assert review["dependency"] is None


def test_restarting_reuses_the_bindings_the_previous_run_created(tmp_path: Path) -> None:
    """The workspace binding directory is deployment configuration, so it persists.

    Holding it only in memory meant a restart forgot which binding this workspace
    used. The warehouse binding became unreachable even though warehouse-control
    still held it, and the catalog binding was worse: the harness minted a fresh
    draft on every construction, so restarting stacked orphaned bindings the
    directory then abandoned.
    """
    first = GovernedConsoleDeployment(tmp_path)
    catalog_binding = first.bindings.catalog_binding_id(TENANT)
    first.bindings.bind_warehouse(tenant_id=TENANT, binding_id="whb-recorded-by-a-command")
    first.close()

    second = GovernedConsoleDeployment(tmp_path)
    try:
        assert second.bindings.catalog_binding_id(TENANT) == catalog_binding
        assert second.bindings.warehouse_binding_id(TENANT) == "whb-recorded-by-a-command"
    finally:
        second.close()


def test_seeding_twice_does_not_leave_two_indistinguishable_decisions(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The state directory persists, so restarting the server re-enters `seed`.

    Minting a second identical stakeholder question each time left a queue of
    copies no reviewer could tell apart, and the printed identifier named only the
    newest.
    """
    first = deployment.seed()

    second = deployment.seed()

    assert second.request_id == first.request_id
    with TestClient(deployment.build_app()) as client:
        items = client.get("/api/v1/inbox").json()["data"]["items"]
    assert [item["request_id"] for item in items] == [first.request_id]


def test_a_localhost_host_is_refused_because_a_browser_does_not_treat_it_as_loopback() -> None:
    """The allowed origin is built from the bound spelling; a browser sends its own."""
    with pytest.raises(ValueError, match="loopback"):
        GovernedConsoleDeployment.require_loopback("localhost")


def test_the_default_state_directory_is_named_for_the_current_user() -> None:
    """`gettempdir()` is the shared `/tmp` on Linux and in CI."""
    assert str(os.getuid()) in default_state_directory().name


def test_the_default_state_directory_stays_out_of_the_repository() -> None:
    """A new directory at the repository root fails the repository-structure gate."""
    repository_root = Path(__file__).resolve().parents[2]

    default = default_state_directory()

    assert not default.is_relative_to(repository_root)


def test_a_non_loopback_host_is_refused() -> None:
    """This harness has no authentication; it may not leave the machine."""
    with pytest.raises(ValueError, match="loopback"):
        GovernedConsoleDeployment.require_loopback("0.0.0.0")


def test_the_run_capability_is_delivered_rather_than_reported_as_undelivered(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The reads exist now, so the harness must actually compose them.

    Publishing `list_activated` and `list_runs_for_contracts` in the owning
    services delivers nothing on its own. Leaving them uncomposed is exactly how
    `catalog-binding` and `semantic-review` came to be reported as undelivered for
    services that had been implemented for two plans.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    runs = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "data-product-runs"
    )
    assert runs["state"] == "ready"
    assert runs["dependency"] is None


def test_a_tenant_with_no_activated_contracts_reads_an_empty_run_listing(
    deployment: GovernedConsoleDeployment,
) -> None:
    """Delivered-and-empty is the honest answer, and it must not be a failure."""
    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 200
    assert response.json()["data"]["runs"] == []


def test_a_run_recorded_by_the_owning_services_reaches_the_console(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The tenant is derived, never stored on the run.

    The evidence record carries no tenant. It is reachable only because the
    contract digest it was witnessed under is activated for this tenant, which is
    the whole of the derivation this capability rests on.
    """
    deployment.lifecycles.activate(tenant_id=TENANT, contract_digest="a" * 64, activated_at=_NOW)
    deployment.evidence.create_run(
        "run-000000000000000000000001", "activation-1", "a" * 64, "b" * 64, "{}", _NOW
    )

    with TestClient(deployment.build_app()) as client:
        listed = client.get("/api/v1/runs").json()["data"]["runs"]

    assert [run["run_id"] for run in listed] == ["run-000000000000000000000001"]
    assert listed[0]["contract_digest"] == "a" * 64


def test_a_run_under_another_tenant_s_contract_is_not_listed(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The derivation is the tenant boundary, so this is the test that matters."""
    deployment.lifecycles.activate(
        tenant_id="tenant-somebody-else", contract_digest="c" * 64, activated_at=_NOW
    )
    deployment.evidence.create_run(
        "run-000000000000000000000002", "activation-2", "c" * 64, "b" * 64, "{}", _NOW
    )

    with TestClient(deployment.build_app()) as client:
        listed = client.get("/api/v1/runs").json()["data"]["runs"]

    assert listed == []


def test_every_store_is_closed_even_when_one_close_raises(tmp_path: Path) -> None:
    """Teardown must not abandon file handles because an earlier close failed.

    The deployment now owns six databases. A chain of nested `finally` blocks grew
    one level per store and silently skipped the rest whenever an early close
    raised, which on this harness leaks the state directory between runs.
    """
    running = GovernedConsoleDeployment(tmp_path)

    def explode() -> None:
        raise RuntimeError("this store refuses to close")

    running.warehouse_repository.close = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="refuses to close"):
        running.close()

    # A closed connection is observed by using it, not by asking the store to
    # carry a flag that exists only for this assertion.
    with pytest.raises(sqlite3.ProgrammingError):
        running.evidence.list_runs_for_contracts(("a" * 64,))


def test_the_advertised_data_product_route_is_actually_exercised(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The capability badge claimed a route no test had ever called.

    `data-product-runs` reported `ready` because two readers were non-None, while
    every call to the data-product half raised `TypeError` and returned 500. A badge
    is only worth what an exercised route makes it worth, so this walks the route the
    badge advertises rather than the wiring behind it.
    """
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        permitted = client.get(f"/api/v1/data-products/{seeded.data_product_ref}")

    assert permitted.status_code == 200
    assert permitted.json()["data"]["data_product_id"] == seeded.data_product_ref


def test_a_data_product_no_policy_permits_is_not_found_rather_than_a_failure(
    deployment: GovernedConsoleDeployment,
) -> None:
    deployment.seed()

    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/data-products/product-nobody-permits")

    assert response.status_code == 404
