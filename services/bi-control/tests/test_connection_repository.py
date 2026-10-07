from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from heinzel_bi_control import (
    DashboardConnectionAuthorityError,
    DashboardConnectionConflict,
    DashboardDatasetConnectionBinding,
    SQLiteDashboardConnectionRepository,
)
from heinzel_contract_model import ArtifactReference


def _binding(*, tenant_id: str = "tenant-a") -> DashboardDatasetConnectionBinding:
    return DashboardDatasetConnectionBinding(
        tenant_id=tenant_id,
        engine_kind="postgresql",
        consumption_object_ref=ArtifactReference(
            artifact_id="product:revenue:consumption",
            version=7,
            digest="1" * 64,
        ),
        namespace="analytics",
        relation_name="revenue_current",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=3,
        warehouse_binding_digest="2" * 64,
        connection_secret_ref="secret://tenant-a/superset-database",
    )


def test_connection_binding_survives_reopen_and_is_tenant_scoped(tmp_path: Path) -> None:
    path = tmp_path / "dashboard-connections.sqlite"
    binding = _binding()
    repository = SQLiteDashboardConnectionRepository(str(path))
    repository.store(binding)
    repository.close()

    reopened = SQLiteDashboardConnectionRepository(str(path))

    assert (
        reopened.resolve(
            tenant_id="tenant-a",
            engine_kind="postgresql",
            consumption_object_ref=binding.consumption_object_ref,
        )
        == binding
    )
    assert (
        reopened.resolve(
            tenant_id="tenant-b",
            engine_kind="postgresql",
            consumption_object_ref=binding.consumption_object_ref,
        )
        is None
    )


def test_connection_binding_replay_is_exact_and_conflicts_are_rejected() -> None:
    binding = _binding()
    repository = SQLiteDashboardConnectionRepository(":memory:")

    assert repository.store(binding) == binding
    assert repository.store(binding) == binding

    with pytest.raises(DashboardConnectionConflict, match="immutable"):
        repository.store(binding.model_copy(update={"relation_name": "changed"}))


def test_corrupt_connection_payload_is_rejected() -> None:
    binding = _binding()
    repository = SQLiteDashboardConnectionRepository(":memory:")
    repository.store(binding)
    repository._connection.execute(
        "UPDATE dashboard_dataset_connections_v1 SET payload = ?",
        (b"{}",),
    )
    repository._connection.commit()

    with pytest.raises(DashboardConnectionAuthorityError, match="invalid"):
        repository.resolve(
            tenant_id="tenant-a",
            engine_kind="postgresql",
            consumption_object_ref=binding.consumption_object_ref,
        )


def test_connection_binding_resolves_from_the_thread_that_serves_the_read(
    tmp_path: Path,
) -> None:
    """A console composes its stores on one thread and serves its reads on another.

    The repository is opened where the console is assembled and read where a request is
    handled, so a connection bound to its creating thread reports every authorization as
    an unavailable authority and no dashboard can ever be published.
    """
    repository = SQLiteDashboardConnectionRepository(str(tmp_path / "connections.sqlite"))
    binding = _binding()
    repository.store(binding)

    with ThreadPoolExecutor(max_workers=1) as elsewhere:
        resolved = elsewhere.submit(
            lambda: repository.resolve(
                tenant_id=binding.tenant_id,
                engine_kind="postgresql",
                consumption_object_ref=binding.consumption_object_ref,
            )
        ).result()

    assert resolved == binding


def test_every_sqlite_store_in_this_service_is_readable_off_its_creating_thread() -> None:
    """The console serves on a worker thread, so no store here may be thread-bound.

    Asserted against the source rather than one store at a time, because the defect is
    invisible until a console is wired and reports it as missing evidence instead.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "heinzel_bi_control"
    thread_bound = sorted(
        module.name
        for module in source.glob("*.py")
        for statement in re.findall(
            r"sqlite3\.connect\((?:[^()]|\([^()]*\))*\)", module.read_text()
        )
        if "check_same_thread" not in statement
    )

    assert thread_bound == []
