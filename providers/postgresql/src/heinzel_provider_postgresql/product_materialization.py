from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

import psycopg
from heinzel_contract_model import ArtifactModel, digest
from heinzel_dbt_adapter import (
    DbtDecimalMagnitudeCheck,
    DbtFailureClassification,
    DbtInvocationAuthority,
    DbtInvocationError,
    DbtInvoker,
    SignedCompiledDbtModel,
)
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_runtime import (
    AnswerProductGenerationReference,
    MaterializationObservation,
    MaterializationRequest,
    QueryGenerationState,
)
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)

_DECIMAL_57_9_EXCLUSIVE_BOUND = Decimal("1e48")


class PostgreSQLMaterializationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    dsn: SecretStr
    consumption_schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    consumption_view_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    control_schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    generation_table_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    generation_pointer_table_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")


class PostgreSQLMaterializedColumn(ArtifactModel):
    ordinal: int = Field(ge=1)
    name: str = Field(min_length=1)
    data_type: str = Field(min_length=1)
    nullable: bool


def postgresql_materialized_schema_digest(
    columns: tuple[PostgreSQLMaterializedColumn, ...],
) -> str:
    return digest(columns)


class _Cursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class _QueryConnection(Protocol):
    def execute(self, query: object, params: tuple[object, ...] = ()) -> _Cursor: ...


class _Connection(_QueryConnection, Protocol):
    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _Connection]
type _MagnitudeCheckResults = tuple[tuple[DbtDecimalMagnitudeCheck, int], ...]


def _postgresql_decimal_magnitude_checks(
    connection: _QueryConnection,
    *,
    target_schema: str,
    model_name: str,
    checks: tuple[DbtDecimalMagnitudeCheck, ...],
    observed_column_names: set[str],
) -> _MagnitudeCheckResults:
    results: list[tuple[DbtDecimalMagnitudeCheck, int]] = []
    for check in checks:
        if check.column_name not in observed_column_names:
            raise ProviderError(
                "PostgreSQL magnitude check column is absent from materialized output",
                classification="invalid_provider_response",
            )
        count_row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}.{} WHERE {} IS NULL OR {} <= %s OR {} >= %s").format(
                sql.Identifier(target_schema),
                sql.Identifier(model_name),
                sql.Identifier(check.column_name),
                sql.Identifier(check.column_name),
                sql.Identifier(check.column_name),
            ),
            (-_DECIMAL_57_9_EXCLUSIVE_BOUND, _DECIMAL_57_9_EXCLUSIVE_BOUND),
        ).fetchone()
        if (
            count_row is None
            or len(count_row) != 1
            or type(count_row[0]) is not int
            or count_row[0] < 0
        ):
            raise ProviderError(
                "PostgreSQL magnitude check result is invalid",
                classification="invalid_provider_response",
            )
        violation_count = count_row[0]
        if violation_count != 0:
            raise ProviderError(
                "PostgreSQL materialization violates its signed decimal magnitude bound",
                classification="integrity_failure",
            )
        results.append((check, violation_count))
    return tuple(results)


def _postgresql_product_generation_commit_reference(
    *,
    tenant_id: str,
    product_id: str,
    product_revision: int,
    product_generation: int,
    model_digest: str,
    relation_identity: tuple[str, str],
    magnitude_checks: _MagnitudeCheckResults,
) -> str:
    authority: dict[str, object] = {
        "domain": "heinzel-postgresql-product-generation-v1",
        "tenant_id": tenant_id,
        "product_id": product_id,
        "product_revision": product_revision,
        "product_generation": product_generation,
        "model_digest": model_digest,
        "relation_identity": relation_identity,
    }
    if magnitude_checks:
        authority["output_magnitude_checks"] = tuple(
            {
                "declaration": check.model_dump(mode="python"),
                "violation_count": violation_count,
            }
            for check, violation_count in magnitude_checks
        )
    return digest(authority)


