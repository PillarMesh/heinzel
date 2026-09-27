from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLIncrementalCursor,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import (
    AcquisitionCeilingExceeded,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    acquisition_intent_key,
)
from heinzel_provider_sdk.acquisition_models import AcquisitionMode
from pydantic import SecretStr, ValidationError

from providers.postgresql.tests.test_acquisition_snapshot import (
    NOW,
    FakeBackend,
    FakeConnection,
    FakeCursor,
    rejected_at_startup,
    schema,
    settings,
)
from providers.postgresql.tests.test_acquisition_snapshot import (
    intent as snapshot_intent,
)


def _checkpoint_payload(
    cursor: PostgreSQLIncrementalCursor,
    *,
    logical_object_ref: str = "orders",
) -> bytes:
    return canonical_bytes(
        {
            "schema_version": "1",
            "objects": (
                {
                    "logical_object_ref": logical_object_ref,
                    "position": "row",
                    "updated_at": cursor.updated_at,
                    "primary_key": cursor.primary_key,
                },
            ),
        }
    )


def _checkpoint_cursor(payload: bytes) -> PostgreSQLIncrementalCursor:
    decoded = json.loads(payload)
    raw_cursor = decoded["objects"][0]
    assert raw_cursor["position"] == "row"
    return PostgreSQLIncrementalCursor.model_validate(
        {
            "updated_at": datetime.fromisoformat(raw_cursor["updated_at"].replace("Z", "+00:00")),
            "primary_key": raw_cursor["primary_key"],
        }
    )


def test_incremental_cursor_preserves_native_key_without_text_coercion() -> None:
    cursor = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        primary_key=Decimal("10.50"),
    )

    assert isinstance(cursor.primary_key, Decimal)
    assert cursor.primary_key == Decimal("10.50")


def test_incremental_cursor_decodes_numeric_string_and_decimal_from_approved_key_type() -> None:
    updated_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    numeric_string_payload = canonical_bytes(
        PostgreSQLIncrementalCursor(updated_at=updated_at, primary_key="00123")
    )
    decimal_payload = canonical_bytes(
        PostgreSQLIncrementalCursor(updated_at=updated_at, primary_key=Decimal("10.50"))
    )

    numeric_string = PostgreSQLIncrementalCursor.from_payload(
        numeric_string_payload,
        key_value_type="string",
    )
    decimal_key = PostgreSQLIncrementalCursor.from_payload(
        decimal_payload,
        key_value_type="decimal",
    )

    assert numeric_string.primary_key == "00123"
    assert isinstance(numeric_string.primary_key, str)
    assert decimal_key.primary_key == Decimal("10.50")
    assert isinstance(decimal_key.primary_key, Decimal)


def test_incremental_cursor_preserves_boolean_key_as_boolean() -> None:
    payload = canonical_bytes(
        PostgreSQLIncrementalCursor(
            updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            primary_key=True,
        )
    )

    cursor = PostgreSQLIncrementalCursor.from_payload(payload, key_value_type="boolean")

    assert cursor.primary_key is True
    assert type(cursor.primary_key) is bool


@pytest.mark.parametrize(
    "payload",
    (
        b'{"primary_key":5,"primary_key":6,"schema_version":"1",'
        b'"updated_at":"2026-09-01T11:50:00.000000Z"}',
        b'{ "primary_key":5,"schema_version":"1","updated_at":"2026-09-01T11:50:00.000000Z"}',
    ),
)
def test_incremental_cursor_rejects_duplicate_or_noncanonical_durable_bytes(payload: bytes) -> None:
    with pytest.raises(ValueError, match="canonical"):
        PostgreSQLIncrementalCursor.from_payload(payload, key_value_type="integer")


