from __future__ import annotations

from .acquisition import AcquisitionEvidenceReceipt
from .store import SQLiteStore


class SQLiteAcquisitionEvidenceWriter:
    """Durable acquisition evidence writer for the acquisition runtime.

    The runtime declares the writer it needs as a structural protocol, so this
    deliberately does not import it: evidence must not depend on the service that
    consumes evidence. Conformance is proved by composing the two, in
    `tests/end-to-end/test_acquisition_evidence_retention.py`.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def append(self, receipt: AcquisitionEvidenceReceipt) -> None:
        self._store.append_acquisition_receipt(receipt)
