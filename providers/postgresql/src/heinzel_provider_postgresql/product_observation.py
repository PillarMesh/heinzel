from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, Protocol, Self, cast

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from heinzel_contract_model import ArtifactModel, digest
from heinzel_dbt_adapter import SignedCompiledDbtModel, compiled_dbt_model_signing_bytes
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_runtime import ProductMaterializationReceipt
from psycopg import sql
from pydantic import ConfigDict, Field, field_validator, model_validator

from .product_materialization import (
    PostgreSQLMaterializationSettings,
    _MagnitudeCheckResults,
    _postgresql_decimal_magnitude_checks,
    _postgresql_product_generation_commit_reference,
)
from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"


class _StrictArtifact(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PostgreSQLObservedProductColumn(_StrictArtifact):
    ordinal: int = Field(ge=1)
    name: str = Field(pattern=_IDENTIFIER_PATTERN)
    type_oid: int = Field(gt=0)
    formatted_type: str = Field(min_length=1, max_length=256)
    nullable: bool
    collation_oid: int | None = Field(default=None, gt=0)
    collation_schema: str | None = Field(default=None, min_length=1, max_length=63)
    collation_name: str | None = Field(default=None, min_length=1, max_length=63)
    collation_provider: str | None = Field(default=None, min_length=1, max_length=1)
    collation_deterministic: bool | None = None

    @model_validator(mode="after")
    def collation_identity_is_complete(self) -> Self:
        values = (
            self.collation_oid,
            self.collation_schema,
            self.collation_name,
            self.collation_provider,
            self.collation_deterministic,
        )
        if any(value is None for value in values) and not all(value is None for value in values):
            raise ValueError("column collation identity must be complete")
        return self


class PostgreSQLObservedUniqueKeyColumn(_StrictArtifact):
    key_position: int = Field(ge=1)
    physical_ordinal: int = Field(ge=1)
    name: str = Field(pattern=_IDENTIFIER_PATTERN)


class PostgreSQLObservedUniqueConstraint(_StrictArtifact):
    constraint_oid: int = Field(gt=0)
    constraint_name: str = Field(min_length=1, max_length=63)
    constraint_kind: Literal["p", "u"]
    backing_index_oid: int = Field(gt=0)
    validated: Literal[True] = True
    deferrable: Literal[False] = False
    initially_deferred: Literal[False] = False
    backing_index_valid: Literal[True] = True
    backing_index_ready: Literal[True] = True
    backing_index_unique: Literal[True] = True
    partial: Literal[False] = False
    expression: Literal[False] = False
    key_columns: tuple[PostgreSQLObservedUniqueKeyColumn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def key_columns_are_ordered_and_distinct(self) -> Self:
        positions = tuple(column.key_position for column in self.key_columns)
        if positions != tuple(range(1, len(self.key_columns) + 1)):
            raise ValueError("unique constraint key positions must be contiguous")
        ordinals = tuple(column.physical_ordinal for column in self.key_columns)
        names = tuple(column.name for column in self.key_columns)
        if len(set(ordinals)) != len(ordinals) or len(set(names)) != len(names):
            raise ValueError("unique constraint key columns must be distinct")
        return self


class PostgreSQLProductSemanticObservation(_StrictArtifact):
    schema_version: Literal["2"] = "2"
    engine_kind: Literal["postgresql"] = "postgresql"
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    product_generation: int = Field(ge=1)
    server_version_num: str = Field(pattern=r"^[0-9]{5,6}$")
    engine_version: str = Field(min_length=1, max_length=128)
    engine_build_digest: str = Field(pattern=_DIGEST_PATTERN)
    current_database: str = Field(min_length=1, max_length=63)
    session_timezone: str = Field(min_length=1, max_length=128)
    database_collation: str = Field(min_length=1, max_length=128)
    database_character_classification: str = Field(min_length=1, max_length=128)
    relation_schema: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    schema_oid: int = Field(gt=0)
    relation_oid: int = Field(gt=0)
    relation_file_node: int = Field(ge=0)
    relation_kind: Literal["r", "p"]
    columns: tuple[PostgreSQLObservedProductColumn, ...] = Field(min_length=1)
    eligible_unique_constraints: tuple[PostgreSQLObservedUniqueConstraint, ...] = ()
    provider_commit_reference: str = Field(pattern=_DIGEST_PATTERN)
    retained_until: datetime
    observed_at: datetime

    @field_validator("retained_until", "observed_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("product observation timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def relation_observation_is_ordered_and_retained(self) -> Self:
        ordinals = tuple(column.ordinal for column in self.columns)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError("product relation columns must be in physical order")
        if self.retained_until <= self.observed_at:
            raise ValueError("product relation retention has expired")
        columns_by_ordinal = {column.ordinal: column for column in self.columns}
        constraint_oids = tuple(
            constraint.constraint_oid for constraint in self.eligible_unique_constraints
        )
        constraint_names = tuple(
            constraint.constraint_name for constraint in self.eligible_unique_constraints
        )
        if len(set(constraint_oids)) != len(constraint_oids) or len(set(constraint_names)) != len(
            constraint_names
        ):
            raise ValueError("eligible unique constraints must be distinct")
        for constraint in self.eligible_unique_constraints:
            for key_column in constraint.key_columns:
                observed_column = columns_by_ordinal.get(key_column.physical_ordinal)
                if (
                    observed_column is None
                    or observed_column.name != key_column.name
                    or observed_column.nullable
                ):
                    raise ValueError(
                        "eligible unique constraint must use observed non-null columns"
                    )
        return self


class _Cursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class _Connection(Protocol):
    def execute(self, query: object, params: tuple[object, ...] = ()) -> _Cursor: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _Connection]


class PostgreSQLProductSemanticObserver:
    def __init__(
        self,
        settings: PostgreSQLMaterializationSettings,
        *,
        signed_model: SignedCompiledDbtModel | None = None,
        trusted_compiler_keys: dict[str, Ed25519PublicKey] | None = None,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings = settings
        self._signed_model = signed_model
        self._trusted_compiler_keys = dict(trusted_compiler_keys or {})
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def observe(
        self, request: ProductMaterializationReceipt
    ) -> PostgreSQLProductSemanticObservation:
        if request.tenant_id != self._settings.tenant_id:
            raise ProviderError("PostgreSQL product observation failed", "authorization_denied")
        signed_model = _validated_signed_model(
            self._signed_model,
            request,
            trusted_compiler_keys=self._trusted_compiler_keys,
        )

        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            generation = connection.execute(
                sql.SQL(
                    "SELECT generation.generation_schema, generation.generation_table, "
                    "generation.provider_commit_reference, generation.retained_until, "
                    "pointer.product_revision, pointer.product_generation, "
                    "pointer.generation_schema, pointer.generation_table, "
                    "pointer.provider_commit_reference "
                    "FROM {}.{} AS generation "
                    "JOIN {}.{} AS pointer ON pointer.tenant_id = generation.tenant_id "
                    "AND pointer.product_id = generation.product_id "
                    "WHERE generation.tenant_id = %s AND generation.product_id = %s "
                    "AND generation.product_revision = %s AND generation.product_generation = %s"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_table_name),
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_pointer_table_name),
                ),
                (
                    request.tenant_id,
                    request.product_id,
                    request.product_revision,
                    request.product_generation,
                ),
            ).fetchone()
            generation_values = _generation_values(generation, request)

            context = connection.execute(
                "SELECT current_database(), current_setting('server_version_num'), "
                "current_setting('server_version'), "
                "current_setting('TimeZone'), database.datcollate, database.datctype, "
                "transaction_timestamp() FROM pg_catalog.pg_database AS database "
                "WHERE database.datname = current_database()"
            ).fetchone()
            context_values = _context_values(context)
            if generation_values.retained_until <= context_values.observed_at:
                raise ProviderError("PostgreSQL product observation failed", "integrity_failure")

            relation = connection.execute(
                "SELECT namespace.oid, relation.oid, relation.relfilenode, relation.relkind, "
                "has_schema_privilege(current_user, namespace.oid, 'USAGE'), "
                "has_table_privilege(current_user, relation.oid, 'SELECT') "
                "FROM pg_catalog.pg_class AS relation "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = %s AND relation.relname = %s "
                "AND relation.relkind IN ('r', 'p')",
                (generation_values.schema_name, generation_values.relation_name),
            ).fetchone()
            relation_values = _relation_values(relation)
            if not relation_values.can_use_schema or not relation_values.can_select_relation:
                raise ProviderError("PostgreSQL product observation failed", "authorization_denied")

            column_rows = connection.execute(
                "SELECT attribute.attnum, attribute.attname, attribute.atttypid, "
                "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
                "NOT attribute.attnotnull, NULLIF(attribute.attcollation, 0), "
                "collation_namespace.nspname, column_collation.collname, "
                "column_collation.collprovider, column_collation.collisdeterministic "
                "FROM pg_catalog.pg_attribute AS attribute "
                "LEFT JOIN pg_catalog.pg_collation AS column_collation "
                "ON column_collation.oid = NULLIF(attribute.attcollation, 0) "
                "LEFT JOIN pg_catalog.pg_namespace AS collation_namespace "
                "ON collation_namespace.oid = column_collation.collnamespace "
                "WHERE attribute.attrelid = %s AND attribute.attnum > 0 "
                "AND NOT attribute.attisdropped ORDER BY attribute.attnum",
                (relation_values.relation_oid,),
            ).fetchall()
            columns = _columns(column_rows)
            magnitude_checks: _MagnitudeCheckResults = ()
            if signed_model is not None:
                if (
                    signed_model.model.target_schema != generation_values.schema_name
                    or signed_model.model.model_name != generation_values.relation_name
                ):
                    raise ProviderError(
                        "PostgreSQL product observation failed", "integrity_failure"
                    )
                magnitude_checks = _postgresql_decimal_magnitude_checks(
                    connection,
                    target_schema=generation_values.schema_name,
                    model_name=generation_values.relation_name,
                    checks=signed_model.model.output_magnitude_checks,
                    observed_column_names={column.name for column in columns},
                )
            observed_commit_reference = _product_generation_commit_reference(
                request=request,
                relation_oid=relation_values.relation_oid,
                relation_file_node=relation_values.relation_file_node,
                magnitude_checks=magnitude_checks,
            )
            if observed_commit_reference != generation_values.provider_commit_reference:
                raise ProviderError("PostgreSQL product observation failed", "integrity_failure")
            constraint_rows = connection.execute(
                "SELECT constraint_row.oid, constraint_row.conname, "
                "constraint_row.contype, constraint_row.convalidated, "
                "constraint_row.condeferrable, constraint_row.condeferred, "
                "constraint_row.conindid, backing_index.indisvalid, "
                "backing_index.indisready, backing_index.indisunique, "
                "backing_index.indpred IS NOT NULL, backing_index.indexprs IS NOT NULL, "
                "key_column.key_position, attribute.attnum, attribute.attname, "
                "attribute.attnotnull FROM pg_catalog.pg_constraint AS constraint_row "
                "JOIN pg_catalog.pg_index AS backing_index "
                "ON backing_index.indexrelid = constraint_row.conindid "
                "CROSS JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY "
                "AS key_column(attribute_number, key_position) "
                "JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = constraint_row.conrelid "
                "AND attribute.attnum = key_column.attribute_number "
                "WHERE constraint_row.conrelid = %s "
                "AND constraint_row.contype IN ('p', 'u') "
                "ORDER BY constraint_row.oid, key_column.key_position",
                (relation_values.relation_oid,),
            ).fetchall()
            eligible_unique_constraints = _eligible_unique_constraints(
                constraint_rows, columns=columns
            )

            return PostgreSQLProductSemanticObservation(
                tenant_id=request.tenant_id,
                product_id=request.product_id,
                product_revision=request.product_revision,
                product_generation=request.product_generation,
                server_version_num=context_values.server_version_num,
                engine_version=context_values.engine_version,
                engine_build_digest=_postgresql_engine_build_digest(context_values),
                current_database=context_values.current_database,
                session_timezone=context_values.session_timezone,
                database_collation=context_values.database_collation,
                database_character_classification=(
                    context_values.database_character_classification
                ),
                relation_schema=generation_values.schema_name,
                relation_name=generation_values.relation_name,
                schema_oid=relation_values.schema_oid,
                relation_oid=relation_values.relation_oid,
                relation_file_node=relation_values.relation_file_node,
                relation_kind=relation_values.relation_kind,
                columns=columns,
                eligible_unique_constraints=eligible_unique_constraints,
                provider_commit_reference=generation_values.provider_commit_reference,
                retained_until=generation_values.retained_until,
                observed_at=context_values.observed_at,
            )
        except ProviderError:
            raise
        except psycopg.Error as error:
            raise _observation_error(error) from None
        except (TypeError, ValueError):
            raise ProviderError(
                "PostgreSQL product observation failed", "invalid_provider_response"
            ) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.execute("ROLLBACK")
                with suppress(Exception):
                    connection.close()


class _GenerationValues(_StrictArtifact):
    schema_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    provider_commit_reference: str = Field(pattern=_DIGEST_PATTERN)
    retained_until: datetime


class _ContextValues(_StrictArtifact):
    current_database: str = Field(min_length=1, max_length=63)
    server_version_num: str = Field(pattern=r"^[0-9]{5,6}$")
    engine_version: str = Field(min_length=1, max_length=128)
    session_timezone: str = Field(min_length=1, max_length=128)
    database_collation: str = Field(min_length=1, max_length=128)
    database_character_classification: str = Field(min_length=1, max_length=128)
    observed_at: datetime


class _RelationValues(_StrictArtifact):
    schema_oid: int = Field(gt=0)
    relation_oid: int = Field(gt=0)
    relation_file_node: int = Field(ge=0)
    relation_kind: Literal["r", "p"]
    can_use_schema: bool
    can_select_relation: bool


class _UniqueConstraintFacts(_StrictArtifact):
    constraint_oid: int = Field(gt=0)
    constraint_name: str = Field(min_length=1, max_length=63)
    constraint_kind: Literal["p", "u"]
    validated: bool
    deferrable: bool
    initially_deferred: bool
    backing_index_oid: int = Field(gt=0)
    backing_index_valid: bool
    backing_index_ready: bool
    backing_index_unique: bool
    partial: bool
    expression: bool


def _generation_values(
    row: tuple[object, ...] | None,
    request: ProductMaterializationReceipt,
) -> _GenerationValues:
    if row is None or len(row) != 9:
        raise ProviderError("PostgreSQL product observation failed", "integrity_failure")
    if (
        row[4] != request.product_revision
        or row[5] != request.product_generation
        or row[6] != row[0]
        or row[7] != row[1]
        or row[8] != row[2]
        or row[2] != request.provider_commit_reference
    ):
        raise ProviderError("PostgreSQL product observation failed", "integrity_failure")
    values = _GenerationValues.model_validate(
        {
            "schema_name": row[0],
            "relation_name": row[1],
            "provider_commit_reference": row[2],
            "retained_until": row[3],
        }
    )
    if values.retained_until.tzinfo is None or values.retained_until.utcoffset() is None:
        raise ValueError("generation retention timestamp is invalid")
    return values.model_copy(update={"retained_until": values.retained_until.astimezone(UTC)})


def _product_generation_commit_reference(
    *,
    request: ProductMaterializationReceipt,
    relation_oid: int,
    relation_file_node: int,
    magnitude_checks: _MagnitudeCheckResults = (),
) -> str:
    return _postgresql_product_generation_commit_reference(
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        product_revision=request.product_revision,
        product_generation=request.product_generation,
        model_digest=request.compiled_model_digest,
        relation_identity=(str(relation_oid), str(relation_file_node)),
        magnitude_checks=magnitude_checks,
    )


def _validated_signed_model(
    signed_model: SignedCompiledDbtModel | None,
    request: ProductMaterializationReceipt,
    *,
    trusted_compiler_keys: dict[str, Ed25519PublicKey],
) -> SignedCompiledDbtModel | None:
    if signed_model is None:
        return None
    try:
        if not set(vars(signed_model)).issubset(type(signed_model).model_fields):
            raise ValueError("signed model contains undeclared fields")
        signing_bytes = compiled_dbt_model_signing_bytes(signed_model.model)
        validated = SignedCompiledDbtModel.model_validate(
            signed_model.model_dump(mode="python"), strict=True
        )
        public_key = trusted_compiler_keys.get(validated.key_id)
        if public_key is None:
            raise InvalidSignature
        signature = base64.b64decode(validated.signature, validate=True)
        public_key.verify(signature, signing_bytes)
    except (AttributeError, binascii.Error, InvalidSignature, TypeError, ValueError):
        raise ProviderError("PostgreSQL product observation failed", "integrity_failure") from None
    if (
        digest(validated.model) != validated.model_digest
        or validated.model_digest != request.compiled_model_digest
        or validated.model.contract_digest != request.contract_digest
        or validated.model.provider != "postgresql"
        or validated.model.input_generation_digests != request.input_generation_digests
    ):
        raise ProviderError("PostgreSQL product observation failed", "integrity_failure")
    return validated


def _context_values(row: tuple[object, ...] | None) -> _ContextValues:
    if row is None or len(row) != 7:
        raise ValueError("PostgreSQL execution context is invalid")
    return _ContextValues.model_validate(
        {
            "current_database": row[0],
            "server_version_num": row[1],
            "engine_version": row[2],
            "session_timezone": row[3],
            "database_collation": row[4],
            "database_character_classification": row[5],
            "observed_at": row[6],
        }
    )


def _postgresql_engine_build_digest(context: _ContextValues) -> str:
    return digest(
        {
            "domain": "heinzel-postgresql-engine-build-v1",
            "version": {
                "server_version_num": context.server_version_num,
                "server_version": context.engine_version,
            },
        }
    )


def _relation_values(row: tuple[object, ...] | None) -> _RelationValues:
    if row is None or len(row) != 6:
        raise ProviderError("PostgreSQL product observation failed", "integrity_failure")
    return _RelationValues.model_validate(
        {
            "schema_oid": row[0],
            "relation_oid": row[1],
            "relation_file_node": row[2],
            "relation_kind": row[3],
            "can_use_schema": row[4],
            "can_select_relation": row[5],
        }
    )


def _columns(rows: list[tuple[object, ...]]) -> tuple[PostgreSQLObservedProductColumn, ...]:
    if not rows or any(len(row) != 10 for row in rows):
        raise ValueError("PostgreSQL column observation is invalid")
    return tuple(
        PostgreSQLObservedProductColumn.model_validate(
            {
                "ordinal": row[0],
                "name": row[1],
                "type_oid": row[2],
                "formatted_type": row[3],
                "nullable": row[4],
                "collation_oid": row[5],
                "collation_schema": row[6],
                "collation_name": row[7],
                "collation_provider": row[8],
                "collation_deterministic": row[9],
            }
        )
        for row in rows
    )


def _eligible_unique_constraints(
    rows: list[tuple[object, ...]],
    *,
    columns: tuple[PostgreSQLObservedProductColumn, ...],
) -> tuple[PostgreSQLObservedUniqueConstraint, ...]:
    if any(len(row) != 16 for row in rows):
        raise ValueError("PostgreSQL unique constraint observation is invalid")
    columns_by_ordinal = {column.ordinal: column for column in columns}
    grouped_rows: dict[int, list[tuple[object, ...]]] = {}
    for row in rows:
        constraint_oid = row[0]
        if type(constraint_oid) is not int:
            raise ValueError("PostgreSQL unique constraint identity is invalid")
        grouped_rows.setdefault(constraint_oid, []).append(row)

    constraints: list[PostgreSQLObservedUniqueConstraint] = []
    for constraint_rows in grouped_rows.values():
        first = constraint_rows[0]
        identity = first[:12]
        if any(row[:12] != identity for row in constraint_rows):
            raise ValueError("PostgreSQL unique constraint facts are inconsistent")
        facts = _UniqueConstraintFacts.model_validate(
            {
                "constraint_oid": first[0],
                "constraint_name": first[1],
                "constraint_kind": first[2],
                "validated": first[3],
                "deferrable": first[4],
                "initially_deferred": first[5],
                "backing_index_oid": first[6],
                "backing_index_valid": first[7],
                "backing_index_ready": first[8],
                "backing_index_unique": first[9],
                "partial": first[10],
                "expression": first[11],
            }
        )
        key_columns: list[dict[str, object]] = []
        all_key_columns_non_null = True
        for row in constraint_rows:
            key_position = row[12]
            physical_ordinal = row[13]
            column_name = row[14]
            observed_non_null = row[15]
            if (
                type(key_position) is not int
                or type(physical_ordinal) is not int
                or type(column_name) is not str
            ):
                raise ValueError("PostgreSQL unique constraint key is invalid")
            observed_column = columns_by_ordinal.get(physical_ordinal)
            if observed_column is None or observed_column.name != column_name:
                raise ValueError("PostgreSQL unique constraint key is not an observed column")
            if type(observed_non_null) is not bool or (
                observed_non_null != (not observed_column.nullable)
            ):
                raise ValueError("PostgreSQL unique constraint nullability is inconsistent")
            all_key_columns_non_null = all_key_columns_non_null and observed_non_null
            key_columns.append(
                {
                    "key_position": key_position,
                    "physical_ordinal": physical_ordinal,
                    "name": column_name,
                }
            )

        eligible = (
            facts.validated
            and not facts.deferrable
            and not facts.initially_deferred
            and facts.backing_index_valid
            and facts.backing_index_ready
            and facts.backing_index_unique
            and not facts.partial
            and not facts.expression
            and all_key_columns_non_null
        )
        if not eligible:
            continue
        constraints.append(
            PostgreSQLObservedUniqueConstraint.model_validate(
                {
                    "constraint_oid": facts.constraint_oid,
                    "constraint_name": facts.constraint_name,
                    "constraint_kind": facts.constraint_kind,
                    "backing_index_oid": facts.backing_index_oid,
                    "validated": facts.validated,
                    "deferrable": facts.deferrable,
                    "initially_deferred": facts.initially_deferred,
                    "backing_index_valid": facts.backing_index_valid,
                    "backing_index_ready": facts.backing_index_ready,
                    "backing_index_unique": facts.backing_index_unique,
                    "partial": facts.partial,
                    "expression": facts.expression,
                    "key_columns": tuple(key_columns),
                }
            )
        )
    return tuple(constraints)


def _observation_error(error: psycopg.Error) -> ProviderError:
    sqlstate = error.sqlstate or ""
    if sqlstate.startswith("28") or sqlstate == "42501":
        classification: ProviderErrorClassification = "authorization_denied"
    elif sqlstate.startswith(("40", "53", "57", "58")):
        classification = "transient_unavailable"
    elif sqlstate.startswith("08") or isinstance(error, psycopg.OperationalError):
        classification = "transient_transport"
    elif sqlstate.startswith("23"):
        classification = "integrity_failure"
    else:
        classification = "statement_rejected"
    return ProviderError("PostgreSQL product observation failed", classification)