def test_incremental_cursor_refuses_naive_timestamp_null_key_and_unknown_input() -> None:
    for values in (
        {"updated_at": datetime(2026, 9, 1, 12, 0), "primary_key": 1},
        {"updated_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC), "primary_key": None},
        {
            "updated_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            "primary_key": 1,
            "unexpected": "authority",
        },
    ):
        with pytest.raises(ValidationError):
            PostgreSQLIncrementalCursor.model_validate(values)


def _ordering_key(row: tuple[object, ...]) -> int:
    """The row's first column, which every ordered query sorts and bounds by.

    The fake backend holds rows as `tuple[object, ...]` -- `open_writer` accepts
    whatever the writer hands it -- so `min`, `max` and `sorted` cannot take
    `row[0]` on trust.
    """

    key = row[0]
    assert isinstance(key, int), row
    return key


class IncrementalBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_time = NOW
        self.oldest_xact_start: datetime | None = None
        self.statistics_unavailable = False
        self.upper_cursor = PostgreSQLIncrementalCursor(
            updated_at=datetime(2026, 9, 1, 11, 55, tzinfo=UTC),
            primary_key=20,
        )
        self.snapshot_cursor_upper = (
            self.upper_cursor.updated_at,
            self.upper_cursor.primary_key,
        )
        self.incremental_count = 2
        self.derive_snapshot_upper = False
        self.derive_incremental_upper = False
        self.pending_writer_row: tuple[object, ...] | None = None
        self.records = [
            (7, Decimal("10.50"), True, None, datetime(2026, 9, 1, 11, 54, tzinfo=UTC)),
            (8, Decimal("11.25"), False, "renewal", datetime(2026, 9, 1, 11, 54, tzinfo=UTC)),
        ]

    def open_writer(self, row: tuple[object, ...]) -> None:
        self.pending_writer_row = row

    def commit_writer(self) -> None:
        assert self.pending_writer_row is not None
        self.records.append(self.pending_writer_row)
        self.pending_writer_row = None


class MultiIncrementalBackend(IncrementalBackend):
    def __init__(self) -> None:
        super().__init__()
        self.derive_incremental_upper = True
        self.subscription_records = [
            (
                60,
                Decimal("20.00"),
                True,
                "trialing",
                datetime(2026, 9, 1, 11, 46, tzinfo=UTC),
            ),
            (
                80,
                Decimal("30.00"),
                True,
                "active",
                datetime(2026, 9, 1, 11, 53, tzinfo=UTC),
            ),
        ]

    def evaluate_privilege_probe(self, statement: str, params: object) -> bool:
        return True


class IncrementalCursor(FakeCursor):
    def execute(self, query: object, params: object = None) -> None:
        super().execute(query, params)
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        backend = self.connection.backend
        assert isinstance(backend, IncrementalBackend)
        records = (
            backend.subscription_records
            if isinstance(backend, MultiIncrementalBackend) and '"subscription""table"' in rendered
            else backend.records
        )
        if "transaction_timestamp()" in rendered:
            self.rows = [(backend.snapshot_time,)]
        elif "snapshot_cursor_upper" in rendered and backend.derive_snapshot_upper:
            assert isinstance(params, tuple) and len(params) == 1
            candidates = [
                (row[4], row[0])
                for row in records
                if isinstance(row[4], datetime) and row[4] <= params[0]
            ]
            self.rows = [max(candidates)] if candidates else []
        elif "pg_catalog.pg_stat_activity" in rendered:
            if backend.statistics_unavailable:
                raise psycopg.errors.InsufficientPrivilege("statistics denied")
            self.rows = [(backend.oldest_xact_start,)]
        elif "incremental_upper" in rendered:
            if backend.derive_incremental_upper:
                assert isinstance(params, tuple) and len(params) == 1
                candidates = [
                    (row[4], row[0])
                    for row in records
                    if isinstance(row[4], datetime)
                    and (row[4] < params[0] if " < %s" in rendered else row[4] <= params[0])
                ]
                self.rows = [max(candidates)] if candidates else []
            else:
                self.rows = [(backend.upper_cursor.updated_at, backend.upper_cursor.primary_key)]
        elif "incremental_count_before_first" in rendered:
            assert isinstance(params, tuple) and len(params) in {1, 2}
            if len(params) == 2:
                upper = (params[0], params[1])
                count = sum((row[4], row[0]) <= upper for row in records)
            else:
                count = sum(
                    row[4] < params[0] if " < %s" in rendered else row[4] <= params[0]
                    for row in records
                )
            self.rows = [(count,)]
        elif "incremental_count" in rendered:
            if backend.derive_incremental_upper:
                assert isinstance(params, tuple) and len(params) == 4
                lower = (params[0], params[1])
                upper = (params[2], params[3])
                self.rows = [(sum(lower < (row[4], row[0]) <= upper for row in records),)]
            else:
                self.rows = [(backend.incremental_count,)]
        elif "snapshot_bounds" in rendered:
            assert isinstance(params, tuple) and len(params) in {1, 2}
            rows = [
                row
                for row in records
                if (
                    row[4] <= params[0]
                    if len(params) == 1
                    else (row[4], row[0]) <= (params[0], params[1])
                )
            ]
            keys = [_ordering_key(row) for row in rows]
            self.rows = [(min(keys), max(keys), len(keys))] if keys else [(None, None, 0)]
        elif "snapshot_rows_before_first" in rendered and self.name is not None:
            assert isinstance(params, tuple) and len(params) == 1
            self.rows = sorted(
                (row for row in records if row[4] <= params[0]),
                key=_ordering_key,
            )
        elif "snapshot_rows" in rendered and self.name is not None:
            assert isinstance(params, tuple) and len(params) == 2
            upper = (params[0], params[1])
            self.rows = sorted(
                (row for row in records if (row[4], row[0]) <= upper),
                key=_ordering_key,
            )
        elif "incremental_rows_before_first" in rendered and self.name is not None:
            assert isinstance(params, tuple) and len(params) in {1, 2}
            if len(params) == 2:
                upper = (params[0], params[1])
                rows = [row for row in records if (row[4], row[0]) <= upper]
            else:
                rows = [
                    row
                    for row in records
                    if (row[4] < params[0] if " < %s" in rendered else row[4] <= params[0])
                ]
            self.rows = sorted(rows, key=lambda row: (row[4], row[0]))
        elif "incremental_rows" in rendered and self.name is not None:
            assert isinstance(params, tuple) and len(params) == 4
            lower = (params[0], params[1])
            upper = (params[2], params[3])
            self.rows = sorted(
                (row for row in records if lower < (row[4], row[0]) <= upper),
                key=lambda row: (row[4], row[0]),
            )


class IncrementalConnection(FakeConnection):
    def cursor(self, name: str | None = None) -> IncrementalCursor:
        return IncrementalCursor(self, name=name)


def _incremental_provider(
    backend: IncrementalBackend,
) -> tuple[PostgreSQLAcquisitionProvider, IncrementalConnection, dict[str, bytes]]:
    connection = IncrementalConnection(backend)
    private_boundaries: dict[str, bytes] = {}
    provider = PostgreSQLAcquisitionProvider(
        settings(),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda tenant_id, reference, payload: (
            private_boundaries.__setitem__(f"{tenant_id}:{reference}", payload)
        ),
    )
    return provider, connection, private_boundaries


def _multi_incremental_provider(
    backend: MultiIncrementalBackend,
) -> tuple[
    PostgreSQLAcquisitionProvider,
    IncrementalConnection,
    tuple[AcquisitionObjectSchema, ...],
    tuple[str, ...],
]:
    base_declaration = settings().objects[0]
    subscriptions = PostgreSQLSourceObjectDeclaration(
        **{
            **base_declaration.model_dump(),
            "logical_object_ref": "subscriptions",
            "table_name": 'subscription"table',
        }
    )
    multi_settings = PostgreSQLAcquisitionSettings(
        dsn=SecretStr("postgresql://private-secret@source/private"),
        connection_handle="opaque-source-capability",
        objects=(base_declaration, subscriptions),
        unrelated_schema_name="private_unrelated",
        max_write_transaction_duration=settings().max_write_transaction_duration,
    )
    connection = IncrementalConnection(backend)
    provider = PostgreSQLAcquisitionProvider(
        multi_settings,
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda _tenant_id, _reference, _payload: None,
    )
    object_refs = ("orders", "subscriptions")
    schemas = (schema(), schema().model_copy(update={"logical_object_ref": "subscriptions"}))
    return provider, connection, schemas, object_refs


def _continuation_intent(
    private_cursor: bytes,
    *,
    mode: AcquisitionMode = "incremental",
) -> AcquisitionIntent:
    object_refs = ("orders",)
    run_intent_ref = "4" * 64
    contract_digest = "2" * 64
    prior_digest = hashlib.sha256(private_cursor).hexdigest()
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref="source-binding-a",
            acquisition_mode=mode,
            object_refs=object_refs,
            prior_checkpoint_revision=1,
        ),
        tenant_id="tenant-a",
        run_intent_ref=run_intent_ref,
        contract_ref="contract-a",
        contract_digest=contract_digest,
        source_binding_ref="source-binding-a",
        source_observation_digest="3" * 64,
        acquisition_mode=mode,
        object_refs=object_refs,
        prior_checkpoint_revision=1,
        prior_checkpoint_digest=prior_digest,
        record_ceiling=10,
        encoded_byte_ceiling=100_000,
        admitted_at=NOW,
    )


