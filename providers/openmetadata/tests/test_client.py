from __future__ import annotations

import traceback
from collections.abc import Callable

import pytest
from httpx import ConnectError, ReadTimeout, Response
from pillarmesh_contract_model import digest
from pillarmesh_provider_openmetadata import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    ClassificationPayload,
    GlossaryTermPayload,
    LineagePayload,
    OpenMetadataClient,
    OpenMetadataSettings,
)
from pillarmesh_provider_openmetadata.client import (
    _description_metadata,
    _description_with_metadata,
    _OpenMetadataCredentials,
)
from pydantic import SecretStr, ValidationError

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_RUNTIME_ID = "00000000-0000-4000-8000-000000000002"
_TERM_ID = "00000000-0000-4000-8000-000000000003"
_PARENT_TERM_ID = "00000000-0000-4000-8000-000000000004"
_CLASSIFICATION_ID = "00000000-0000-4000-8000-000000000005"
_WRONG_ENTITY_ID = "00000000-0000-4000-8000-000000000006"
_DATA_PRODUCT_ID = "00000000-0000-4000-8000-000000000007"
_DOMAIN_ID = "00000000-0000-4000-8000-000000000008"
_RELATED_TERM_ID = "00000000-0000-4000-8000-000000000009"
_UP_VOTER_ID = "00000000-0000-4000-8000-000000000010"
_DOWN_VOTER_ID = "00000000-0000-4000-8000-000000000011"
_OWNER_ID = "00000000-0000-4000-8000-000000000012"
_REVIEWER_ID = "00000000-0000-4000-8000-000000000013"
_TEAM_ID = "00000000-0000-4000-8000-000000000014"
_PERSONA_ID = "00000000-0000-4000-8000-000000000015"
_PIPELINE_ID = "00000000-0000-4000-8000-000000000016"
_TENANT_A_TERM_ID = "00000000-0000-4000-8000-000000000017"
_TENANT_B_TERM_ID = "00000000-0000-4000-8000-000000000018"
_NAMESPACE_NAME = "pm-968bb22cb2fc16a3bacfbea1"
_RUNTIME_NAME = "pm-a607919ea6e8047b38e4d6f9"
_TERM_NAME = "pm-0cae9afc772ff0a6e37098f6"
_CLASSIFICATION_NAME = "pm-754ba436ad8a765e2b2a56ff"
_TAG_NAME = "pm-6b815d4afc56e4ec98d97fe2"
_RUNTIME_POLICY_NAME = "pm-47b7aa5d7c9a18ad6374d3ff"
_RUNTIME_ROLE_NAME = "pm-9e96921a9419be296013e46e"
_CAPTURED_GLOSSARY_RESPONSE: dict[str, object] = {
    "id": _NAMESPACE_ID,
    "name": _NAMESPACE_NAME,
    "fullyQualifiedName": _NAMESPACE_NAME,
    "displayName": "PillarMesh namespace",
    "description": "PillarMesh managed tenant catalog namespace.",
    "version": 0.1,
    "updatedAt": 1_755_663_200_000,
    "updatedBy": "admin",
    "href": f"http://127.0.0.1:8585/api/v1/glossaries/{_NAMESPACE_ID}",
    "deleted": False,
    "provider": "system",
    "mutuallyExclusive": False,
    "entityStatus": "Approved",
}
_POLICY_ID = "00000000-0000-4000-8000-000000000101"
_ROLE_ID = "00000000-0000-4000-8000-000000000102"
_TAG_ID = "00000000-0000-4000-8000-000000000103"
_CAPTURED_CHANGE_DESCRIPTION: dict[str, object] = {
    "fieldsAdded": [{"name": "displayName", "newValue": {"locale": "en"}}],
    "fieldsUpdated": [{"name": "description", "oldValue": "previous", "newValue": "current"}],
    "fieldsDeleted": [{"name": "owners", "oldValue": []}],
    "previousVersion": 0.1,
    "changeSummary": {
        "description": {
            "changeSource": "Manual",
            "changedBy": "admin",
            "changedAt": 1_755_663_200_000,
        }
    },
}
_CAPTURED_INCREMENTAL_CHANGE_DESCRIPTION: dict[str, object] = {
    "fieldsUpdated": [{"name": "displayName", "oldValue": "old", "newValue": "new"}],
    "previousVersion": 0.1,
    "changeSummary": {
        "displayName": {
            "changeSource": "Automated",
            "changedBy": "admin",
            "changedAt": 1_755_663_200_000,
        }
    },
}
_CAPTURED_GLOSSARY_HISTORY_RESPONSE: dict[str, object] = _CAPTURED_GLOSSARY_RESPONSE | {
    "changeDescription": _CAPTURED_CHANGE_DESCRIPTION,
    "incrementalChangeDescription": _CAPTURED_INCREMENTAL_CHANGE_DESCRIPTION,
}
_CAPTURED_GLOSSARY_TERM_RESPONSE: dict[str, object] = {
    "id": _TERM_ID,
    "name": _TERM_NAME,
    "fullyQualifiedName": f"{_NAMESPACE_NAME}.{_TERM_NAME}",
    "glossary": {
        "id": _NAMESPACE_ID,
        "type": "glossary",
        "name": _NAMESPACE_NAME,
        "fullyQualifiedName": _NAMESPACE_NAME,
    },
    "description": "PillarMesh managed glossary term.",
    "changeDescription": _CAPTURED_CHANGE_DESCRIPTION,
    "incrementalChangeDescription": _CAPTURED_INCREMENTAL_CHANGE_DESCRIPTION,
    "conceptMappings": [
        {
            "conceptIri": "https://example.invalid/concepts/term",
            "mappingType": "EXACT_MATCH",
            "schemeIri": "urn:example:concept-scheme",
            "source": "example",
        }
    ],
    "dataProducts": [{"id": _DATA_PRODUCT_ID, "type": "dataProduct", "name": "product"}],
    "domains": [{"id": _DOMAIN_ID, "type": "domain", "name": "domain"}],
    "relatedTerms": [
        {
            "relationType": "broader",
            "term": {"id": _RELATED_TERM_ID, "type": "glossaryTerm", "name": "related"},
        }
    ],
    "references": [{"name": "Example glossary", "endpoint": "https://example.invalid"}],
    "tags": [
        {
            "tagFQN": "classification.tag",
            "source": "Classification",
            "labelType": "Manual",
            "state": "Confirmed",
            "appliedAt": 1_755_663_200_000,
        }
    ],
    "votes": {
        "upVotes": 2,
        "downVotes": 1,
        "upVoters": [{"id": _UP_VOTER_ID, "type": "user", "name": "up-voter"}],
        "downVoters": [{"id": _DOWN_VOTER_ID, "type": "user", "name": "down-voter"}],
    },
}
_CAPTURED_TAG_RESPONSE: dict[str, object] = {
    "id": _TAG_ID,
    "name": _TAG_NAME,
    "description": "PillarMesh managed classification tag.",
    "autoClassificationEnabled": True,
    "autoClassificationPriority": 50,
    "classification": {
        "id": _CLASSIFICATION_ID,
        "type": "classification",
        "name": _CLASSIFICATION_NAME,
    },
    "deleted": False,
    "deprecated": False,
    "disabled": False,
    "domains": [{"id": _DOMAIN_ID, "type": "domain", "name": "domain"}],
    "entityStatus": "Approved",
    "fullyQualifiedName": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}",
    "href": f"https://example.invalid/api/v1/tags/{_TAG_ID}",
    "mutuallyExclusive": False,
    "owners": [{"id": _OWNER_ID, "type": "user", "name": "owner"}],
    "provider": "system",
    "recognizers": [
        {
            "name": "exact-term",
            "enabled": True,
            "isSystemDefault": False,
            "recognizerConfig": {
                "type": "exact_terms",
                "exactTerms": ["synthetic"],
                "supportedLanguage": "en",
                "regexFlags": {"dotAll": True, "multiline": False, "ignoreCase": True},
            },
            "confidenceThreshold": 0.6,
            "exceptionList": [{"entityLink": "<#E::table::service.database.schema.table>"}],
            "version": 0.1,
            "updatedAt": 1_755_663_200_000,
            "updatedBy": "admin",
            "target": "content",
        }
    ],
    "reviewers": [{"id": _REVIEWER_ID, "type": "user", "name": "reviewer"}],
    "updatedAt": 1_755_663_200_000,
    "updatedBy": "admin",
    "version": 0.1,
}
_SENSITIVE_RESPONSE_VALUE = "test-only-sensitive-response-value"
_SENSITIVE_LOGIN_VALUE = "test-only-sensitive-login-value"
_SENSITIVE_TRANSPORT_VALUE = "test-only-sensitive-transport-value"
_SENSITIVE_JSON_VALUE = "test-only-sensitive-json-value"
_SENSITIVE_AUTHENTICATION_VALUE = "test-only-sensitive-authentication-value"
_LINEAGE_FROM_ID = "11111111-1111-4111-8111-111111111111"
_LINEAGE_TO_ID = "22222222-2222-4222-8222-222222222222"
_LINEAGE_DESCRIPTION = _description_with_metadata("validation", {"producer_ref": "validation"})
_LINEAGE_IDENTIFIER = (
    '{"fromEntity":{"id":"11111111-1111-4111-8111-111111111111",'
    '"type":"glossaryTerm"},"toEntity":{"id":"22222222-2222-4222-8222-222222222222",'
    '"type":"glossaryTerm"},"description":"validation\\n\\nPillarMesh metadata v1: '
    'eyJwcm9kdWNlcl9yZWYiOiJ2YWxpZGF0aW9uIn0."}'
)
_CAPTURED_USER_RESPONSE: dict[str, object] = {
    "id": _RUNTIME_ID,
    "name": _RUNTIME_NAME,
    "allowImpersonation": False,
    "authenticationMechanism": {
        "authType": "BASIC",
        "config": {"password": _SENSITIVE_RESPONSE_VALUE},
    },
    "teams": [{"id": _TEAM_ID, "type": "team", "name": "team"}],
    "inheritedRoles": [{"id": _ROLE_ID, "type": "role", "name": "role"}],
    "inheritedPersonas": [{"id": _PERSONA_ID, "type": "persona", "name": "persona"}],
    "domains": [{"id": _DOMAIN_ID, "type": "domain", "name": "domain"}],
    "personaPreferences": [],
}


def test_governed_metadata_uses_a_provider_durable_markdown_trailer() -> None:
    description = _description_with_metadata("Customer definition.", {"owner_ref": "finance"})

    visible, metadata = _description_metadata(description, required=frozenset({"owner_ref"}))

    assert "<!--" not in description
    assert "`" not in description
    assert "PillarMesh metadata v1: " in description
    assert description.endswith(".")
    assert visible == "Customer definition."
    assert metadata == {"owner_ref": "finance"}


_CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE: dict[str, object] = _CAPTURED_GLOSSARY_RESPONSE | {
    "owners": [{"id": _OWNER_ID, "type": "user", "name": "owner"}],
    "domains": [],
    "dataProducts": [],
    "votes": {"upVotes": 0, "downVotes": 0},
}


class _AuthenticatedTransport:
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        raise AssertionError(f"unexpected {method} {url}")


class FailingTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> None:
        raise ConnectError("refused", request=None)


class TextHealthTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return Response(200, text="OK")


class VersionTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        assert url.endswith("/api/v1/system/version")
        return _response(
            200,
            {
                "version": "1.13.3",
                "revision": "255f6694913b84797064a42859cda3f2a3425dc6",
                "timestamp": 1785479611993,
            },
        )


class SearchDocumentTransport(_AuthenticatedTransport):
    def __init__(self, *, found_identifier: str = _TERM_ID) -> None:
        self._found_identifier = found_identifier

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaryTerms/name/" in url:
            return _response(200, _CAPTURED_GLOSSARY_TERM_RESPONSE)
        if "/api/v1/search/get/glossary_term_search_index/doc/" in url:
            return _response(
                200,
                {
                    "id": self._found_identifier,
                    "name": _TERM_NAME,
                    "fullyQualifiedName": f"{_NAMESPACE_NAME}.{_TERM_NAME}",
                },
            )
        raise AssertionError(f"unexpected GET {url}")


class LoginTransport:
    def __init__(self) -> None:
        self.login_payload: object = None
        self.health_headers: object = None

    def get(self, url: str, **kwargs: object) -> Response:
        self.health_headers = kwargs["headers"]
        return Response(200, text="OK")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        assert method == "POST"
        assert url.endswith("/api/v1/users/login")
        self.login_payload = kwargs["json"]
        return _response(
            200,
            {
                "accessToken": "acceptance-token",
                "refreshToken": "refresh-token",
                "tokenType": "Bearer ",
                "expiryDuration": 3600,
            },
        )


