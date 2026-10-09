from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import httpx
from heinzel_contract_model import canonical_bytes, digest, display_label
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import (
    BiApplyResult,
    BiDashboardDefinition,
    BiDataset,
    BiDimensionProjection,
    BiLifecycleState,
    BiMetricProjection,
    BiVisualIntent,
)
from heinzel_provider_sdk.errors import ProviderErrorClassification

from .provider import SupersetClientError, SupersetDashboard

_PAGE_SIZE = 100
_LIST_RESPONSE_KEYS = frozenset(
    {
        "count",
        "description_columns",
        "ids",
        "label_columns",
        "list_columns",
        "list_title",
        "order_columns",
        "result",
    }
)
_WRITE_RESPONSE_KEYS = frozenset({"data", "id", "last_modified_time", "message", "result"})
_DETAIL_RESPONSE_KEYS = frozenset(
    {"description_columns", "id", "label_columns", "result", "show_columns", "show_title"}
)


@dataclass(frozen=True, slots=True, repr=False)
class SupersetCredentials:
    base_url: str
    username: str
    password: str
    database_uri: str

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://") or self.base_url.endswith("/"):
            raise ValueError("Superset base URL must be an HTTPS origin without a trailing slash")
        if not all((self.username, self.password, self.database_uri)):
            raise ValueError("Superset credentials are incomplete")


@dataclass(frozen=True, slots=True)
class SupersetHttpResponse:
    status_code: int
    payload: object


class SupersetHttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: object | None = None,
        params: Mapping[str, str] | None = None,
    ) -> SupersetHttpResponse: ...


class HttpxSupersetTransport:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=10.0)

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: object | None = None,
        params: Mapping[str, str] | None = None,
    ) -> SupersetHttpResponse:
        response = self._client.request(
            method,
            url,
            headers=headers,
            json=json,
            params=params,
        )
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        return SupersetHttpResponse(status_code=response.status_code, payload=payload)


class SupersetCredentialResolver(Protocol):
    def resolve(self, *, secret_reference: str) -> SupersetCredentials: ...


