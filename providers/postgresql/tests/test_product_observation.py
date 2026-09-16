from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pillarmesh_contract_model import digest
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_provider_postgresql import (
    PostgreSQLMaterializationSettings,
    PostgreSQLObservedProductColumn,
    PostgreSQLObservedUniqueConstraint,
    PostgreSQLProductSemanticObservation,
    PostgreSQLProductSemanticObserver,
)
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_runtime import ProductMaterializationReceipt
from pydantic import SecretStr

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
_INPUT_CARDINALITY_EVIDENCE_DIGEST = "7" * 64
_COMPILER_KEY_ID = "compiler-key-1"
_COMPILER_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)


def _commit_reference(
    *,
    relation_oid: int = 16425,
    relation_file_node: int = 16425,
    model_digest: str = "b" * 64,
    magnitude_checks: tuple[DbtDecimalMagnitudeCheck, ...] = (),
) -> str:
    authority: dict[str, object] = {
        "domain": "pillarmesh-postgresql-product-generation-v1",
        "tenant_id": "tenant-a",
        "product_id": "product-revenue",
        "product_revision": 2,
        "product_generation": 3,
        "model_digest": model_digest,
        "relation_identity": (str(relation_oid), str(relation_file_node)),
    }
    if magnitude_checks:
        authority["output_magnitude_checks"] = tuple(
            {
                "declaration": check.model_dump(mode="python"),
                "violation_count": 0,
            }
            for check in magnitude_checks
        )
    return digest(authority)


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _Connection:
    def __init__(self, results: list[list[tuple[object, ...]]]) -> None:
        self._results = results
        self.statements: list[tuple[object, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, statement: object, parameters: tuple[object, ...] = ()) -> _Cursor:
        self.statements.append((statement, parameters))
        return _Cursor(self._results.pop(0) if self._results else [])

    def close(self) -> None:
        self.closed = True


def _settings() -> PostgreSQLMaterializationSettings:
    return PostgreSQLMaterializationSettings(
        tenant_id="tenant-a",
        dsn=SecretStr("postgresql://observer:private@localhost/database"),
        consumption_schema_name="consumption",
        consumption_view_name="product_revenue",
        control_schema_name="product_control",
        generation_table_name="product_generations",
        generation_pointer_table_name="product_generation_pointers",
    )


def _request(**changes: object) -> ProductMaterializationReceipt:
    values: dict[str, object] = {
        "run_id": "run-a",
        "tenant_id": "tenant-a",
        "product_id": "product-revenue",
        "product_revision": 2,
        "product_generation": 3,
        "contract_digest": "1" * 64,
        "input_generation_digests": ("2" * 64,),
        "input_cardinality_evidence_digest": _INPUT_CARDINALITY_EVIDENCE_DIGEST,
        "execution_authorization_digest": "7" * 64,
        "legality_decision_digest": "8" * 64,
        "physical_plan_digest": "9" * 64,
        "compiled_model_digest": "b" * 64,
        "output_schema_digest": "3" * 64,
        "output_row_count": 1,
        "provider_commit_reference": _commit_reference(),
        "dbt_manifest_digest": "4" * 64,
        "dbt_run_results_digest": "5" * 64,
        "lineage_digest": "6" * 64,
        "quality_assertion_count": 0,
        "quality_disposition": "not_asserted",
        "committed_at": _NOW - timedelta(minutes=1),
        "retained_until": _NOW + timedelta(hours=1),
    }
    values.update(changes)
    return ProductMaterializationReceipt.model_validate(values)


def _signed_model(
    checks: tuple[DbtDecimalMagnitudeCheck, ...],
) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="product_revenue_g3",
        contract_digest="1" * 64,
        provider="postgresql",
        input_generation_digests=("2" * 64,),
        target_schema="product_generation_3",
        output_columns=("region", "total_revenue", "tax"),
        output_magnitude_checks=checks,
        compiled_sql="SELECT region, total_revenue FROM raw.revenue",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id=_COMPILER_KEY_ID,
        signature=base64.b64encode(
            _COMPILER_PRIVATE_KEY.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


def _trusted_compiler_keys() -> dict[str, Ed25519PublicKey]:
    return {_COMPILER_KEY_ID: _COMPILER_PRIVATE_KEY.public_key()}


def _magnitude_request(signed_model: SignedCompiledDbtModel) -> ProductMaterializationReceipt:
    return _request(
        compiled_model_digest=signed_model.model_digest,
        provider_commit_reference=_commit_reference(
            model_digest=signed_model.model_digest,
            magnitude_checks=signed_model.model.output_magnitude_checks,
        ),
    )


def _successful_results(
    *,
    pointer_revision: int = 2,
    pointer_generation: int = 3,
    retained_until: datetime = _NOW + timedelta(hours=1),
    can_use_schema: bool = True,
    can_select_relation: bool = True,
    pointer_commit_reference: str | None = None,
    constraint_rows: list[tuple[object, ...]] | None = None,
    second_column_nullable: bool = False,
) -> list[list[tuple[object, ...]]]:
    commit_reference = pointer_commit_reference or _commit_reference()
    return [
        [],
        [
            (
                "product_generation_3",
                "product_revenue_g3",
                commit_reference,
                retained_until,
                pointer_revision,
                pointer_generation,
                "product_generation_3",
                "product_revenue_g3",
                commit_reference,
            )
        ],
        [
            (
                "postgres",
                "140020",
                "14.20 (Homebrew)",
                "America/Los_Angeles",
                "C",
                "C",
                _NOW,
            )
        ],
        [(2200, 16425, 16425, "r", can_use_schema, can_select_relation)],
        [
            (1, "region", 25, "text", False, 100, "pg_catalog", "default", "d", True),
            (
                2,
                "total_revenue",
                1700,
                "numeric",
                second_column_nullable,
                None,
                None,
                None,
                None,
                None,
            ),
        ],
        constraint_rows
        if constraint_rows is not None
        else [
            (
                16430,
                "product_revenue_g3_pkey",
                "p",
                True,
                False,
                False,
                16430,
                True,
                True,
                True,
                False,
                False,
                1,
                1,
                "region",
                True,
            )
        ],
        [],
    ]


def test_observer_returns_actual_context_and_exact_owned_generation() -> None:
    connection = _Connection(_successful_results())
    observer = PostgreSQLProductSemanticObserver(_settings(), connect=lambda _dsn: connection)

    observation = observer.observe(_request())

    assert observation.tenant_id == "tenant-a"
    assert observation.product_revision == 2
    assert observation.product_generation == 3
    assert observation.engine_version == "14.20 (Homebrew)"
    assert observation.server_version_num == "140020"
    assert observation.engine_build_digest == digest(
        {
            "domain": "pillarmesh-postgresql-engine-build-v1",
            "version": {
                "server_version_num": "140020",
                "server_version": "14.20 (Homebrew)",
            },
        }
    )
    assert observation.current_database == "postgres"
    assert observation.session_timezone == "America/Los_Angeles"
    assert observation.database_collation == "C"
    assert observation.database_character_classification == "C"
    assert observation.relation_schema == "product_generation_3"
    assert observation.relation_name == "product_revenue_g3"
    assert observation.schema_oid == 2200
    assert observation.relation_oid == 16425
    assert observation.relation_file_node == 16425
    assert observation.provider_commit_reference == _commit_reference()
    assert tuple(column.name for column in observation.columns) == ("region", "total_revenue")
    assert observation.columns[0].formatted_type == "text"
    assert observation.columns[0].collation_name == "default"
    assert observation.columns[1].collation_oid is None
    assert len(observation.eligible_unique_constraints) == 1
    primary_key = observation.eligible_unique_constraints[0]
    assert primary_key.constraint_kind == "p"
    assert tuple(
        (column.key_position, column.physical_ordinal, column.name)
        for column in primary_key.key_columns
    ) == ((1, 1, "region"),)
    assert observation.observed_at == _NOW
    assert connection.closed is True
    assert connection.statements[0][0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert connection.statements[-1][0] == "ROLLBACK"
    assert "private" not in repr(observation)
    assert "private" not in observation.model_dump_json()


def test_observer_reconstructs_ordered_signed_magnitude_commit_reference() -> None:
    checks = (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        DbtDecimalMagnitudeCheck(column_name="tax"),
    )
    signed_model = _signed_model(checks)
    request = _magnitude_request(signed_model)
    results = _successful_results(pointer_commit_reference=request.provider_commit_reference)
    results[4].append((3, "tax", 1700, "numeric", False, None, None, None, None, None))
    results[5:5] = [[(0,)], [(0,)]]
    connection = _Connection(results)
    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=lambda _dsn: connection,
    )

    observation = observer.observe(request)

    assert observation.provider_commit_reference == _commit_reference(
        model_digest=signed_model.model_digest,
        magnitude_checks=checks,
    )
    magnitude_statement, magnitude_parameters = connection.statements[5]
    assert "Identifier('total_revenue')" in repr(magnitude_statement)
    assert magnitude_parameters == (Decimal("-1e48"), Decimal("1e48"))
    second_magnitude_statement, second_magnitude_parameters = connection.statements[6]
    assert "Identifier('tax')" in repr(second_magnitude_statement)
    assert second_magnitude_parameters == (Decimal("-1e48"), Decimal("1e48"))


def test_observer_rejects_post_execute_magnitude_mutation() -> None:
    checks = (DbtDecimalMagnitudeCheck(column_name="total_revenue"),)
    signed_model = _signed_model(checks)
    request = _magnitude_request(signed_model)
    results = _successful_results(pointer_commit_reference=request.provider_commit_reference)
    results.insert(5, [(1,)])
    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=lambda _dsn: _Connection(results),
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(request)

    assert caught.value.classification == "integrity_failure"


@pytest.mark.parametrize(
    "checks",
    [
        (
            DbtDecimalMagnitudeCheck(column_name="tax"),
            DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        ),
        (DbtDecimalMagnitudeCheck(column_name="tax"),),
    ],
    ids=("reordered", "substituted"),
)
def test_observer_rejects_reordered_or_substituted_signed_checks_before_connecting(
    checks: tuple[DbtDecimalMagnitudeCheck, ...],
) -> None:
    original = _signed_model(
        (
            DbtDecimalMagnitudeCheck(column_name="total_revenue"),
            DbtDecimalMagnitudeCheck(column_name="tax"),
        )
    )
    substituted = _signed_model(checks).model_copy(update={"model_digest": original.model_digest})
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=substituted,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=connect,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_magnitude_request(original))

    assert caught.value.classification == "integrity_failure"
    assert connected is False


@pytest.mark.parametrize(
    "trusted_compiler_keys",
    [
        {},
        {_COMPILER_KEY_ID: Ed25519PrivateKey.generate().public_key()},
    ],
    ids=("missing", "untrusted"),
)
def test_observer_rejects_missing_or_untrusted_compiler_key_before_connecting(
    trusted_compiler_keys: dict[str, Ed25519PublicKey],
) -> None:
    signed_model = _signed_model((DbtDecimalMagnitudeCheck(column_name="total_revenue"),))
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=trusted_compiler_keys,
        connect=connect,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_magnitude_request(signed_model))

    assert caught.value.classification == "integrity_failure"
    assert connected is False


