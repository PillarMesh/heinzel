from __future__ import annotations

import sqlite3

from pillarmesh_provider_sdk import AcquisitionAcknowledgement, LandReceipt
from pydantic import BaseModel, ConfigDict


class GenerationLedgerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: LandReceipt
    acknowledgement: AcquisitionAcknowledgement


class GenerationLedger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS raw_generation_ledger ("
            "generation_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "contract_ref TEXT NOT NULL, contract_revision INTEGER NOT NULL, "
            "trigger_window TEXT NOT NULL, segment_digest TEXT NOT NULL, "
            "destination_binding_ref TEXT NOT NULL, payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, contract_ref, contract_revision, trigger_window, "
            "segment_digest, destination_binding_ref))"
        )

    @classmethod
    def in_memory(cls) -> GenerationLedger:
        return cls(sqlite3.connect(":memory:"))

    def load(self, generation_key: str) -> LandReceipt | None:
        record = self.load_record(generation_key)
        return None if record is None else record.receipt

    def load_record(self, generation_key: str) -> GenerationLedgerRecord | None:
        row = self._connection.execute(
            "SELECT payload FROM raw_generation_ledger WHERE generation_key = ?",
            (generation_key,),
        ).fetchone()
        if row is None:
            return None
        return GenerationLedgerRecord.model_validate_json(bytes(row[0]))

    def record(
        self,
        *,
        generation_key: str,
        receipt: LandReceipt,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> None:
        record = GenerationLedgerRecord(receipt=receipt, acknowledgement=acknowledgement)
        with self._connection:
            self._connection.execute(
                "INSERT INTO raw_generation_ledger ("
                "generation_key, tenant_id, contract_ref, contract_revision, trigger_window, "
                "segment_digest, destination_binding_ref, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_key,
                    receipt.tenant_id,
                    receipt.contract_ref,
                    receipt.contract_revision,
                    receipt.trigger_window,
                    receipt.segment_digest,
                    receipt.destination_binding_ref,
                    record.model_dump_json().encode(),
                ),
            )
