"""Tests for the one-off Heinzel rename tool.

Private and run explicitly: uv run pytest docs/superpowers/release-tools -q -p no:randomly
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_TOOL_PATH = Path(__file__).with_name("rename_to_heinzel.py")


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rename_to_heinzel", _TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "test@example.invalid")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), "-c", "commit.gpgsign=false", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _make_repository(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", "fixture")
    return repository


def _snapshot(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in sorted(repository.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }


def test_specific_rules_are_applied_before_the_general_rules_they_overlap() -> None:
    text = 'name = "pillarmesh-m0-workspace"\nuv run pillarmesh-m0 activate\n'

    renamed = tool.rename_text(text)

    assert renamed == 'name = "heinzel-workspace"\nuv run heinzel-authoring activate\n'


def test_milestone_hash_domains_drop_the_milestone_instead_of_becoming_authoring() -> None:
    assert tool.rename_text('"pillarmesh-m0-batch-v1"') == '"heinzel-batch-v1"'


def test_milestone_identifiers_are_renamed_in_every_case_style() -> None:
    text = (
        "Plan3AConfig OfflinePlan2Harness plan2_config teardown_plan3a "
        "PILLARMESH_PLAN3A_STATE_PATH pillarmesh-plan3a-run-v1 run_plan4a docs/plan3b/setup.md"
    )

    renamed = tool.rename_text(text)

    assert renamed == (
        "WarehouseLifecycleConfig OfflineSemanticFormationHarness semantic_formation_config "
        "teardown_warehouse_lifecycle HEINZEL_WAREHOUSE_LIFECYCLE_STATE_PATH "
        "heinzel-warehouse-lifecycle-run-v1 run_source_acquisition "
        "docs/request-fulfillment/setup.md"
    )


@pytest.mark.parametrize(
    "protected",
    [
        "Copyright 2026 PillarMesh",
        "karthik@pillarmesh.com",
        "Karthik@PillarMesh.com",
        "contact@pillarmesh.com",
        "https://pillarmesh.com/x",
        "a product of PillarMesh",
        '"name": "PillarMesh"',
        'name = "PillarMesh"',
    ],
)
def test_allowed_company_references_survive_exactly(protected: str) -> None:
    text = f"before {protected} after pillarmesh_runtime\n"

    assert tool.rename_text(text) == f"before {protected} after heinzel_runtime\n"


def test_only_the_registry_and_org_prefixes_are_protected_in_repository_urls() -> None:
    text = "ghcr.io/pillarmesh/pillarmesh https://github.com/PillarMesh/pillarmesh.git"

    renamed = tool.rename_text(text)

    assert renamed == "ghcr.io/pillarmesh/heinzel https://github.com/PillarMesh/heinzel.git"


def test_non_author_name_fields_are_not_protected() -> None:
    assert tool.rename_text('display_name = "PillarMesh"') == 'display_name = "Heinzel"'


def test_a_lookalike_domain_is_not_protected() -> None:
    assert tool.rename_text("x@pillarmesh.company") == "x@heinzel.company"


def test_the_rename_moves_nested_directories_and_files_and_rewrites_text(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path,
        {
            "services/runtime/src/pillarmesh_runtime/deep/pillarmesh_inner/module.py": (
                "from pillarmesh_runtime import x\n"
            ),
            "deploy/pillarmesh-runtime.yaml": "image: ghcr.io/pillarmesh/pillarmesh-runtime\n",
            "NOTICE": "Copyright 2026 PillarMesh\npillarmesh_runtime\n",
            "tests/release/test_scan.py": "FORBIDDEN = 'pillarmesh'\n",
            "docs/superpowers/pillarmesh-notes.md": "pillarmesh plan2\n",
            "apps/console/web/public/licenses/pillarmesh.txt": "PillarMesh\n",
            "uv.lock": 'name = "pillarmesh-runtime"\n',
            "tests/acceptance/run_plan2.py": "import tests.acceptance.run_plan2\n",
            "docs/plan3a/setup.md": "See docs/plan3a/teardown.md\n",
        },
    )

    exit_code = tool.main([], root=repository)

    assert exit_code == 0
    files = _snapshot(repository)
    assert files["services/runtime/src/heinzel_runtime/deep/heinzel_inner/module.py"] == (
        b"from heinzel_runtime import x\n"
    )
    assert files["deploy/heinzel-runtime.yaml"] == b"image: ghcr.io/pillarmesh/heinzel-runtime\n"
    assert files["tests/acceptance/run_semantic_formation.py"] == (
        b"import tests.acceptance.run_semantic_formation\n"
    )
    assert (
        files["docs/warehouse-lifecycle/setup.md"] == b"See docs/warehouse-lifecycle/teardown.md\n"
    )
    assert not (repository / "services/runtime/src/pillarmesh_runtime").exists()
    assert not (repository / "docs/plan3a").exists()


def test_excluded_paths_are_never_moved_or_rewritten(tmp_path: Path) -> None:
    excluded = {
        "NOTICE": "Copyright 2026 PillarMesh\npillarmesh_runtime\n",
        "LICENSE": "pillarmesh\n",
        "THIRD_PARTY_NOTICES.md": "pillarmesh\n",
        "tests/release/test_scan.py": "FORBIDDEN = 'pillarmesh plan2'\n",
        "docs/superpowers/pillarmesh-notes.md": "pillarmesh plan2\n",
        "apps/console/web/public/licenses/pillarmesh.txt": "PillarMesh\n",
        "uv.lock": 'name = "pillarmesh-runtime"\n',
    }
    repository = _make_repository(tmp_path, {**excluded, "src/pillarmesh_a.py": "pillarmesh\n"})

    assert tool.main([], root=repository) == 0

    files = _snapshot(repository)
    for path, content in excluded.items():
        assert files[path] == content.encode("utf-8")
    assert files["src/heinzel_a.py"] == b"heinzel\n"


def test_path_renames_move_the_file_and_its_references(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path,
        {
            "services/compiler/legality/rules/M0-PG-SNAPSHOT-SNOWFLAKE-001.json": (
                '{"rule_id":"M0-PG-SNAPSHOT-SNOWFLAKE-001"}'
            ),
            "tests/ci/test_plan3a_witness_runner.py": "from tests.ci.run_plan3a_witness import x\n",
        },
    )

    assert tool.main([], root=repository) == 0

    files = _snapshot(repository)
    assert files["services/compiler/legality/rules/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001.json"] == (
        b'{"rule_id":"SNAPSHOT-POSTGRESQL-SNOWFLAKE-001"}'
    )
    assert files["tests/ci/test_warehouse_lifecycle_witness_runner.py"] == (
        b"from tests.ci.run_warehouse_lifecycle_witness import x\n"
    )


def test_missing_path_rename_sources_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _make_repository(tmp_path, {"README.md": "hello\n"})

    assert tool.main(["--dry-run"], root=repository) == 0

    output = capsys.readouterr().out
    assert "tests/acceptance/run_plan2.py" in output.split("Missing PATH_RENAMES")[1]


def test_two_paths_mapping_to_one_new_path_are_refused_before_any_change(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path,
        {
            "src/pillarmesh-m0-workspace.py": "a\n",
            "src/pillarmesh-workspace.py": "b\n",
            "b.md": "pillarmesh\n",
        },
    )
    before = _snapshot(repository)

    assert tool.main([], root=repository) == 1

    assert _snapshot(repository) == before
    assert _git(repository, "status", "--porcelain") == ""


def test_a_move_onto_an_existing_path_is_refused(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path, {"src/pillarmesh_a.py": "a\n", "src/heinzel_a.py": "b\n"}
    )

    with pytest.raises(tool.RenameError, match="already exists"):
        tool.build_plan(repository)


def test_dry_run_prints_the_plan_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _make_repository(
        tmp_path, {"src/pillarmesh_a/x.py": "import pillarmesh_a\nPILLARMESH_X=1\n"}
    )
    before = _snapshot(repository)

    assert tool.main(["--dry-run"], root=repository) == 0

    output = capsys.readouterr().out
    assert _snapshot(repository) == before
    assert _git(repository, "status", "--porcelain") == ""
    assert "src/pillarmesh_a/x.py -> src/heinzel_a/x.py" in output
    assert "Files to rewrite: 1" in output
    assert "'pillarmesh_' -> 'heinzel_'" in output


def test_a_dirty_tree_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository = _make_repository(tmp_path, {"src/pillarmesh_a.py": "pillarmesh\n"})
    (repository / "untracked.txt").write_text("x\n")

    assert tool.main([], root=repository) == 1

    assert "not clean" in capsys.readouterr().err
    assert (repository / "src/pillarmesh_a.py").read_text() == "pillarmesh\n"


def test_non_utf8_text_fails_before_any_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _make_repository(
        tmp_path,
        {"a/pillarmesh_first.py": "pillarmesh\n", "z/latin1.txt": b"caf\xe9 pillarmesh\n"},
    )
    before = _snapshot(repository)

    assert tool.main([], root=repository) == 1

    assert "z/latin1.txt: not valid UTF-8" in capsys.readouterr().err
    assert _snapshot(repository) == before


def test_binary_files_are_left_alone_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = b"\x89PNG\x00\x00pillarmesh\xff"
    repository = _make_repository(tmp_path, {"image.png": binary})

    assert tool.main([], root=repository) == 0

    assert (repository / "image.png").read_bytes() == binary
    assert "not rewritten): image.png" in capsys.readouterr().out


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path,
        {"src/pillarmesh_a/x.py": "pillarmesh_a karthik@pillarmesh.com Plan3AConfig\n"},
    )
    assert tool.main([], root=repository) == 0
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", "renamed")
    after_first = _snapshot(repository)

    assert tool.main([], root=repository) == 0

    assert _snapshot(repository) == after_first
    assert after_first["src/heinzel_a/x.py"] == (
        b"heinzel_a karthik@pillarmesh.com WarehouseLifecycleConfig\n"
    )