def _continuation_intent_for_refs(
    private_cursor: bytes,
    object_refs: tuple[str, ...],
) -> AcquisitionIntent:
    return _continuation_intent(private_cursor).model_copy(
        update={
            "object_refs": object_refs,
            "intent_key": acquisition_intent_key(
                tenant_id="tenant-a",
                run_intent_ref="4" * 64,
                contract_digest="2" * 64,
                source_binding_ref="source-binding-a",
                acquisition_mode="incremental",
                object_refs=object_refs,
                prior_checkpoint_revision=1,
            ),
        }
    )


def _incremental_intent(private_cursor: bytes) -> AcquisitionIntent:
    return _continuation_intent(private_cursor)


def test_incremental_uses_native_strict_lower_and_inclusive_lagged_upper_cursor() -> None:
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = _checkpoint_payload(lower)
    backend = IncrementalBackend()
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(
        _incremental_intent(private_cursor),
        (schema(),),
        private_cursor,
    )
    records = tuple(session)
    completion = session.complete()

    assert [record.source_updated_at for record in records] == [
        datetime(2026, 9, 1, 11, 54, tzinfo=UTC),
        datetime(2026, 9, 1, 11, 54, tzinfo=UTC),
    ]
    boundary = completion.boundaries[0]
    assert boundary.lower_cursor_digest == hashlib.sha256(private_cursor).hexdigest()
    assert (
        boundary.upper_cursor_digest
        == hashlib.sha256(completion.candidate_cursor_payload).hexdigest()
    )
    query, parameters, _name = next(
        call for call in connection.calls if "incremental_rows" in call[0]
    )
    assert ") > (%s, %s)" in query and ") <= (%s, %s)" in query
    assert parameters == (
        lower.updated_at,
        lower.primary_key,
        backend.upper_cursor.updated_at,
        backend.upper_cursor.primary_key,
    )
    assert _checkpoint_cursor(completion.candidate_cursor_payload) == backend.upper_cursor
    assert completion.cursor_version == "postgresql-incremental-v1"
    assert connection.committed and connection.closed
    assert boundary.query_shape_digest == digest(
        {
            "columns": settings().objects[0].field_names,
            "lower_cursor_position": "row",
            "upper_cursor_position": "row",
            "upper_selection": {"columns": ("updated_at",), "operator": "<="},
            "row_predicate": {
                "lower_columns": ("updated_at", 'order"id'),
                "lower_operator": ">",
                "upper_columns": ("updated_at", 'order"id'),
                "upper_operator": "<=",
            },
            "order_by": ("updated_at", 'order"id'),
        }
    )


