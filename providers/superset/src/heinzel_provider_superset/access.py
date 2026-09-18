from __future__ import annotations

from typing import Literal, Protocol, Self

from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProviderError,
    AccessEffectResult,
    AccessPermission,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import (
    HttpSupersetClient,
    HttpxSupersetTransport,
    SupersetCredentialResolver,
    SupersetHttpTransport,
)
from .provider import SupersetClientError


class _AccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SupersetAccessTarget(_AccessModel):
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    provider_resource_ref: str = Field(min_length=1)
    dashboard_id: int = Field(ge=1)
    grant_scoped_role_id: int = Field(ge=1)
    base_role_ids: tuple[int, ...]
    credential_secret_ref: str = Field(min_length=1)
    fields: tuple[str, ...] = Field(min_length=1)
    permissions: tuple[AccessPermission, ...] = ("dashboard", "view")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def authority_sets_are_canonical(self) -> Self:
        if self.base_role_ids != tuple(sorted(self.base_role_ids)) or len(
            self.base_role_ids
        ) != len(set(self.base_role_ids)):
            raise ValueError("base Superset role ids must be unique and canonical")
        if any(role_id < 1 for role_id in self.base_role_ids):
            raise ValueError("base Superset role ids must be positive")
        if self.grant_scoped_role_id in self.base_role_ids:
            raise ValueError("grant-scoped Superset role must not be a base role")
        if self.fields != tuple(sorted(self.fields)) or len(self.fields) != len(set(self.fields)):
            raise ValueError("Superset access fields must be unique and canonical")
        if self.permissions != tuple(sorted(self.permissions)) or len(self.permissions) != len(
            set(self.permissions)
        ):
            raise ValueError("Superset access permissions must be unique and canonical")
        if self.permissions != ("dashboard", "view"):
            raise ValueError("Superset access requires dashboard and view permissions")
        return self


class SupersetAccessTargetAuthority(Protocol):
    def resolve(self, command: AccessEffectCommand) -> SupersetAccessTarget | None: ...


class SupersetAccessAuthorityUnavailable(RuntimeError):
    pass


class SupersetAccessAuthorityInvalid(RuntimeError):
    pass


class CredentialScopedSupersetAccessEffectProvider:
    surface: Literal["superset"] = "superset"
    provider_version = "superset-access-v1"

    def __init__(
        self,
        *,
        targets: SupersetAccessTargetAuthority,
        credentials: SupersetCredentialResolver,
        transport: SupersetHttpTransport | None = None,
    ) -> None:
        self._targets = targets
        self._credentials = credentials
        self._transport = transport or HttpxSupersetTransport()

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        try:
            target = self._targets.resolve(command)
        except (SupersetAccessAuthorityUnavailable, OSError, TimeoutError):
            raise self._error(command, "transient_failure") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None
        if target is None or not self._matches(command, target):
            raise self._error(command, "permanent_failure")
        try:
            credentials = self._credentials.resolve(secret_reference=target.credential_secret_ref)
        except (OSError, TimeoutError):
            raise self._error(command, "transient_failure") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None

        expected_roles, desired_roles = self._role_transition(command, target)
        try:
            HttpSupersetClient(
                credentials=credentials,
                transport=self._transport,
            ).reconcile_dashboard_roles(
                dashboard_id=target.dashboard_id,
                expected_role_ids=expected_roles,
                desired_role_ids=desired_roles,
            )
        except SupersetClientError as error:
            raise self._error(command, self._failure(error.classification)) from None
        except Exception:
            raise self._error(command, "permanent_failure") from None

        return AccessEffectResult(
            surface=self.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "heinzel.superset-access-effect.v1",
                    "provider_version": self.provider_version,
                    "command": command,
                    "target": target,
                }
            ),
        )

    @staticmethod
    def _matches(command: AccessEffectCommand, target: SupersetAccessTarget) -> bool:
        return (
            command.surface == "superset"
            and target.tenant_id == command.tenant_id
            and target.grant_id == command.grant_id
            and target.grant_revision == command.grant_revision
            and target.principal_ref == command.principal_ref
            and target.provider_resource_ref == command.provider_resource_ref
            and target.fields == command.fields
            and target.permissions == command.permissions
            and target.scope_digest == command.scope_digest
        )

    @staticmethod
    def _role_transition(
        command: AccessEffectCommand, target: SupersetAccessTarget
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        roles_with_grant = tuple(sorted((*target.base_role_ids, target.grant_scoped_role_id)))
        if command.action == "apply":
            return target.base_role_ids, roles_with_grant
        return roles_with_grant, target.base_role_ids

    @staticmethod
    def _failure(classification: str) -> AccessEffectFailure:
        if classification in {"transient_transport", "transient_unavailable", "throttled"}:
            return "transient_failure"
        if classification == "ambiguous_outcome":
            return "ambiguous_outcome"
        return "permanent_failure"

    @staticmethod
    def _error(
        command: AccessEffectCommand, outcome: AccessEffectFailure
    ) -> AccessEffectProviderError:
        return AccessEffectProviderError(
            outcome=outcome,
            provider_receipt_digest=digest(
                {
                    "domain": "heinzel.superset-access-effect-error.v1",
                    "command": command,
                    "outcome": outcome,
                }
            ),
        )