class HttpSupersetClient:
    def __init__(
        self,
        *,
        credentials: SupersetCredentials,
        transport: SupersetHttpTransport,
        page_size: int = _PAGE_SIZE,
    ) -> None:
        if page_size < 1 or page_size > _PAGE_SIZE:
            raise ValueError("Superset page size must be between 1 and 100")
        self._credentials = credentials
        self._transport = transport
        self._page_size = page_size
        self._access_token: str | None = None
        self._csrf_token_value: str | None = None

    def prepare_dashboard(self, *, definition: BiDashboardDefinition) -> None:
        dataset = self._lookup_dataset(stable_key=definition.dataset_stable_key)
        if dataset is not None and (
            dataset.get("schema") != definition.dataset_namespace
            or dataset.get("table_name") != definition.dataset_relation_name
        ):
            raise _client_error("integrity_failure")

        database_key = _database_key(definition)
        database = self._lookup("database", "database_name", database_key)
        if database is None:
            database_id = self._create(
                "database",
                {
                    "database_name": database_key,
                    "sqlalchemy_uri": self._credentials.database_uri,
                },
            )
        else:
            #
            # The connection is reconciled, not adopted. Superset stores the connection string it
            # was created with, and the credential behind it rotates: the one resolved for this
            # apply is the one that works now, and the one Superset holds may be an earlier
            # deployment's. Left alone, every query through this dashboard is refused while the
            # dashboard itself reports as published -- drift nothing surfaces, because the receipt
            # records an applied dashboard and not a connection that still authenticates.
            #
            # Written unconditionally because it cannot be compared: Superset masks the password on
            # read, so a connection whose only change is the credential is indistinguishable from
            # one that is current. A connection edited inside Superset is overwritten for the same
            # reason this is a desired state at all -- what the governed definition names is what
            # Superset must hold.
            database_id = _identifier(database)
            self._update(
                "database", database_id, {"sqlalchemy_uri": self._credentials.database_uri}
            )

        dataset_metadata = _metadata(
            stable_key=definition.dataset_stable_key,
            managed_digest=definition.contract_digest,
            lifecycle_state="active",
            generation=definition.dataset_generation,
        )
        dataset_create_payload: dict[str, object] = {
            "database": database_id,
            "schema": definition.dataset_namespace,
            "table_name": definition.dataset_relation_name,
        }
        if dataset is None:
            dataset_id = self._create("dataset", dataset_create_payload)
            self._update("dataset", dataset_id, {"description": dataset_metadata})
        else:
            dataset_id = _identifier(dataset)
            if _description(dataset) != dataset_metadata:
                self._update("dataset", dataset_id, {"description": dataset_metadata})

        expected_chart_keys: set[str] = set()
        titles = _chart_titles(definition)
        for index, visual_intent in enumerate(definition.visual_intents):
            chart_key = _chart_key(definition, index)
            expected_chart_keys.add(chart_key)
            chart_metadata = _metadata(
                stable_key=chart_key,
                managed_digest=definition.desired_digest,
                lifecycle_state="active",
            )
            chart_payload: dict[str, object] = {
                "slice_name": titles[index],
                "viz_type": _VIZ_TYPES[visual_intent],
                "datasource_id": dataset_id,
                "datasource_type": "table",
                "description": chart_metadata,
                "params": _canonical_json(
                    {
                        **_form_data(visual_intent, definition, dataset_id=dataset_id),
                        "heinzel": json.loads(chart_metadata),
                    }
                ),
            }
            chart = self._lookup_chart(stable_key=chart_key)
            if chart is None:
                self._create("chart", chart_payload)
            elif _description(chart) != chart_metadata or chart.get("slice_name") != titles[index]:
                self._update("chart", _identifier(chart), chart_payload)

        for chart, candidate_chart_key in self._managed_charts(definition):
            if candidate_chart_key in expected_chart_keys:
                continue
            metadata = _metadata(
                stable_key=candidate_chart_key,
                managed_digest=definition.desired_digest,
                lifecycle_state="archived",
            )
            if _description(chart) != metadata:
                self._update("chart", _identifier(chart), {"description": metadata})

    def get_dataset(self, *, stable_key: str) -> BiDataset | None:
        dataset = self._lookup_dataset(stable_key=stable_key)
        if dataset is None:
            return None
        metadata = _parsed_metadata(_description(dataset))
        generation = metadata.get("generation")
        namespace = dataset.get("schema")
        relation_name = dataset.get("table_name")
        if (
            metadata.get("stable_key") != stable_key
            or type(generation) is not int
            or not isinstance(namespace, str)
            or not namespace
            or not isinstance(relation_name, str)
            or not relation_name
        ):
            raise _client_error("invalid_provider_response")
        return BiDataset(
            stable_key=stable_key,
            generation=generation,
            namespace=namespace,
            relation_name=relation_name,
        )

    def get_dashboard(self, *, stable_key: str) -> SupersetDashboard | None:
        dashboard = self._lookup("dashboard", "slug", stable_key)
        if dashboard is None:
            return None
        metadata_value = dashboard.get("certification_details")
        if not isinstance(metadata_value, str):
            raise _client_error("invalid_provider_response")
        metadata = _parsed_metadata(metadata_value)
        managed_digest = metadata.get("managed_digest")
        lifecycle_state = _lifecycle_state(metadata.get("lifecycle_state"))
        relative_url = dashboard.get("url")
        if (
            metadata.get("stable_key") != stable_key
            or not isinstance(managed_digest, str)
            or not isinstance(relative_url, str)
            or not relative_url.startswith("/")
        ):
            raise _client_error("invalid_provider_response")
        return SupersetDashboard(
            external_id=str(_identifier(dashboard)),
            stable_key=stable_key,
            managed_digest=managed_digest,
            lifecycle_state=lifecycle_state,
            external_url=self._credentials.base_url + relative_url,
        )

    def get_dashboard_chart_ids(self, *, stable_key: str) -> tuple[int, ...]:
        dashboard = self._lookup("dashboard", "slug", stable_key)
        if dashboard is None:
            return ()
        payload = self._request("GET", f"/api/v1/dashboard/{_identifier(dashboard)}/charts")
        if not isinstance(payload, dict) or set(payload) != {"result"}:
            raise _client_error("invalid_provider_response")
        result = payload.get("result")
        if not isinstance(result, list):
            raise _client_error("invalid_provider_response")
        identifiers: list[int] = []
        for chart in result:
            if not isinstance(chart, dict):
                raise _client_error("invalid_provider_response")
            identifiers.append(_identifier(chart))
        canonical = tuple(sorted(identifiers))
        if len(canonical) != len(set(canonical)):
            raise _client_error("invalid_provider_response")
        return canonical

    def create_dashboard(self, *, definition: BiDashboardDefinition) -> SupersetDashboard:
        dashboard_id = self._create("dashboard", self._dashboard_payload(definition))
        return self._dashboard_after_write(dashboard_id, definition)

    def update_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard:
        dashboard_id = _external_identifier(external_id)
        self._update("dashboard", dashboard_id, self._dashboard_payload(definition))
        return self._dashboard_after_write(dashboard_id, definition)

    def archive_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard:
        dashboard_id = _external_identifier(external_id)
        for chart, chart_key in self._managed_charts(definition):
            self._update(
                "chart",
                _identifier(chart),
                {
                    "description": _metadata(
                        stable_key=chart_key,
                        managed_digest=definition.desired_digest,
                        lifecycle_state="archived",
                    )
                },
            )
        self._update("dashboard", dashboard_id, self._dashboard_payload(definition))
        return self._dashboard_after_write(dashboard_id, definition)

    def reconcile_dashboard_charts(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> None:
        dashboard_id = _external_identifier(external_id)
        expected_chart_keys = {
            _chart_key(definition, index)
            for index, _visual_intent in enumerate(definition.visual_intents)
        }
        managed_charts = self._managed_charts(definition)
        observed_keys = {chart_key for _chart, chart_key in managed_charts}
        if not expected_chart_keys.issubset(observed_keys):
            raise _client_error("integrity_failure")
        for chart, chart_key in managed_charts:
            chart_id = _identifier(chart)
            payload = self._request("GET", f"/api/v1/chart/{chart_id}")
            if (
                not isinstance(payload, dict)
                or "result" not in payload
                or not set(payload).issubset(_DETAIL_RESPONSE_KEYS)
            ):
                raise _client_error("invalid_provider_response")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise _client_error("invalid_provider_response")
            current_dashboard_ids = _role_ids(result.get("dashboards", []))
            desired_dashboard_ids = (dashboard_id,) if chart_key in expected_chart_keys else ()
            if current_dashboard_ids != desired_dashboard_ids:
                self._update("chart", chart_id, {"dashboards": list(desired_dashboard_ids)})

    def reconcile_dashboard_roles(
        self,
        *,
        dashboard_id: int,
        expected_role_ids: tuple[int, ...],
        desired_role_ids: tuple[int, ...],
    ) -> None:
        current_role_ids = self.get_dashboard_role_ids(dashboard_id=dashboard_id)
        if current_role_ids == desired_role_ids:
            return
        if current_role_ids != expected_role_ids:
            raise _client_error("integrity_failure")
        try:
            self._update("dashboard", dashboard_id, {"roles": list(desired_role_ids)})
        except SupersetClientError as error:
            if error.classification in {
                "ambiguous_outcome",
                "throttled",
                "transient_transport",
                "transient_unavailable",
            }:
                raise _client_error("ambiguous_outcome") from None
            raise
        if self.get_dashboard_role_ids(dashboard_id=dashboard_id) != desired_role_ids:
            raise _client_error("ambiguous_outcome")

    def get_dashboard_role_ids(self, *, dashboard_id: int) -> tuple[int, ...]:
        payload = self._request("GET", f"/api/v1/dashboard/{dashboard_id}")
        if (
            not isinstance(payload, dict)
            or "result" not in payload
            or not set(payload).issubset(_DETAIL_RESPONSE_KEYS)
        ):
            raise _client_error("invalid_provider_response")
        result = payload.get("result")
        if not isinstance(result, dict) or "roles" not in result:
            raise _client_error("invalid_provider_response")
        return _role_ids(result.get("roles"))

    def _dashboard_payload(self, definition: BiDashboardDefinition) -> dict[str, object]:
        payload: dict[str, object] = {
            "dashboard_title": definition.title,
            "slug": definition.stable_external_key,
            "published": definition.lifecycle_state == "active",
            "json_metadata": "{}",
            "certification_details": _metadata(
                stable_key=definition.stable_external_key,
                managed_digest=definition.desired_digest,
                lifecycle_state=definition.lifecycle_state,
            ),
        }
        #
        # A dashboard published without a layout is not a dashboard with a default layout: it
        # is a dashboard with none, and Superset drops every chart into its smallest slot in
        # the top-left corner with the rest of the canvas empty. The product composes the
        # layout because the product decided what is on it.
        placed = self._placed_charts(definition)
        if placed:
            payload["position_json"] = _position_json(definition, placed)
        return payload

    def _placed_charts(self, definition: BiDashboardDefinition) -> tuple[tuple[int, str], ...]:
        """This dashboard's charts, in the order its visual intents were declared.

        Resolved from one listing rather than one lookup per intent, and tolerant of a chart
        that is not there: a layout is a presentation of what exists, and refusing to write a
        dashboard because one of its charts is missing would turn a cosmetic gap into a failed
        publication.
        """
        found = {chart_key: chart for chart, chart_key in self._managed_charts(definition)}
        titles = _chart_titles(definition)
        placed: list[tuple[int, str]] = []
        for index in range(len(definition.visual_intents)):
            chart = found.get(_chart_key(definition, index))
            if chart is not None:
                placed.append((_identifier(chart), titles[index]))
        return tuple(placed)

    def _dashboard_after_write(
        self, dashboard_id: int, definition: BiDashboardDefinition
    ) -> SupersetDashboard:
        try:
            observed = self.get_dashboard(stable_key=definition.stable_external_key)
        except SupersetClientError as error:
            if error.classification in {
                "transient_transport",
                "transient_unavailable",
                "throttled",
            }:
                raise _client_error("ambiguous_outcome") from None
            raise
        if observed is None:
            raise _client_error("ambiguous_outcome")
        if observed.external_id != str(dashboard_id):
            raise _client_error("integrity_failure")
        return observed

    def _lookup(self, kind: str, key: str, value: str) -> Mapping[str, object] | None:
        matches = tuple(item for item in self._iter_resources(kind) if item.get(key) == value)
        if len(matches) > 1:
            raise _client_error("integrity_failure")
        return matches[0] if matches else None

    def _lookup_chart(self, *, stable_key: str) -> Mapping[str, object] | None:
        matches = tuple(
            chart
            for chart in self._iter_resources("chart")
            if _chart_stable_key(chart) == stable_key
        )
        if len(matches) > 1:
            raise _client_error("integrity_failure")
        return matches[0] if matches else None

    def _managed_charts(
        self, definition: BiDashboardDefinition
    ) -> tuple[tuple[Mapping[str, object], str], ...]:
        """Every chart this dashboard owns, with the key it owns it by.

        Found by the key the chart carries in its own metadata rather than by its name, because
        its name is what a person reads and a person may rename it. A chart renamed inside
        Superset is still this dashboard's to reconcile; one that merely happens to be called
        what this dashboard would have called it is not.
        """
        prefix = _chart_key_prefix(definition)
        found: list[tuple[Mapping[str, object], str]] = []
        for chart in self._iter_resources("chart"):
            chart_key = _chart_stable_key(chart)
            if chart_key is not None and chart_key.startswith(prefix):
                found.append((chart, chart_key))
        if len({chart_key for _chart, chart_key in found}) != len(found):
            raise _client_error("integrity_failure")
        return tuple(found)

    def _lookup_dataset(self, *, stable_key: str) -> Mapping[str, object] | None:
        matches = tuple(
            dataset
            for dataset in self._iter_resources("dataset")
            if _is_managed_dataset(dataset, stable_key=stable_key)
        )
        if len(matches) > 1:
            raise _client_error("integrity_failure")
        return matches[0] if matches else None

    def _iter_resources(self, kind: str) -> tuple[Mapping[str, object], ...]:
        page = 0
        resources: list[Mapping[str, object]] = []
        while True:
            payload = self._request(
                "GET",
                f"/api/v1/{kind}/",
                params={"q": f"(page:{page},page_size:{self._page_size})"},
            )
            if (
                not isinstance(payload, dict)
                or not {"count", "result"}.issubset(payload)
                or not set(payload).issubset(_LIST_RESPONSE_KEYS)
            ):
                raise _client_error("invalid_provider_response")
            count = payload.get("count")
            result = payload.get("result")
            if type(count) is not int or count < 0 or not isinstance(result, list):
                raise _client_error("invalid_provider_response")
            for candidate in result:
                if not isinstance(candidate, dict):
                    raise _client_error("invalid_provider_response")
                _identifier(candidate)
                resources.append(candidate)
            if len(resources) >= count:
                if len(resources) != count:
                    raise _client_error("invalid_provider_response")
                return tuple(resources)
            if not result:
                raise _client_error("invalid_provider_response")
            page += 1

    def _create(self, kind: str, payload: Mapping[str, object]) -> int:
        response = self._request("POST", f"/api/v1/{kind}/", json=payload, mutation=True)
        return _write_identifier(response)

    def _update(self, kind: str, resource_id: int, payload: Mapping[str, object]) -> None:
        response = self._request(
            "PUT", f"/api/v1/{kind}/{resource_id}", json=payload, mutation=True
        )
        if _write_identifier(response) != resource_id:
            raise _client_error("invalid_provider_response")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: Mapping[str, str] | None = None,
        mutation: bool = False,
    ) -> object:
        try:
            response = self._transport.request(
                method=method,
                url=self._credentials.base_url + path,
                headers=self._headers(mutation=mutation),
                json=json,
                params=params,
            )
        except (TimeoutError, httpx.TimeoutException):
            raise _client_error(
                "ambiguous_outcome" if mutation else "transient_transport"
            ) from None
        except (OSError, httpx.TransportError):
            raise _client_error(
                "ambiguous_outcome" if mutation else "transient_transport"
            ) from None
        except SupersetClientError:
            raise
        except Exception:
            raise _client_error("invalid_provider_response") from None
        return _successful_payload(response)

    def _headers(self, *, mutation: bool) -> Mapping[str, str]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if mutation:
            headers["X-CSRFToken"] = self._csrf_token()
            headers["Referer"] = self._credentials.base_url + "/"
        return headers

    def _token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        try:
            response = self._transport.request(
                method="POST",
                url=self._credentials.base_url + "/api/v1/security/login",
                headers={},
                json={
                    "username": self._credentials.username,
                    "password": self._credentials.password,
                    "provider": "db",
                    "refresh": True,
                },
            )
        except (TimeoutError, httpx.TimeoutException, OSError, httpx.TransportError):
            raise _client_error("transient_transport") from None
        except Exception:
            raise _client_error("invalid_provider_response") from None
        payload = _successful_payload(response)
        if (
            not isinstance(payload, dict)
            or "access_token" not in payload
            or not set(payload).issubset({"access_token", "refresh_token"})
        ):
            raise _client_error("invalid_provider_response")
        token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise _client_error("invalid_provider_response")
        if refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token):
            raise _client_error("invalid_provider_response")
        self._access_token = token
        return token

    def _csrf_token(self) -> str:
        if self._csrf_token_value is not None:
            return self._csrf_token_value
        try:
            response = self._transport.request(
                method="GET",
                url=self._credentials.base_url + "/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        except (TimeoutError, httpx.TimeoutException, OSError, httpx.TransportError):
            raise _client_error("transient_transport") from None
        except Exception:
            raise _client_error("invalid_provider_response") from None
        payload = _successful_payload(response)
        if not isinstance(payload, dict) or set(payload) != {"result"}:
            raise _client_error("invalid_provider_response")
        token = payload.get("result")
        if not isinstance(token, str) or not token:
            raise _client_error("invalid_provider_response")
        self._csrf_token_value = token
        return token


class CredentialScopedSupersetProvider:
    def __init__(
        self,
        *,
        resolver: SupersetCredentialResolver,
        transport: SupersetHttpTransport | None = None,
        provider_version: str = "4.1.1",
        page_size: int = _PAGE_SIZE,
    ) -> None:
        self._resolver = resolver
        self._transport = transport or HttpxSupersetTransport()
        self._provider_version = provider_version
        self._page_size = page_size

    @property
    def provider_kind(self) -> Literal["superset"]:
        return "superset"

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        from .provider import SupersetProvider

        try:
            credentials = self._resolver.resolve(secret_reference=definition.connection_secret_ref)
        except (OSError, TimeoutError):
            raise ProviderError(
                "Superset credential resolution failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "Superset credential resolution failed", "permanent_configuration"
            ) from None
        client = HttpSupersetClient(
            credentials=credentials,
            transport=self._transport,
            page_size=self._page_size,
        )
        return SupersetProvider(
            client,
            provider_version=self._provider_version,
        ).apply(definition)


# The governed intent is not Superset's vocabulary. `bar` and `line` name what the contract asked
# for; these name the plugin that draws it. Passing the intent through as a `viz_type` produced a
# chart Superset had no plugin for, which it renders as an empty one.
_VIZ_TYPES: Final[dict[BiVisualIntent, str]] = {
    "bar": "echarts_timeseries_bar",
    "line": "echarts_timeseries_line",
    "number": "big_number_total",
    "table": "table",
}
_ROW_LIMIT: Final = 10_000


def _superset_metric(projection: BiMetricProjection) -> dict[str, object]:
    """One governed metric as Superset's adhoc metric, aggregating the bound column.

    `SIMPLE` rather than a SQL expression: the aggregate and the column are separate governed
    facts, and handing Superset a string it parses would make this the one place a column name
    reaches a query without the server knowing which column it is.
    """
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": projection.column_name},
        "aggregate": projection.aggregate.upper(),
        # The legend and the axis of a published chart are read by the stakeholder who asked the
        # question, not by whoever bound the column, so they carry the metric as the console's
        # own result table writes it rather than the identifier underneath.
        "label": display_label(projection.output_name),
        "hasCustomLabel": True,
    }