def test_incremental_falls_back_to_declared_lag_when_statistics_are_denied() -> None:
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = _checkpoint_payload(lower)
    backend = IncrementalBackend()
    backend.statistics_unavailable = True
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(
        _incremental_intent(private_cursor),
        (schema(),),
        private_cursor,
    )
    tuple(session)

    upper_parameters = next(
        parameters for query, parameters, _name in connection.calls if "incremental_upper" in query
    )
    assert upper_parameters == (datetime(2026, 9, 1, 11, 55, tzinfo=UTC),)
    assert "ROLLBACK TO SAVEPOINT heinzel_xact_horizon" in tuple(
        query for query, _parameters, _name in connection.calls
    )
    assert connection.committed and connection.closed


def test_incremental_tightens_lag_to_oldest_visible_transaction_start() -> None:
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 45, tzinfo=UTC),
        primary_key=1,
    )
    private_cursor = _checkpoint_payload(lower)
    backend = IncrementalBackend()
    backend.oldest_xact_start = datetime(2026, 9, 1, 11, 52, tzinfo=UTC)
    acquisition_provider, connection, private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(
        _incremental_intent(private_cursor),
        (schema(),),
        private_cursor,
    )
    tuple(session)
    completion = session.complete()

    upper_parameters = next(
        parameters for query, parameters, _name in connection.calls if "incremental_upper" in query
    )
    assert upper_parameters == (datetime(2026, 9, 1, 11, 52, tzinfo=UTC),)
    detail = next(iter(private_boundaries.values()))
    assert b'"oldest_xact_start":"2026-09-01T11:52:00.000000Z"' in detail
    assert completion.boundaries[0].query_shape_digest == digest(
        {
            "columns": settings().objects[0].field_names,
            "lower_cursor_position": "row",
            "upper_cursor_position": "row",
            "upper_selection": {"columns": ("updated_at",), "operator": "<"},
            "row_predicate": {
                "lower_columns": ("updated_at", 'order"id'),
                "lower_operator": ">",
                "upper_columns": ("updated_at", 'order"id'),
                "upper_operator": "<=",
            },
            "order_by": ("updated_at", 'order"id'),
        }
    )


