from __future__ import annotations

from pathlib import Path

from heinzel_evidence import SQLiteAcquisitionEvidenceWriter, SQLiteStore

from services.runtime.tests.test_acquisition import _intent, _runner


def test_a_prepared_acquisition_retains_its_receipt_for_its_own_tenant(tmp_path: Path) -> None:
    """The runtime's own receipt reaches durable storage and is read back by tenant.

    Every acquisition writer in the estate was a list in a test double, so a receipt
    the runner built was discarded when the process ended. This drives the real
    runner into the real store rather than asserting the two would fit: it is the
    only statement here that the seam is closed, and it is what makes a listed
    receipt evidence of work the runtime actually recorded.
    """
    path = tmp_path / "evidence.sqlite3"
    store = SQLiteStore.open(path)
    runner, observation, *_rest = _runner(evidence_delegate=SQLiteAcquisitionEvidenceWriter(store))

    result = runner.prepare(_intent(observation))

    tenant_id = result.evidence.tenant_id
    assert store.list_acquisition_receipts(tenant_id) == (result.evidence,)
    assert store.list_acquisition_receipts("tenant-without-acquisitions") == ()
    store.close()

    assert SQLiteStore.open(path).list_acquisition_receipts(tenant_id) == (result.evidence,)