class PasswordRotationTransport:
    def __init__(self, *, change_password_status_code: int = 200) -> None:
        self.change_password_status_code = change_password_status_code
        self.change_requests: list[tuple[str, str, object, object]] = []
        self.health_headers: list[object] = []
        self.login_payloads: list[object] = []

    def get(self, url: str, **kwargs: object) -> Response:
        assert url.endswith("/api/v1/system/health")
        self.health_headers.append(kwargs["headers"])
        return Response(200, text="OK")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            self.login_payloads.append(kwargs["json"])
            return _response(
                200,
                {
                    "accessToken": f"rotation-token-{len(self.login_payloads)}",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        self.change_requests.append((method, url, kwargs["json"], kwargs["headers"]))
        if self.change_password_status_code != 200:
            return _response(self.change_password_status_code, {"message": "rejected"})
        return Response(200, text="Password Updated Successfully")


class NamespaceCreationTransport:
    def __init__(self) -> None:
        self.payload: object = None

    def get(self, url: str, **kwargs: object) -> Response:
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        assert method == "POST"
        assert url.endswith("/api/v1/glossaries")
        self.payload = kwargs["json"]
        assert isinstance(self.payload, dict)
        return _response(
            201,
            {
                "id": _NAMESPACE_ID,
                "name": self.payload["name"],
                "fullyQualifiedName": self.payload["name"],
            },
        )


class CommittedNamespaceReplayTransport(_AuthenticatedTransport):
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.post_attempts = 0
        self.namespace: dict[str, str] | None = None

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url and self.namespace is not None:
            return _response(200, self.namespace)
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        assert method == "POST"
        assert url.endswith("/api/v1/glossaries")
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        self.post_attempts += 1
        self.namespace = {
            "id": _NAMESPACE_ID,
            "name": str(payload["name"]),
            "fullyQualifiedName": str(payload["name"]),
        }
        if self.post_attempts == 1 and self.failure == "timeout":
            raise ReadTimeout("committed request timed out", request=None)
        if self.post_attempts == 1 and self.failure == "conflict":
            return _response(409, {"message": "already exists"})
        return _response(201, self.namespace)


class GlossaryTermConvergenceTransport(_AuthenticatedTransport):
    def __init__(self) -> None:
        self.patch_failure: str | None = None
        self.term_payload: dict[str, object] | None = None
        self.post_attempts = 0
        self.patch_requests: list[tuple[str, object, object]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": _NAMESPACE_NAME,
                    "fullyQualifiedName": _NAMESPACE_NAME,
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(200, {"id": _RUNTIME_ID, "name": _RUNTIME_NAME})
        if "/api/v1/glossaryTerms/name/" in url and self.term_payload is not None:
            term_response = self._term_response()
            if kwargs.get("params") != {"fields": "owners"}:
                term_response.pop("owners")
            return _response(200, term_response)
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/api/v1/glossaryTerms"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self.post_attempts += 1
            self.term_payload = payload.copy()
            return _response(201, self._term_response())
        if method == "PATCH" and url.endswith(f"/api/v1/glossaryTerms/{_TERM_ID}"):
            patch = kwargs["json"]
            assert isinstance(patch, list)
            assert self.term_payload is not None
            self.patch_requests.append((url, patch, kwargs["headers"]))
            if self.patch_failure == "conflict":
                return _response(409, {"message": "concurrent update"})
            for operation in patch:
                assert isinstance(operation, dict)
                path = operation["path"]
                assert isinstance(path, str)
                self.term_payload[path.removeprefix("/")] = operation["value"]
            if self.patch_failure == "timeout":
                raise ReadTimeout("committed request timed out", request=None)
            if self.patch_failure == "committed-conflict":
                return _response(409, {"message": "concurrent update"})
            response = _response(200, self._term_response())
            if self.patch_failure == "stale-readback":
                self.term_payload["description"] = "Original definition."
            return response
        raise AssertionError(f"unexpected {method} {url}")

    def _term_response(self) -> dict[str, object]:
        assert self.term_payload is not None
        return {
            "id": _TERM_ID,
            "name": self.term_payload["name"],
            "fullyQualifiedName": f"{_NAMESPACE_NAME}.{self.term_payload['name']}",
            "displayName": self.term_payload["displayName"],
            "description": self.term_payload["description"],
            "glossary": {
                "id": _NAMESPACE_ID,
                "type": "glossary",
                "name": _NAMESPACE_NAME,
            },
            "owners": self.term_payload["owners"],
        }


class NonCanonicalNamespaceCreationTransport(NamespaceCreationTransport):
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        response = super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/api/v1/glossaries"):
            return _response(201, {"id": "namespace-id", "name": _NAMESPACE_NAME})
        return response


class CapturedGlossaryResponseTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        assert url.endswith("/api/v1/glossaries/name/pm-968bb22cb2fc16a3bacfbea1")
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        assert method == "POST"
        assert url.endswith("/api/v1/glossaries")
        return _response(201, _CAPTURED_GLOSSARY_RESPONSE)


class WrongCreateIdentityTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        return _response(
            201,
            {
                "id": _NAMESPACE_ID,
                "name": _SENSITIVE_RESPONSE_VALUE,
                "fullyQualifiedName": _SENSITIVE_RESPONSE_VALUE,
            },
        )


class WrongGetIdentityTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            {
                "id": _NAMESPACE_ID,
                "name": _SENSITIVE_RESPONSE_VALUE,
                "fullyQualifiedName": _SENSITIVE_RESPONSE_VALUE,
            },
        )


class CapturedGlossaryHistoryGetTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        assert "/api/v1/glossaries/name/" in url
        return _response(200, _CAPTURED_GLOSSARY_HISTORY_RESPONSE)


class UnknownFieldChangeMemberTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE
            | {
                "changeDescription": {
                    "fieldsUpdated": [{"name": "description", "unexpected": "must not be accepted"}]
                }
            },
        )


class WrongChangeSummaryTimestampTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE
            | {
                "changeDescription": {
                    "changeSummary": {
                        "description": {
                            "changedAt": "1755663200000",
                        }
                    }
                }
            },
        )


class NonCanonicalEntityReferenceTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE
            | {"domains": [{"id": "not-a-canonical-uuid", "type": "domain"}]},
        )


class CapturedGlossaryTermResponseTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": _NAMESPACE_NAME,
                    "fullyQualifiedName": _NAMESPACE_NAME,
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(200, {"id": _RUNTIME_ID, "name": _RUNTIME_NAME})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        assert method == "POST"
        assert url.endswith("/api/v1/glossaryTerms")
        return _response(201, _CAPTURED_GLOSSARY_TERM_RESPONSE)


class WrongGlossaryTermRelationshipTransport(_AuthenticatedTransport):
    def __init__(self, *, relationship: str) -> None:
        self.relationship = relationship

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": "pm-968bb22cb2fc16a3bacfbea1",
                    "fullyQualifiedName": "pm-968bb22cb2fc16a3bacfbea1",
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(
                200,
                {"id": _RUNTIME_ID, "name": "pm-a607919ea6e8047b38e4d6f9"},
            )
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        response: dict[str, object] = {
            "id": _TERM_ID,
            "name": payload["name"],
            "fullyQualifiedName": f"{payload['glossary']}.{payload['name']}",
            "glossary": {
                "id": _NAMESPACE_ID,
                "type": "glossary",
                "name": payload["glossary"],
                "fullyQualifiedName": payload["glossary"],
            },
        }
        if self.relationship == "wrong-glossary":
            response["glossary"] = {
                "id": _NAMESPACE_ID,
                "type": "glossary",
                "name": "another-tenant-namespace",
                "fullyQualifiedName": "another-tenant-namespace",
            }
        if self.relationship == "unexpected-parent":
            response["parent"] = {
                "id": _PARENT_TERM_ID,
                "type": "glossaryTerm",
                "name": "unexpected-parent",
            }
        return _response(201, response)


class WrongGetGlossaryTermRelationshipTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(404, {"message": "missing"})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(
            200,
            {
                "id": _TERM_ID,
                "name": "pm-0cae9afc772ff0a6e37098f6",
                "fullyQualifiedName": ("pm-968bb22cb2fc16a3bacfbea1.pm-0cae9afc772ff0a6e37098f6"),
                "glossary": {
                    "id": _NAMESPACE_ID,
                    "type": "glossary",
                    "name": "another-tenant-namespace",
                },
            },
        )


class UnknownConceptMappingFieldTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(404, {"message": "missing"})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(
            200,
            _CAPTURED_GLOSSARY_TERM_RESPONSE
            | {
                "conceptMappings": [
                    {
                        "conceptIri": "https://example.invalid/concepts/term",
                        "mappingType": "EXACT_MATCH",
                        "unexpected": "must not be accepted",
                    }
                ]
            },
        )


class WrongConceptMappingTypeTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(404, {"message": "missing"})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(
            200,
            _CAPTURED_GLOSSARY_TERM_RESPONSE
            | {
                "conceptMappings": [
                    {
                        "conceptIri": "https://example.invalid/concepts/term",
                        "mappingType": "INVALID_MATCH",
                    }
                ]
            },
        )


class UnknownTermRelationFieldTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(404, {"message": "missing"})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(
            200,
            _CAPTURED_GLOSSARY_TERM_RESPONSE
            | {
                "relatedTerms": [
                    {
                        "term": {"id": _RELATED_TERM_ID, "type": "glossaryTerm"},
                        "unexpected": "must not be accepted",
                    }
                ]
            },
        )


class WrongVoteCountTypeTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(404, {"message": "missing"})
        assert "/api/v1/glossaryTerms/name/" in url
        return _response(200, _CAPTURED_GLOSSARY_TERM_RESPONSE | {"votes": {"upVotes": "2"}})


class CapturedTagResponseTransport(_AuthenticatedTransport):
    def __init__(self) -> None:
        self.patch_completed = False

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": _NAMESPACE_NAME,
                    "fullyQualifiedName": _NAMESPACE_NAME,
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(200, {"id": _RUNTIME_ID, "name": _RUNTIME_NAME})
        if "/api/v1/glossaryTerms/name/" in url:
            return _response(404, {"message": "missing"})
        if "/api/v1/classifications/name/" in url or "/api/v1/tags/name/" in url:
            return _response(404, {"message": "missing"})
        assert url.endswith(f"/api/v1/glossaryTerms/{_TERM_ID}")
        tags = [{"tagFQN": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}"}] if self.patch_completed else []
        return _response(200, _CAPTURED_GLOSSARY_TERM_RESPONSE | {"tags": tags})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/api/v1/glossaryTerms"):
            return _response(201, _CAPTURED_GLOSSARY_TERM_RESPONSE)
        if method == "POST" and url.endswith("/api/v1/classifications"):
            return _response(
                201,
                {
                    "id": _CLASSIFICATION_ID,
                    "name": _CLASSIFICATION_NAME,
                    "fullyQualifiedName": _CLASSIFICATION_NAME,
                },
            )
        if method == "POST" and url.endswith("/api/v1/tags"):
            return _response(201, _CAPTURED_TAG_RESPONSE)
        assert method == "PATCH"
        assert url.endswith(f"/api/v1/glossaryTerms/{_TERM_ID}")
        self.patch_completed = True
        return _response(
            200,
            _CAPTURED_GLOSSARY_TERM_RESPONSE
            | {
                "tags": [
                    {
                        "tagFQN": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}",
                        "source": "Classification",
                        "labelType": "Manual",
                        "state": "Confirmed",
                    }
                ]
            },
        )