def _chart_titles(definition: BiDashboardDefinition) -> tuple[str, ...]:
    """What each chart of this dashboard is called, in the order its intents are declared.

    Named after what it draws rather than after the key it is reconciled by. The key is a digest
    with an index on it, which is the right thing for finding a chart again and the wrong thing
    to put above it on a dashboard someone opens to read an answer.

    Computed for the whole dashboard at once because two intents over the same metric and
    dimension describe themselves identically -- a bar and a line of the same series -- and two
    charts under one title on one dashboard read as the same chart drawn twice.
    """
    described = tuple(
        _described_series(intent, definition.metric_projections, definition.dimension_projections)
        for intent in definition.visual_intents
    )
    return tuple(
        description if described.count(description) == 1 else f"{description} ({intent})"
        for description, intent in zip(described, definition.visual_intents, strict=True)
    )


def _described_series(
    visual_intent: BiVisualIntent,
    metrics: tuple[BiMetricProjection, ...],
    dimensions: tuple[BiDimensionProjection, ...],
) -> str:
    """The metrics a chart draws, and the dimension it draws them along where it has one."""
    drawn = ", ".join(display_label(projection.output_name) for projection in metrics)
    plotted = visual_intent != "number" and bool(dimensions)
    if not plotted:
        return drawn
    return f"{drawn} by {display_label(dimensions[0].output_name)}"