def test_active_transaction_horizon_excludes_equal_timestamp_with_adverse_key_order() -> None:
    backend = IncrementalBackend()
    backend.derive_incremental_upper = True
    backend.records = [
        (10, Decimal("10.00"), True, "safe", datetime(2026, 9, 1, 11, 51, tzinfo=UTC)),
        (100, Decimal("12.00"), True, "visible", datetime(2026, 9, 1, 11, 52, tzinfo=UTC)),
    ]
    backend.oldest_xact_start = datetime(2026, 9, 1, 11, 52, tzinfo=UTC)
    backend.open_writer(
        (50, Decimal("11.00"), True, "late", datetime(2026, 9, 1, 11, 52, tzinfo=UTC))
    )
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    first_cursor = _checkpoint_payload(lower)
    first_provider, first_connection, _private_boundaries = _incremental_provider(backend)

    first_session = first_provider.open_acquisition(
        _incremental_intent(first_cursor),
        (schema(),),
        first_cursor,
    )
    tuple(first_session)
    acknowledged_cursor = first_session.complete().candidate_cursor_payload
    upper_query = next(
        query
        for query, _parameters, _name in first_connection.calls
        if "incremental_upper" in query
    )
    assert '"updated_at" < %s' in upper_query
    backend.commit_writer()

    backend.oldest_xact_start = None
    backend.snapshot_time = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    second_provider, _second_connection, _private_boundaries = _incremental_provider(backend)
    second_session = second_provider.open_acquisition(
        _incremental_intent(acknowledged_cursor),
        (schema(),),
        acknowledged_cursor,
    )
    records = tuple(second_session)

    keys = [
        next(field.value for field in record.fields if field.name == 'order"id')
        for record in records
    ]
    assert 50 in keys


def test_incremental_refuses_count_ceiling_before_opening_row_cursor() -> None:
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = _checkpoint_payload(lower)
    backend = IncrementalBackend()
    backend.incremental_count = 11
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    with pytest.raises(AcquisitionCeilingExceeded):
        acquisition_provider.open_acquisition(
            _incremental_intent(private_cursor),
            (schema(),),
            private_cursor,
        )

    assert connection.named_cursor_reads == 0
    assert connection.rolled_back and connection.closed


def test_corrupt_prior_cursor_is_classified_as_integrity_failure() -> None:
    corrupt_cursor = (
        b'{ "primary_key":5,"schema_version":"1","updated_at":"2026-09-01T11:50:00.000000Z"}'
    )
    backend = IncrementalBackend()
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(
            _incremental_intent(corrupt_cursor),
            (schema(),),
            corrupt_cursor,
        )

    assert captured.value.reason_code == "integrity_failure"
    assert connection.closed is False


def test_malformed_checkpoint_timestamp_is_classified_as_integrity_failure() -> None:
    malformed_cursor = canonical_bytes(
        {
            "schema_version": "1",
            "objects": (
                {
                    "logical_object_ref": "orders",
                    "position": "row",
                    "updated_at": "not-a-timestamp",
                    "primary_key": 5,
                },
            ),
        }
    )
    backend = IncrementalBackend()
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(
            _incremental_intent(malformed_cursor),
            (schema(),),
            malformed_cursor,
        )

    assert captured.value.reason_code == "integrity_failure"
    assert connection.closed is False


