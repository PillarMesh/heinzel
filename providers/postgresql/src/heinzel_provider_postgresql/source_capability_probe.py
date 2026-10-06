"""The PostgreSQL probe a connection broker validates a source binding with.

`SourceBindingService.validate` drives a binding from `validating` to `ready` only on evidence a
`SourceCapabilityProbe` returned. This is that probe for PostgreSQL. It makes two observations
against the source the binding names, in this order:

* the denial probe, which establishes that the connecting role cannot reach outside the
  declaration -- the same property `PostgreSQLAcquisitionProvider` refuses an acquisition for, and
  against the same subject, `PostgreSQLAcquisitionSettings.unrelated_schema_name`. It is first
  deliberately: a role that reaches further than it was declared is not one to read a source with,
  so nothing is read until the server has refused it.
* the positive probe, which establishes that the declared objects *are* reachable, by running the
  provider's own `observe_source`. That is the same call the acquisition runtime makes, so a
  binding this probe admits is one the acquisition can observe -- and it carries the provider's
  full least-privilege gate (`_require_admitted_metadata`), not a restatement of it here.

Both digests are taken over what the server answered, never over what was asked: the positive
digest is over the observed object identities, schemas, keys and columns, and the denial digest is
over the roles, relations and SQLSTATEs the server returned. Neither includes the observation's
clock reading, so observing an unchanged source twice yields the same digest; the moment belongs
to the evidence's own `observed_at`.

`SourceBindingValidationEvidence` types both `positive_probe_succeeded` and
`denial_probe_succeeded` as `Literal[True]`. So there is no evidence for a probe that failed, and
this module never tries to compose one: every refusal raises `AcquisitionProviderError` and the
only path that reaches the constructor is the one where both probes passed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

import psycopg
from heinzel_connection_broker import (
    PrivateSourceCapability,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
)
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionProviderError,
    AcquisitionSourceObservation,
    SourceObservationRequest,
)
from heinzel_provider_sdk.errors import (
    AcquisitionProviderErrorClassification,
    AcquisitionProviderReasonCode,
)
from psycopg import sql

from .acquisition import PostgreSQLAcquisitionProvider
from .acquisition_settings import PostgreSQLAcquisitionSettings
from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)

__all__ = [
    "PostgreSQLSourceCapabilityAuthority",
    "PostgreSQLSourceCapabilityProbe",
]

_POSITIVE_PROBE_DOMAIN = "heinzel-postgresql-source-positive-probe-v1"
_DENIAL_PROBE_DOMAIN = "heinzel-postgresql-source-denial-probe-v1"
_CAPABILITY_PROFILE_DOMAIN = "heinzel-postgresql-source-capability-profile-v1"
_EVIDENCE_DOMAIN = "heinzel-postgresql-source-validation-evidence-v1"
# The observation is named by its content, because this probe stores no copy of it: whoever holds
# the observation can recompute the digest and see whether it is the one the evidence admitted.
_SOURCE_OBSERVATION_PREFIX = "source-observation:postgresql:"
_EVIDENCE_PREFIX = "source-validation:postgresql:"
# `ERRCODE_INSUFFICIENT_PRIVILEGE`. The denial probe admits this and nothing else: a relation that
# is absent, or a server that is unreachable, is not the source refusing the role.
_INSUFFICIENT_PRIVILEGE_SQLSTATE = "42501"
# Every relation kind the provider's own least-privilege predicate counts as an undeclared
# relation: ordinary, view, materialized view, foreign and partitioned.
_PROBED_RELATION_KINDS = ("r", "v", "m", "f", "p")


class _Cursor(Protocol):
    def execute(self, query: object, params: object = None) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def __iter__(self) -> Iterator[tuple[object, ...]]: ...

    def close(self) -> None: ...

    def __enter__(self) -> _Cursor: ...

    def __exit__(self, *args: object) -> object: ...


class _Connection(Protocol):
    def execute(self, query: str) -> object: ...

    def cursor(self, name: str | None = None) -> _Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _Connection]


class PostgreSQLSourceCapabilityAuthority(Protocol):
    """Resolves the references a private source capability carries into acquisition settings.

    `PrivateSourceCapability.endpoint_reference` and `credential_reference` are references, not
    secrets: which store they name, and what connection detail sits behind them, is the
    deployment's answer and never this provider's. The authority is also where the declaration
    comes from, so this probe can hold the settings it is handed against the binding rather than
    deriving a declaration of its own.
    """

    def resolve_source_acquisition(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        credential_revision: int,
        endpoint_reference: str,
        credential_reference: str,
    ) -> PostgreSQLAcquisitionSettings: ...


@dataclass(frozen=True, slots=True)
class _ObservedDenial:
    """What the server answered when the connecting role reached outside its declaration."""

    connecting_role: str
    unrelated_schema_name: str
    refused_relations: tuple[tuple[str, str, str], ...]


class PostgreSQLSourceCapabilityProbe:
    """Satisfies `heinzel_connection_broker.SourceCapabilityProbe` for PostgreSQL sources."""

    def __init__(
        self,
        *,
        settings_authority: PostgreSQLSourceCapabilityAuthority,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings_authority = settings_authority
        # Retained unsubstituted as well as resolved: the positive probe hands it back to
        # `PostgreSQLAcquisitionProvider`, which decides for itself whether a startup denial can
        # be probed, and a substituted driver reaches no server to probe.
        self._injected_connect = connect
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> SourceBindingValidationEvidence:
        """Return the evidence for a source whose both probes passed, or raise.

        There is no third outcome. `positive_probe_succeeded` and `denial_probe_succeeded` are
        `Literal[True]`, so evidence recording a failed probe is not a value this model has; a
        refusal is an `AcquisitionProviderError` whose classification says which kind it was.
        """
        settings = self._resolved_settings(binding=binding, capability=capability)
        denial = self._probe_denial(settings)
        observation = self._probe_positive(
            binding=binding, settings=settings, observed_at=observed_at
        )
        positive_probe_digest = _positive_probe_digest(observation)
        denial_probe_digest = _denial_probe_digest(denial)
        capability_profile_digest = _capability_profile_digest(
            binding=binding,
            settings=settings,
            positive_probe_digest=positive_probe_digest,
            denial_probe_digest=denial_probe_digest,
        )
        return SourceBindingValidationEvidence(
            evidence_id=_EVIDENCE_PREFIX
            + digest(
                {
                    "domain": _EVIDENCE_DOMAIN,
                    "tenant_id": binding.tenant_id,
                    "binding_id": binding.binding_id,
                    "binding_revision": binding.revision,
                    "credential_revision": capability.credential_revision,
                    "capability_profile_digest": capability_profile_digest,
                    "observed_at": observed_at,
                }
            ),
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            credential_revision=capability.credential_revision,
            provider_kind="postgresql",
            positive_probe_succeeded=True,
            positive_probe_digest=positive_probe_digest,
            denial_probe_succeeded=True,
            denial_probe_digest=denial_probe_digest,
            source_observation_ref=_SOURCE_OBSERVATION_PREFIX + positive_probe_digest,
            capability_profile_digest=capability_profile_digest,
            observed_at=observed_at,
        )

    def _resolved_settings(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
    ) -> PostgreSQLAcquisitionSettings:
        """The settings the capability's references name, held against the binding.

        The declaration matters beyond naming the right relations: the provider's least-privilege
        predicate is evaluated against every object in these settings, so settings declaring more
        than the binding approved would have the denial probe prove least privilege over a wider
        declaration than the binding carries. Equality is required, not containment.
        """
        if binding.provider_kind != "postgresql" or capability.provider_kind != "postgresql":
            raise _provider_error("permanent_configuration")
        try:
            settings = self._settings_authority.resolve_source_acquisition(
                tenant_id=capability.tenant_id,
                binding_id=capability.binding_id,
                credential_revision=capability.credential_revision,
                endpoint_reference=capability.endpoint_reference,
                credential_reference=capability.credential_reference,
            )
        except AcquisitionProviderError:
            raise
        except Exception:
            # The authority resolves a credential, and whatever it raises may quote the DSN it
            # was resolving. Its message stays out of this one and out of the chained cause.
            raise _provider_error("permanent_configuration") from None
        if not isinstance(settings, PostgreSQLAcquisitionSettings):
            raise _provider_error("permanent_configuration")
        if settings.connection_handle != binding.connection_handle:
            raise _provider_error("permanent_configuration")
        declared = tuple(sorted(item.logical_object_ref for item in settings.objects))
        if declared != binding.approved_object_refs:
            raise _provider_error("permanent_configuration")
        return settings

    def _probe_denial(self, settings: PostgreSQLAcquisitionSettings) -> _ObservedDenial:
        """Observe the server refusing the connecting role every reach outside its declaration."""
        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connecting_role = _require_least_privileged_role(
                connection, settings.unrelated_schema_name
            )
            relations = _observed_unrelated_relations(connection, settings.unrelated_schema_name)
            refused = _refused_unrelated_reads(
                connection, settings.unrelated_schema_name, relations
            )
            connection.rollback()
            connection.close()
            connection = None
            return _ObservedDenial(
                connecting_role=connecting_role,
                unrelated_schema_name=settings.unrelated_schema_name,
                refused_relations=refused,
            )
        except AcquisitionProviderError:
            _abort_connection(connection)
            raise
        except psycopg.Error as error:
            _abort_connection(connection)
            raise _translate_driver_error(error) from None
        except (TypeError, ValueError):
            _abort_connection(connection)
            raise _provider_error("permanent_configuration") from None
        except Exception:
            _abort_connection(connection)
            raise _provider_error("integrity_failure") from None

    def _probe_positive(
        self,
        *,
        binding: SourceConnectionBinding,
        settings: PostgreSQLAcquisitionSettings,
        observed_at: datetime,
    ) -> AcquisitionSourceObservation:
        """Observe the declared objects through the provider the acquisition itself would use."""
        provider = PostgreSQLAcquisitionProvider(
            settings,
            connect=self._injected_connect,
            startup_denial_probe=self._startup_denial_probe,
            # The observation is stamped with the moment this validation is being evidenced for,
            # so the evidence and the observation it digests cannot disagree about when they
            # happened.
            clock=lambda: observed_at,
            private_boundary_reference_factory=_refuse_private_boundary,
            private_boundary_writer=_refuse_private_boundary_write,
        )
        observation = provider.observe_source(
            SourceObservationRequest(
                tenant_id=binding.tenant_id,
                source_binding_ref=binding.binding_id,
                object_refs=binding.approved_object_refs,
            )
        )
        if (
            observation.provider_kind != "postgresql"
            or observation.tenant_id != binding.tenant_id
            or observation.source_binding_ref != binding.binding_id
            or tuple(item.logical_object_ref for item in observation.object_observations)
            != binding.approved_object_refs
        ):
            raise _provider_error("invalid_provider_response")
        return observation


def _require_least_privileged_role(connection: _Connection, unrelated_schema_name: str) -> str:
    """The connecting role, once the server has answered that it holds nothing it should not.

    Every answer here is the server's, read back under the role the capability resolved to: who
    it is, whether the unrelated schema is there to be refused on, whether `USAGE` on it is
    withheld, and whether the role carries a cluster-wide attribute that would make any relation
    reachable whatever the grants say. `current_user` must equal `session_user` for the rest to
    mean anything -- answers taken inside `SET ROLE` describe the assumed role, not the one whose
    credential was resolved.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, session_user, "
            "(SELECT count(*) FROM pg_catalog.pg_namespace namespace "
            "WHERE namespace.nspname = %s), "
            "(SELECT has_schema_privilege(current_user, namespace.oid, 'USAGE') "
            "FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname = %s), "
            "role.rolsuper, role.rolcreatedb, role.rolcreaterole, "
            "role.rolreplication, role.rolbypassrls "
            "FROM pg_catalog.pg_roles role WHERE role.rolname = current_user",
            (unrelated_schema_name, unrelated_schema_name),
        )
        row = cursor.fetchone()
    if row is None or len(row) != 9:
        raise _provider_error("invalid_provider_response")
    connecting_role, session_role, schemas_named, usage_granted = row[0], row[1], row[2], row[3]
    if (
        not isinstance(connecting_role, str)
        or not isinstance(session_role, str)
        or type(schemas_named) is not int
    ):
        raise _provider_error("invalid_provider_response")
    if connecting_role != session_role:
        raise _provider_error("authorization_denied")
    if schemas_named != 1:
        # The unrelated schema is the subject this probe is refused on. Without it the probe
        # would pass by having nothing to try, which is the one way it must not pass -- the same
        # reason the provider's own predicate requires the schema to exist.
        raise _provider_error("permanent_configuration")
    if usage_granted is not False:
        raise _provider_error("authorization_denied")
    if any(attribute is not False for attribute in row[4:]):
        raise _provider_error("authorization_denied")
    return connecting_role