class ClassificationAttachmentTransport(_AuthenticatedTransport):
    def __init__(
        self,
        *,
        subject_get_id: str = _TERM_ID,
        patch_id: str = _TERM_ID,
        patch_contains_requested_tag: bool = True,
        post_patch_contains_requested_tag: bool = True,
        require_explicit_tag_field: bool = False,
        tag_relationship: str = "exact",
    ) -> None:
        self.subject_get_id = subject_get_id
        self.patch_id = patch_id
        self.patch_contains_requested_tag = patch_contains_requested_tag
        self.post_patch_contains_requested_tag = post_patch_contains_requested_tag
        self.require_explicit_tag_field = require_explicit_tag_field
        self.tag_relationship = tag_relationship
        self.term_name = ""
        self.classification_name = ""
        self.tag_name = ""
        self.patch_completed = False

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": "pm-968bb22cb2fc16a3bacfbea1",
                    "fullyQualifiedName": "pm-968bb22cb2fc16a3bacfbea1",
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(
                200,
                {"id": _RUNTIME_ID, "name": "pm-a607919ea6e8047b38e4d6f9"},
            )
        if "/api/v1/glossaryTerms/name/" in url:
            return _response(404, {"message": "missing"})
        if "/api/v1/classifications/name/" in url or "/api/v1/tags/name/" in url:
            return _response(404, {"message": "missing"})
        assert url.endswith(f"/api/v1/glossaryTerms/{_TERM_ID}")
        tag_fqn = f"{self.classification_name}.{self.tag_name}"
        requested_tags = kwargs.get("params") == {"fields": "tags"}
        tags = (
            [{"tagFQN": tag_fqn}]
            if self.patch_completed
            and self.post_patch_contains_requested_tag
            and (not self.require_explicit_tag_field or requested_tags)
            else []
        )
        return _response(
            200,
            {
                "id": self.subject_get_id,
                "name": self.term_name,
                "fullyQualifiedName": f"pm-968bb22cb2fc16a3bacfbea1.{self.term_name}",
                "glossary": {
                    "id": _NAMESPACE_ID,
                    "type": "glossary",
                    "name": "pm-968bb22cb2fc16a3bacfbea1",
                },
                "tags": tags,
            },
        )

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        payload = kwargs["json"]
        if method == "POST" and url.endswith("/api/v1/glossaryTerms"):
            assert isinstance(payload, dict)
            self.term_name = str(payload["name"])
            return _response(
                201,
                {
                    "id": _TERM_ID,
                    "name": self.term_name,
                    "fullyQualifiedName": f"{payload['glossary']}.{self.term_name}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": payload["glossary"],
                    },
                },
            )
        if method == "POST" and url.endswith("/api/v1/classifications"):
            assert isinstance(payload, dict)
            self.classification_name = str(payload["name"])
            return _response(
                201,
                {
                    "id": _CLASSIFICATION_ID,
                    "name": self.classification_name,
                    "fullyQualifiedName": self.classification_name,
                },
            )
        if method == "POST" and url.endswith("/api/v1/tags"):
            assert isinstance(payload, dict)
            self.tag_name = str(payload["name"])
            response: dict[str, object] = {
                "id": _TAG_ID,
                "name": self.tag_name,
                "fullyQualifiedName": f"{self.classification_name}.{self.tag_name}",
                "classification": {
                    "id": _CLASSIFICATION_ID,
                    "type": "classification",
                    "name": self.classification_name,
                },
            }
            if self.tag_relationship == "wrong-classification":
                response["classification"] = {
                    "id": _CLASSIFICATION_ID,
                    "type": "classification",
                    "name": "another-classification",
                }
            if self.tag_relationship == "unexpected-parent":
                response["parent"] = {
                    "id": _TAG_ID,
                    "type": "tag",
                    "name": "unexpected-parent",
                }
            return _response(201, response)
        assert method == "PATCH"
        self.patch_completed = True
        tag_fqn = f"{self.classification_name}.{self.tag_name}"
        tags = [{"tagFQN": tag_fqn}] if self.patch_contains_requested_tag else []
        return _response(
            200,
            {
                "id": self.patch_id,
                "name": self.term_name,
                "fullyQualifiedName": f"pm-968bb22cb2fc16a3bacfbea1.{self.term_name}",
                "glossary": {
                    "id": _NAMESPACE_ID,
                    "type": "glossary",
                    "name": "pm-968bb22cb2fc16a3bacfbea1",
                },
                "tags": tags,
            },
        )


class UnknownRecognizerConfigurationFieldTransport(CapturedTagResponseTransport):
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/tags"):
            return _response(
                201,
                _CAPTURED_TAG_RESPONSE
                | {
                    "recognizers": [
                        {
                            "name": "exact-term",
                            "recognizerConfig": {
                                "type": "exact_terms",
                                "exactTerms": ["synthetic"],
                                "supportedLanguage": "en",
                                "regexFlags": {},
                                "unexpected": "must not be accepted",
                            },
                        }
                    ]
                },
            )
        return super().request(method, url, **kwargs)


class CapturedUserResponseTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        assert (
            "/api/v1/users/name/" in url
            or "/api/v1/policies/name/" in url
            or "/api/v1/roles/name/" in url
        )
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        assert method == "POST"
        if url.endswith("/api/v1/policies"):
            return _response(201, {"id": _POLICY_ID, "name": _RUNTIME_POLICY_NAME})
        if url.endswith("/api/v1/roles"):
            return _response(201, {"id": _ROLE_ID, "name": _RUNTIME_ROLE_NAME})
        assert url.endswith("/api/v1/users")
        return _response(201, _CAPTURED_USER_RESPONSE)


class GlossaryLookupTransport(_AuthenticatedTransport):
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.urls.append(url)
        if url.endswith(
            "/api/v1/glossaryTerms/name/pm-968bb22cb2fc16a3bacfbea1.pm-5a81cbddc8d6cc4d0d36d0bb"
        ):
            return _response(
                200,
                {
                    "id": _TERM_ID,
                    "name": "pm-5a81cbddc8d6cc4d0d36d0bb",
                    "fullyQualifiedName": (
                        "pm-968bb22cb2fc16a3bacfbea1.pm-5a81cbddc8d6cc4d0d36d0bb"
                    ),
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": _NAMESPACE_NAME,
                    },
                    "description": _description_with_metadata(
                        "restored term",
                        {"owner_ref": "runtime", "provenance_ref": "validation"},
                    ),
                },
            )
        return _response(404, {"message": "missing"})


class ServiceIdentityCreationTransport:
    def __init__(self) -> None:
        self.payload: object = None
        self.policy_payload: object = None
        self.role_payload: object = None

    def get(self, url: str, **kwargs: object) -> Response:
        return _response(404, {"message": "missing"})

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        assert method == "POST"
        if url.endswith("/api/v1/policies"):
            self.policy_payload = kwargs["json"]
            return _response(201, {"id": _POLICY_ID, "name": _RUNTIME_POLICY_NAME})
        if url.endswith("/api/v1/roles"):
            self.role_payload = kwargs["json"]
            return _response(201, {"id": _ROLE_ID, "name": _RUNTIME_ROLE_NAME})
        if url.endswith("/api/v1/users"):
            self.payload = kwargs["json"]
            return _response(201, {"id": _RUNTIME_ID, "name": _RUNTIME_NAME})
        raise AssertionError(f"unexpected POST {url}")


class NonUuidPolicyIdentifierTransport(ServiceIdentityCreationTransport):
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/policies"):
            return _response(201, {"id": "not-a-uuid", "name": _RUNTIME_POLICY_NAME})
        return super().request(method, url, **kwargs)


class RuntimeDenialTransport(_AuthenticatedTransport):
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.urls.append(url)
        return _response(403, {"message": "denied"})


class DeletionTransport:
    def __init__(
        self,
        *,
        delete_response: Response | None = None,
        verification_response: Response | None = None,
    ) -> None:
        self.deleted_url: str | None = None
        self.namespace_present = True
        self.delete_response = (
            delete_response
            if delete_response is not None
            else _response(200, _CAPTURED_GLOSSARY_RESPONSE)
        )
        self.verification_response = verification_response

    def get(self, url: str, **kwargs: object) -> Response:
        if not self.namespace_present and url.endswith(f"/api/v1/glossaries/{_NAMESPACE_ID}"):
            return _response(404, {"message": "missing"})
        if self.verification_response is not None and url.endswith(
            f"/api/v1/glossaries/{_NAMESPACE_ID}"
        ):
            return self.verification_response
        name = url.rsplit("/", maxsplit=1)[-1]
        return _response(
            200,
            {"id": _NAMESPACE_ID, "name": name, "fullyQualifiedName": name},
        )

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        assert method == "DELETE"
        self.deleted_url = url
        self.namespace_present = False
        return self.delete_response