def test_empty_incremental_commits_lagged_upper_cursor_without_fabricating_records() -> None:
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = _checkpoint_payload(lower)
    backend = IncrementalBackend()
    backend.derive_incremental_upper = True
    backend.records = []
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(
        _incremental_intent(private_cursor),
        (schema(),),
        private_cursor,
    )
    assert tuple(session) == ()

    completion = session.complete()
    assert completion.boundaries[0].record_count == 0
    assert _checkpoint_cursor(completion.candidate_cursor_payload) == lower
    assert completion.candidate_cursor_payload == private_cursor
    assert connection.committed and connection.closed


def test_snapshot_produces_same_compound_cursor_version_required_by_incrementals() -> None:
    backend = IncrementalBackend()
    acquisition_provider, _connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(snapshot_intent(), (schema(),), None)
    tuple(session)
    completion = session.complete()

    cursor = _checkpoint_cursor(completion.candidate_cursor_payload)
    assert cursor == backend.upper_cursor
    assert completion.cursor_version == "postgresql-incremental-v1"


def test_snapshot_checkpoint_carries_every_object_cursor_in_intent_order() -> None:
    backend = MultiIncrementalBackend()
    acquisition_provider, _connection, schemas, object_refs = _multi_incremental_provider(backend)
    snapshot = snapshot_intent().model_copy(
        update={
            "object_refs": object_refs,
            "intent_key": acquisition_intent_key(
                tenant_id="tenant-a",
                run_intent_ref="1" * 64,
                contract_digest="2" * 64,
                source_binding_ref="source-binding-a",
                acquisition_mode="snapshot",
                object_refs=object_refs,
                prior_checkpoint_revision=0,
            ),
        }
    )

    session = acquisition_provider.open_acquisition(snapshot, schemas, None)
    tuple(session)
    checkpoint = json.loads(session.complete().candidate_cursor_payload)

    assert checkpoint["schema_version"] == "1"
    assert [item["logical_object_ref"] for item in checkpoint["objects"]] == list(object_refs)


def test_incremental_advances_every_object_cursor_without_scope_substitution() -> None:
    backend = MultiIncrementalBackend()
    acquisition_provider, connection, schemas, object_refs = _multi_incremental_provider(backend)
    orders_lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    subscriptions_lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 45, tzinfo=UTC),
        primary_key=50,
    )
    private_cursor = canonical_bytes(
        {
            "schema_version": "1",
            "objects": (
                {
                    "logical_object_ref": "orders",
                    "position": "row",
                    "updated_at": orders_lower.updated_at,
                    "primary_key": orders_lower.primary_key,
                },
                {
                    "logical_object_ref": "subscriptions",
                    "position": "row",
                    "updated_at": subscriptions_lower.updated_at,
                    "primary_key": subscriptions_lower.primary_key,
                },
            ),
        }
    )

    session = acquisition_provider.open_acquisition(
        _continuation_intent_for_refs(private_cursor, object_refs),
        schemas,
        private_cursor,
    )
    records = tuple(session)
    completion = session.complete()
    checkpoint = json.loads(completion.candidate_cursor_payload)

    assert [boundary.logical_object_ref for boundary in completion.boundaries] == list(object_refs)
    assert [item["logical_object_ref"] for item in checkpoint["objects"]] == list(object_refs)
    assert [item["primary_key"] for item in checkpoint["objects"]] == [8, 80]
    assert len(records) == 4
    assert connection.named_cursor_reads == 2

    reordered = canonical_bytes(
        {
            "schema_version": "1",
            "objects": tuple(reversed(checkpoint["objects"])),
        }
    )
    second_provider, second_connection, second_schemas, _refs = _multi_incremental_provider(backend)
    with pytest.raises(AcquisitionProviderError) as captured:
        second_provider.open_acquisition(
            _continuation_intent_for_refs(reordered, object_refs),
            second_schemas,
            reordered,
        )
    assert captured.value.reason_code == "integrity_failure"
    assert second_connection.closed is False


def test_multi_object_incremental_applies_one_cumulative_record_ceiling() -> None:
    backend = MultiIncrementalBackend()
    backend.derive_incremental_upper = False
    backend.incremental_count = 6
    acquisition_provider, connection, schemas, object_refs = _multi_incremental_provider(backend)
    lower = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = canonical_bytes(
        {
            "schema_version": "1",
            "objects": tuple(
                {
                    "logical_object_ref": object_ref,
                    "position": "row",
                    "updated_at": lower.updated_at,
                    "primary_key": lower.primary_key,
                }
                for object_ref in object_refs
            ),
        }
    )

    with pytest.raises(AcquisitionCeilingExceeded):
        acquisition_provider.open_acquisition(
            _continuation_intent_for_refs(private_cursor, object_refs),
            schemas,
            private_cursor,
        )

    assert connection.named_cursor_reads == 0
    assert connection.rolled_back and connection.closed