def _observed_unrelated_relations(
    connection: _Connection, unrelated_schema_name: str
) -> tuple[tuple[str, str], ...]:
    """The relations the server reports in the unrelated schema, by name and kind.

    The catalog is readable without `USAGE` on the schema, which is what makes this possible at
    all: the role can see that these relations exist and still be unable to read one.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relation.relname, relation.relkind FROM pg_catalog.pg_class relation "
            "JOIN pg_catalog.pg_namespace namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = %s AND relation.relkind = ANY(%s) "
            "ORDER BY relation.relname",
            (unrelated_schema_name, list(_PROBED_RELATION_KINDS)),
        )
        rows = cursor.fetchall()
    observed: list[tuple[str, str]] = []
    for row in rows:
        if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
            raise _provider_error("invalid_provider_response")
        observed.append((row[0], row[1]))
    if not observed:
        raise _provider_error("permanent_configuration")
    return tuple(observed)


def _refused_unrelated_reads(
    connection: _Connection,
    unrelated_schema_name: str,
    relations: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str, str], ...]:
    """Attempt to read every relation in the unrelated schema, and return what was refused.

    A privilege answer from the catalog says what the server believes; this says what it does.
    Only `insufficient_privilege` counts: any other failure is the statement or the server going
    wrong rather than the role being held out, and a read that *succeeds* is the binding's
    declaration being wider in practice than on paper.
    """
    refused: list[tuple[str, str, str]] = []
    for relation_name, relation_kind in relations:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("/* source_denial_probe */ SELECT 1 FROM {} LIMIT 1").format(
                        sql.Identifier(unrelated_schema_name, relation_name)
                    )
                )
                cursor.fetchall()
        except psycopg.Error as error:
            sqlstate = error.sqlstate or ""
            if sqlstate != _INSUFFICIENT_PRIVILEGE_SQLSTATE:
                raise _translate_driver_error(error) from None
            refused.append((relation_name, relation_kind, sqlstate))
            # The refusal aborted the transaction, and the next attempt would fail for that
            # rather than for its own privileges.
            connection.rollback()
        else:
            raise _provider_error("authorization_denied")
    return tuple(refused)


def _positive_probe_digest(observation: AcquisitionSourceObservation) -> str:
    """A digest over what the source turned out to be, not over when it was looked at.

    `observed_at` and the connection handle are left out deliberately. The first is a clock
    reading, which would make two observations of an unchanged source disagree; the second is
    configuration this probe was handed rather than anything the server answered. The moment is
    carried by the evidence's own `observed_at`, and the handle by the capability profile.
    """
    return digest(
        {
            "domain": _POSITIVE_PROBE_DOMAIN,
            "provider_kind": observation.provider_kind,
            "object_observations": tuple(
                {
                    "logical_object_ref": item.logical_object_ref,
                    "object_identity": item.provider_observation.object_identity,
                    "object_kind": item.provider_observation.object_kind,
                    "schema_digest": item.provider_observation.schema_digest,
                    "columns": item.provider_observation.columns,
                    "key_name": item.provider_observation.key_name,
                    "key_type": item.provider_observation.key_type,
                    "key_nullable": item.provider_observation.key_nullable,
                    "key_constraint": item.provider_observation.key_constraint,
                    "stable_key_order": item.provider_observation.stable_key_order,
                    "read_only": item.provider_observation.read_only,
                    "capabilities": item.provider_observation.capabilities,
                    "snapshot_semantics": item.provider_observation.snapshot_semantics,
                    "evidence_safe": item.provider_observation.evidence_safe,
                }
                for item in observation.object_observations
            ),
        }
    )


def _denial_probe_digest(observed: _ObservedDenial) -> str:
    """A digest over the refusals themselves: the role, the relations, and each SQLSTATE."""
    return digest(
        {
            "domain": _DENIAL_PROBE_DOMAIN,
            "connecting_role": observed.connecting_role,
            "unrelated_schema_name": observed.unrelated_schema_name,
            "refused_relations": tuple(
                {"relation_name": name, "relation_kind": kind, "sqlstate": sqlstate}
                for name, kind, sqlstate in observed.refused_relations
            ),
        }
    )


def _capability_profile_digest(
    *,
    binding: SourceConnectionBinding,
    settings: PostgreSQLAcquisitionSettings,
    positive_probe_digest: str,
    denial_probe_digest: str,
) -> str:
    """A digest over everything the ready binding's authority rests on.

    Both probe digests are part of it, so a source whose schema drifted or a role whose grants
    widened produces a different profile -- which is the point: the binding's `ready` state names
    this profile, and a profile that could not change would say nothing about the source.
    """
    return digest(
        {
            "domain": _CAPABILITY_PROFILE_DOMAIN,
            "provider_kind": "postgresql",
            "connection_handle": binding.connection_handle,
            "account_mode": binding.account_mode,
            "approved_object_refs": binding.approved_object_refs,
            "declared_objects": tuple(
                {
                    "logical_object_ref": declaration.logical_object_ref,
                    "schema_name": declaration.schema_name,
                    "table_name": declaration.table_name,
                    "field_names": declaration.field_names,
                    "key_name": declaration.key_name,
                    "source_updated_at_field": declaration.source_updated_at_field,
                }
                for declaration in sorted(
                    settings.objects, key=lambda item: item.logical_object_ref
                )
            ),
            "unrelated_schema_name": settings.unrelated_schema_name,
            "positive_probe_digest": positive_probe_digest,
            "denial_probe_digest": denial_probe_digest,
        }
    )


def _refuse_private_boundary(tenant_id: str, reference: str) -> str:
    """Refuse to name a private boundary payload: observing a source writes none.

    `PostgreSQLAcquisitionProvider` requires these collaborators because an acquisition needs
    them. `observe_source` does not, so rather than hand it somewhere to put a payload -- or a
    sink that discarded one -- this refuses, and a provider that ever did reach for one during a
    validation fails here instead of silently losing it.
    """
    raise _provider_error("permanent_configuration")


def _refuse_private_boundary_write(tenant_id: str, reference: str, payload: bytes) -> None:
    raise _provider_error("permanent_configuration")


def _abort_connection(connection: _Connection | None) -> None:
    if connection is None:
        return
    with suppress(Exception):
        connection.rollback()
    with suppress(Exception):
        connection.close()


def _provider_error(reason_code: AcquisitionProviderReasonCode) -> AcquisitionProviderError:
    classification_by_reason: dict[
        AcquisitionProviderReasonCode, AcquisitionProviderErrorClassification
    ] = {
        "authorization_denied": "authorization_denied",
        "integrity_failure": "integrity_failure",
        "invalid_provider_response": "invalid_provider_response",
        "permanent_configuration": "permanent_configuration",
        "provider_unavailable": "transient_unavailable",
        "rate_limited": "throttled",
        "statement_rejected": "statement_rejected",
        "transport_failure": "transient_transport",
    }
    return AcquisitionProviderError(
        provider_kind="postgresql",
        classification=classification_by_reason[reason_code],
        reason_code=reason_code,
    )


def _translate_driver_error(error: psycopg.Error) -> AcquisitionProviderError:
    """Classify a driver failure, keeping a rejection distinguishable from a transient one."""
    sqlstate = error.sqlstate or ""
    if isinstance(
        error,
        (
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.InvalidAuthorizationSpecification,
            psycopg.errors.InvalidPassword,
        ),
    ) or sqlstate.startswith("28"):
        return _provider_error("authorization_denied")
    if sqlstate == "53300":
        return _provider_error("rate_limited")
    if isinstance(error, psycopg.InterfaceError) or sqlstate.startswith("08"):
        return _provider_error("transport_failure")
    if sqlstate.startswith(("40", "53", "57", "58")) or isinstance(error, psycopg.OperationalError):
        return _provider_error("provider_unavailable")
    if isinstance(error, psycopg.DataError):
        return _provider_error("invalid_provider_response")
    if isinstance(error, psycopg.IntegrityError):
        return _provider_error("integrity_failure")
    if isinstance(error, psycopg.DatabaseError):
        return _provider_error("statement_rejected")
    return _provider_error("provider_unavailable")