class PostgreSQLMaterializationWarehouse:
    def __init__(
        self,
        *,
        settings: PostgreSQLMaterializationSettings,
        signed_model: SignedCompiledDbtModel,
        invoker: DbtInvoker,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._signed_model = signed_model
        self._invoker = invoker
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )
        self._clock = clock

    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        self._require_authority(request)
        lock_connection = self._acquire_target_lock()
        try:
            self._require_fresh_target(lock_connection)
            try:
                invocation_receipt = self._invoker.invoke(
                    signed_model=self._signed_model,
                    authority=DbtInvocationAuthority(
                        contract_digest=request.contract_digest,
                        provider="postgresql",
                        input_generation_digests=request.input_generation_digests,
                    ),
                )
            except DbtInvocationError as error:
                classification: ProviderErrorClassification
                match error.classification:
                    case DbtFailureClassification.INVALID_SIGNATURE:
                        classification = "integrity_failure"
                    case DbtFailureClassification.AUTHORITY_MISMATCH:
                        classification = "authorization_denied"
                    case DbtFailureClassification.MALFORMED_OUTPUT:
                        classification = "invalid_provider_response"
                    case (
                        DbtFailureClassification.INVOCATION_FAILED
                        | DbtFailureClassification.NONZERO_EXIT
                    ):
                        classification = "ambiguous_outcome"
                raise ProviderError(
                    "PostgreSQL materialization did not produce verified dbt evidence",
                    classification=classification,
                ) from None
            columns, row_count, relation_identity, magnitude_checks = self._inspect_output()
            return MaterializationObservation(
                provider_commit_reference=self._provider_commit_reference(
                    request=request,
                    relation_identity=relation_identity,
                    magnitude_checks=magnitude_checks,
                ),
                output_schema_digest=postgresql_materialized_schema_digest(columns),
                output_row_count=row_count,
                dbt_manifest_digest=invocation_receipt.manifest_digest,
                dbt_run_results_digest=invocation_receipt.run_results_digest,
                lineage_digest=invocation_receipt.lineage_digest,
                quality_assertion_count=invocation_receipt.quality_assertion_count,
                quality_disposition=invocation_receipt.quality_disposition,
                # The columns the engine was held to, not the ones the plan declared. Each pair
                # here comes from a count query against the materialized relation, and
                # `_postgresql_decimal_magnitude_checks` raises `integrity_failure` on any
                # non-zero count, so a pair reaching this point is engine-verified evidence.
                #
                # Filtered on zero rather than taken wholesale: if that refusal were ever
                # relaxed, attesting a violated column would tell the runner the engine held to
                # a bound it broke. Omitting a column makes the runner refuse, which is the
                # direction a mistake here must fail in.
                magnitude_asserted_columns=tuple(
                    check.column_name
                    for check, violation_count in magnitude_checks
                    if violation_count == 0
                ),
            )
        finally:
            with suppress(Exception):
                lock_connection.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                    (
                        self._signed_model.model.target_schema,
                        self._signed_model.model.model_name,
                    ),
                )
            with suppress(Exception):
                lock_connection.close()

    def switch_consumption_view(
        self,
        request: MaterializationRequest,
        observation: MaterializationObservation,
    ) -> None:
        self._require_authority(request)
        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connection.execute("BEGIN")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                ),
            )
            connection.execute(
                sql.SQL("LOCK TABLE {}.{} IN ACCESS SHARE MODE").format(
                    sql.Identifier(self._signed_model.model.target_schema),
                    sql.Identifier(self._signed_model.model.model_name),
                )
            )
            columns, row_count, relation_identity, magnitude_checks = self._inspect_output(
                connection
            )
            expected_reference = self._provider_commit_reference(
                request=request,
                relation_identity=relation_identity,
                magnitude_checks=magnitude_checks,
            )
            if (
                observation.provider_commit_reference != expected_reference
                or observation.output_schema_digest
                != postgresql_materialized_schema_digest(columns)
                or observation.output_row_count != row_count
            ):
                raise ProviderError(
                    "PostgreSQL materialization output changed before publication",
                    classification="integrity_failure",
                )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (request.tenant_id, request.product_id),
            )
            pointer_row = connection.execute(
                sql.SQL(
                    "SELECT product_revision, product_generation, generation_schema, "
                    "generation_table, provider_commit_reference, retained_until "
                    "FROM {}.{} WHERE tenant_id = %s AND product_id = %s FOR UPDATE"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_pointer_table_name),
                ),
                (request.tenant_id, request.product_id),
            ).fetchone()
            requested_generation = (request.product_revision, request.product_generation)
            if pointer_row is not None:
                if (
                    len(pointer_row) != 6
                    or type(pointer_row[0]) is not int
                    or type(pointer_row[1]) is not int
                ):
                    raise ProviderError(
                        "PostgreSQL product generation pointer is invalid",
                        classification="invalid_provider_response",
                    )
                current_generation = (pointer_row[0], pointer_row[1])
                if current_generation > requested_generation:
                    raise ProviderError(
                        "PostgreSQL product generation pointer cannot move backwards",
                        classification="integrity_failure",
                    )
                if current_generation == requested_generation:
                    generation_row = self._read_generation(connection, request)
                    expected_identity = (
                        self._signed_model.model.target_schema,
                        self._signed_model.model.model_name,
                        observation.provider_commit_reference,
                    )
                    if (
                        pointer_row[2:5] != expected_identity
                        or generation_row is None
                        or generation_row[:3] != expected_identity
                    ):
                        raise ProviderError(
                            "PostgreSQL product generation replay conflicts with durable authority",
                            classification="integrity_failure",
                        )
                    if pointer_row[5] != generation_row[3]:
                        raise ProviderError(
                            "PostgreSQL generation retention conflicts with durable authority",
                            classification="integrity_failure",
                        )
                    connection.commit()
                    return
            committed_at = self._clock()
            retained_until = committed_at + timedelta(seconds=request.retention_seconds)
            connection.execute(
                sql.SQL("CREATE OR REPLACE VIEW {}.{} AS SELECT * FROM {}.{}").format(
                    sql.Identifier(self._settings.consumption_schema_name),
                    sql.Identifier(self._settings.consumption_view_name),
                    sql.Identifier(self._signed_model.model.target_schema),
                    sql.Identifier(self._signed_model.model.model_name),
                )
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.{} (tenant_id, product_id, product_revision, "
                    "product_generation, generation_schema, generation_table, "
                    "provider_commit_reference, retained_until) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, product_id, product_revision, product_generation) "
                    "DO NOTHING"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_table_name),
                ),
                (
                    request.tenant_id,
                    request.product_id,
                    request.product_revision,
                    request.product_generation,
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                    observation.provider_commit_reference,
                    retained_until,
                ),
            )
            generation_row = self._read_generation(connection, request)
            if generation_row != (
                self._signed_model.model.target_schema,
                self._signed_model.model.model_name,
                observation.provider_commit_reference,
                retained_until,
            ):
                raise ProviderError(
                    "PostgreSQL product generation identity conflicts with durable authority",
                    classification="integrity_failure",
                )
            pointer_values = (
                request.product_revision,
                request.product_generation,
                self._signed_model.model.target_schema,
                self._signed_model.model.model_name,
                observation.provider_commit_reference,
                retained_until,
            )
            if pointer_row is None:
                published_pointer = connection.execute(
                    sql.SQL(
                        "INSERT INTO {}.{} (tenant_id, product_id, product_revision, "
                        "product_generation, generation_schema, generation_table, "
                        "provider_commit_reference, retained_until, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING product_revision, product_generation, generation_schema, "
                        "generation_table, provider_commit_reference, retained_until"
                    ).format(
                        sql.Identifier(self._settings.control_schema_name),
                        sql.Identifier(self._settings.generation_pointer_table_name),
                    ),
                    (request.tenant_id, request.product_id, *pointer_values, committed_at),
                ).fetchone()
            else:
                published_pointer = connection.execute(
                    sql.SQL(
                        "UPDATE {}.{} SET product_revision = %s, product_generation = %s, "
                        "generation_schema = %s, generation_table = %s, "
                        "provider_commit_reference = %s, retained_until = %s, updated_at = %s "
                        "WHERE tenant_id = %s AND product_id = %s AND product_revision = %s "
                        "AND product_generation = %s RETURNING product_revision, "
                        "product_generation, generation_schema, generation_table, "
                        "provider_commit_reference, retained_until"
                    ).format(
                        sql.Identifier(self._settings.control_schema_name),
                        sql.Identifier(self._settings.generation_pointer_table_name),
                    ),
                    (
                        *pointer_values,
                        committed_at,
                        request.tenant_id,
                        request.product_id,
                        pointer_row[0],
                        pointer_row[1],
                    ),
                ).fetchone()
            if published_pointer != pointer_values:
                raise ProviderError(
                    "PostgreSQL product generation pointer changed during publication",
                    classification="integrity_failure",
                )
            connection.commit()
        except psycopg.Error as error:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            raise _postgresql_materialization_error(error) from None
        except BaseException:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            raise
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    def _read_generation(
        self, connection: _Connection, request: MaterializationRequest
    ) -> tuple[object, ...] | None:
        return connection.execute(
            sql.SQL(
                "SELECT generation_schema, generation_table, provider_commit_reference, "
                "retained_until FROM {}.{} WHERE tenant_id = %s AND product_id = %s "
                "AND product_revision = %s AND product_generation = %s"
            ).format(
                sql.Identifier(self._settings.control_schema_name),
                sql.Identifier(self._settings.generation_table_name),
            ),
            (
                request.tenant_id,
                request.product_id,
                request.product_revision,
                request.product_generation,
            ),
        ).fetchone()

    def _require_authority(self, request: MaterializationRequest) -> None:
        model = self._signed_model.model
        physical_plan = request.physical_plan
        physical_checks = tuple(
            (check.column_name, check.precision, check.scale)
            for check in physical_plan.decimal_output_checks
        )
        compiled_checks = tuple(
            (check.column_name, check.precision, check.scale)
            for check in model.output_magnitude_checks
        )
        if (
            request.tenant_id != self._settings.tenant_id
            or self._signed_model.model_digest != request.compiled_model_digest
            or digest(physical_plan) != request.physical_plan_digest
            or physical_plan.provider != model.provider
            or physical_plan.contract_digest != model.contract_digest
            or (physical_plan.source.landing_receipt_digest,) != model.input_generation_digests
            or physical_plan.target.namespace != model.target_schema
            or physical_plan.target.relation_name != model.model_name
            or physical_plan.emitted_statement != model.compiled_sql
            or physical_plan.output_columns != model.output_columns
            or physical_checks != compiled_checks
            or physical_plan.expected_output_schema_digest != request.expected_output_schema_digest
            or model.contract_digest != request.contract_digest
            or model.provider != "postgresql"
            or model.input_generation_digests != request.input_generation_digests
        ):
            raise ProviderError(
                "PostgreSQL materialization authority does not match the request",
                classification="authorization_denied",
            )

    def _acquire_target_lock(self) -> _Connection:
        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connection.execute(
                "SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))",
                (
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                ),
            )
            connection.rollback()
            return connection
        except psycopg.Error as error:
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            raise _postgresql_materialization_error(error) from None

    def _require_fresh_target(self, connection: _Connection) -> None:
        try:
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            target_row = connection.execute(
                sql.SQL(
                    "SELECT EXISTS (SELECT 1 FROM {}.{} "
                    "WHERE generation_schema = %s AND generation_table = %s "
                    "AND retained_until > transaction_timestamp()), "
                    "to_regclass(format('%%I.%%I', %s::text, %s::text)) IS NOT NULL"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_table_name),
                ),
                (
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                ),
            ).fetchone()
        except psycopg.Error as error:
            with suppress(Exception):
                connection.rollback()
            raise _postgresql_materialization_error(error) from None
        else:
            with suppress(Exception):
                connection.rollback()
        if (
            target_row is None
            or len(target_row) != 2
            or type(target_row[0]) is not bool
            or type(target_row[1]) is not bool
        ):
            raise ProviderError(
                "PostgreSQL materialization target observation is invalid",
                classification="invalid_provider_response",
            )
        if target_row[0] or target_row[1]:
            raise ProviderError(
                "PostgreSQL materialization requires a fresh output relation",
                classification="integrity_failure",
            )

    def _inspect_output(
        self, connection: _Connection | None = None
    ) -> tuple[
        tuple[PostgreSQLMaterializedColumn, ...],
        int,
        tuple[str, str],
        _MagnitudeCheckResults,
    ]:
        owns_connection = connection is None
        try:
            if connection is None:
                connection = connect_attributing_startup_denial(
                    self._connect,
                    self._settings.dsn.get_secret_value(),
                    probe=self._startup_denial_probe,
                )
            column_rows = connection.execute(
                "SELECT ordinal_position, column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                ),
            ).fetchall()
            columns = tuple(_column(row) for row in column_rows)
            if not columns:
                raise ValueError("dbt materialization output does not exist")
            count_row = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(self._signed_model.model.target_schema),
                    sql.Identifier(self._signed_model.model.model_name),
                )
            ).fetchone()
            identity_row = connection.execute(
                "SELECT c.oid::text, c.relfilenode::text FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
                (
                    self._signed_model.model.target_schema,
                    self._signed_model.model.model_name,
                ),
            ).fetchone()
            if (
                count_row is None
                or type(count_row[0]) is not int
                or identity_row is None
                or len(identity_row) != 2
                or not all(isinstance(value, str) and value for value in identity_row)
            ):
                raise ValueError("dbt materialization output observation is invalid")
            magnitude_checks = self._inspect_output_magnitudes(connection, columns)
            return (
                columns,
                count_row[0],
                cast(tuple[str, str], identity_row),
                magnitude_checks,
            )
        except psycopg.Error as error:
            raise _postgresql_materialization_error(error) from None
        finally:
            if owns_connection and connection is not None:
                with suppress(Exception):
                    connection.close()

    def _inspect_output_magnitudes(
        self,
        connection: _Connection,
        columns: tuple[PostgreSQLMaterializedColumn, ...],
    ) -> _MagnitudeCheckResults:
        return _postgresql_decimal_magnitude_checks(
            connection,
            target_schema=self._signed_model.model.target_schema,
            model_name=self._signed_model.model.model_name,
            checks=self._signed_model.model.output_magnitude_checks,
            observed_column_names={column.name for column in columns},
        )

    def _provider_commit_reference(
        self,
        *,
        request: MaterializationRequest,
        relation_identity: tuple[str, str],
        magnitude_checks: _MagnitudeCheckResults,
    ) -> str:
        return _postgresql_product_generation_commit_reference(
            tenant_id=request.tenant_id,
            product_id=request.product_id,
            product_revision=request.product_revision,
            product_generation=request.product_generation,
            model_digest=self._signed_model.model_digest,
            relation_identity=relation_identity,
            magnitude_checks=magnitude_checks,
        )


