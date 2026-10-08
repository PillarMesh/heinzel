from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import BiDashboardDefinition, dashboard_external_key
from heinzel_provider_superset import (
    CredentialScopedSupersetProvider,
    HttpSupersetClient,
    SupersetCredentials,
    SupersetHttpResponse,
)
from heinzel_provider_superset.client import _database_key


@dataclass
class _Resource:
    resource_id: int
    payload: dict[str, object]


def _as_list(value: object) -> list[object]:
    """Narrow a payload member the fake API stores as `object` to the list it is."""
    assert isinstance(value, list), value
    return value


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow a recorded request body from `object` to the mapping it is."""
    assert isinstance(value, Mapping), value
    return value


class _SupersetApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, str], object | None]] = []
        self.resources: dict[str, list[_Resource]] = {
            "database": [],
            "dataset": [],
            "chart": [],
            "dashboard": [],
        }
        self.next_id = 1
        self.lose_after: tuple[str, str] | None = None
        self.forced_status: int | None = None
        self.forced_payload: object | None = None
        self.timeout_on: tuple[str, str] | None = None
        self.malformed_kind: str | None = None
        self.login_payload: object = {"access_token": "session-token"}
        self.include_list_title = False
        self.reject_dataset_post_description = False
        self.include_write_data = False
        self.include_write_last_modified_time = False
        self.reject_dashboard_managed_json_metadata = False

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: object | None = None,
        params: Mapping[str, str] | None = None,
    ) -> SupersetHttpResponse:
        path = url.removeprefix("https://superset.test")
        self.calls.append((method, path, headers, json))
        if self.timeout_on == (method, path):
            raise TimeoutError("database-password")
        if self.forced_status is not None:
            return SupersetHttpResponse(status_code=self.forced_status, payload=self.forced_payload)
        if path == "/api/v1/security/login":
            return SupersetHttpResponse(status_code=200, payload=self.login_payload)
        if path == "/api/v1/security/csrf_token/":
            assert headers == {"Authorization": "Bearer session-token"}
            return SupersetHttpResponse(status_code=200, payload={"result": "csrf-token"})
        expected_headers = {"Authorization": "Bearer session-token"}
        if method in {"POST", "PUT"}:
            expected_headers["X-CSRFToken"] = "csrf-token"
            expected_headers["Referer"] = "https://superset.test/"
        assert headers == expected_headers
        parts = path.strip("/").split("/")
        kind = parts[2]
        if method == "GET" and len(parts) == 3:
            if self.malformed_kind == kind:
                return SupersetHttpResponse(status_code=200, payload={"result": "invalid"})
            values = [self._wire(kind, item) for item in self.resources[kind]]
            assert params is not None
            page_match = re.fullmatch(r"\(page:(\d+),page_size:(\d+)\)", params["q"])
            assert page_match is not None
            page = int(page_match.group(1))
            page_size = int(page_match.group(2))
            values = values[page * page_size : (page + 1) * page_size]
            payload: dict[str, object] = {
                "count": len(self.resources[kind]),
                "result": values,
            }
            if self.include_list_title:
                payload["list_title"] = f"List {kind}"
            return SupersetHttpResponse(status_code=200, payload=payload)
        if method == "GET" and len(parts) == 5 and parts[4] == "charts":
            dashboard_id = int(parts[3])
            charts = [
                self._wire("chart", chart)
                for chart in self.resources["chart"]
                if dashboard_id in _as_list(chart.payload.get("dashboards", []))
            ]
            return SupersetHttpResponse(status_code=200, payload={"result": charts})
        if method == "GET":
            resource_id = int(parts[3])
            resource = next(
                item for item in self.resources[kind] if item.resource_id == resource_id
            )
            return SupersetHttpResponse(
                status_code=200,
                payload={
                    "description_columns": {},
                    "id": resource_id,
                    "label_columns": {},
                    "result": self._wire(kind, resource),
                    "show_columns": [],
                    "show_title": f"Show {kind}",
                },
            )
        if method == "POST":
            assert isinstance(json, dict)
            if kind == "dataset" and self.reject_dataset_post_description and "description" in json:
                return SupersetHttpResponse(
                    status_code=400,
                    payload={"message": {"description": ["Unknown field."]}},
                )
            if kind == "dashboard" and self.reject_dashboard_managed_json_metadata:
                metadata = json.get("json_metadata")
                if metadata != "{}":
                    return SupersetHttpResponse(
                        status_code=400,
                        payload={"message": {"json_metadata": ["Unknown field."]}},
                    )
            resource = _Resource(self.next_id, dict(json))
            self.next_id += 1
            self.resources[kind].append(resource)
            if self.lose_after == (method, path):
                self.lose_after = None
                raise TimeoutError("secret-token database-password")
            payload = {"id": resource.resource_id}
            if self.include_write_data:
                payload["data"] = {"id": resource.resource_id}
            return SupersetHttpResponse(status_code=201, payload=payload)
        assert method == "PUT"
        resource_id = int(parts[3])
        resource = next(item for item in self.resources[kind] if item.resource_id == resource_id)
        assert isinstance(json, dict)
        resource.payload.update(json)
        if self.lose_after == (method, "/".join(("", *parts[:3], str(resource_id)))):
            self.lose_after = None
            raise TimeoutError("secret-token database-password")
        payload = {"id": resource_id}
        if self.include_write_last_modified_time:
            payload["last_modified_time"] = 1_789_416_000.0
        return SupersetHttpResponse(status_code=200, payload=payload)

    @staticmethod
    def _wire(kind: str, item: _Resource) -> dict[str, object]:
        payload = dict(item.payload)
        payload["id"] = item.resource_id
        if kind == "dashboard":
            payload["url"] = f"/superset/dashboard/{item.resource_id}/"
        return payload


class _Resolver:
    def __init__(self, credentials: SupersetCredentials) -> None:
        self.credentials = credentials
        self.references: list[str] = []

    def resolve(self, *, secret_reference: str) -> SupersetCredentials:
        self.references.append(secret_reference)
        return self.credentials


class _FailingResolver:
    def resolve(self, *, secret_reference: str) -> SupersetCredentials:
        del secret_reference
        raise KeyError("database-password")


def _definition(**updates: object) -> BiDashboardDefinition:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "dashboard_id": "revenue",
        "version": 3,
        "revision": 1,
        "stable_external_key": dashboard_external_key(
            tenant_id="tenant-a", dashboard_id="revenue", version=3
        ),
        "desired_digest": "b" * 64,
        "prior_desired_digest": None,
        "title": "Revenue overview",
        "contract_digest": "e" * 64,
        "contract_signature": "test-signature",
        "dataset_stable_key": "dataset-orders",
        "dataset_generation": 7,
        "dataset_namespace": "analytics",
        "dataset_relation_name": "revenue_current",
        "connection_secret_ref": "secret://tenant-a/superset-database",
        "metric_refs": ("metric:revenue:v2",),
        "dimension_refs": ("dimension:region:v1",),
        "filter_refs": ("dimension:status:v1",),
        "metric_projections": (
            {
                "semantic_ref": "metric:revenue:v2",
                "aggregate": "sum",
                "column_name": "revenue_total",
                "output_name": "revenue_total",
            },
        ),
        "dimension_projections": (
            {
                "semantic_ref": "dimension:region:v1",
                "column_name": "region",
                "output_name": "region",
            },
        ),
        "visual_intents": ("bar", "number"),
        "lifecycle_state": "active",
    }
    values.update(updates)
    return BiDashboardDefinition.model_validate(values)


def _stable_database_key() -> str:
    """The name the client looks a database up by, derived as the client derives it."""
    return _database_key(_definition())


def _provider(api: _SupersetApi) -> tuple[CredentialScopedSupersetProvider, _Resolver]:
    credentials = SupersetCredentials(
        base_url="https://superset.test",
        username="service-user",
        password="database-password",
        database_uri="postgresql://user:database-password@warehouse/db",
    )
    resolver = _Resolver(credentials)
    return CredentialScopedSupersetProvider(resolver=resolver, transport=api, page_size=1), resolver


def test_http_composition_authenticates_and_creates_the_complete_dashboard_graph() -> None:
    api = _SupersetApi()
    provider, resolver = _provider(api)

    receipt = provider.apply(_definition())

    assert receipt.external_url == "https://superset.test/superset/dashboard/5/"
    assert resolver.references == ["secret://tenant-a/superset-database"]
    assert [call[1] for call in api.calls if call[0] == "POST"] == [
        "/api/v1/security/login",
        "/api/v1/database/",
        "/api/v1/dataset/",
        "/api/v1/chart/",
        "/api/v1/chart/",
        "/api/v1/dashboard/",
    ]
    assert sum(call[1] == "/api/v1/security/login" for call in api.calls) == 1
    assert sum(call[1] == "/api/v1/security/csrf_token/" for call in api.calls) == 1
    assert api.resources["dataset"][0].payload["schema"] == "analytics"
    assert api.resources["dataset"][0].payload["table_name"] == "revenue_current"
    assert [chart.payload["dashboards"] for chart in api.resources["chart"]] == [[5], [5]]
    read_client = HttpSupersetClient(credentials=resolver.credentials, transport=api)
    assert read_client.get_dashboard_chart_ids(stable_key=_definition().stable_external_key) == (
        3,
        4,
    )
    assert "database-password" not in repr(provider)
    assert "database-password" not in repr(resolver.credentials)
    assert "database-password" not in receipt.model_dump_json()


def test_http_composition_accepts_superset_refresh_token_login_response() -> None:
    api = _SupersetApi()
    api.login_payload = {
        "access_token": "session-token",
        "refresh_token": "unused-refresh-token",
    }
    provider, _ = _provider(api)

    receipt = provider.apply(_definition())

    assert receipt.lifecycle_state == "active"


def test_http_composition_accepts_superset_list_title_metadata() -> None:
    api = _SupersetApi()
    api.include_list_title = True
    provider, _ = _provider(api)

    receipt = provider.apply(_definition())

    assert receipt.lifecycle_state == "active"


def test_http_composition_sends_same_origin_referrer_for_csrf_mutations() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)

    provider.apply(_definition())

    mutation_headers = [headers for method, _path, headers, _json in api.calls if method == "POST"]
    assert mutation_headers[1]["Referer"] == "https://superset.test/"


def test_http_composition_sets_dataset_metadata_after_native_creation() -> None:
    api = _SupersetApi()
    api.reject_dataset_post_description = True
    provider, _ = _provider(api)

    provider.apply(_definition())

    dataset_calls = [
        (method, payload)
        for method, path, _headers, payload in api.calls
        if path.startswith("/api/v1/dataset/") and method in {"POST", "PUT"}
    ]
    assert dataset_calls[0][0] == "POST"
    assert "description" not in _as_mapping(dataset_calls[0][1])
    assert dataset_calls[1][0] == "PUT"
    assert "description" in _as_mapping(dataset_calls[1][1])


def test_http_composition_accepts_superset_write_data_metadata() -> None:
    api = _SupersetApi()
    api.include_write_data = True
    provider, _ = _provider(api)

    receipt = provider.apply(_definition())

    assert receipt.lifecycle_state == "active"


def test_http_composition_uses_certification_details_for_managed_dashboard_metadata() -> None:
    api = _SupersetApi()
    api.reject_dashboard_managed_json_metadata = True
    provider, _ = _provider(api)

    provider.apply(_definition())

    dashboard = api.resources["dashboard"][0].payload
    assert dashboard["json_metadata"] == "{}"
    assert json.loads(str(dashboard["certification_details"]))["stable_key"].startswith(
        "pm-dashboard-"
    )


def test_http_composition_accepts_superset_write_timestamp_metadata() -> None:
    api = _SupersetApi()
    api.include_write_last_modified_time = True
    provider, _ = _provider(api)
    created = provider.apply(_definition())

    updated = provider.apply(
        _definition(
            revision=2,
            desired_digest="c" * 64,
            prior_desired_digest=created.desired_digest,
            lifecycle_state="archived",
        )
    )

    assert updated.lifecycle_state == "archived"


def test_http_composition_exact_replay_uses_paginated_stable_key_lookup() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)
    definition = _definition()
    created = provider.apply(definition)

    replayed = provider.apply(definition)

    assert replayed == created
    assert len(api.resources["dashboard"]) == 1
    assert len(api.resources["chart"]) == 2
    assert any(call[0] == "GET" and call[1] == "/api/v1/chart/" for call in api.calls)
    chart_membership_writes = [
        call
        for call in api.calls
        if call[0] == "PUT"
        and call[1].startswith("/api/v1/chart/")
        and isinstance(call[3], dict)
        and "dashboards" in call[3]
    ]
    assert len(chart_membership_writes) == 2


def test_http_composition_repairs_managed_chart_membership_on_exact_replay() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)
    definition = _definition()
    provider.apply(definition)
    api.resources["chart"][0].payload["dashboards"] = []

    provider.apply(definition)

    assert [chart.payload["dashboards"] for chart in api.resources["chart"]] == [[5], [5]]


def test_http_composition_ignores_unmanaged_dataset_descriptions() -> None:
    api = _SupersetApi()
    api.resources["dataset"].append(
        _Resource(
            resource_id=90,
            payload={
                "schema": "unrelated",
                "table_name": "human_owned",
                "description": "Maintained outside Heinzel",
            },
        )
    )
    provider, _ = _provider(api)

    provider.apply(_definition())

    managed = [
        item
        for item in api.resources["dataset"]
        if item.payload.get("table_name") == "revenue_current"
    ]
    assert len(managed) == 1


def test_http_composition_refuses_duplicate_managed_dataset_identity() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)
    provider.apply(_definition())
    duplicate = api.resources["dataset"][0]
    api.resources["dataset"].append(_Resource(resource_id=90, payload=dict(duplicate.payload)))

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "integrity_failure"


def test_http_composition_refuses_incomplete_metadata_claiming_managed_identity() -> None:
    api = _SupersetApi()
    api.resources["dataset"].append(
        _Resource(
            resource_id=90,
            payload={
                "schema": "analytics",
                "table_name": "revenue_current",
                "description": '{"stable_key":"dataset-orders"}',
            },
        )
    )
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "invalid_provider_response"
    assert not any(
        call[0] in {"POST", "PUT"} and not call[1].startswith("/api/v1/security/")
        for call in api.calls
    )


def test_http_composition_refuses_managed_dataset_address_drift() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)
    provider.apply(_definition())
    api.resources["dataset"][0].payload["table_name"] = "manually_changed"
    mutation_count = sum(
        call[0] in {"POST", "PUT"} and not call[1].startswith("/api/v1/security/")
        for call in api.calls
    )

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "integrity_failure"
    assert (
        sum(
            call[0] in {"POST", "PUT"} and not call[1].startswith("/api/v1/security/")
            for call in api.calls
        )
        == mutation_count
    )


def test_http_composition_updates_and_archives_managed_objects() -> None:
    api = _SupersetApi()
    provider, _ = _provider(api)
    provider.apply(_definition())

    updated = provider.apply(
        _definition(
            revision=2,
            desired_digest="c" * 64,
            prior_desired_digest="b" * 64,
            visual_intents=("line",),
        )
    )
    assert [chart.payload["dashboards"] for chart in api.resources["chart"]] == [[5], []]
    archived = provider.apply(
        _definition(
            revision=3,
            desired_digest="d" * 64,
            prior_desired_digest="c" * 64,
            visual_intents=("line",),
            lifecycle_state="archived",
        )
    )

    assert updated.desired_digest == "c" * 64
    assert archived.lifecycle_state == "archived"
    assert [chart.payload["dashboards"] for chart in api.resources["chart"]] == [[5], []]
    assert any(call[:2] == ("PUT", "/api/v1/dashboard/5") for call in api.calls)
    assert any(call[:2] == ("PUT", "/api/v1/chart/3") for call in api.calls)


def test_http_composition_recovers_after_dashboard_create_response_loss() -> None:
    api = _SupersetApi()
    api.lose_after = ("POST", "/api/v1/dashboard/")
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())
    assert captured.value.classification == "ambiguous_outcome"
    assert "database-password" not in str(captured.value)

    receipt = provider.apply(_definition())
    assert receipt.desired_digest == "b" * 64
    assert len(api.resources["dashboard"]) == 1


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (401, "authorization_denied"),
        (403, "authorization_denied"),
        (404, "statement_rejected"),
        (409, "integrity_failure"),
        (429, "throttled"),
        (500, "transient_unavailable"),
    ],
)
def test_http_statuses_are_classified_without_response_details(
    status: int, classification: str
) -> None:
    api = _SupersetApi()
    api.forced_status = status
    api.forced_payload = {"message": "database-password"}
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == classification
    assert "database-password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_http_client_rejects_malformed_authentication_schema() -> None:
    api = _SupersetApi()
    api.forced_status = 200
    api.forced_payload = {"access_token": ["secret-token"]}
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "invalid_provider_response"
    assert "secret-token" not in str(captured.value)


def test_http_client_classifies_read_timeout_without_leaking_transport_details() -> None:
    api = _SupersetApi()
    api.timeout_on = ("GET", "/api/v1/database/")
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "transient_transport"
    assert "database-password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_http_client_rejects_a_malformed_paginated_resource_schema() -> None:
    api = _SupersetApi()
    api.malformed_kind = "database"
    provider, _ = _provider(api)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "invalid_provider_response"


def test_credential_resolution_failure_is_classified_and_sanitized() -> None:
    provider = CredentialScopedSupersetProvider(
        resolver=_FailingResolver(), transport=_SupersetApi()
    )

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "permanent_configuration"
    assert "database-password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_credentials_have_a_redacted_representation() -> None:
    credentials = SupersetCredentials(
        base_url="https://superset.test",
        username="service-user",
        password="database-password",
        database_uri="postgresql://user:database-password@warehouse/db",
    )

    assert "service-user" not in repr(credentials)
    assert "database-password" not in repr(credentials)


def test_an_existing_database_is_reconciled_to_the_credential_this_apply_resolved() -> None:
    """Superset stores the connection it was created with, and the credential behind it rotates.

    Left as it was, every query through the dashboard is refused while the dashboard itself reports
    as published: the receipt records an applied dashboard, not a connection that still
    authenticates, so nothing surfaces the drift.

    Written rather than compared because Superset masks the password on read, which makes a
    connection whose only change is the credential indistinguishable from a current one.
    """
    api = _SupersetApi()
    definition = _definition()
    stale = "postgresql://user:a-password-from-an-earlier-deployment@warehouse/db"
    api.resources["database"].append(
        _Resource(41, {"database_name": _stable_database_key(), "sqlalchemy_uri": stale})
    )
    provider, resolver = _provider(api)

    provider.apply(definition)

    assert [call[1] for call in api.calls if call[0] == "PUT"].count("/api/v1/database/41") == 1
    assert api.resources["database"][0].payload["sqlalchemy_uri"] == (
        resolver.credentials.database_uri
    )
    # Reconciled rather than duplicated: one database, still the one that was already there.
    assert [call[1] for call in api.calls if call[0] == "POST"].count("/api/v1/database/") == 0
    assert len(api.resources["database"]) == 1
    assert api.resources["database"][0].resource_id == 41


def test_a_database_that_is_created_is_not_then_written_again() -> None:
    """The reconciliation is for a connection Superset already holds, not every apply.

    A create already carries the current credential, so writing it a second time would be a
    mutation that changes nothing -- and `POST` then `PUT` on one resource reads, in a Superset
    audit log, as a connection that was corrected rather than one that was right.
    """
    api = _SupersetApi()
    provider, _ = _provider(api)

    provider.apply(_definition())

    assert [call[1] for call in api.calls if call[0] == "PUT"].count("/api/v1/database/1") == 0


def _chart_params(api: _SupersetApi) -> dict[str, object]:
    """The form data the one created chart carries, as Superset stores it."""
    chart = next(
        item for item in api.resources["chart"] if "chart-000" in str(item.payload["slice_name"])
    )
    params = chart.payload["params"]
    assert isinstance(params, str)
    return dict(json.loads(params))


@pytest.mark.parametrize(
    ("intent", "viz_type"),
    [
        ("bar", "echarts_timeseries_bar"),
        ("line", "echarts_timeseries_line"),
        ("number", "big_number_total"),
        ("table", "table"),
    ],
)
def test_each_governed_intent_becomes_the_superset_plugin_that_draws_it(
    intent: str, viz_type: str
) -> None:
    """`bar` names what the contract asked for; it is not a Superset plugin.

    Passed through as a `viz_type` it produced a chart Superset had no plugin for, which it
    renders empty. Each of these was confirmed against a running Superset.
    """
    api = _SupersetApi()
    provider, _ = _provider(api)

    provider.apply(_definition(visual_intents=(intent,)))

    chart = next(
        item for item in api.resources["chart"] if "chart-000" in str(item.payload["slice_name"])
    )
    assert chart.payload["viz_type"] == viz_type
    assert _chart_params(api)["viz_type"] == viz_type


def test_an_axis_chart_asks_for_the_metric_along_the_bound_dimension() -> None:
    """Without this the chart has a datasource and no query, and Superset answers `Empty query?`.

    The aggregate and the column stay separate rather than being handed over as a SQL string, so
    the one place a column name reaches a query is one Superset parses as a column.
    """
    api = _SupersetApi()
    provider, _ = _provider(api)

    provider.apply(_definition(visual_intents=("bar",)))
    params = _chart_params(api)

    assert params["x_axis"] == "region"
    assert params["metrics"] == [
        {
            "expressionType": "SIMPLE",
            "column": {"column_name": "revenue_total"},
            "aggregate": "SUM",
            "label": "revenue_total",
            "hasCustomLabel": True,
        }
    ]
    # The governed metadata the provider reconciles against is still carried beside the query.
    assert "heinzel" in params


def test_a_single_number_asks_for_one_metric_and_a_table_groups_by_what_it_has() -> None:
    """The plugins read different shapes: `metric` singular against `metrics`, and a table groups.

    A table with no dimension is a table over the whole relation, which a running Superset renders
    as the single row that is -- so neither it nor a number needs one.
    """
    api = _SupersetApi()
    provider, _ = _provider(api)
    provider.apply(_definition(visual_intents=("number",)))
    number = _chart_params(api)

    assert number["metric"] == {
        "expressionType": "SIMPLE",
        "column": {"column_name": "revenue_total"},
        "aggregate": "SUM",
        "label": "revenue_total",
        "hasCustomLabel": True,
    }
    assert "metrics" not in number
    assert number["subheader"] == "Revenue overview"

    grouped = _SupersetApi()
    grouped_provider, _ = _provider(grouped)
    grouped_provider.apply(_definition(visual_intents=("table",)))
    table = _chart_params(grouped)

    assert table["query_mode"] == "aggregate"
    assert table["groupby"] == ["region"]

    ungrouped = _SupersetApi()
    ungrouped_provider, _ = _provider(ungrouped)
    ungrouped_provider.apply(_definition(visual_intents=("table",), dimension_projections=()))

    assert _chart_params(ungrouped)["groupby"] == []