def test_observer_rejects_malformed_compiler_signature_before_connecting() -> None:
    signed_model = _signed_model(
        (DbtDecimalMagnitudeCheck(column_name="total_revenue"),)
    ).model_copy(update={"signature": "not-base64!"})
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=connect,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_magnitude_request(signed_model))

    assert caught.value.classification == "integrity_failure"
    assert connected is False


def test_observer_rejects_hidden_nested_model_fields_before_connecting() -> None:
    signed_model = _signed_model((DbtDecimalMagnitudeCheck(column_name="total_revenue"),))
    object.__setattr__(signed_model.model.output_magnitude_checks[0], "hidden", "untrusted")
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=connect,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_magnitude_request(signed_model))

    assert caught.value.classification == "integrity_failure"
    assert connected is False


def test_observer_preserves_legacy_commit_reference_for_empty_checks() -> None:
    signed_model = _signed_model(())
    request = _request(compiled_model_digest=signed_model.model_digest)
    legacy_reference = _commit_reference(model_digest=signed_model.model_digest)
    request = request.model_copy(update={"provider_commit_reference": legacy_reference})
    results = _successful_results(pointer_commit_reference=legacy_reference)
    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        signed_model=signed_model,
        trusted_compiler_keys=_trusted_compiler_keys(),
        connect=lambda _dsn: _Connection(results),
    )

    observation = observer.observe(request)

    assert observation.provider_commit_reference == legacy_reference