def _column(row: tuple[object, ...]) -> PostgreSQLMaterializedColumn:
    if (
        len(row) != 4
        or type(row[0]) is not int
        or not isinstance(row[1], str)
        or not isinstance(row[2], str)
        or row[3] not in {"YES", "NO"}
    ):
        raise ValueError("dbt materialization output schema is invalid")
    return PostgreSQLMaterializedColumn(
        ordinal=row[0],
        name=row[1],
        data_type=row[2],
        nullable=row[3] == "YES",
    )


class PostgreSQLProductGenerationAuthority:
    def __init__(
        self,
        settings: PostgreSQLMaterializationSettings,
        *,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState:
        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            generation_row = connection.execute(
                sql.SQL(
                    "SELECT to_regclass(generation_schema || '.' || generation_table) IS NOT NULL "
                    "FROM {}.{} WHERE tenant_id = %s AND product_id = %s AND product_revision = %s "
                    "AND product_generation = %s"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_table_name),
                ),
                (
                    self._settings.tenant_id,
                    reference.product_ref.artifact_id,
                    reference.product_ref.version,
                    reference.generation,
                ),
            ).fetchone()
            pointer_row = connection.execute(
                sql.SQL(
                    "SELECT product_generation FROM {}.{} "
                    "WHERE tenant_id = %s AND product_id = %s AND product_revision = %s"
                ).format(
                    sql.Identifier(self._settings.control_schema_name),
                    sql.Identifier(self._settings.generation_pointer_table_name),
                ),
                (
                    self._settings.tenant_id,
                    reference.product_ref.artifact_id,
                    reference.product_ref.version,
                ),
            ).fetchone()
            if (
                generation_row is None
                or len(generation_row) != 1
                or type(generation_row[0]) is not bool
            ):
                raise ProviderError(
                    "PostgreSQL product generation state is invalid",
                    classification="invalid_provider_response",
                )
            if pointer_row is None:
                current_generation = None
            elif len(pointer_row) != 1 or type(pointer_row[0]) is not int or pointer_row[0] < 1:
                raise ProviderError(
                    "PostgreSQL product generation pointer is invalid",
                    classification="invalid_provider_response",
                )
            else:
                current_generation = pointer_row[0]
            if not generation_row[0] and current_generation is None:
                raise ProviderError(
                    "PostgreSQL product generation authority is unavailable",
                    classification="integrity_failure",
                )
            return QueryGenerationState(
                addressable=generation_row[0],
                current_generation=current_generation,
            )
        except psycopg.Error as error:
            raise _postgresql_materialization_error(error) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()


def _postgresql_materialization_error(error: psycopg.Error) -> ProviderError:
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
    return ProviderError("PostgreSQL materialization failed", classification)