def _form_data(
    visual_intent: BiVisualIntent,
    definition: BiDashboardDefinition,
    *,
    dataset_id: int,
) -> dict[str, object]:
    """What Superset queries this chart with: the governed metric, by the governed dimension.

    Written per plugin because each reads a different shape -- a big number takes one `metric`
    where the others take `metrics`, and a table groups where the time-series charts take an
    `x_axis`. The shapes here are the ones a running Superset was confirmed to render; a plugin
    given the wrong one draws nothing and reports an empty query rather than an error.
    """
    metrics = [_superset_metric(projection) for projection in definition.metric_projections]
    dimensions = [projection.column_name for projection in definition.dimension_projections]
    shared: dict[str, object] = {
        "datasource": f"{dataset_id}__table",
        "viz_type": _VIZ_TYPES[visual_intent],
    }
    if visual_intent == "number":
        return {**shared, "metric": metrics[0], "subheader": definition.title}
    if visual_intent == "table":
        # Grouped by the dimensions there are, and over the whole relation when there are none,
        # which Superset renders as the single row that is.
        return {
            **shared,
            "query_mode": "aggregate",
            "groupby": dimensions,
            "metrics": metrics,
            "row_limit": _ROW_LIMIT,
        }
    # A bar and a line are drawn along an axis. `DashboardDesiredState` refuses either without a
    # dimension, so reaching here with none would be an artifact that never should have stored.
    return {
        **shared,
        "x_axis": dimensions[0],
        "metrics": metrics,
        "groupby": [],
        "row_limit": _ROW_LIMIT,
    }