class OwnerAssignmentTransport:
    def __init__(self, patch_response: Response) -> None:
        self.calls: list[tuple[str, str]] = []
        self.patch_response = patch_response

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append(("GET", url))
        if "/api/v1/glossaries/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(
                200,
                {"id": _NAMESPACE_ID, "name": name, "fullyQualifiedName": name},
            )
        if "/api/v1/users/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(200, {"id": _OWNER_ID, "name": name})
        raise AssertionError(f"unexpected GET {url}")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        assert method == "PATCH"
        assert url.endswith(f"/api/v1/glossaries/{_NAMESPACE_ID}")
        return self.patch_response


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object, object]] = []
        self._term_names: list[str] = []
        self._classification_name = ""
        self._classification_attached = False
        self._descriptions: dict[str, str] = {}
        self._term_payloads: dict[str, dict[str, object]] = {}

    def get(self, url: str, **kwargs: object) -> Response:
        self.requests.append(("GET", url, None, kwargs["headers"]))
        if "/lineage/getLineageEdge/" in url:
            return _response(
                200,
                {
                    "edge": {
                        "description": self._descriptions["lineage"],
                    }
                },
            )
        if f"/lineage/glossaryTerm/{_LINEAGE_FROM_ID}?" in url:
            return _response(
                200,
                {
                    "entity": {
                        "id": _LINEAGE_FROM_ID,
                        "type": "glossaryTerm",
                        "name": self._term_names[0],
                    },
                    "nodes": [
                        {
                            "id": _LINEAGE_TO_ID,
                            "type": "glossaryTerm",
                            "name": self._term_names[1],
                        }
                    ],
                    "upstreamEdges": [],
                    "downstreamEdges": [
                        {
                            "fromEntity": _LINEAGE_FROM_ID,
                            "toEntity": _LINEAGE_TO_ID,
                            "lineageDetails": {"description": self._descriptions["lineage"]},
                        }
                    ],
                },
            )
        if "/glossaries/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(
                200,
                {"id": _NAMESPACE_ID, "name": name, "fullyQualifiedName": name},
            )
        if "/users/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(200, {"id": _RUNTIME_ID, "name": name})
        if "/policies/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(200, {"id": _POLICY_ID, "name": name})
        if "/roles/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(200, {"id": _ROLE_ID, "name": name})
        if url.endswith(f"/glossaryTerms/name/{_NAMESPACE_NAME}.{_TERM_NAME}"):
            if not self._term_names:
                return _response(404, {"message": "missing"})
            term_payload = self._term_payloads[_LINEAGE_FROM_ID]
            return _response(
                200,
                {
                    "id": _LINEAGE_FROM_ID,
                    "name": self._term_names[0],
                    "fullyQualifiedName": f"{_NAMESPACE_NAME}.{self._term_names[0]}",
                    "displayName": term_payload["displayName"],
                    "description": term_payload["description"],
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": _NAMESPACE_NAME,
                    },
                    "owners": [{"id": _RUNTIME_ID, "type": "user", "name": _RUNTIME_NAME}],
                },
            )
        if "/glossaryTerms/name/" in url:
            return _response(404, {"message": "missing"})
        if url.endswith(f"/classifications/name/{_CLASSIFICATION_NAME}"):
            if not self._classification_name:
                return _response(404, {"message": "missing"})
            return _response(
                200,
                {
                    "id": _CLASSIFICATION_ID,
                    "name": self._classification_name,
                    "fullyQualifiedName": self._classification_name,
                    "description": self._descriptions[_CLASSIFICATION_ID],
                    "owners": [],
                    "autoClassificationConfig": {
                        "enabled": False,
                        "conflictResolution": "highest_confidence",
                        "minimumConfidence": 0.6,
                        "requireExplicitMatch": True,
                    },
                },
            )
        if "/classifications/name/" in url or "/tags/name/" in url:
            return _response(404, {"message": "missing"})
        if f"/glossaryTerms/{_LINEAGE_FROM_ID}" in url:
            tags = (
                [{"tagFQN": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}"}]
                if self._classification_attached
                else []
            )
            return _response(
                200,
                {
                    "id": _LINEAGE_FROM_ID,
                    "name": self._term_names[0],
                    "fullyQualifiedName": f"{_NAMESPACE_NAME}.{self._term_names[0]}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": _NAMESPACE_NAME,
                    },
                    "tags": tags,
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        self.requests.append((method, url, kwargs.get("json"), kwargs["headers"]))
        if method == "POST" and url.endswith("/glossaryTerms"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self._term_names.append(str(payload["name"]))
            identifier = _LINEAGE_FROM_ID if len(self._term_names) == 1 else _LINEAGE_TO_ID
            self._term_payloads[identifier] = payload
            return _response(
                201,
                {
                    "id": identifier,
                    "name": payload["name"],
                    "fullyQualifiedName": f"{payload['glossary']}.{payload['name']}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": payload["glossary"],
                    },
                },
            )
        if method == "POST" and url.endswith("/classifications"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self._classification_name = str(payload["name"])
            self._descriptions[_CLASSIFICATION_ID] = str(payload["description"])
            return _response(
                201,
                {
                    "id": _CLASSIFICATION_ID,
                    "name": payload["name"],
                    "fullyQualifiedName": payload["name"],
                    "description": payload["description"],
                    "autoClassificationConfig": {
                        "enabled": False,
                        "conflictResolution": "highest_confidence",
                        "minimumConfidence": 0.6,
                        "requireExplicitMatch": True,
                    },
                },
            )
        if method == "POST" and url.endswith("/tags"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            return _response(
                201,
                {
                    "id": _TAG_ID,
                    "name": payload["name"],
                    "fullyQualifiedName": f"{payload['classification']}.{payload['name']}",
                    "classification": {
                        "id": _CLASSIFICATION_ID,
                        "type": "classification",
                        "name": payload["classification"],
                    },
                },
            )
        if method == "PATCH" and "/glossaryTerms/" in url:
            payload = kwargs["json"]
            assert isinstance(payload, list)
            self._classification_attached = True
            return _response(
                200,
                {
                    "id": _LINEAGE_FROM_ID,
                    "name": self._term_names[0],
                    "fullyQualifiedName": f"{_NAMESPACE_NAME}.{self._term_names[0]}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": _NAMESPACE_NAME,
                    },
                    "tags": [{"tagFQN": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}"}],
                },
            )
        if method == "PUT" and url.endswith("/lineage"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            edge = payload["edge"]
            assert isinstance(edge, dict)
            details = edge["lineageDetails"]
            assert isinstance(details, dict)
            self._descriptions["lineage"] = str(details["description"])
            return Response(200)
        raise AssertionError(f"unexpected {method} {url}")


class ExactLineageTransport:
    def __init__(self, edge_response: Response) -> None:
        self.edge_response = edge_response
        self.edge_present = True
        self.requests: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.requests.append(("GET", url))
        assert url.endswith(f"/api/v1/lineage/getLineageEdge/{_LINEAGE_FROM_ID}/{_LINEAGE_TO_ID}")
        if not self.edge_present:
            return _response(404, {"message": "missing"})
        return self.edge_response

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        self.requests.append((method, url))
        assert method == "DELETE"
        assert url.endswith(
            f"/api/v1/lineage/glossaryTerm/{_LINEAGE_FROM_ID}/glossaryTerm/{_LINEAGE_TO_ID}"
        )
        self.edge_present = False
        return Response(200)


class LineageCreationTransport(_AuthenticatedTransport):
    def __init__(self, *, observed_edge: str = "exact") -> None:
        self.observed_edge = observed_edge
        self.term_names: list[str] = []
        self.lineage_get_urls: list[str] = []
        self.submitted_lineage: object = None
        self.lineage_details: dict[str, object] | None = None

    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaries/name/" in url:
            return _response(
                200,
                {
                    "id": _NAMESPACE_ID,
                    "name": "pm-968bb22cb2fc16a3bacfbea1",
                    "fullyQualifiedName": "pm-968bb22cb2fc16a3bacfbea1",
                },
            )
        if "/api/v1/users/name/" in url:
            return _response(
                200,
                {"id": _RUNTIME_ID, "name": "pm-a607919ea6e8047b38e4d6f9"},
            )
        if "/api/v1/glossaryTerms/name/" in url:
            return _response(404, {"message": "missing"})
        self.lineage_get_urls.append(url)
        if "/api/v1/lineage/getLineageEdge/" in url:
            if self.observed_edge == "missing":
                return _response(404, {"message": "missing"})
            return _response(
                200,
                {
                    "edge": self.lineage_details
                    or {
                        "description": _description_with_metadata(
                            "validation", {"producer_ref": "validation"}
                        )
                    }
                },
            )
        assert url.endswith(
            f"/api/v1/lineage/glossaryTerm/{_LINEAGE_FROM_ID}?upstreamDepth=0&downstreamDepth=1"
        )
        to_identifier = _WRONG_ENTITY_ID if self.observed_edge == "mismatch" else _LINEAGE_TO_ID
        edges: list[dict[str, object]] = []
        if self.observed_edge != "missing-graph":
            edges.append(
                {
                    "fromEntity": _LINEAGE_FROM_ID,
                    "toEntity": to_identifier,
                    "lineageDetails": {
                        "description": (self.lineage_details or {"description": "validation"})[
                            "description"
                        ]
                    },
                }
            )
        return _response(
            200,
            {
                "entity": {
                    "id": _LINEAGE_FROM_ID,
                    "type": "glossaryTerm",
                    "name": self.term_names[0],
                },
                "nodes": [
                    {
                        "id": to_identifier,
                        "type": (
                            "table" if self.observed_edge == "wrong-target-type" else "glossaryTerm"
                        ),
                        "name": self.term_names[1],
                    }
                ],
                "upstreamEdges": [],
                "downstreamEdges": edges,
            },
        )

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/api/v1/glossaryTerms"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            self.term_names.append(str(payload["name"]))
            identifier = _LINEAGE_FROM_ID if len(self.term_names) == 1 else _LINEAGE_TO_ID
            return _response(
                201,
                {
                    "id": identifier,
                    "name": payload["name"],
                    "fullyQualifiedName": f"{payload['glossary']}.{payload['name']}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": payload["glossary"],
                    },
                },
            )
        assert method == "PUT"
        assert url.endswith("/api/v1/lineage")
        self.submitted_lineage = kwargs["json"]
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        edge = payload["edge"]
        assert isinstance(edge, dict)
        details = edge["lineageDetails"]
        assert isinstance(details, dict)
        self.lineage_details = details
        return Response(200)


class RestartableLineageTransport(LineageCreationTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        if "/api/v1/glossaryTerms/name/" in url and self.term_names:
            name = url.rsplit("/", maxsplit=1)[-1].split(".")[-1]
            if name in self.term_names:
                identifier = (
                    _LINEAGE_FROM_ID if self.term_names.index(name) == 0 else _LINEAGE_TO_ID
                )
                return _response(
                    200,
                    {
                        "id": identifier,
                        "name": name,
                        "fullyQualifiedName": f"pm-968bb22cb2fc16a3bacfbea1.{name}",
                        "glossary": {
                            "id": _NAMESPACE_ID,
                            "type": "glossary",
                            "name": "pm-968bb22cb2fc16a3bacfbea1",
                        },
                    },
                )
        return super().get(url, **kwargs)


class TwoTenantTermTransport:
    def __init__(self) -> None:
        self._created = 0
        self.deleted_urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> Response:
        if "/glossaries/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(
                200,
                {"id": _NAMESPACE_ID, "name": name, "fullyQualifiedName": name},
            )
        if "/users/name/" in url:
            name = url.rsplit("/", maxsplit=1)[-1]
            return _response(200, {"id": _RUNTIME_ID, "name": name})
        if "/glossaryTerms/name/" in url:
            return _response(404, {"message": "missing"})
        raise AssertionError(f"unexpected GET {url}")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "POST" and url.endswith("/api/v1/users/login"):
            return _response(
                200,
                {
                    "accessToken": "acceptance-token",
                    "refreshToken": "refresh-token",
                    "tokenType": "Bearer",
                    "expiryDuration": 3600,
                },
            )
        if method == "POST" and url.endswith("/glossaryTerms"):
            self._created += 1
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            identifier = _TENANT_A_TERM_ID if self._created == 1 else _TENANT_B_TERM_ID
            return _response(
                201,
                {
                    "id": identifier,
                    "name": payload["name"],
                    "fullyQualifiedName": f"{payload['glossary']}.{payload['name']}",
                    "glossary": {
                        "id": _NAMESPACE_ID,
                        "type": "glossary",
                        "name": payload["glossary"],
                    },
                },
            )
        if method == "DELETE":
            self.deleted_urls.append(url)
            identifier = url.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
            return _response(200, {"id": identifier, "name": _TERM_NAME})
        raise AssertionError(f"unexpected {method} {url}")


class UnexpectedEntityFieldTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE | {"unexpected": "must not be silently ignored"},
        )


class WrongTypeGlossaryFieldTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return _response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE
            | {"updatedAt": {"sensitiveValue": _SENSITIVE_RESPONSE_VALUE}},
        )


class SensitiveInvalidLoginResponseTransport:
    def get(self, url: str, **kwargs: object) -> Response:
        raise AssertionError("login response validation must fail before the health request")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        assert method == "POST"
        assert url.endswith("/api/v1/users/login")
        return _response(200, {"accessToken": {"sensitiveValue": _SENSITIVE_LOGIN_VALUE}})


class SensitiveInvalidHealthResponseTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        assert url.endswith("/api/v1/system/health")
        return _response(200, {"status": {"sensitiveValue": _SENSITIVE_RESPONSE_VALUE}})


class UnexpectedTransportFailure(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        raise RuntimeError("transport detail must never reach a caller")


class SecretTransportDriverError(RuntimeError):
    pass


class SecretTransportFailure(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        raise SecretTransportDriverError(_SENSITIVE_TRANSPORT_VALUE)


class SecretOwnerAssignmentTransportFailure(OwnerAssignmentTransport):
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        if method == "PATCH":
            raise SecretTransportDriverError(_SENSITIVE_TRANSPORT_VALUE)
        return super().request(method, url, **kwargs)


class SecretJsonDriverError(RuntimeError):
    pass


class SecretJsonResponse(Response):
    def json(self, **kwargs: object) -> object:
        raise SecretJsonDriverError(_SENSITIVE_JSON_VALUE)


class SecretJsonFailureTransport(_AuthenticatedTransport):
    def get(self, url: str, **kwargs: object) -> Response:
        return SecretJsonResponse(200, content=b"not-json")


class SecretAuthenticationDriverError(RuntimeError):
    pass


class SecretAuthenticationFailureTransport:
    def get(self, url: str, **kwargs: object) -> Response:
        raise AssertionError("authentication must fail before the health request")

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        raise SecretAuthenticationDriverError(_SENSITIVE_AUTHENTICATION_VALUE)


def _response(status_code: int, body: dict[str, object]) -> Response:
    return Response(status_code, json=body)


def settings() -> OpenMetadataSettings:
    return OpenMetadataSettings(base_url="http://127.0.0.1:8585")


def credentials() -> _OpenMetadataCredentials:
    return _OpenMetadataCredentials(
        username="admin@open-metadata.org",
        password=SecretStr("test-only-admin-password"),
        runtime_password=SecretStr("test-only-runtime-password"),
        administrator_password=SecretStr("test-only-administrator-password"),
    )


def authenticated_client(transport: _AuthenticatedTransport) -> OpenMetadataClient:
    return OpenMetadataClient(settings=settings(), credentials=credentials(), transport=transport)


def owner_assignment_fixture(
    transport: OwnerAssignmentTransport,
    *,
    owner_tenant_key: str = "tenant-a",
) -> tuple[OpenMetadataClient, CatalogObjectRef, CatalogObjectRef]:
    client = authenticated_client(transport)
    reference = client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )
    owner = client.ensure_service_identity(
        tenant_key=owner_tenant_key,
        identity="administrator",
        idempotency_key="operation-a",
    )
    return client, reference, owner


def test_transport_exception_is_classified_without_leaking_httpx() -> None:
    transport = FailingTransport()
    client = authenticated_client(transport)

    with pytest.raises(CatalogProviderError) as captured:
        client.health()

    assert captured.value.classification == "transient"
    assert "ConnectError" not in str(captured.value)


def test_build_identity_is_observed_from_the_authoritative_version_endpoint() -> None:
    identity = authenticated_client(VersionTransport()).build_identity()

    assert identity.provider_version == "1.13.3"
    assert identity.revision == "255f6694913b84797064a42859cda3f2a3425dc6"
    assert identity.build_timestamp == 1785479611993


def test_health_accepts_the_openmetadata_terminal_text_response() -> None:
    client = authenticated_client(TextHealthTransport())

    assert client.health().status == "healthy"


def test_search_verification_requires_the_exact_restored_glossary_term_document() -> None:
    client = authenticated_client(SearchDocumentTransport())

    client.assert_object_searchable(tenant_key="tenant-a", identity="term")


def test_search_verification_rejects_a_different_provider_document() -> None:
    client = authenticated_client(SearchDocumentTransport(found_identifier=_WRONG_ENTITY_ID))

    with pytest.raises(CatalogProviderError) as captured:
        client.assert_object_searchable(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert str(captured.value) == "OpenMetadata restored metadata failed search verification"


def test_password_login_exchanges_credentials_for_a_bearer_token() -> None:
    transport = LoginTransport()
    client = OpenMetadataClient(
        settings=settings(),
        credentials=_OpenMetadataCredentials(
            username="admin@open-metadata.org",
            password=SecretStr("admin"),
            runtime_password=SecretStr("test-only-runtime-password"),
            administrator_password=SecretStr("test-only-administrator-password"),
        ),
        transport=transport,
    )

    client.health()

    assert transport.login_payload == {
        "email": "admin@open-metadata.org",
        "password": "YWRtaW4=",
    }
    assert transport.health_headers == {"Authorization": "Bearer acceptance-token"}


def test_admin_password_rotation_uses_self_service_endpoint_and_refreshes_the_token() -> None:
    transport = PasswordRotationTransport()
    client = authenticated_client(transport)

    client.health()
    client.rotate_admin_password(SecretStr("test-only-rotated-password"))
    client.health()

    assert transport.change_requests == [
        (
            "PUT",
            "http://127.0.0.1:8585/api/v1/users/changePassword",
            {
                "oldPassword": "test-only-admin-password",
                "newPassword": "test-only-rotated-password",
                "confirmPassword": "test-only-rotated-password",
                "requestType": "SELF",
            },
            {"Authorization": "Bearer rotation-token-1"},
        )
    ]
    assert transport.login_payloads == [
        {
            "email": "admin@open-metadata.org",
            "password": "dGVzdC1vbmx5LWFkbWluLXBhc3N3b3Jk",
        },
        {
            "email": "admin@open-metadata.org",
            "password": "dGVzdC1vbmx5LXJvdGF0ZWQtcGFzc3dvcmQ=",
        },
    ]
    assert transport.health_headers == [
        {"Authorization": "Bearer rotation-token-1"},
        {"Authorization": "Bearer rotation-token-2"},
    ]


def test_service_identity_password_rotation_uses_the_exact_managed_username() -> None:
    transport = PasswordRotationTransport()
    client = authenticated_client(transport)

    client.health()
    client.rotate_service_identity_password(
        tenant_key="tenant-a",
        identity="runtime",
        new_password=SecretStr("test-only-new-runtime-password"),
    )

    assert transport.change_requests == [
        (
            "PUT",
            "http://127.0.0.1:8585/api/v1/users/changePassword",
            {
                "username": _RUNTIME_NAME,
                "newPassword": "test-only-new-runtime-password",
                "confirmPassword": "test-only-new-runtime-password",
                "requestType": "USER",
            },
            {"Authorization": "Bearer rotation-token-1"},
        )
    ]


def test_rejected_admin_password_rotation_preserves_the_current_credential_and_token() -> None:
    transport = PasswordRotationTransport(change_password_status_code=400)
    client = authenticated_client(transport)

    client.health()
    with pytest.raises(CatalogProviderError) as captured:
        client.rotate_admin_password(SecretStr("test-only-rotated-password"))
    client.health()

    assert captured.value.classification == "invalid_request"
    assert transport.change_requests == [
        (
            "PUT",
            "http://127.0.0.1:8585/api/v1/users/changePassword",
            {
                "oldPassword": "test-only-admin-password",
                "newPassword": "test-only-rotated-password",
                "confirmPassword": "test-only-rotated-password",
                "requestType": "SELF",
            },
            {"Authorization": "Bearer rotation-token-1"},
        )
    ]
    assert transport.login_payloads == [
        {
            "email": "admin@open-metadata.org",
            "password": "dGVzdC1vbmx5LWFkbWluLXBhc3N3b3Jk",
        }
    ]
    assert transport.health_headers == [
        {"Authorization": "Bearer rotation-token-1"},
        {"Authorization": "Bearer rotation-token-1"},
    ]


def test_namespace_creation_supplies_the_required_description() -> None:
    transport = NamespaceCreationTransport()
    client = authenticated_client(transport)

    client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    assert isinstance(transport.payload, dict)
    assert transport.payload == {
        "name": transport.payload["name"],
        "displayName": "PillarMesh tenant-a",
        "description": "PillarMesh managed tenant catalog namespace.",
    }


def test_committed_timeout_is_transient_and_replay_converges_without_duplicate_namespace() -> None:
    transport = CommittedNamespaceReplayTransport(failure="timeout")
    client = authenticated_client(transport)

    with pytest.raises(CatalogProviderError) as captured:
        client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    replayed = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    assert captured.value.classification == "transient"
    assert replayed.stable_identity == "namespace:tenant-a:namespace"
    assert transport.post_attempts == 1


def test_committed_409_converges_and_replay_reads_the_same_namespace() -> None:
    transport = CommittedNamespaceReplayTransport(failure="conflict")
    client = authenticated_client(transport)

    reference = client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    replayed = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    assert reference == replayed
    assert transport.post_attempts == 1


def test_entity_response_rejects_noncanonical_provider_id_without_retaining_payload() -> None:
    client = authenticated_client(NonCanonicalNamespaceCreationTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert "namespace-id" not in rendered_exception
    assert "namespace-id" not in repr(captured.value)


def test_captured_openmetadata_glossary_response_validates() -> None:
    client = authenticated_client(CapturedGlossaryResponseTransport())

    reference = client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")

    assert reference.stable_identity == "namespace:tenant-a:namespace"


@pytest.mark.parametrize(
    "transport, action",
    [
        (
            WrongCreateIdentityTransport(),
            lambda client: client.ensure_tenant_namespace(
                tenant_key="tenant-a", idempotency_key="operation-a"
            ),
        ),
        (
            WrongGetIdentityTransport(),
            lambda client: client.get_object(tenant_key="tenant-a", identity="namespace"),
        ),
    ],
    ids=("create", "get-by-name"),
)
def test_entity_responses_are_bound_to_the_requested_identity_without_retaining_payload(
    transport: _AuthenticatedTransport,
    action: Callable[[OpenMetadataClient], object],
) -> None:
    client = authenticated_client(transport)

    with pytest.raises(CatalogProviderError, match="identity validation") as captured:
        action(client)

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert _SENSITIVE_RESPONSE_VALUE not in rendered_exception
    assert _SENSITIVE_RESPONSE_VALUE not in repr(captured.value)


def test_captured_openmetadata_glossary_history_response_validates() -> None:
    client = authenticated_client(CapturedGlossaryHistoryGetTransport())

    snapshot = client.get_object(tenant_key="tenant-a", identity="namespace")

    assert snapshot.object_kind == "namespace"


def test_unknown_field_change_members_are_rejected() -> None:
    client = authenticated_client(UnknownFieldChangeMemberTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="namespace")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_change_summary_timestamps_reject_the_wrong_type() -> None:
    client = authenticated_client(WrongChangeSummaryTimestampTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="namespace")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_entity_references_reject_noncanonical_provider_ids() -> None:
    client = authenticated_client(NonCanonicalEntityReferenceTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="namespace")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("collection", "body"),
    [
        (
            "users",
            _CAPTURED_USER_RESPONSE
            | {
                "personaPreferences": [
                    {"personaId": "not-a-canonical-uuid", "personaName": "analyst"}
                ]
            },
        ),
        (
            "tags",
            _CAPTURED_TAG_RESPONSE
            | {
                "recognizers": [
                    {
                        "id": "not-a-canonical-uuid",
                        "name": "exact-term",
                        "recognizerConfig": {
                            "type": "exact_terms",
                            "exactTerms": ["synthetic"],
                            "supportedLanguage": "en",
                            "regexFlags": {},
                        },
                    }
                ]
            },
        ),
        (
            "tags",
            _CAPTURED_TAG_RESPONSE
            | {
                "recognizers": [
                    {
                        "name": "exact-term",
                        "recognizerConfig": {
                            "type": "exact_terms",
                            "exactTerms": ["synthetic"],
                            "supportedLanguage": "en",
                            "regexFlags": {},
                        },
                        "exceptionList": [
                            {
                                "entityLink": "<#E::table::service.database.schema.table>",
                                "feedbackId": "not-a-canonical-uuid",
                            }
                        ],
                    }
                ]
            },
        ),
    ],
    ids=("persona", "recognizer", "feedback"),
)
def test_nested_provider_ids_require_canonical_uuids(
    collection: str,
    body: dict[str, object],
) -> None:
    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        OpenMetadataClient._entity(collection, _response(200, body))

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_captured_openmetadata_glossary_term_response_validates() -> None:
    client = authenticated_client(CapturedGlossaryTermResponseTransport())

    reference = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    assert reference.stable_identity == "glossary_term:tenant-a:term"


def test_changed_glossary_term_payload_converges_without_creating_a_duplicate() -> None:
    transport = GlossaryTermConvergenceTransport()
    client = authenticated_client(transport)
    original_payload = GlossaryTermPayload(
        name="Customer",
        definition="Original customer definition.",
        owner_ref="runtime",
        provenance_ref="validation",
    )
    changed_payload = original_payload.model_copy(
        update={"definition": "Materially narrowed customer definition."}
    )

    reference = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=original_payload,
        idempotency_key="operation-a",
    )
    changed_reference = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=changed_payload,
        idempotency_key="operation-b",
    )
    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity=reference.stable_identity)

    assert changed_reference == reference
    assert transport.patch_requests == [
        (
            f"http://127.0.0.1:8585/api/v1/glossaryTerms/{_TERM_ID}",
            [
                {
                    "op": "replace",
                    "path": "/description",
                    "value": _description_with_metadata(
                        "Materially narrowed customer definition.",
                        {"owner_ref": "runtime", "provenance_ref": "validation"},
                    ),
                }
            ],
            {
                "Authorization": "Bearer acceptance-token",
                "Content-Type": "application/json-patch+json",
            },
        )
    ]
    assert snapshot.normalized_payload == {
        "name": "Customer",
        "definition": "Materially narrowed customer definition.",
        "owner_ref": "runtime",
        "provenance_ref": "validation",
    }
    assert transport.post_attempts == 1


def test_unchanged_glossary_term_payload_causes_no_mutation() -> None:
    transport = GlossaryTermConvergenceTransport()
    client = authenticated_client(transport)
    payload = GlossaryTermPayload(
        name="Customer",
        definition="Customer definition.",
        owner_ref="runtime",
        provenance_ref="validation",
    )

    first = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=payload,
        idempotency_key="operation-a",
    )
    second = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=payload,
        idempotency_key="operation-b",
    )

    assert second == first
    assert transport.post_attempts == 1
    assert transport.patch_requests == []


