from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pillarmesh_connection_broker import SQLiteSourceBindingRepository
from pillarmesh_contract_model import canonical_bytes
from pillarmesh_runtime import AcquisitionRuntimeError, source_binding_resolver

from services.runtime.tests.test_acquisition import BINDING_REF, _intent, _runner
from services.runtime.tests.test_binding_resolution import _binding, _capability


def _repository(path: Path) -> SQLiteSourceBindingRepository:
    """A store holding the binding the runner's own intent names.

    The identifiers have to match `_intent`, or every case here resolves to "not
    found" and three different failures all read as a denial -- which is precisely
    the conflation under test.
    """
    repository = SQLiteSourceBindingRepository(str(path))
    repository.create(_binding(BINDING_REF), _capability(BINDING_REF))
    return repository


def _reason_for(resolver: object) -> tuple[str, ...]:
    runner, observation, _s, _p, _r, _st, _a, evidence, _e = _runner(
        binding_resolver=resolver  # type: ignore[arg-type]
    )
    with pytest.raises(AcquisitionRuntimeError):
        runner.prepare(_intent(observation))
    return evidence.receipts[-1].reason_codes


def test_a_broker_failure_reaches_the_receipt_as_what_it_actually_was(
    tmp_path: Path,
) -> None:
    """The receipt is the durable, tenant-visible record, so its reason must be true.

    Handing `repository.load` to the runner directly produced `authorization_denied`
    for every one of these, because the runner's catch-all classifies anything it
    does not recognise as a denial. Since acquisition receipts are now retained,
    that verdict is not discarded at the end of a run -- it is stored and shown.
    """
    # Deliberately empty: the runner asks for a binding this store never held.
    absent = SQLiteSourceBindingRepository(str(tmp_path / "absent.sqlite"))

    unavailable = _repository(tmp_path / "unavailable.sqlite")
    unavailable.close()

    corrupt_path = tmp_path / "corrupt.sqlite"
    corrupt = _repository(corrupt_path)
    corrupt.close()
    connection = sqlite3.connect(corrupt_path)
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (
            canonical_bytes(_binding(BINDING_REF).model_copy(update={"tenant_id": "tenant-b"})),
            "tenant-a",
            BINDING_REF,
        ),
    )
    connection.commit()
    connection.close()

    assert _reason_for(source_binding_resolver(absent)) == ("authorization_denied",)
    assert _reason_for(source_binding_resolver(unavailable)) == ("provider_unavailable",)
    assert _reason_for(
        source_binding_resolver(SQLiteSourceBindingRepository(str(corrupt_path)))
    ) == ("integrity_failure",)