def test_snapshot_before_first_cursor_allows_an_empty_safe_prefix() -> None:
    backend = IncrementalBackend()
    backend.derive_snapshot_upper = True
    backend.records = [
        (30, Decimal("11.00"), True, "inside-lag", datetime(2026, 9, 1, 11, 58, tzinfo=UTC))
    ]
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(snapshot_intent(), (schema(),), None)

    assert tuple(session) == ()
    checkpoint = json.loads(session.complete().candidate_cursor_payload)
    assert checkpoint["objects"][0] == {
        "logical_object_ref": "orders",
        "position": "before_first",
        "primary_key": None,
        "updated_at": "2026-09-01T11:55:00.000000Z",
    }
    assert session.complete().boundaries[0].query_shape_digest == digest(
        {
            "columns": settings().objects[0].field_names,
            "order_by": 'order"id',
            "cursor_position": "before_first",
            "row_predicate": {
                "upper_columns": ("updated_at",),
                "upper_operator": "<=",
            },
        }
    )
    assert connection.committed and connection.closed


@pytest.mark.parametrize(
    ("key_value_type", "column_metadata", "wrong_driver_key"),
    (
        ("boolean", ('order"id', "boolean", "bool", "NO"), 1),
        ("integer", ('order"id', "bigint", "int8", "NO"), True),
        ("decimal", ('order"id', "numeric", "numeric", "NO"), "10.00"),
        ("string", ('order"id', "text", "text", "NO"), 10),
        (
            "timestamp",
            ('order"id', "timestamp with time zone", "timestamptz", "NO"),
            datetime(2026, 9, 1, 11, 55),
        ),
    ),
)
def test_snapshot_rejects_each_wrong_native_driver_cursor_key_type(
    key_value_type: str,
    column_metadata: tuple[object, ...],
    wrong_driver_key: object,
) -> None:
    backend = IncrementalBackend()
    backend.columns[0] = column_metadata
    backend.snapshot_cursor_upper = (NOW, wrong_driver_key)
    approved_fields = (
        schema().fields[0].model_copy(update={"value_type": key_value_type}),
        *schema().fields[1:],
    )
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(
            snapshot_intent(),
            (schema(fields=approved_fields),),
            None,
        )

    assert captured.value.reason_code == "invalid_provider_response"
    assert connection.rolled_back and connection.closed


def test_incremental_after_before_first_cursor_acquires_first_safe_rows() -> None:
    backend = IncrementalBackend()
    backend.derive_snapshot_upper = True
    backend.records = [
        (30, Decimal("11.00"), True, "inside-lag", datetime(2026, 9, 1, 11, 58, tzinfo=UTC))
    ]
    snapshot_provider, _snapshot_connection, _private_boundaries = _incremental_provider(backend)
    snapshot_session = snapshot_provider.open_acquisition(snapshot_intent(), (schema(),), None)
    assert tuple(snapshot_session) == ()
    private_cursor = snapshot_session.complete().candidate_cursor_payload

    backend.snapshot_time = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    backend.derive_incremental_upper = True
    provider, connection, _private_boundaries = _incremental_provider(backend)
    session = provider.open_acquisition(
        _incremental_intent(private_cursor),
        (schema(),),
        private_cursor,
    )

    records = tuple(session)
    completion = session.complete()
    assert [record.fields[0].value for record in records] == [30]
    assert _checkpoint_cursor(completion.candidate_cursor_payload).primary_key == 30
    assert completion.boundaries[0].query_shape_digest == digest(
        {
            "columns": settings().objects[0].field_names,
            "lower_cursor_position": "before_first",
            "upper_cursor_position": "row",
            "upper_selection": {"columns": ("updated_at",), "operator": "<="},
            "row_predicate": {
                "lower_columns": None,
                "lower_operator": None,
                "upper_columns": ("updated_at", 'order"id'),
                "upper_operator": "<=",
            },
            "order_by": ("updated_at", 'order"id'),
        }
    )
    assert connection.committed and connection.closed


