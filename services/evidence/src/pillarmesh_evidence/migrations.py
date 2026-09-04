import hashlib

MIGRATION_VERSION = 3
_VERSION_2_SQL = """
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_store
ON runs((1)) WHERE state IN ('created', 'running');

CREATE TABLE IF NOT EXISTS run_private_state (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    acceptance_key INTEGER,
    segment_path TEXT
);

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
# Version 3 only adds tables, so it is applied to an existing version 2 database
# rather than refusing it. `VERSION_2_CHECKSUM` is derived from the same text that
# created such a database, so a schema altered outside these migrations is refused
# instead of being upgraded to a shape nobody verified.
VERSION_3_SQL = """
CREATE TABLE IF NOT EXISTS acquisition_evidence_receipts (
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id)
);

CREATE TRIGGER IF NOT EXISTS acquisition_evidence_receipts_no_update
BEFORE UPDATE ON acquisition_evidence_receipts
BEGIN SELECT RAISE(ABORT, 'acquisition evidence is append-only'); END;

CREATE TRIGGER IF NOT EXISTS acquisition_evidence_receipts_no_delete
BEFORE DELETE ON acquisition_evidence_receipts
BEGIN SELECT RAISE(ABORT, 'acquisition evidence is append-only'); END;
""".strip()
VERSION_2_CHECKSUM = hashlib.sha256(_VERSION_2_SQL.encode("utf-8")).hexdigest()
MIGRATION_SQL = f"{_VERSION_2_SQL}\n\n{VERSION_3_SQL}"
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_SQL.encode("utf-8")).hexdigest()
