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


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('state_directory / "plan2.sqlite"', 'state_directory / "semantic-formation.sqlite"'),
        ('root="$RUNNER_TEMP/plan3a"', 'root="$RUNNER_TEMP/warehouse-lifecycle"'),
        (
            '"user.email", "plan2@example.invalid"',
            '"user.email", "semantic-formation@example.invalid"',
        ),
        ("tmp_path / 'state-plan2.db'", "tmp_path / 'state-semantic-formation.db'"),
        (
            "/absolute/private/plan3b and plan4a.",
            "/absolute/private/request-fulfillment and source-acquisition.",
        ),
    ],
)
def test_bare_milestone_names_are_renamed(text: str, expected: str) -> None:
    assert tool.rename_text(text) == expected


def test_bare_milestone_catch_all_leaves_longer_tokens_and_protected_spans_alone() -> None:
    text = "plan2x plan22 xplan2 karthik@pillarmesh.com/plan2 Plan 2"

    renamed = tool.rename_text(text)

    assert renamed == "plan2x plan22 xplan2 karthik@pillarmesh.com/semantic-formation Plan 2"


def test_a_bare_org_and_repository_reference_keeps_the_org_name() -> None:
    text = "the name 'PillarMesh/pillarmesh'. Both"

    assert tool.rename_text(text) == "the name 'PillarMesh/heinzel'. Both"


def test_git_records_the_moves_as_renames(tmp_path: Path) -> None:
    body = "".join(
        f"line {index} of a module that is long enough to be similar\n" for index in range(40)
    )
    repository = _make_repository(
        tmp_path, {"src/pillarmesh_runtime/module.py": "import pillarmesh_runtime\n" + body}
    )

    assert tool.main([], root=repository) == 0
    _git(repository, "add", "-A")

    status = _git(repository, "diff", "--cached", "-M", "--name-status")
    assert status.startswith("R")
    assert "src/pillarmesh_runtime/module.py\tsrc/heinzel_runtime/module.py" in status


def test_paths_with_non_ascii_characters_and_spaces_move(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path, {"docs/café notes/pillarmesh guide.md": "pillarmesh\n"})

    assert tool.main([], root=repository) == 0

    files = _snapshot(repository)
    assert files["docs/café notes/heinzel guide.md"] == b"heinzel\n"
    assert "docs/café notes/pillarmesh guide.md" not in files


def test_a_move_whose_parent_path_is_a_file_is_refused(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path, {"src/heinzel_pkg": "a file\n", "src/pillarmesh_pkg/module.py": "x\n"}
    )

    with pytest.raises(tool.RenameError, match="which is a file"):
        tool.build_plan(repository)


def test_the_console_package_lock_is_rewritten_not_excluded(tmp_path: Path) -> None:
    lock = (
        '{\n  "name": "@pillarmesh/console",\n'
        '  "packages": {"": {"name": "@pillarmesh/console"}}\n}\n'
    )
    repository = _make_repository(tmp_path, {"apps/console/package-lock.json": lock})

    assert tool.main([], root=repository) == 0

    assert (repository / "apps/console/package-lock.json").read_text() == lock.replace(
        "@pillarmesh/", "@heinzel/"
    )
    assert not tool.is_excluded("apps/console/package-lock.json")


def test_the_residual_check_uses_the_release_scanners_exact_patterns() -> None:
    scanner = _load_module(
        "public_tree_scanner", _REPOSITORY_ROOT / "tests/release/test_public_tree.py"
    )

    assert [pattern.pattern for pattern in tool._SCANNER_ALLOWED] == [
        pattern.pattern for pattern in scanner._ALLOWED_REFERENCE_PATTERNS
    ]
    assert tool._COMPANY_PATTERN.pattern == scanner._COMPANY_PATTERN.pattern


def test_a_chained_move_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _make_repository(tmp_path, {"first.txt": "a\n", "second.txt": "b\n"})
    monkeypatch.setattr(
        tool, "PATH_RENAMES", {"first.txt": "second.txt", "second.txt": "third.txt"}
    )

    with pytest.raises(tool.RenameError, match="chained"):
        tool.build_plan(repository)


def test_targets_that_differ_only_in_case_are_refused(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path, {"src/pillarmesh_x.py": "a\n", "src/Heinzel_x.py": "b\n"}
    )

    with pytest.raises(tool.RenameError, match="case-insensitively"):
        tool.build_plan(repository)


def test_a_failed_git_mv_names_the_recovery_commands(tmp_path: Path) -> None:
    repository = _make_repository(
        tmp_path, {"src/pillarmesh_a.py": "a\n", "src/pillarmesh_b.py": "b\n"}
    )
    plan = tool.build_plan(repository)
    (repository / "src/pillarmesh_b.py").unlink()

    with pytest.raises(tool.RenameError, match=r"git reset --hard && git clean -fd"):
        tool.apply_plan(repository, plan)


@pytest.mark.parametrize("target", ["pillarmesh_runtime/module.py", "docs/plan3a/setup.md"])
def test_a_symlink_to_an_old_name_is_refused(tmp_path: Path, target: str) -> None:
    repository = _make_repository(tmp_path, {"README.md": "x\n"})
    (repository / "link").symlink_to(target)
    _git(repository, "add", "link")
    _git(repository, "commit", "-q", "-m", "link")

    with pytest.raises(tool.RenameError, match="symlink link"):
        tool.build_plan(repository)


def test_binary_file_report_says_only_raw_bytes_were_seen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _make_repository(tmp_path, {"image.png": b"\x00pillarmesh"})

    assert tool.main(["--dry-run"], root=repository) == 0

    assert "raw bytes only" in capsys.readouterr().out


def test_a_source_whose_target_is_already_tracked_is_not_reported_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _make_repository(
        tmp_path,
        {
            "tests/acceptance/run_semantic_formation.py": "x\n",
            "docs/warehouse-lifecycle/setup.md": "x\n",
        },
    )

    assert tool.main(["--dry-run"], root=repository) == 0

    missing = capsys.readouterr().out.split("Missing PATH_RENAMES")[1].splitlines()[0]
    assert "tests/acceptance/run_plan2.py" not in missing
    assert "docs/plan3a/" not in missing
    assert "tests/acceptance/run_plan3a.py" in missing