def test_committed_glossary_term_patch_timeout_is_transient_and_replay_converges() -> None:
    transport = GlossaryTermConvergenceTransport()
    client = authenticated_client(transport)
    original_payload = GlossaryTermPayload(
        name="Customer",
        definition="Original definition.",
        owner_ref="runtime",
        provenance_ref="validation",
    )
    changed_payload = original_payload.model_copy(update={"definition": "Changed definition."})
    client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=original_payload,
        idempotency_key="operation-a",
    )
    transport.patch_failure = "timeout"

    with pytest.raises(CatalogProviderError) as captured:
        client.ensure_glossary_term(
            tenant_key="tenant-a",
            identity="term",
            payload=changed_payload,
            idempotency_key="operation-b",
        )

    replayed = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=changed_payload,
        idempotency_key="operation-c",
    )

    assert captured.value.classification == "transient"
    assert replayed.stable_identity == "glossary_term:tenant-a:term"
    assert len(transport.patch_requests) == 1
    assert transport.post_attempts == 1


@pytest.mark.parametrize(
    ("failure", "expected_definition", "raises_conflict"),
    [
        ("conflict", "Original definition.", True),
        ("committed-conflict", "Changed definition.", False),
    ],
    ids=("unresolved", "already-converged"),
)
def test_glossary_term_patch_conflict_requires_converged_readback(
    failure: str,
    expected_definition: str,
    raises_conflict: bool,
) -> None:
    transport = GlossaryTermConvergenceTransport()
    client = authenticated_client(transport)
    original_payload = GlossaryTermPayload(
        name="Customer",
        definition="Original definition.",
        owner_ref="runtime",
        provenance_ref="validation",
    )
    changed_payload = original_payload.model_copy(update={"definition": "Changed definition."})
    client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=original_payload,
        idempotency_key="operation-a",
    )
    transport.patch_failure = failure

    if raises_conflict:
        with pytest.raises(CatalogProviderError) as captured:
            client.ensure_glossary_term(
                tenant_key="tenant-a",
                identity="term",
                payload=changed_payload,
                idempotency_key="operation-b",
            )
        assert captured.value.classification == "conflict"
    else:
        client.ensure_glossary_term(
            tenant_key="tenant-a",
            identity="term",
            payload=changed_payload,
            idempotency_key="operation-b",
        )

    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity="glossary_term:tenant-a:term")
    assert snapshot.normalized_payload["definition"] == expected_definition
    assert len(transport.patch_requests) == 1
    assert transport.post_attempts == 1


def test_glossary_term_patch_rejects_a_stale_independent_readback() -> None:
    transport = GlossaryTermConvergenceTransport()
    client = authenticated_client(transport)
    original_payload = GlossaryTermPayload(
        name="Customer",
        definition="Original definition.",
        owner_ref="runtime",
        provenance_ref="validation",
    )
    client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=original_payload,
        idempotency_key="operation-a",
    )
    transport.patch_failure = "stale-readback"

    with pytest.raises(CatalogProviderError, match="verification response") as captured:
        client.ensure_glossary_term(
            tenant_key="tenant-a",
            identity="term",
            payload=original_payload.model_copy(update={"definition": "Changed definition."}),
            idempotency_key="operation-b",
        )

    assert captured.value.classification == "permanent"
    assert len(transport.patch_requests) == 1
    assert transport.post_attempts == 1


