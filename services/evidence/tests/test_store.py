import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pillarmesh_evidence import (
    ActiveRunError,
    InvalidStateTransition,
    MigrationError,
    RunRecord,
    SQLiteStore,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
HISTORICAL_V1_CHECKSUM = "20649936aff7f5922006c25ce7e6aa24b8faa847a3d788e51be42f932e4890b3"
HISTORICAL_V1_SQL = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (kind, digest)
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    namespace TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    PRIMARY KEY (namespace, ref_key)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    activation_key TEXT NOT NULL UNIQUE,
    contract_digest TEXT NOT NULL,
    summary_digest TEXT NOT NULL,
    signed_graph_json TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('created','running','succeeded','failed','non_conforming')),
    checkpoint TEXT NOT NULL,
    batch_id TEXT,
    acceptance_key INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_store
ON runs((1)) WHERE state IN ('created', 'running');

CREATE TABLE IF NOT EXISTS evidence_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    producer TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    previous_digest TEXT,
    event_digest TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, event_digest)
);

CREATE TRIGGER IF NOT EXISTS evidence_events_no_update
BEFORE UPDATE ON evidence_events
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

CREATE TRIGGER IF NOT EXISTS evidence_events_no_delete
BEFORE DELETE ON evidence_events
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
""".strip()


def store(path: Path) -> SQLiteStore:
    return SQLiteStore.open(path)


def create_historical_v1_database(path: Path) -> None:
    assert hashlib.sha256(HISTORICAL_V1_SQL.encode("utf-8")).hexdigest() == HISTORICAL_V1_CHECKSUM
    connection = sqlite3.connect(path)
    connection.executescript(HISTORICAL_V1_SQL)
    connection.execute(
        "INSERT INTO schema_metadata(singleton, version, checksum) VALUES (1, ?, ?)",
        (1, HISTORICAL_V1_CHECKSUM),
    )
    connection.execute(
        """INSERT INTO runs(
            run_id, activation_key, contract_digest, summary_digest, signed_graph_json,
            state, checkpoint, batch_id, acceptance_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "run-1",
            "activation-1",
            "a" * 64,
            "b" * 64,
            "{}",
            "created",
            "created",
            None,
            42,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    connection.commit()
    connection.close()


def test_evidence_chain_continues_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "m0.sqlite3"
    first = store(path)
    first.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    event_one = first.append_event("run-1", "activation", NOW, "contract", {"digest": "a" * 64})
    first.close()

    resumed = store(path)
    event_two = resumed.append_event(
        "run-1", "graph_verified", NOW, "runtime", {"digest": "c" * 64}
    )

    assert event_two.sequence == 2
    assert event_two.previous_digest == event_one.event_digest
    assert resumed.verify_chain("run-1") is True


def test_evidence_rows_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    path = tmp_path / "m0.sqlite3"
    database = store(path)
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.append_event("run-1", "activation", NOW, "contract", {})
    connection = sqlite3.connect(path)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE evidence_events SET event_type = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM evidence_events")
    connection.close()


def test_checkpoint_and_event_are_atomic(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)

    with pytest.raises(TypeError, match="floating-point"):
        database.advance_checkpoint_with_event(
            "run-1",
            expected_checkpoint="created",
            checkpoint="graph_verified",
            state="running",
            event_type="graph_verified",
            occurred_at=NOW,
            producer="runtime",
            attributes={"forbidden": 1.5},
        )

    assert database.get_run("run-1").checkpoint == "created"
    assert database.trace("run-1") == ()


def test_publish_verification_persists_artifacts_summary_and_reference_atomically(
    tmp_path: Path,
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    payloads = (
        ("intent_ir", b'{"artifact":"iir"}'),
        ("physical_plan", b'{"artifact":"plan"}'),
        ("legality_decision", b'{"artifact":"decision"}'),
        ("signed_execution_graph", b'{"artifact":"signed-graph"}'),
    )
    artifacts = tuple(
        (kind, hashlib.sha256(payload).hexdigest(), payload) for kind, payload in payloads
    )
    summary_payload = b'{"artifact":"activation-summary"}'
    summary_digest = hashlib.sha256(summary_payload).hexdigest()

    database.publish_verification(artifacts, summary_digest, summary_payload)

    for kind, artifact_digest, payload in artifacts:
        assert database.load_artifact(kind, artifact_digest) == payload
    assert database.load_artifact("activation_summary", summary_digest) == summary_payload
    assert database.resolve_artifact_ref("activation_summaries", summary_digest) == summary_digest


def test_publish_verification_rolls_back_every_write_on_third_artifact_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    payloads = (
        ("intent_ir", b'{"artifact":"iir"}'),
        ("physical_plan", b'{"artifact":"plan"}'),
        ("legality_decision", b'{"artifact":"decision"}'),
        ("signed_execution_graph", b'{"artifact":"signed-graph"}'),
    )
    artifacts = tuple(
        (kind, hashlib.sha256(payload).hexdigest(), payload) for kind, payload in payloads
    )
    summary_payload = b'{"artifact":"activation-summary"}'
    summary_digest = hashlib.sha256(summary_payload).hexdigest()
    original_save = database._save_artifact
    writes = 0

    def fail_third_write(kind: str, artifact_digest: str, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise RuntimeError("injected third artifact failure")
        original_save(kind, artifact_digest, payload)

    monkeypatch.setattr(database, "_save_artifact", fail_third_write)

    with pytest.raises(RuntimeError, match="third artifact"):
        database.publish_verification(artifacts, summary_digest, summary_payload)

    for kind, artifact_digest, _payload in artifacts:
        assert database.load_artifact(kind, artifact_digest) is None
    assert database.load_artifact("activation_summary", summary_digest) is None
    assert database.resolve_artifact_ref("activation_summaries", summary_digest) is None
    assert database._connection.in_transaction is False


@pytest.mark.parametrize("mismatch", ["parent", "summary"])
def test_publish_verification_rejects_payload_digest_mismatch_before_writing(
    tmp_path: Path, mismatch: str
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    parent_payload = b'{"artifact":"iir"}'
    parent_digest = hashlib.sha256(parent_payload).hexdigest()
    summary_payload = b'{"artifact":"activation-summary"}'
    summary_digest = hashlib.sha256(summary_payload).hexdigest()
    if mismatch == "parent":
        parent_digest = "f" * 64
    else:
        summary_digest = "f" * 64

    with pytest.raises(ValueError, match="digest does not match payload"):
        database.publish_verification(
            (("intent_ir", parent_digest, parent_payload),),
            summary_digest,
            summary_payload,
        )

    assert database.load_artifact("intent_ir", parent_digest) is None
    assert database.load_artifact("activation_summary", summary_digest) is None
    assert database.resolve_artifact_ref("activation_summaries", summary_digest) is None


def test_publish_verification_rolls_back_commit_failure_and_preserves_error(
    tmp_path: Path,
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    parent_payload = b'{"artifact":"iir"}'
    parent_digest = hashlib.sha256(parent_payload).hexdigest()
    summary_payload = b'{"artifact":"activation-summary"}'
    summary_digest = hashlib.sha256(summary_payload).hexdigest()

    def deny_commit(
        action_code: int,
        action_name: str | None,
        _argument: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        if action_code == sqlite3.SQLITE_TRANSACTION and action_name == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    database._connection.set_authorizer(deny_commit)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        database.publish_verification(
            (("intent_ir", parent_digest, parent_payload),),
            summary_digest,
            summary_payload,
        )
    database._connection.set_authorizer(None)

    assert database._connection.in_transaction is False
    assert database.load_artifact("intent_ir", parent_digest) is None
    assert database.load_artifact("activation_summary", summary_digest) is None
    assert database.resolve_artifact_ref("activation_summaries", summary_digest) is None


def test_publish_verification_rolls_back_parent_missing_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    parent_payload = b'{"artifact":"iir"}'
    parent_digest = hashlib.sha256(parent_payload).hexdigest()
    summary_payload = b'{"artifact":"activation-summary"}'
    summary_digest = hashlib.sha256(summary_payload).hexdigest()
    original_bind = database.bind_artifact_ref

    def bind_then_delete_parent(namespace: str, ref_key: str, artifact_digest: str) -> None:
        original_bind(namespace, ref_key, artifact_digest)
        database._connection.execute(
            "DELETE FROM artifacts WHERE kind = 'intent_ir' AND digest = ?",
            (parent_digest,),
        )

    monkeypatch.setattr(database, "bind_artifact_ref", bind_then_delete_parent)

    with pytest.raises(RuntimeError, match="publication resolution"):
        database.publish_verification(
            (("intent_ir", parent_digest, parent_payload),),
            summary_digest,
            summary_payload,
        )

    assert database._connection.in_transaction is False
    assert database.load_artifact("intent_ir", parent_digest) is None
    assert database.load_artifact("activation_summary", summary_digest) is None
    assert database.resolve_artifact_ref("activation_summaries", summary_digest) is None


def test_record_extraction_persists_artifacts_private_path_and_checkpoint_atomically(
    tmp_path: Path,
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.transition_run("run-1", "created", "running", "snapshot_opened", NOW)
    database.set_acceptance_key("run-1", 984201)
    source_payload = b'{"schema_version":"1","snapshot_identity":"snapshot-1"}'
    manifest_payload = b'{"schema_version":"2","segment_name":"segment.csv"}'
    source_digest = hashlib.sha256(source_payload).hexdigest()
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    segment_path = tmp_path / "private" / "segment.csv"

    event = database.record_extraction(
        "run-1",
        source_boundary_digest=source_digest,
        source_boundary_payload=source_payload,
        manifest_digest=manifest_digest,
        manifest_payload=manifest_payload,
        segment_path=segment_path,
        batch_id="batch-1",
        row_count=1,
        encoded_bytes=64,
        occurred_at=NOW,
        producer="runtime",
    )

    assert database.load_artifact("source_boundary", source_digest) == source_payload
    assert database.load_artifact("segment_manifest", manifest_digest) == manifest_payload
    assert database.get_private_state("run-1").segment_path == segment_path
    assert database.get_run("run-1").checkpoint == "extraction_completed"
    assert event.event_type == "extraction_completed"
    assert event.attributes["manifest_digest"] == manifest_digest
    assert event.attributes["source_boundary_digest"] == source_digest


def test_record_extraction_rolls_back_every_write_when_event_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.transition_run("run-1", "created", "running", "snapshot_opened", NOW)
    database.set_acceptance_key("run-1", 984201)
    source_payload = b'{"schema_version":"1","snapshot_identity":"snapshot-1"}'
    manifest_payload = b'{"schema_version":"2","segment_name":"segment.csv"}'
    source_digest = hashlib.sha256(source_payload).hexdigest()
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    segment_path = tmp_path / "private" / "segment.csv"

    def fail_event(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(database, "_append_event", fail_event)
    with pytest.raises(RuntimeError, match="injected event failure"):
        database.record_extraction(
            "run-1",
            source_boundary_digest=source_digest,
            source_boundary_payload=source_payload,
            manifest_digest=manifest_digest,
            manifest_payload=manifest_payload,
            segment_path=segment_path,
            batch_id="batch-1",
            row_count=1,
            encoded_bytes=64,
            occurred_at=NOW,
            producer="runtime",
        )

    assert database.load_artifact("source_boundary", source_digest) is None
    assert database.load_artifact("segment_manifest", manifest_digest) is None
    assert database.get_private_state("run-1").segment_path is None
    assert database.get_run("run-1").checkpoint == "snapshot_opened"
    assert database.trace("run-1") == ()


def test_record_extraction_rolls_back_base_exception_and_closes_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedInterrupt(BaseException):
        pass

    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.transition_run("run-1", "created", "running", "snapshot_opened", NOW)
    database.set_acceptance_key("run-1", 984201)
    source_payload = b'{"schema_version":"1","snapshot_identity":"snapshot-1"}'
    manifest_payload = b'{"schema_version":"2","segment_name":"segment.csv"}'
    source_digest = hashlib.sha256(source_payload).hexdigest()
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    segment_path = tmp_path / "private" / "segment.csv"

    def interrupt_after_partial_writes(*_args: object, **_kwargs: object) -> None:
        raise InjectedInterrupt("injected interrupt")

    monkeypatch.setattr(database, "_append_event", interrupt_after_partial_writes)
    with pytest.raises(InjectedInterrupt, match="injected interrupt"):
        database.record_extraction(
            "run-1",
            source_boundary_digest=source_digest,
            source_boundary_payload=source_payload,
            manifest_digest=manifest_digest,
            manifest_payload=manifest_payload,
            segment_path=segment_path,
            batch_id="batch-1",
            row_count=1,
            encoded_bytes=64,
            occurred_at=NOW,
            producer="runtime",
        )

    assert database._connection.in_transaction is False
    assert database.load_artifact("source_boundary", source_digest) is None
    assert database.load_artifact("segment_manifest", manifest_digest) is None
    assert database.get_private_state("run-1").segment_path is None
    assert database.get_run("run-1").checkpoint == "snapshot_opened"
    assert database.trace("run-1") == ()


def test_terminal_state_cannot_regress(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.transition_run("run-1", "created", "succeeded", "terminal", NOW)

    with pytest.raises(InvalidStateTransition):
        database.transition_run("run-1", "succeeded", "running", "snapshot_opened", NOW)


def test_only_one_different_activation_can_be_active(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    original = database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    repeated = database.create_run("run-2", "activation-1", "a" * 64, "b" * 64, "{}", NOW)

    assert repeated.run_id == original.run_id
    with pytest.raises(ActiveRunError):
        database.create_run("run-3", "activation-2", "c" * 64, "d" * 64, "{}", NOW)


def test_v1_database_is_refused_without_application_table_access_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "m0.sqlite3"
    create_historical_v1_database(path)
    original_bytes = path.read_bytes()
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    with pytest.raises(
        MigrationError,
        match=(
            "database schema version 1 is unsupported; required version 2; "
            "start with a fresh state path"
        ),
    ):
        store(path)

    assert path.read_bytes() == original_bytes
    application_tables = ("artifacts", "artifact_refs", "runs", "evidence_events")
    assert not any(
        table in statement.lower() for statement in statements for table in application_tables
    )


def test_private_state_is_durable_immutable_and_not_returned_by_run_or_trace(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m0.sqlite3"
    database = store(path)
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)

    database.set_acceptance_key("run-1", 7)
    database.set_segment_path("run-1", Path("/private/staging/segment.csv"))
    state = database.get_private_state("run-1")
    assert state.acceptance_key == 7
    assert state.segment_path == Path("/private/staging/segment.csv")
    assert "acceptance_key" not in database.get_run("run-1").model_dump_json()
    assert "/private/staging/segment.csv" not in database.get_run("run-1").model_dump_json()
    database.append_event("run-1", "activated", NOW, "contract", {"digest": "a" * 64})
    serialized_trace = database.trace("run-1")[0].model_dump_json()
    assert "acceptance_key" not in serialized_trace
    assert "/private/staging/segment.csv" not in serialized_trace

    database.set_acceptance_key("run-1", 7)
    database.set_segment_path("run-1", Path("/private/staging/segment.csv"))
    with pytest.raises(ValueError, match="cannot change"):
        database.set_acceptance_key("run-1", 8)
    with pytest.raises(ValueError, match="cannot change"):
        database.set_segment_path("run-1", Path("/private/staging/replaced.csv"))
    database.close()

    resumed = store(path)
    assert resumed.get_private_state("run-1") == state


def test_private_state_rejects_missing_run(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")

    with pytest.raises(KeyError, match="missing"):
        database.get_private_state("missing")
    with pytest.raises(KeyError, match="missing"):
        database.set_acceptance_key("missing", 7)
    with pytest.raises(KeyError, match="missing"):
        database.set_segment_path("missing", Path("/private/staging/segment.csv"))


def test_migration_checksum_and_newer_version_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "m0.sqlite3"
    database = store(path)
    database.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_metadata SET checksum = 'tampered'")
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError, match="checksum"):
        store(path)

    newer = tmp_path / "newer.sqlite3"
    database = store(newer)
    database.close()
    connection = sqlite3.connect(newer)
    connection.execute("UPDATE schema_metadata SET version = 3")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="newer"):
        store(newer)


def test_ref_prefix_matches_literally_not_as_a_like_pattern(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.bind_artifact_ref("contracts", f"orders_sync:{1:020d}", "a" * 64)
    database.bind_artifact_ref("contracts", f"ordersXsync:{9:020d}", "b" * 64)
    database.bind_artifact_ref("contracts", f"100%off:{2:020d}", "c" * 64)
    database.bind_artifact_ref("contracts", f"unrelated:{3:020d}", "d" * 64)

    assert database.list_artifact_refs("contracts", "orders_sync:") == (
        (f"orders_sync:{1:020d}", "a" * 64),
    )
    assert database.list_artifact_refs("contracts", "100%off:") == (
        (f"100%off:{2:020d}", "c" * 64),
    )


def test_interrupted_migration_never_leaves_a_schema_without_its_version_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m0.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)

    class FailingInsert:
        """Fails exactly the metadata insert, as a crash between the two writes would."""

        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def execute(self, statement: str, *arguments: object) -> sqlite3.Cursor:
            if statement.lstrip().upper().startswith("INSERT INTO SCHEMA_METADATA"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._wrapped.execute(statement, *arguments)

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def __setattr__(self, name: str, value: object) -> None:
            if name == "_wrapped":
                super().__setattr__(name, value)
            else:
                setattr(self._wrapped, name, value)

    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(FailingInsert(connection))._migrate()  # type: ignore[arg-type]
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
    ).fetchall()
    connection.close()

    assert tables == []
    # The half-written database must still be openable rather than bricked forever.
    store(path).close()


def test_replayed_activated_run_does_not_duplicate_lifecycle_evidence(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    lifecycle_events = (
        ("draft_created", {"contract_digest": "a" * 64}),
        ("activation", {"summary_digest": "b" * 64}),
    )

    first = database.create_activated_run(
        "run-1",
        "activation-1",
        "a" * 64,
        "b" * 64,
        "{}",
        NOW,
        acceptance_key=42,
        lifecycle_events=lifecycle_events,
        producer="contract",
    )
    replayed = database.create_activated_run(
        "run-1",
        "activation-1",
        "a" * 64,
        "b" * 64,
        "{}",
        NOW,
        acceptance_key=42,
        lifecycle_events=lifecycle_events,
        producer="contract",
    )

    assert replayed == first
    assert tuple(event.event_type for event in database.trace("run-1")) == (
        "draft_created",
        "activation",
    )


def _settled_run(
    database: SQLiteStore, run_id: str, activation_key: str, contract_digest: str
) -> None:
    """Create a run and settle it, because only one run may be active at a time."""
    database.create_run(run_id, activation_key, contract_digest, "b" * 64, "{}", NOW)
    database.transition_run(run_id, "created", "succeeded", "closed", NOW)


def test_runs_list_for_the_contracts_they_were_witnessed_under(tmp_path: Path) -> None:
    """A run carries a contract digest, and a contract digest carries the tenant.

    The store could only read a run by its own identifier or by its activation key,
    so nothing could gather the runs belonging to a tenant. Listing by contract
    digest is the evidence half of deriving a run's tenant without storing one on
    an append-only record.
    """
    database = store(tmp_path / "m0.sqlite3")
    _settled_run(database, "run-1", "activation-1", "a" * 64)
    _settled_run(database, "run-2", "activation-2", "a" * 64)
    _settled_run(database, "run-3", "activation-3", "c" * 64)

    listed = database.list_runs_for_contracts(("a" * 64,))

    assert {record.run_id for record in listed} == {"run-1", "run-2"}


def test_listing_no_contracts_returns_no_runs_rather_than_every_run(tmp_path: Path) -> None:
    """A tenant with no activated contracts must not see the whole estate.

    An empty digest set is what a tenant with nothing activated produces. This
    pins the answer as behaviour rather than as an implementation detail: it holds
    whether the filter short-circuits in Python or collapses in SQL.
    """
    database = store(tmp_path / "m0.sqlite3")
    _settled_run(database, "run-1", "activation-1", "a" * 64)

    assert database.list_runs_for_contracts(()) == ()


def test_listed_runs_are_ordered_newest_first_and_are_deterministic(tmp_path: Path) -> None:
    database = store(tmp_path / "m0.sqlite3")
    database.create_run("run-1", "activation-1", "a" * 64, "b" * 64, "{}", NOW)
    database.transition_run("run-1", "created", "succeeded", "closed", NOW)
    later = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    database.create_run("run-2", "activation-2", "a" * 64, "b" * 64, "{}", later)
    database.transition_run("run-2", "created", "succeeded", "closed", later)

    listed = database.list_runs_for_contracts(("a" * 64,))

    assert [record.run_id for record in listed] == ["run-2", "run-1"]


def test_a_store_can_be_opened_for_use_from_a_threadpool_worker(tmp_path: Path) -> None:
    """SQLite connections carry thread affinity; server routes do not.

    A console route runs its backend in a threadpool, so a store opened on the
    composing thread raises `ProgrammingError` the first time a worker reads it.
    Opening thread-tolerantly is the caller's explicit choice, so it is a named
    argument rather than the default.
    """
    import threading

    database = SQLiteStore.open(tmp_path / "m0.sqlite3", check_same_thread=False)
    _settled_run(database, "run-1", "activation-1", "a" * 64)
    listed: list[tuple[RunRecord, ...]] = []

    worker = threading.Thread(
        target=lambda: listed.append(database.list_runs_for_contracts(("a" * 64,)))
    )
    worker.start()
    worker.join()

    assert [record.run_id for record in listed[0]] == ["run-1"]


def test_runs_are_ordered_by_instant_rather_than_by_timestamp_text(tmp_path: Path) -> None:
    """`_timestamp` keeps each datetime's own offset, so text order is not time order.

    A store only requires timestamps to be timezone-aware, not UTC, and a service's
    clock is injectable. Sorting the stored ISO text lexicographically therefore put
    a run recorded at 12:00+05:30 (06:30Z) ahead of one at 09:00Z, reversing
    newest-first for any estate that records under more than one offset.
    """
    database = store(tmp_path / "m0.sqlite3")
    earlier = datetime(2026, 9, 1, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    later = datetime(2026, 9, 1, 9, tzinfo=UTC)
    database.create_run("run-earlier", "activation-1", "a" * 64, "b" * 64, "{}", earlier)
    database.transition_run("run-earlier", "created", "succeeded", "closed", earlier)
    database.create_run("run-later", "activation-2", "a" * 64, "b" * 64, "{}", later)
    database.transition_run("run-later", "created", "succeeded", "closed", later)

    listed = database.list_runs_for_contracts(("a" * 64,))

    assert [record.run_id for record in listed] == ["run-later", "run-earlier"]