def _chart_key_prefix(definition: BiDashboardDefinition) -> str:
    return f"{definition.stable_external_key}-chart-"


def _chart_key(definition: BiDashboardDefinition, index: int) -> str:
    return f"{_chart_key_prefix(definition)}{index:03d}"


def _chart_stable_key(resource: Mapping[str, object]) -> str | None:
    """The governed key a chart carries, or `None` for a chart this deployment does not manage.

    Superset's own charts have a free-text description and people write in it, so anything that
    is not the metadata this provider writes is somebody else's chart rather than a corrupt one.
    """
    description = resource.get("description")
    if not isinstance(description, str) or not description:
        return None
    try:
        metadata = json.loads(description)
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    stable_key = metadata.get("stable_key")
    return stable_key if isinstance(stable_key, str) and stable_key else None


# Superset's grid is twelve columns across, and a unit of height is eight pixels.
#
# A lone chart is 400 pixels tall, which is a chart. At 464 it sat in the corner of an empty
# canvas; at 720 three days of a daily series became three slabs the height of a door. Height
# is not the thing that makes a single answer fill a dashboard -- a chart that reads at a
# glance is -- and a canvas with air under it is a canvas, not a mistake.
_GRID_COLUMNS: Final = 12
_ROW_HEIGHT_UNITS: Final = 50
_ROW_HEIGHT_UNITS_PAIRED: Final = 44


