from __future__ import annotations

from pathlib import Path

import pytest
from pillarmesh_contract_model import ApprovedSemanticVersion
from pillarmesh_semantic_registry import ApprovedSemanticCompiler, SQLiteSemanticVersionRepository

from .test_approval import NOW, valid_input


def test_semantic_version_repository_allocates_tenant_versions_and_replays_exact_material(
    tmp_path: Path,
) -> None:
    repository = SQLiteSemanticVersionRepository(str(tmp_path / "semantic-version.sqlite"))
    compiled = ApprovedSemanticCompiler(repository).compile(valid_input(), now=NOW)

    assert isinstance(compiled, ApprovedSemanticVersion)

    first = compiled
    replay = repository.store(compiled)
    changed = compiled.model_copy(update={"approval_ids": ("approval-owner-2",)})
    next_version = repository.store(changed)

    assert first.version == 1
    assert replay == first
    assert next_version.version == 2
    assert first.semantic_version_id != next_version.semantic_version_id


def test_semantic_version_storage_rolls_back_without_burning_a_sequence_or_losing_bindings(
    tmp_path: Path,
) -> None:
    repository = SQLiteSemanticVersionRepository(str(tmp_path / "semantic-version.sqlite"))
    repository._connection.execute(
        "CREATE TRIGGER reject_semantic_version BEFORE INSERT ON semantic_versions "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )

    with pytest.raises(Exception, match="injected write failure"):
        ApprovedSemanticCompiler(repository).compile(valid_input(), now=NOW)

    repository._connection.execute("DROP TRIGGER reject_semantic_version")
    stored = ApprovedSemanticCompiler(repository).compile(valid_input(), now=NOW)

    assert isinstance(stored, ApprovedSemanticVersion)

    assert stored.version == 1
    assert repository.load_approval_ids(stored) == ("approval-owner",)
