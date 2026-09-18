from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import (
    BiApplyResult,
    BiDashboardDefinition,
    BiDataset,
    BiLifecycleState,
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
            database_id = _identifier(database)

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
        for index, visual_intent in enumerate(definition.visual_intents):
            chart_key = f"{definition.stable_external_key}-chart-{index:03d}"
            expected_chart_keys.add(chart_key)
            chart_metadata = _metadata(
                stable_key=chart_key,
                managed_digest=definition.desired_digest,
                lifecycle_state="active",
            )
            chart_payload: dict[str, object] = {
                "slice_name": chart_key,
                "viz_type": visual_intent,
                "datasource_id": dataset_id,
                "datasource_type": "table",
                "description": chart_metadata,
                "params": _canonical_json({"heinzel": json.loads(chart_metadata)}),
            }
            chart = self._lookup("chart", "slice_name", chart_key)
            if chart is None:
                self._create("chart", chart_payload)
            elif _description(chart) != chart_metadata:
                self._update("chart", _identifier(chart), chart_payload)

        for chart in self._iter_resources("chart"):
            candidate_chart_key = chart.get("slice_name")
            if (
                isinstance(candidate_chart_key, str)
                and candidate_chart_key.startswith(f"{definition.stable_external_key}-chart-")
                and candidate_chart_key not in expected_chart_keys
            ):
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
        for chart in self._iter_resources("chart"):
            chart_key = chart.get("slice_name")
            if isinstance(chart_key, str) and chart_key.startswith(
                f"{definition.stable_external_key}-chart-"
            ):
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
            f"{definition.stable_external_key}-chart-{index:03d}"
            for index, _visual_intent in enumerate(definition.visual_intents)
        }
        managed_charts = tuple(
            chart
            for chart in self._iter_resources("chart")
            if isinstance(chart.get("slice_name"), str)
            and str(chart["slice_name"]).startswith(f"{definition.stable_external_key}-chart-")
        )
        observed_keys = {str(chart["slice_name"]) for chart in managed_charts}
        if not expected_chart_keys.issubset(observed_keys):
            raise _client_error("integrity_failure")
        for chart in managed_charts:
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
            desired_dashboard_ids = (
                (dashboard_id,) if chart.get("slice_name") in expected_chart_keys else ()
            )
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

    @staticmethod
    def _dashboard_payload(definition: BiDashboardDefinition) -> dict[str, object]:
        return {
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