def _position_json(definition: BiDashboardDefinition, placed: tuple[tuple[int, str], ...]) -> str:
    """Where each chart sits on the published dashboard.

    One chart takes the width, because a single panel in the corner of an empty canvas reads
    as a dashboard that failed to finish rather than one with a single answer on it. More than
    one pairs across the grid, which is as much density as a governed dashboard earns: these
    are the visual intents one contract admitted, not a workspace somebody is arranging.
    """
    per_row = 1 if len(placed) == 1 else 2
    width = _GRID_COLUMNS // per_row
    height = _ROW_HEIGHT_UNITS if per_row == 1 else _ROW_HEIGHT_UNITS_PAIRED
    grid_children: list[str] = []
    layout: dict[str, object] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": definition.title}},
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "parents": ["ROOT_ID"],
            "children": grid_children,
        },
    }
    for index, (chart_id, title) in enumerate(placed):
        row_id = f"ROW-{index // per_row}"
        row = layout.get(row_id)
        if row is None:
            row = {
                "type": "ROW",
                "id": row_id,
                "parents": ["ROOT_ID", "GRID_ID"],
                "children": [],
                "meta": {"background": "BACKGROUND_TRANSPARENT"},
            }
            layout[row_id] = row
            grid_children.append(row_id)
        node_id = f"CHART-{index:03d}"
        layout[node_id] = {
            "type": "CHART",
            "id": node_id,
            "parents": ["ROOT_ID", "GRID_ID", row_id],
            "children": [],
            "meta": {
                "chartId": chart_id,
                "width": width,
                "height": height,
                "sliceName": title,
            },
        }
        children = row["children"]  # type: ignore[index]
        assert isinstance(children, list)
        children.append(node_id)
    return _canonical_json(layout)