def test_lagged_snapshot_cursor_captures_writer_committed_after_snapshot() -> None:
    backend = IncrementalBackend()
    backend.derive_snapshot_upper = True
    backend.records = [
        (20, Decimal("10.00"), True, "safe", datetime(2026, 9, 1, 11, 55, tzinfo=UTC)),
        (40, Decimal("12.00"), True, "recent", datetime(2026, 9, 1, 12, 0, tzinfo=UTC)),
    ]
    backend.bounds = (20, 40, 2)
    backend.open_writer(
        (30, Decimal("11.00"), True, "late", datetime(2026, 9, 1, 11, 58, tzinfo=UTC))
    )
    snapshot_provider, _snapshot_connection, _private_boundaries = _incremental_provider(backend)

    snapshot_session = snapshot_provider.open_acquisition(snapshot_intent(), (schema(),), None)
    snapshot_records = tuple(snapshot_session)
    snapshot_completion = snapshot_session.complete()
    acknowledged_cursor = snapshot_completion.candidate_cursor_payload
    snapshot_keys = [
        next(field.value for field in record.fields if field.name == 'order"id')
        for record in snapshot_records
    ]
    assert snapshot_keys == [20]
    assert (
        snapshot_completion.boundaries[0].upper_cursor_digest
        == hashlib.sha256(acknowledged_cursor).hexdigest()
    )
    backend.commit_writer()

    backend.snapshot_time = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    backend.upper_cursor = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        primary_key=40,
    )
    backend.incremental_count = 2
    incremental_provider, _incremental_connection, _private_boundaries = _incremental_provider(
        backend
    )
    incremental_session = incremental_provider.open_acquisition(
        _incremental_intent(acknowledged_cursor),
        (schema(),),
        acknowledged_cursor,
    )

    records = tuple(incremental_session)
    keys = [
        next(field.value for field in record.fields if field.name == 'order"id')
        for record in records
    ]
    assert keys == [30, 40]


def test_reconciliation_streams_complete_snapshot_without_advancing_incremental_cursor() -> None:
    prior = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    private_cursor = _checkpoint_payload(prior)
    backend = IncrementalBackend()
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    session = acquisition_provider.open_acquisition(
        _continuation_intent(private_cursor, mode="reconciliation"),
        (schema(),),
        private_cursor,
    )
    records = tuple(session)
    completion = session.complete()

    assert len(records) == 2
    boundary = completion.boundaries[0]
    assert boundary.acquisition_mode == "reconciliation"
    assert boundary.record_count == 2
    assert boundary.snapshot_identity_digest is not None
    assert boundary.key_range_digest is not None
    assert boundary.lower_cursor_digest == hashlib.sha256(private_cursor).hexdigest()
    assert boundary.upper_cursor_digest == hashlib.sha256(private_cursor).hexdigest()
    assert completion.candidate_cursor_payload == private_cursor
    assert completion.cursor_version == "postgresql-incremental-v1"
    reconciliation_query = next(
        query
        for query, _parameters, name in connection.calls
        if name == "heinzel_reconciliation_0000"
    )
    assert 'ORDER BY "order""id"' in reconciliation_query


def test_reconciliation_rejects_cursor_for_a_different_object_scope() -> None:
    prior = PostgreSQLIncrementalCursor(
        updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
        primary_key=5,
    )
    wrong_scope_cursor = _checkpoint_payload(prior, logical_object_ref="subscriptions")
    backend = IncrementalBackend()
    acquisition_provider, connection, _private_boundaries = _incremental_provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(
            _continuation_intent(wrong_scope_cursor, mode="reconciliation"),
            (schema(),),
            wrong_scope_cursor,
        )

    assert captured.value.reason_code == "integrity_failure"
    assert connection.closed is False


def test_rejected_credentials_at_incremental_startup_are_authorization_denied() -> None:
    private_cursor = _checkpoint_payload(
        PostgreSQLIncrementalCursor(
            updated_at=datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
            primary_key=5,
        )
    )
    acquisition_provider, probe_calls = rejected_at_startup(psycopg.errors.InvalidPassword())

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(
            _incremental_intent(private_cursor),
            (schema(),),
            private_cursor,
        )

    assert captured.value.classification == "authorization_denied"
    assert len(probe_calls) == 1