def test_observer_rejects_wrong_tenant_before_opening_connection() -> None:
    connected = False

    def connect(_dsn: str) -> _Connection:
        nonlocal connected
        connected = True
        return _Connection([])

    observer = PostgreSQLProductSemanticObserver(_settings(), connect=connect)

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(tenant_id="tenant-b"))

    assert caught.value.classification == "authorization_denied"
    assert str(caught.value) == "PostgreSQL product observation failed"
    assert connected is False


@pytest.mark.parametrize(
    "results",
    [
        pytest.param(_successful_results(pointer_generation=4), id="non-current-generation"),
        pytest.param(_successful_results(pointer_revision=3), id="non-current-revision"),
        pytest.param(
            _successful_results(retained_until=_NOW),
            id="expired-retention",
        ),
        pytest.param(
            _successful_results(pointer_commit_reference="b" * 64),
            id="conflicting-pointer-identity",
        ),
    ],
)
def test_observer_rejects_stale_generation_authority(
    results: list[list[tuple[object, ...]]],
) -> None:
    observer = PostgreSQLProductSemanticObserver(
        _settings(), connect=lambda _dsn: _Connection(results)
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "integrity_failure"
    assert str(caught.value) == "PostgreSQL product observation failed"


def test_observer_rejects_nonaddressable_relation() -> None:
    results = _successful_results()
    results[3] = []
    observer = PostgreSQLProductSemanticObserver(
        _settings(), connect=lambda _dsn: _Connection(results)
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "integrity_failure"


@pytest.mark.parametrize(
    ("can_use_schema", "can_select_relation"),
    [(False, True), (True, False)],
)
def test_observer_rejects_relation_without_read_privilege(
    can_use_schema: bool,
    can_select_relation: bool,
) -> None:
    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        connect=lambda _dsn: _Connection(
            _successful_results(
                can_use_schema=can_use_schema,
                can_select_relation=can_select_relation,
            )
        ),
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "authorization_denied"


def test_observer_classifies_driver_failure_without_leaking_details() -> None:
    secret = "private-host-statement"
    observer = PostgreSQLProductSemanticObserver(
        _settings(),
        connect=lambda _dsn: (_ for _ in ()).throw(psycopg.OperationalError(secret)),
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "transient_transport"
    assert secret not in str(caught.value)


def test_observer_rejects_partially_malformed_column_observation() -> None:
    results = _successful_results()
    results[4].append((3, "incomplete"))
    observer = PostgreSQLProductSemanticObserver(
        _settings(), connect=lambda _dsn: _Connection(results)
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "invalid_provider_response"


def test_observer_omits_constraints_that_are_not_eligible_semantic_keys() -> None:
    rows = _successful_results()[5]
    rows.extend(
        [
            (
                16431,
                "nullable_key",
                "u",
                True,
                False,
                False,
                16431,
                True,
                True,
                True,
                False,
                False,
                1,
                2,
                "total_revenue",
                False,
            ),
            (
                16432,
                "deferred_key",
                "u",
                True,
                True,
                False,
                16432,
                True,
                True,
                True,
                False,
                False,
                1,
                1,
                "region",
                True,
            ),
            (
                16433,
                "unvalidated_key",
                "u",
                False,
                False,
                False,
                16433,
                True,
                True,
                True,
                False,
                False,
                1,
                1,
                "region",
                True,
            ),
            (
                16434,
                "partial_key",
                "u",
                True,
                False,
                False,
                16434,
                True,
                True,
                True,
                True,
                False,
                1,
                1,
                "region",
                True,
            ),
            (
                16435,
                "expression_key",
                "u",
                True,
                False,
                False,
                16435,
                True,
                True,
                True,
                False,
                True,
                1,
                1,
                "region",
                True,
            ),
            (
                16436,
                "invalid_index_key",
                "u",
                True,
                False,
                False,
                16436,
                False,
                True,
                True,
                False,
                False,
                1,
                1,
                "region",
                True,
            ),
            (
                16437,
                "unready_index_key",
                "u",
                True,
                False,
                False,
                16437,
                True,
                False,
                True,
                False,
                False,
                1,
                1,
                "region",
                True,
            ),
            (
                16438,
                "nonunique_index_key",
                "u",
                True,
                False,
                False,
                16438,
                True,
                True,
                False,
                False,
                False,
                1,
                1,
                "region",
                True,
            ),
        ]
    )
    connection = _Connection(_successful_results(constraint_rows=rows, second_column_nullable=True))
    observer = PostgreSQLProductSemanticObserver(_settings(), connect=lambda _dsn: connection)

    observation = observer.observe(_request())

    assert tuple(
        constraint.constraint_name for constraint in observation.eligible_unique_constraints
    ) == ("product_revenue_g3_pkey",)


def test_observer_rejects_constraint_key_that_disagrees_with_observed_column() -> None:
    rows: list[tuple[object, ...]] = [
        (
            16430,
            "product_revenue_g3_pkey",
            "p",
            True,
            False,
            False,
            16430,
            True,
            True,
            True,
            False,
            False,
            1,
            1,
            "other",
            True,
        )
    ]
    observer = PostgreSQLProductSemanticObserver(
        _settings(), connect=lambda _dsn: _Connection(_successful_results(constraint_rows=rows))
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request())

    assert caught.value.classification == "invalid_provider_response"


def test_observer_preserves_catalog_order_for_composite_unique_constraint() -> None:
    rows: list[tuple[object, ...]] = [
        (
            16440,
            "product_revenue_region_total_key",
            "u",
            True,
            False,
            False,
            16440,
            True,
            True,
            True,
            False,
            False,
            1,
            1,
            "region",
            True,
        ),
        (
            16440,
            "product_revenue_region_total_key",
            "u",
            True,
            False,
            False,
            16440,
            True,
            True,
            True,
            False,
            False,
            2,
            2,
            "total_revenue",
            True,
        ),
    ]
    observer = PostgreSQLProductSemanticObserver(
        _settings(), connect=lambda _dsn: _Connection(_successful_results(constraint_rows=rows))
    )

    observation = observer.observe(_request())

    assert tuple(
        (column.key_position, column.physical_ordinal, column.name)
        for column in observation.eligible_unique_constraints[0].key_columns
    ) == ((1, 1, "region"), (2, 2, "total_revenue"))


def test_observation_preserves_strictly_increasing_physical_ordinals_with_gaps() -> None:
    columns = (
        PostgreSQLObservedProductColumn(
            ordinal=1,
            name="region",
            type_oid=25,
            formatted_type="text",
            nullable=False,
            collation_oid=100,
            collation_schema="pg_catalog",
            collation_name="default",
            collation_provider="d",
            collation_deterministic=True,
        ),
        PostgreSQLObservedProductColumn(
            ordinal=3,
            name="revenue",
            type_oid=1700,
            formatted_type="numeric",
            nullable=False,
        ),
    )

    observation = PostgreSQLProductSemanticObservation(
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=2,
        product_generation=3,
        engine_version="14.20",
        server_version_num="140020",
        engine_build_digest="b" * 64,
        current_database="postgres",
        session_timezone="UTC",
        database_collation="C",
        database_character_classification="C",
        relation_schema="product_generation_3",
        relation_name="product_revenue_g3",
        schema_oid=2200,
        relation_oid=16425,
        relation_file_node=16425,
        relation_kind="r",
        columns=columns,
        eligible_unique_constraints=(
            PostgreSQLObservedUniqueConstraint.model_validate(
                {
                    "constraint_oid": 16430,
                    "constraint_name": "product_revenue_g3_pkey",
                    "constraint_kind": "p",
                    "backing_index_oid": 16430,
                    "key_columns": ({"key_position": 1, "physical_ordinal": 1, "name": "region"},),
                }
            ),
        ),
        provider_commit_reference=_commit_reference(),
        retained_until=_NOW + timedelta(hours=1),
        observed_at=_NOW,
    )

    assert tuple(column.ordinal for column in observation.columns) == (1, 3)