def _database_key(definition: BiDashboardDefinition) -> str:
    return (
        "pm-database-"
        + digest(
            {
                "domain": "heinzel-superset-database-v1",
                "tenant_id": definition.tenant_id,
                "connection_secret_ref": definition.connection_secret_ref,
            }
        )[:24]
    )


def _metadata(
    *,
    stable_key: str,
    managed_digest: str,
    lifecycle_state: str,
    generation: int | None = None,
) -> str:
    value: dict[str, object] = {
        "stable_key": stable_key,
        "managed_digest": managed_digest,
        "lifecycle_state": lifecycle_state,
    }
    if generation is not None:
        value["generation"] = generation
    return _canonical_json(value)


def _canonical_json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _parsed_metadata(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise _client_error("invalid_provider_response") from None
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise _client_error("invalid_provider_response")
    return parsed


def _description(resource: Mapping[str, object]) -> str:
    value = resource.get("description")
    if not isinstance(value, str):
        raise _client_error("invalid_provider_response")
    return value


def _is_managed_dataset(resource: Mapping[str, object], *, stable_key: str) -> bool:
    description = resource.get("description")
    if not isinstance(description, str):
        return False
    try:
        metadata = json.loads(description)
    except ValueError:
        return False
    if not isinstance(metadata, dict):
        return False
    if metadata.get("stable_key") != stable_key:
        return False
    managed_digest = metadata.get("managed_digest")
    generation = metadata.get("generation")
    if (
        set(metadata) != {"stable_key", "managed_digest", "lifecycle_state", "generation"}
        or not isinstance(managed_digest, str)
        or len(managed_digest) != 64
        or any(character not in "0123456789abcdef" for character in managed_digest)
        or metadata.get("lifecycle_state") != "active"
        or type(generation) is not int
        or generation < 1
    ):
        raise _client_error("invalid_provider_response")
    return True


def _lifecycle_state(value: object) -> BiLifecycleState:
    if value == "active":
        return "active"
    if value == "archived":
        return "archived"
    raise _client_error("invalid_provider_response")


def _identifier(resource: Mapping[str, object]) -> int:
    value = resource.get("id")
    if type(value) is not int or value < 1:
        raise _client_error("invalid_provider_response")
    return value


def _write_identifier(payload: object) -> int:
    if (
        not isinstance(payload, dict)
        or "id" not in payload
        or not set(payload).issubset(_WRITE_RESPONSE_KEYS)
    ):
        raise _client_error("invalid_provider_response")
    return _identifier(payload)


def _external_identifier(value: str) -> int:
    try:
        identifier = int(value)
    except ValueError:
        raise _client_error("invalid_provider_response") from None
    if identifier < 1:
        raise _client_error("invalid_provider_response")
    return identifier


def _role_ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise _client_error("invalid_provider_response")
    role_ids: list[int] = []
    for item in value:
        role_id = item if type(item) is int else item.get("id") if isinstance(item, dict) else None
        if type(role_id) is not int or role_id < 1:
            raise _client_error("invalid_provider_response")
        role_ids.append(role_id)
    canonical = tuple(sorted(role_ids))
    if len(canonical) != len(set(canonical)):
        raise _client_error("invalid_provider_response")
    return canonical


def _successful_payload(response: SupersetHttpResponse) -> object:
    status = response.status_code
    if 200 <= status < 300:
        return response.payload
    if status in {401, 403}:
        raise _client_error("authorization_denied")
    if status == 404:
        raise _client_error("statement_rejected")
    if status == 409:
        raise _client_error("integrity_failure")
    if status == 429:
        raise _client_error("throttled")
    if status >= 500:
        raise _client_error("transient_unavailable")
    if 400 <= status < 500:
        raise _client_error("statement_rejected")
    raise _client_error("invalid_provider_response")


def _client_error(classification: ProviderErrorClassification) -> SupersetClientError:
    return SupersetClientError(classification=classification)