@pytest.mark.parametrize("relationship", ["wrong-glossary", "unexpected-parent"])
def test_glossary_term_response_is_bound_to_the_expected_glossary_and_parent(
    relationship: str,
) -> None:
    client = authenticated_client(WrongGlossaryTermRelationshipTransport(relationship=relationship))

    with pytest.raises(CatalogProviderError, match="relationship validation") as captured:
        client.ensure_glossary_term(
            tenant_key="tenant-a",
            identity="term",
            payload=GlossaryTermPayload(
                name="term",
                definition="PillarMesh managed glossary term.",
                owner_ref="runtime",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_glossary_term_get_is_bound_to_the_tenant_namespace() -> None:
    client = authenticated_client(WrongGetGlossaryTermRelationshipTransport())

    with pytest.raises(CatalogProviderError, match="relationship validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_unknown_concept_mapping_fields_are_rejected() -> None:
    client = authenticated_client(UnknownConceptMappingFieldTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_concept_mapping_types_reject_unknown_enum_values() -> None:
    client = authenticated_client(WrongConceptMappingTypeTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_unknown_term_relation_fields_are_rejected() -> None:
    client = authenticated_client(UnknownTermRelationFieldTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_votes_reject_wrong_count_types() -> None:
    client = authenticated_client(WrongVoteCountTypeTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="term")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_captured_openmetadata_tag_response_validates() -> None:
    client = authenticated_client(CapturedTagResponseTransport())
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    reference = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="validation",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    assert reference.stable_identity == "classification:tenant-a:classification"


def test_classification_accepts_the_same_tenant_logical_identity_with_colons() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    logical_identity = "pillarmesh:semantic-version:entity:customer"
    client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity=logical_identity,
        payload=GlossaryTermPayload(
            name="Customer",
            definition="A governed customer.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    reference = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=logical_identity,
            classification_ref="validation",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    assert reference.stable_identity == "classification:tenant-a:classification"


def test_classification_rejects_a_foreign_stable_reference_before_provider_effects() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)

    with pytest.raises(CatalogProviderError, match="does not belong") as captured:
        client.ensure_classification(
            tenant_key="tenant-a",
            identity="classification",
            payload=ClassificationPayload(
                subject_ref="glossary_term:tenant-b:term",
                classification_ref="validation",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "authorization"
    assert transport.requests == []


@pytest.mark.parametrize(
    "transport",
    [
        ClassificationAttachmentTransport(subject_get_id=_WRONG_ENTITY_ID),
        ClassificationAttachmentTransport(patch_id=_WRONG_ENTITY_ID),
        ClassificationAttachmentTransport(patch_contains_requested_tag=False),
    ],
    ids=("wrong-subject-get", "wrong-patch-subject", "missing-patch-tag"),
)
def test_classification_attachment_requires_the_exact_subject_and_requested_tag(
    transport: ClassificationAttachmentTransport,
) -> None:
    client = authenticated_client(transport)
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="classification response") as captured:
        client.ensure_classification(
            tenant_key="tenant-a",
            identity="classification",
            payload=ClassificationPayload(
                subject_ref=source.stable_identity,
                classification_ref="validation",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_classification_attachment_requires_independent_persistence_read_back() -> None:
    client = authenticated_client(
        ClassificationAttachmentTransport(post_patch_contains_requested_tag=False)
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="classification response") as captured:
        client.ensure_classification(
            tenant_key="tenant-a",
            identity="classification",
            payload=ClassificationPayload(
                subject_ref=source.stable_identity,
                classification_ref="validation",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_classification_attachment_requests_tags_for_independent_read_back() -> None:
    client = authenticated_client(
        ClassificationAttachmentTransport(require_explicit_tag_field=True)
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    classification = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="validation",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    assert classification.stable_identity == "classification:tenant-a:classification"


@pytest.mark.parametrize("tag_relationship", ["wrong-classification", "unexpected-parent"])
def test_classification_tag_response_is_bound_to_the_expected_classification_and_parent(
    tag_relationship: str,
) -> None:
    client = authenticated_client(
        ClassificationAttachmentTransport(tag_relationship=tag_relationship)
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="relationship validation") as captured:
        client.ensure_classification(
            tenant_key="tenant-a",
            identity="classification",
            payload=ClassificationPayload(
                subject_ref=source.stable_identity,
                classification_ref="validation",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_recognizer_configuration_fields_are_rejected() -> None:
    client = authenticated_client(UnknownRecognizerConfigurationFieldTransport())
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="PillarMesh managed glossary term.",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.ensure_classification(
            tenant_key="tenant-a",
            identity="classification",
            payload=ClassificationPayload(
                subject_ref=source.stable_identity,
                classification_ref="validation",
                provenance_ref="validation",
            ),
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_captured_openmetadata_runtime_user_response_validates() -> None:
    client = authenticated_client(CapturedUserResponseTransport())

    reference = client.ensure_service_identity(
        tenant_key="tenant-a", identity="runtime", idempotency_key="operation-a"
    )

    assert reference.stable_identity == "service_identity:tenant-a:runtime"


def test_glossary_term_lookup_uses_its_fully_qualified_name() -> None:
    transport = GlossaryLookupTransport()
    client = authenticated_client(transport)

    snapshot = client.get_object(tenant_key="tenant-a", identity="validation-term-from")

    assert snapshot.object_kind == "glossary_term"
    assert transport.urls[-1].endswith(
        "/api/v1/glossaryTerms/name/pm-968bb22cb2fc16a3bacfbea1.pm-5a81cbddc8d6cc4d0d36d0bb"
    )


def test_service_identity_uses_an_administrator_created_password() -> None:
    transport = ServiceIdentityCreationTransport()
    private_credentials = credentials()
    client = OpenMetadataClient(
        settings=settings(),
        credentials=private_credentials,
        transport=transport,
    )

    client.ensure_service_identity(
        tenant_key="tenant-a",
        identity="runtime",
        idempotency_key="operation-a",
    )

    assert isinstance(transport.payload, dict)
    assert transport.payload["createPasswordType"] == "ADMIN_CREATE"
    assert transport.payload["password"] == transport.payload["confirmPassword"]
    assert digest({"password": transport.payload["password"]}) == digest(
        {"password": private_credentials.runtime_password.get_secret_value()}
    )
    assert "test-only-runtime-password" not in repr(private_credentials)
    assert transport.payload["roles"] == [_ROLE_ID]
    assert isinstance(transport.policy_payload, dict)
    assert transport.policy_payload["rules"] == [
        {
            "name": "deny-unowned-resources",
            "description": "Deny runtime access to resources it does not own.",
            "resources": ["all"],
            "operations": ["All"],
            "effect": "deny",
            "condition": "!isOwner()",
        }
    ]
    assert isinstance(transport.role_payload, dict)
    assert transport.role_payload["policies"] == [_RUNTIME_POLICY_NAME]


def test_nested_resource_response_rejects_a_non_uuid_provider_identifier() -> None:
    client = authenticated_client(NonUuidPolicyIdentifierTransport())

    with pytest.raises(
        CatalogProviderError, match="response failed provider validation"
    ) as captured:
        client.ensure_service_identity(
            tenant_key="tenant-a",
            identity="runtime",
            idempotency_key="operation-a",
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_administration_denial_probes_an_admin_only_endpoint() -> None:
    transport = RuntimeDenialTransport()
    client = authenticated_client(transport)

    client.assert_administration_denied()

    assert transport.urls == [
        "http://127.0.0.1:8585/api/v1/users/generateRandomPwd",
    ]


def test_existing_other_namespace_requires_authorization_denial() -> None:
    transport = RuntimeDenialTransport()
    client = authenticated_client(transport)

    client.assert_other_tenant_namespace_denied(tenant_key="other-tenant")

    assert "/api/v1/glossaries/name/" in transport.urls[0]


def test_absence_probe_accepts_only_an_exact_provider_not_found_result() -> None:
    client = authenticated_client(NamespaceCreationTransport())

    client.assert_object_absent(tenant_key="tenant-a", identity="missing-object")


def test_recorded_namespace_is_deleted_by_exact_provider_id() -> None:
    transport = DeletionTransport()
    client = authenticated_client(transport)
    reference = client.ensure_tenant_namespace(
        tenant_key="other-tenant",
        idempotency_key="operation-a",
    )

    client.delete_object(reference)

    assert transport.deleted_url == (
        f"http://127.0.0.1:8585/api/v1/glossaries/{_NAMESPACE_ID}?recursive=true&hardDelete=true"
    )


def test_discovered_namespace_is_deleted_and_verified_by_exact_provider_id() -> None:
    transport = DeletionTransport()
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    discovered = client.discovered_resources()
    client.delete_recorded_resource(collection="glossaries", identifier=_NAMESPACE_ID)

    assert [(resource.collection, resource.identifier) for resource in discovered] == [
        ("glossaries", _NAMESPACE_ID)
    ]
    assert client.recorded_resource_is_absent(
        collection="glossaries",
        identifier=_NAMESPACE_ID,
    )
    assert transport.deleted_url == (
        f"http://127.0.0.1:8585/api/v1/glossaries/{_NAMESPACE_ID}?recursive=true&hardDelete=true"
    )


def test_delete_accepts_the_openmetadata_entity_response_shape() -> None:
    transport = DeletionTransport(delete_response=_response(200, _CAPTURED_GLOSSARY_RESPONSE))
    client = authenticated_client(transport)
    reference = client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    client.delete_object(reference)

    assert client.discovered_resources() == ()


@pytest.mark.parametrize(
    ("collection", "identifier", "body"),
    [
        (
            "users",
            _RUNTIME_ID,
            {
                "id": _RUNTIME_ID,
                "name": "user",
                "personas": [],
                "incrementalChangeDescription": {"previousVersion": 0.1},
            },
        ),
        (
            "classifications",
            _CLASSIFICATION_ID,
            {"id": _CLASSIFICATION_ID, "name": "c", "domains": []},
        ),
        ("tags", _TAG_ID, {"id": _TAG_ID, "name": "tag", "dataProducts": []}),
        ("roles", _ROLE_ID, {"id": _ROLE_ID, "name": "role", "domains": []}),
        ("policies", _POLICY_ID, {"id": _POLICY_ID, "name": "policy", "domains": []}),
    ],
    ids=("user", "classification", "tag", "role", "policy"),
)
def test_delete_accepts_documented_collection_specific_fields(
    collection: str,
    identifier: str,
    body: dict[str, object],
) -> None:
    client = authenticated_client(DeletionTransport(delete_response=_response(200, body)))

    client.delete_recorded_resource(collection=collection, identifier=identifier)


def test_delete_rejects_a_tag_application_timestamp_with_the_wrong_type() -> None:
    body = _CAPTURED_GLOSSARY_TERM_RESPONSE | {
        "tags": [
            {
                "tagFQN": "classification.tag",
                "source": "Classification",
                "labelType": "Manual",
                "state": "Confirmed",
                "appliedAt": "1755663200000",
            }
        ]
    }
    client = authenticated_client(DeletionTransport(delete_response=_response(200, body)))

    with pytest.raises(CatalogProviderError, match="delete response failed") as captured:
        client.delete_recorded_resource(collection="glossaryTerms", identifier=_TERM_ID)

    assert captured.value.classification == "permanent"


@pytest.mark.parametrize(
    "response",
    [
        _response(200, _CAPTURED_GLOSSARY_RESPONSE | {"unexpected": "forbidden"}),
        _response(200, _CAPTURED_GLOSSARY_RESPONSE | {"updatedAt": "1755663200000"}),
        Response(200, content=b"{"),
        Response(204),
    ],
    ids=("unknown-field", "wrong-type", "malformed-json", "undocumented-empty-204"),
)
def test_delete_rejects_undocumented_or_malformed_success_responses(response: Response) -> None:
    transport = DeletionTransport(delete_response=response)
    client = authenticated_client(transport)
    reference = client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="delete response failed") as captured:
        client.delete_object(reference)

    assert captured.value.classification == "permanent"
    assert len(client.discovered_resources()) == 1


def test_delete_rejects_an_entity_response_for_a_different_identifier() -> None:
    transport = DeletionTransport(
        delete_response=_response(
            200,
            _CAPTURED_GLOSSARY_RESPONSE | {"id": _WRONG_ENTITY_ID},
        )
    )
    client = authenticated_client(transport)
    reference = client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="delete response failed") as captured:
        client.delete_object(reference)

    assert captured.value.classification == "permanent"
    assert len(client.discovered_resources()) == 1


@pytest.mark.parametrize(
    "response",
    [
        _response(200, _CAPTURED_GLOSSARY_RESPONSE | {"unexpected": "forbidden"}),
        _response(200, _CAPTURED_GLOSSARY_RESPONSE | {"id": _WRONG_ENTITY_ID}),
        Response(200, content=b"{"),
        Response(204),
    ],
    ids=("unknown-field", "different-entity", "malformed-json", "undocumented-empty-204"),
)
def test_exact_absence_check_rejects_unvalidated_success_responses(response: Response) -> None:
    transport = DeletionTransport(verification_response=response)
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    with pytest.raises(CatalogProviderError, match="verification response failed") as captured:
        client.recorded_resource_is_absent(
            collection="glossaries",
            identifier=_NAMESPACE_ID,
        )

    assert captured.value.classification == "permanent"


def test_exact_deletion_treats_only_an_http_404_as_already_absent() -> None:
    missing_transport = DeletionTransport(delete_response=_response(404, {"message": "missing"}))
    missing_client = authenticated_client(missing_transport)
    missing_client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-a",
    )

    missing_client.delete_recorded_resource(
        collection="glossaries",
        identifier=_NAMESPACE_ID,
    )

    assert missing_client.discovered_resources() == ()

    rejected_transport = DeletionTransport(delete_response=_response(410, {"message": "missing"}))
    rejected_client = authenticated_client(rejected_transport)
    rejected_client.ensure_tenant_namespace(
        tenant_key="tenant-a",
        idempotency_key="operation-b",
    )

    with pytest.raises(CatalogProviderError) as captured:
        rejected_client.delete_recorded_resource(
            collection="glossaries",
            identifier=_NAMESPACE_ID,
        )

    assert captured.value.classification == "permanent"
    assert len(rejected_client.discovered_resources()) == 1


def test_assign_owner_accepts_the_openmetadata_glossary_response_shape() -> None:
    transport = OwnerAssignmentTransport(_response(200, _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE))
    client, reference, owner = owner_assignment_fixture(transport)

    client.assign_owner(reference, owner)

    assert transport.calls[-1] == (
        "PATCH",
        f"http://127.0.0.1:8585/api/v1/glossaries/{_NAMESPACE_ID}",
    )


@pytest.mark.parametrize(
    "response",
    [
        _response(
            200,
            _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE | {"unexpected": "forbidden"},
        ),
        _response(
            200,
            _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE | {"updatedAt": "1755663200000"},
        ),
        Response(200, content=b"{"),
        Response(204),
    ],
    ids=("unknown-field", "wrong-type", "malformed-json", "undocumented-empty-204"),
)
def test_assign_owner_rejects_undocumented_or_malformed_success_responses(
    response: Response,
) -> None:
    transport = OwnerAssignmentTransport(response)
    client, reference, owner = owner_assignment_fixture(transport)

    with pytest.raises(CatalogProviderError, match="owner response failed") as captured:
        client.assign_owner(reference, owner)

    assert captured.value.classification == "permanent"


@pytest.mark.parametrize(
    "body",
    [
        _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE | {"id": _WRONG_ENTITY_ID},
        _CAPTURED_GLOSSARY_RESPONSE,
    ],
    ids=("different-entity", "missing-owner"),
)
def test_assign_owner_rejects_a_response_that_does_not_confirm_the_assignment(
    body: dict[str, object],
) -> None:
    transport = OwnerAssignmentTransport(_response(200, body))
    client, reference, owner = owner_assignment_fixture(transport)

    with pytest.raises(CatalogProviderError, match="owner response failed") as captured:
        client.assign_owner(reference, owner)

    assert captured.value.classification == "permanent"


def test_assign_owner_rejects_cross_tenant_references_without_touching_transport() -> None:
    transport = OwnerAssignmentTransport(_response(200, _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE))
    client, reference, owner = owner_assignment_fixture(
        transport,
        owner_tenant_key="tenant-b",
    )
    transport.calls.clear()

    with pytest.raises(CatalogProviderError, match="same tenant") as captured:
        client.assign_owner(reference, owner)

    assert captured.value.classification == "invalid_request"
    assert transport.calls == []


def test_same_identity_in_two_tenants_keeps_distinct_exact_provider_ids() -> None:
    transport = TwoTenantTermTransport()
    client = authenticated_client(transport)
    payload = GlossaryTermPayload(
        name="shared",
        definition="shared definition",
        owner_ref="runtime",
        provenance_ref="validation",
    )

    first = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="shared",
        payload=payload,
        idempotency_key="operation-a",
    )
    second = client.ensure_glossary_term(
        tenant_key="tenant-b",
        identity="shared",
        payload=payload,
        idempotency_key="operation-b",
    )
    client.delete_object(first)

    assert first.tenant_key == "tenant-a"
    assert second.tenant_key == "tenant-b"
    assert first.stable_identity != second.stable_identity
    assert transport.deleted_urls == [
        f"http://127.0.0.1:8585/api/v1/glossaryTerms/{_TENANT_A_TERM_ID}"
        "?recursive=true&hardDelete=true"
    ]


def test_catalog_snapshot_payload_is_deeply_immutable() -> None:
    input_tags = ["validation"]
    normalized_payload = {"name": "shared", "tags": input_tags}
    snapshot = CatalogObjectSnapshot(
        tenant_key="tenant-a",
        stable_identity="glossary_term:tenant-a:shared",
        logical_identity="shared",
        object_kind="glossary_term",
        normalized_payload=normalized_payload,
        normalized_digest=digest(normalized_payload),
    )

    with pytest.raises(TypeError):
        snapshot.normalized_payload["name"] = "mutated"

    input_tags.append("mutated")

    assert snapshot.normalized_payload["tags"] == ("validation",)
    assert snapshot.normalized_digest == digest(snapshot.normalized_payload)


def test_catalog_snapshot_rejects_a_digest_unrelated_to_its_normalized_payload() -> None:
    with pytest.raises(ValueError, match="normalized payload"):
        CatalogObjectSnapshot(
            tenant_key="tenant-a",
            stable_identity="glossary_term:tenant-a:shared",
            logical_identity="shared",
            object_kind="glossary_term",
            normalized_payload={"name": "shared"},
            normalized_digest="f" * 64,
        )


def test_unknown_entity_response_fields_are_rejected() -> None:
    client = authenticated_client(UnexpectedEntityFieldTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="namespace")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None


def test_documented_glossary_response_fields_reject_the_wrong_type() -> None:
    client = authenticated_client(WrongTypeGlossaryFieldTransport())

    with pytest.raises(CatalogProviderError, match="provider validation") as captured:
        client.get_object(tenant_key="tenant-a", identity="namespace")

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert _SENSITIVE_RESPONSE_VALUE not in rendered_exception
    assert _SENSITIVE_RESPONSE_VALUE not in repr(captured.value)


def test_login_response_validation_does_not_retain_sensitive_response_values() -> None:
    client = OpenMetadataClient(
        settings=settings(),
        credentials=credentials(),
        transport=SensitiveInvalidLoginResponseTransport(),
    )

    with pytest.raises(CatalogProviderError, match="authentication response failed") as captured:
        client.health()

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert _SENSITIVE_LOGIN_VALUE not in rendered_exception
    assert _SENSITIVE_LOGIN_VALUE not in repr(captured.value)


def test_health_response_validation_does_not_retain_sensitive_response_values() -> None:
    client = authenticated_client(SensitiveInvalidHealthResponseTransport())

    with pytest.raises(CatalogProviderError, match="health response failed") as captured:
        client.health()

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert _SENSITIVE_RESPONSE_VALUE not in rendered_exception
    assert _SENSITIVE_RESPONSE_VALUE not in repr(captured.value)


def test_acknowledgement_validation_does_not_retain_sensitive_response_values() -> None:
    with pytest.raises(CatalogProviderError, match="response failed") as captured:
        OpenMetadataClient._acknowledgement(
            _response(200, {"sensitiveValue": _SENSITIVE_RESPONSE_VALUE})
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert _SENSITIVE_RESPONSE_VALUE not in rendered_exception
    assert _SENSITIVE_RESPONSE_VALUE not in repr(captured.value)


def test_lineage_acknowledgement_rejects_an_undocumented_204() -> None:
    with pytest.raises(CatalogProviderError, match="lineage response failed") as captured:
        OpenMetadataClient._acknowledgement(Response(204))

    assert captured.value.classification == "permanent"


def test_public_settings_reject_credential_fields() -> None:
    with pytest.raises(ValidationError):
        OpenMetadataSettings(
            base_url="http://127.0.0.1:8585",
            username="admin@open-metadata.org",
            password=SecretStr("test-only-password"),
        )


def test_client_without_private_credentials_fails_before_making_a_request() -> None:
    client = OpenMetadataClient(settings=settings(), transport=TextHealthTransport())

    with pytest.raises(CatalogProviderError, match="credentials") as captured:
        client.health()

    assert captured.value.classification == "authentication"


def test_unexpected_transport_failures_are_sanitized_at_the_provider_boundary() -> None:
    client = authenticated_client(UnexpectedTransportFailure())

    with pytest.raises(CatalogProviderError) as captured:
        client.health()

    assert captured.value.classification == "transient"
    assert "transport detail" not in str(captured.value)


def test_transport_failures_do_not_retain_secret_driver_exceptions() -> None:
    client = authenticated_client(SecretTransportFailure())

    with pytest.raises(CatalogProviderError) as captured:
        client.health()

    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert captured.value.classification == "transient"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _SENSITIVE_TRANSPORT_VALUE not in rendered_exception
    assert "SecretTransportDriverError" not in rendered_exception
    assert _SENSITIVE_TRANSPORT_VALUE not in repr(captured.value)
    assert "SecretTransportDriverError" not in repr(captured.value)


def test_mutating_transport_failures_do_not_retain_secret_driver_exceptions() -> None:
    transport = SecretOwnerAssignmentTransportFailure(
        _response(200, _CAPTURED_OWNER_ASSIGNED_GLOSSARY_RESPONSE)
    )
    client, reference, owner = owner_assignment_fixture(transport)

    with pytest.raises(CatalogProviderError) as captured:
        client.assign_owner(reference, owner)

    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert captured.value.classification == "transient"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _SENSITIVE_TRANSPORT_VALUE not in rendered_exception
    assert "SecretTransportDriverError" not in rendered_exception
    assert _SENSITIVE_TRANSPORT_VALUE not in repr(captured.value)
    assert "SecretTransportDriverError" not in repr(captured.value)


def test_json_failures_do_not_retain_secret_driver_exceptions() -> None:
    client = authenticated_client(SecretJsonFailureTransport())

    with pytest.raises(CatalogProviderError, match="not valid JSON") as captured:
        client.health()

    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _SENSITIVE_JSON_VALUE not in rendered_exception
    assert "SecretJsonDriverError" not in rendered_exception
    assert _SENSITIVE_JSON_VALUE not in repr(captured.value)
    assert "SecretJsonDriverError" not in repr(captured.value)


def test_authentication_failures_do_not_retain_secret_driver_exceptions() -> None:
    client = OpenMetadataClient(
        settings=settings(),
        credentials=credentials(),
        transport=SecretAuthenticationFailureTransport(),
    )

    with pytest.raises(CatalogProviderError, match="authentication request failed") as captured:
        client.health()

    rendered_exception = "".join(traceback.format_exception(captured.value))
    assert captured.value.classification == "transient"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _SENSITIVE_AUTHENTICATION_VALUE not in rendered_exception
    assert "SecretAuthenticationDriverError" not in rendered_exception
    assert _SENSITIVE_AUTHENTICATION_VALUE not in repr(captured.value)
    assert "SecretAuthenticationDriverError" not in repr(captured.value)


def test_classification_is_attached_to_subject_and_lineage_uses_put() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    namespace = client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    assert namespace.stable_identity == "namespace:tenant-a:namespace"
    client.ensure_service_identity(
        tenant_key="tenant-a",
        identity="runtime",
        idempotency_key="operation-a",
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="term definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    target = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="target",
        payload=GlossaryTermPayload(
            name="target",
            definition="target definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="validation",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    client.ensure_lineage(
        tenant_key="tenant-a",
        identity="lineage",
        payload=LineagePayload(
            from_ref=source.stable_identity,
            to_ref=target.stable_identity,
            producer_ref="validation",
            evidence_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    patch = next(request for request in transport.requests if request[0] == "PATCH")
    glossary_term = next(
        request
        for request in transport.requests
        if request[0] == "POST" and request[1].endswith("/glossaryTerms")
    )
    assert glossary_term[2] == {
        "name": glossary_term[2]["name"],
        "displayName": "term",
        "description": _description_with_metadata(
            "term definition",
            {"owner_ref": "runtime", "provenance_ref": "validation"},
        ),
        "glossary": _NAMESPACE_NAME,
        "owners": [{"id": _RUNTIME_ID, "type": "user"}],
    }
    assert patch[2] == [
        {
            "op": "add",
            "path": "/tags/-",
            "value": {
                "tagFQN": f"{_CLASSIFICATION_NAME}.{_TAG_NAME}",
                "source": "Classification",
                "labelType": "Manual",
                "state": "Confirmed",
            },
        }
    ]
    assert patch[3] == {
        "Authorization": "Bearer acceptance-token",
        "Content-Type": "application/json-patch+json",
    }
    assert any(
        request[0] == "PUT" and request[1].endswith("/lineage") for request in transport.requests
    )


def test_publication_references_use_governed_descriptions_and_round_trip_readback() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    client.ensure_service_identity(
        tenant_key="tenant-a", identity="runtime", idempotency_key="operation-a"
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="term definition",
            owner_ref="runtime",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )
    classification = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="classification-a",
            provenance_ref="provenance-b",
        ),
        idempotency_key="operation-a",
    )
    target = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="target",
        payload=GlossaryTermPayload(
            name="target",
            definition="target definition",
            owner_ref="runtime",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )
    lineage = client.ensure_lineage(
        tenant_key="tenant-a",
        identity="lineage",
        payload=LineagePayload(
            from_ref=source.stable_identity,
            to_ref=target.stable_identity,
            producer_ref="producer-a",
            evidence_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    term_payloads = tuple(
        request[2]
        for request in transport.requests
        if request[0] == "POST" and request[1].endswith("/glossaryTerms")
    )
    classification_payload = next(
        request[2]
        for request in transport.requests
        if request[0] == "POST" and request[1].endswith("/classifications")
    )
    lineage_payload = next(
        request[2]
        for request in transport.requests
        if request[0] == "PUT" and request[1].endswith("/lineage")
    )

    assert tuple(payload["description"] for payload in term_payloads) == (
        _description_with_metadata(
            "term definition",
            {"owner_ref": "runtime", "provenance_ref": "provenance-a"},
        ),
        _description_with_metadata(
            "target definition",
            {"owner_ref": "runtime", "provenance_ref": "provenance-a"},
        ),
    )
    assert classification_payload["description"] == _description_with_metadata(
        "PillarMesh managed classification.",
        {
            "classification_ref": "classification-a",
            "provenance_ref": "provenance-b",
            "subject_ref": source.stable_identity,
        },
    )
    assert lineage_payload["edge"]["lineageDetails"]["description"] == _description_with_metadata(
        "validation", {"producer_ref": "producer-a"}
    )
    # OpenMetadata 1.13.3 persists glossary-term entity edges but logs its own
    # "Unsupported Entity Type ... for column lineage" message. PillarMesh sends
    # no column-lineage payload, and independently verifies both the exact edge and
    # graph before treating the operation as successful.
    assert "columnsLineage" not in lineage_payload["edge"]["lineageDetails"]
    assert "sqlQuery" not in lineage_payload["edge"]["lineageDetails"]
    source_snapshot = client.get_object(tenant_key="tenant-a", identity=source.stable_identity)
    assert source_snapshot.normalized_payload["provenance_ref"] == "provenance-a"
    assert (
        client.get_object(
            tenant_key="tenant-a", identity=classification.stable_identity
        ).normalized_payload["classification_ref"]
        == "classification-a"
    )
    lineage_snapshot = client.get_object(tenant_key="tenant-a", identity=lineage.stable_identity)
    assert lineage_snapshot.normalized_payload["producer_ref"] == "producer-a"


def test_get_object_resolves_every_stable_reference_kind_through_provider_http_reads() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    namespace = client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    client.ensure_service_identity(
        tenant_key="tenant-a", identity="runtime", idempotency_key="operation-a"
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="term",
            definition="term definition",
            owner_ref="runtime",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )
    classification = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="classification-a",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )
    snapshots = tuple(
        client.get_object(tenant_key="tenant-a", identity=reference.stable_identity)
        for reference in (namespace, source, classification)
    )

    assert tuple(snapshot.object_kind for snapshot in snapshots) == (
        "namespace",
        "glossary_term",
        "classification",
    )
    assert tuple(snapshot.logical_identity for snapshot in snapshots) == (
        "namespace",
        "term",
        "classification",
    )
    assert all(snapshot.tenant_key == "tenant-a" for snapshot in snapshots)


def test_fresh_client_get_object_returns_the_canonical_namespace_snapshot() -> None:
    transport = RecordingTransport()
    namespace = authenticated_client(transport).ensure_tenant_namespace(
        tenant_key="tenant-a", idempotency_key="operation-a"
    )

    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity=namespace.stable_identity)

    assert snapshot.logical_identity == "namespace"
    assert snapshot.normalized_payload == {"name": "tenant-a", "namespace": "tenant-a"}


def test_fresh_client_get_object_returns_the_canonical_glossary_term_snapshot() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    client.ensure_service_identity(
        tenant_key="tenant-a", identity="runtime", idempotency_key="operation-a"
    )
    term = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="Human term",
            definition="Human definition",
            owner_ref="runtime",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )

    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity=term.stable_identity)

    assert snapshot.logical_identity == "term"
    assert snapshot.normalized_payload == {
        "name": "Human term",
        "definition": "Human definition",
        "owner_ref": "runtime",
        "provenance_ref": "provenance-a",
    }


def test_fresh_client_get_object_returns_the_canonical_classification_snapshot() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    client.ensure_service_identity(
        tenant_key="tenant-a", identity="runtime", idempotency_key="operation-a"
    )
    subject = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="term",
        payload=GlossaryTermPayload(
            name="Human term",
            definition="Human definition",
            owner_ref="runtime",
            provenance_ref="provenance-a",
        ),
        idempotency_key="operation-a",
    )
    classification = client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=subject.stable_identity,
            classification_ref="classification-a",
            provenance_ref="provenance-b",
        ),
        idempotency_key="operation-a",
    )

    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity=classification.stable_identity)

    assert snapshot.logical_identity == "classification"
    assert snapshot.normalized_payload == {
        "subject_ref": subject.stable_identity,
        "classification_ref": "classification-a",
        "provenance_ref": "provenance-b",
    }


def test_get_object_reads_a_lineage_stable_reference_through_its_independent_edge_path() -> None:
    client = authenticated_client(LineageCreationTransport())
    reference = _create_lineage(client)

    snapshot = client.get_object(tenant_key="tenant-a", identity=reference.stable_identity)

    assert snapshot.object_kind == "lineage"
    assert snapshot.stable_identity == reference.stable_identity


def test_lineage_readback_survives_a_fresh_client_after_restart() -> None:
    transport = RestartableLineageTransport()
    reference = _create_lineage(authenticated_client(transport))

    snapshot = OpenMetadataClient(
        settings=settings(), credentials=credentials(), transport=transport
    ).get_object(tenant_key="tenant-a", identity=reference.stable_identity)

    assert snapshot.object_kind == "lineage"
    assert snapshot.stable_identity == reference.stable_identity
    assert snapshot.logical_identity == "lineage"
    assert snapshot.normalized_payload == {
        "from_ref": "glossary_term:tenant-a:source",
        "to_ref": "glossary_term:tenant-a:target",
        "producer_ref": "validation",
        "evidence_ref": "validation",
    }


def _create_lineage(client: OpenMetadataClient) -> CatalogObjectRef:
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="source",
        payload=GlossaryTermPayload(
            name="source",
            definition="source definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    target = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="target",
        payload=GlossaryTermPayload(
            name="target",
            definition="target definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    return client.ensure_lineage(
        tenant_key="tenant-a",
        identity="lineage",
        payload=LineagePayload(
            from_ref=source.stable_identity,
            to_ref=target.stable_identity,
            producer_ref="validation",
            evidence_ref="validation",
        ),
        idempotency_key="operation-a",
    )


def test_lineage_creation_reads_and_validates_the_exact_persisted_edge() -> None:
    transport = LineageCreationTransport()
    client = authenticated_client(transport)

    reference = _create_lineage(client)

    assert reference.stable_identity.startswith("lineage:tenant-a:")
    assert transport.submitted_lineage == {
        "edge": {
            "fromEntity": {"id": _LINEAGE_FROM_ID, "type": "glossaryTerm"},
            "toEntity": {"id": _LINEAGE_TO_ID, "type": "glossaryTerm"},
            "lineageDetails": {
                "description": _description_with_metadata(
                    "validation", {"producer_ref": "validation"}
                ),
            },
        }
    }
    assert transport.lineage_get_urls == [
        (
            "http://127.0.0.1:8585/api/v1/lineage/getLineageEdge/"
            f"{_LINEAGE_FROM_ID}/{_LINEAGE_TO_ID}"
        ),
        (
            "http://127.0.0.1:8585/api/v1/lineage/glossaryTerm/"
            f"{_LINEAGE_FROM_ID}?upstreamDepth=0&downstreamDepth=1"
        ),
    ]


@pytest.mark.parametrize(
    "observed_edge",
    ["missing", "missing-graph", "mismatch", "wrong-target-type"],
)
def test_lineage_creation_rejects_a_missing_or_mismatched_persisted_edge(
    observed_edge: str,
) -> None:
    client = authenticated_client(LineageCreationTransport(observed_edge=observed_edge))

    with pytest.raises(CatalogProviderError, match="lineage verification") as captured:
        _create_lineage(client)

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_nested_openmetadata_side_effects_expose_exact_cleanup_identifiers() -> None:
    transport = RecordingTransport()
    client = authenticated_client(transport)
    client.ensure_tenant_namespace(tenant_key="tenant-a", idempotency_key="operation-a")
    client.ensure_service_identity(
        tenant_key="tenant-a",
        identity="runtime",
        idempotency_key="operation-a",
    )
    source = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="source",
        payload=GlossaryTermPayload(
            name="source",
            definition="source definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    target = client.ensure_glossary_term(
        tenant_key="tenant-a",
        identity="target",
        payload=GlossaryTermPayload(
            name="target",
            definition="target definition",
            owner_ref="runtime",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    client.ensure_classification(
        tenant_key="tenant-a",
        identity="classification",
        payload=ClassificationPayload(
            subject_ref=source.stable_identity,
            classification_ref="validation",
            provenance_ref="validation",
        ),
        idempotency_key="operation-a",
    )
    client.ensure_lineage(
        tenant_key="tenant-a",
        identity="lineage",
        payload=LineagePayload(
            from_ref=source.stable_identity,
            to_ref=target.stable_identity,
            producer_ref="validation",
            evidence_ref="validation",
        ),
        idempotency_key="operation-a",
    )

    discovered = {
        (resource.collection, resource.identifier) for resource in client.discovered_resources()
    }

    assert ("policies", _POLICY_ID) in discovered
    assert ("roles", _ROLE_ID) in discovered
    assert ("tags", _TAG_ID) in discovered
    assert ("lineage", _LINEAGE_IDENTIFIER) in discovered


def test_recorded_lineage_cleanup_uses_exact_routes_and_independent_absence_read() -> None:
    transport = ExactLineageTransport(
        _response(
            200,
            {
                "edge": {
                    "sqlQuery": "select source from synthetic",
                    "columnsLineage": [
                        {
                            "fromColumns": ["service.database.schema.source"],
                            "toColumn": "service.database.schema.target",
                            "function": "identity",
                        }
                    ],
                    "pipeline": {"id": _PIPELINE_ID, "type": "pipeline", "name": "daily"},
                    "description": _LINEAGE_DESCRIPTION,
                    "source": "Manual",
                    "createdAt": 1_755_663_200_000,
                    "createdBy": "admin",
                    "updatedAt": 1_755_663_200_001,
                    "updatedBy": "admin",
                    "assetEdges": 1,
                    "tempLineageTables": [
                        {"fromEntity": "temporary-source", "toEntity": "temporary-target"}
                    ],
                }
            },
        )
    )
    client = authenticated_client(transport)

    assert not client.recorded_resource_is_absent(
        collection="lineage",
        identifier=_LINEAGE_IDENTIFIER,
    )
    client.delete_recorded_resource(collection="lineage", identifier=_LINEAGE_IDENTIFIER)
    assert client.recorded_resource_is_absent(
        collection="lineage",
        identifier=_LINEAGE_IDENTIFIER,
    )

    assert transport.requests == [
        (
            "GET",
            "http://127.0.0.1:8585/api/v1/lineage/getLineageEdge/"
            f"{_LINEAGE_FROM_ID}/{_LINEAGE_TO_ID}",
        ),
        (
            "DELETE",
            "http://127.0.0.1:8585/api/v1/lineage/glossaryTerm/"
            f"{_LINEAGE_FROM_ID}/glossaryTerm/{_LINEAGE_TO_ID}",
        ),
        (
            "GET",
            "http://127.0.0.1:8585/api/v1/lineage/getLineageEdge/"
            f"{_LINEAGE_FROM_ID}/{_LINEAGE_TO_ID}",
        ),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"edge": {"description": "wrong-provenance"}},
        {"edge": {"description": "validation", "undocumented": "value"}},
        {"notEdge": {"description": "validation"}},
    ],
    ids=("wrong-provenance", "unknown-field", "missing-edge"),
)
def test_recorded_lineage_read_rejects_malformed_or_different_edges(
    body: dict[str, object],
) -> None:
    client = authenticated_client(ExactLineageTransport(_response(200, body)))

    with pytest.raises(CatalogProviderError, match="lineage verification") as captured:
        client.recorded_resource_is_absent(
            collection="lineage",
            identifier=_LINEAGE_IDENTIFIER,
        )

    assert captured.value.classification == "permanent"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_recorded_lineage_read_does_not_treat_a_non_404_failure_as_absent() -> None:
    client = authenticated_client(
        ExactLineageTransport(_response(503, {"message": "temporarily unavailable"}))
    )

    with pytest.raises(CatalogProviderError) as captured:
        client.recorded_resource_is_absent(
            collection="lineage",
            identifier=_LINEAGE_IDENTIFIER,
        )

    assert captured.value.classification == "transient"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
